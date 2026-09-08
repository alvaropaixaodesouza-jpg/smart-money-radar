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
    --exclude='radar.html' --exclude='report.md' --exclude='.env' \
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
install -o radar -g radar -m 600 /tmp/.env.keep "$APP/.env"
rm -f /tmp/.env.keep /tmp/radar-app.tgz
chown -R radar:radar "$APP"

cd "$APP"
sudo -u radar /opt/fomoradar/venv/bin/pip install -q -e ".[api,dev]"
sudo -u radar /opt/fomoradar/venv/bin/python -m pytest -q 2>&1 | tail -1

cd "$APP/site"
sudo -u radar npm install --silent --no-fund --no-audit
sudo -u radar npm run build 2>&1 | grep -E "error|Complete!" | tail -1

systemctl restart radar-api radar-site radar-bot radar-receive
sleep 5
for u in radar-api radar-site radar-bot; do printf '   %-12s %s\n' "$u" "$(systemctl is-active $u)"; done
REMOTE

echo "==> checking"
for p in / /leaderboard /api/health; do
  printf '   %s  %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "http://${HOST#*@}$p")" "$p"
done
echo "==> done"
