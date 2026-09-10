#!/usr/bin/env bash
# Install the collector extension into the server's Chrome, permanently.
#
# --load-extension stopped working in Chrome 137 and by 152 the feature flag that used to bring it
# back is gone too: the flag is accepted, appears in chrome://version, and does nothing. The
# supported way to put an extension on a machine you own is the enterprise policy, which needs the
# extension packed and served from an update manifest. Both can live on this box.
#
# Force-installed means it also cannot be turned off by accident from the browser UI, which is the
# right property for the one component the whole pipeline depends on.
set -euo pipefail

SRC=/opt/fomoradar/app/extension
PY=/opt/fomoradar/venv/bin/python
DEST=/opt/fomoradar/ext
POLICY=/etc/opt/chrome/policies/managed/fomoradar.json

install -d -o radar -g radar -m 0755 "$DEST"

# The key is generated once and kept: it is what fixes the extension's id, and a new id would look
# to Chrome like a different extension every deploy.
if [ ! -f "$DEST/key.pem" ]; then
  echo "==> generating the signing key"
  rm -f /opt/fomoradar/app/extension.crx /opt/fomoradar/app/extension.pem
  sudo -u radar google-chrome --pack-extension="$SRC" --no-message-box >/dev/null 2>&1 || true
  mv /opt/fomoradar/app/extension.pem "$DEST/key.pem"
fi

echo "==> packing"
rm -f /opt/fomoradar/app/extension.crx
sudo -u radar google-chrome --pack-extension="$SRC" --pack-extension-key="$DEST/key.pem" \
  --no-message-box >/dev/null 2>&1 || true
mv /opt/fomoradar/app/extension.crx "$DEST/collector.crx"

# Chrome derives the id from the public key: sha256 of the DER, first sixteen bytes, hex digits
# mapped onto a-p.
ID=$(openssl rsa -in "$DEST/key.pem" -pubout -outform DER 2>/dev/null \
     | openssl dgst -sha256 -binary | xxd -p -c 64 | cut -c1-32 | tr '0-9a-f' 'a-p')
VERSION=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SRC/manifest.json" | head -1)
echo "==> id $ID  version $VERSION"

# Chrome's extension updater refuses a file:// update manifest, so both it and the packed
# extension are served over loopback by radar-crx.service.
BASE=http://127.0.0.1:8098
cat > "$DEST/update.xml" <<XML
<?xml version='1.0' encoding='UTF-8'?>
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='$ID'>
    <updatecheck codebase='$BASE/collector.crx' version='$VERSION' />
  </app>
</gupdate>
XML

chown -R radar:radar "$DEST"
chmod 0644 "$DEST/collector.crx" "$DEST/update.xml"
chmod 0600 "$DEST/key.pem"

# The receiver token is handed over by policy rather than typed into the popup over VNC. It is
# read from the server's own .env, never printed, and lands in a root-only file.
TOKEN=$(sed -n 's/^RECEIVER_TOKEN=//p' /opt/fomoradar/app/.env | head -1)

install -d -m 0755 /etc/opt/chrome/policies/managed
cat > "$POLICY" <<JSON
{
  "ExtensionInstallForcelist": ["$ID;$BASE/update.xml"],
  "ExtensionInstallSources": ["$BASE/*"],
  "ExtensionAllowedTypes": ["extension"],
  "BlockExternalExtensions": false,
  "3rdparty": {
    "extensions": {
      "$ID": {
        "endpoint": "http://127.0.0.1:8787/ingest",
        "token": "$TOKEN",
        "intervalMinutes": 120,
        "enabled": true,
        "withPositions": true
      }
    }
  }
}
JSON
# Chrome reads its policies as the account it runs under, so root-only is too tight - it silently
# ignores a file it cannot read and un-installs the extension it was told to install. The token in
# here guards a loopback endpoint on this same machine, and radar is the account that uses it.
chown root:radar "$POLICY"
chmod 0640 "$POLICY"

echo "==> policy written to $POLICY"
cp /opt/fomoradar/app/deploy/systemd/radar-crx.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now radar-crx

# Chrome checks an update manifest on its own schedule - hours - so restarting the browser is not
# enough to pick up a version packed a minute ago. Without this the script reports installing a
# version it did not install, which is worse than failing: three fixes in a row were tested
# against code that was never there.
echo "==> forcing a clean install of $VERSION"
systemctl stop radar-browser || true
sleep 2
rm -rf "/opt/fomoradar/chrome-profile/Default/Extensions/$ID"
"$PY" - "$ID" <<'PYEOF' || true
import json, pathlib, sys
p = pathlib.Path('/opt/fomoradar/chrome-profile/Default/Preferences')
if p.exists():
    d = json.loads(p.read_text())
    d.get('extensions', {}).get('settings', {}).pop(sys.argv[1], None)
    p.write_text(json.dumps(d))
PYEOF
chown -R radar:radar /opt/fomoradar/chrome-profile
systemctl start radar-browser

# Say what actually landed, not what was asked for.
for _ in $(seq 1 20); do
  sleep 5
  got=$(ls -d "/opt/fomoradar/chrome-profile/Default/Extensions/$ID/"*/ 2>/dev/null | head -1)
  [ -n "$got" ] && break
done
echo "==> installed: ${got:-NOTHING}"
echo "$ID" > "$DEST/extension-id.txt"
echo "==> restarted. id is in $DEST/extension-id.txt"
