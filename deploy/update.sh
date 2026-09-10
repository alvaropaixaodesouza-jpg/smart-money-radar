#!/usr/bin/env bash
# Ship the working tree to the server and restart what needs restarting.
#
# The database is never touched: it lives on the server and is the thing being collected into.
# Secrets are never touched either — .env is written once at provision time and stays put.
set -euo pipefail

HOST="${RADAR_HOST:-root@193.233.209.98}"
KEY="${RADAR_KEY:-$HOME/.ssh/fomoradar}"
APP=/opt/fomoradar/app

echo "==> packing"
tar --exclude='.venv' --exclude='node_modules' --exclude='dist' --exclude='.git' \
    --exclude='.astro' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='*.egg-info' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='*scored*.json' --exclude='*.har' --exclude='fomo_state' \
    --exclude='radar.html' --exclude='report.md' --exclude='.env' --exclude='.fonts' \
    -czf /tmp/radar-app.tgz .

echo "==> uploading"
scp -q -i "$KEY" /tmp/radar-app.tgz "$HOST:/tmp/radar-app.tgz"

echo "==> installing"
ssh -i "$KEY" "$HOST" bash -s <<'REMOTE'
set -euo pipefail
APP=/opt/fomoradar/app
# .env is preserved across every deploy: it is the one file the server owns, not the repo
cp "$APP/.env" /tmp/.env.keep
tar -xzf /tmp/radar-app.tgz -C "$APP"

# Windows line endings do not survive contact with a shell. The repo is edited on a Windows box,
# git converts on checkout, and any tool that writes a file with platform newlines puts them back
# - which is how install-extension.sh once failed with `set: pipefail: invalid option name`, a
# message that reads like a bash bug and is a carriage return. Normalising here rather than
# trusting the packing machine means no future edit anywhere can break a deploy this way.
# The carriage return is built with printf rather than written as an escape, because a literal one
# in this file would be stripped by the very normalising this line performs.
CR=$(printf '\015')
find "$APP/deploy" -type f \( -name '*.sh' -o -name '*.service' -o -name '*.timer' \
     -o -name '*.conf' -o -name 'Caddyfile' \) -exec sed -i "s/${CR}\$//" {} +
install -o radar -g radar -m 600 /tmp/.env.keep "$APP/.env"
rm -f /tmp/.env.keep /tmp/radar-app.tgz
chown -R radar:radar "$APP"

cd "$APP"
sudo -u radar /opt/fomoradar/venv/bin/pip install -q -e ".[api,dev]"
sudo -u radar /opt/fomoradar/venv/bin/python -m pytest -q 2>&1 | tail -1

cd "$APP/site"
sudo -u radar npm install --silent --no-fund --no-audit
# Astro bakes `site` into the SSR bundle at build time, so the canonical, og:url and og:image
# addresses are decided here rather than by the unit file. Without this the pages ship claiming
# whatever the config default happens to be, which is a domain we do not serve.
SITE_URL="$(sed -n 's/^PUBLIC_SITE_URL=//p' "$APP/.env" | tail -1)"
sudo -u radar env PUBLIC_SITE_URL="$SITE_URL" npm run build 2>&1 | grep -E "error|Complete!" | tail -1

systemctl restart radar-api radar-site radar-bot radar-receive
sleep 5
for u in radar-api radar-site radar-bot; do printf '   %-12s %s\n' "$u" "$(systemctl is-active $u)"; done
REMOTE

echo "==> checking"
# The bare IP now redirects to the canonical host, so checking it would only ever prove that the
# redirect works. Ask the address the site itself claims to be.
SITE=$(ssh -i "$KEY" "$HOST" "sed -n 's/^PUBLIC_SITE_URL=//p' $APP/.env | tail -1")
SITE=${SITE:-http://${HOST#*@}}
for p in / /leaderboard /api/health; do
  printf '   %s  %s%s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$SITE$p")" "$SITE" "$p"
done
echo "==> done"
