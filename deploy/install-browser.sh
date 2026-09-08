#!/usr/bin/env bash
# Put a real Chrome on the server, on a virtual display, with the collector extension loaded.
#
# fomo.family is behind Cloudflare, which refuses every non-browser client — so its data has to
# come from a browser somebody is logged into. Until now that browser was on a laptop at home and
# an SSH tunnel carried the collections to the server. This moves it onto the server itself.
#
# The open question this answers is whether Cloudflare accepts a datacentre address from a genuine
# Chrome. Nothing here bypasses anything: it is an ordinary Chrome on an ordinary X display, and a
# person signs into it once by hand over VNC. If Cloudflare challenges, a person answers it.
#
# The VNC server listens on loopback only and is reachable exclusively through an SSH tunnel.
set -euo pipefail

PROFILE=/opt/fomoradar/chrome-profile
EXT=/opt/fomoradar/app/extension
DISPLAY_NUM=99

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# xvfb: the virtual display. fluxbox: a window manager, without which Chrome's own dialogs cannot
# be moved or focused. x11vnc: how a person reaches that display. The fonts stop Chrome rendering
# every page in boxes.
apt-get install -y -qq xvfb fluxbox x11vnc xdotool \
  fonts-liberation fonts-noto-color-emoji libnss3 libgbm1 libasound2t64 >/dev/null

if ! command -v google-chrome >/dev/null; then
  echo "==> google chrome"
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -qq
  apt-get install -y -qq google-chrome-stable >/dev/null
fi
google-chrome --version

echo "==> profile"
install -d -o radar -g radar -m 0700 "$PROFILE"

echo "==> units"
cp /opt/fomoradar/app/deploy/systemd/radar-xvfb.service /etc/systemd/system/
cp /opt/fomoradar/app/deploy/systemd/radar-browser.service /etc/systemd/system/
cp /opt/fomoradar/app/deploy/systemd/radar-vnc.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now radar-xvfb radar-browser
systemctl status radar-xvfb radar-browser --no-pager -n 3 || true

cat <<EOF

==> done. To sign in once:

  on your machine:   ssh -i ~/.ssh/fomoradar -L 5900:127.0.0.1:5900 root@193.233.209.98 \\
                       'systemctl start radar-vnc; sleep 3600'
  then point any VNC viewer at  127.0.0.1:5900

  Sign into fomo.family the way you normally do. The profile persists, so this is once.
  Stop the VNC server afterwards:  systemctl stop radar-vnc

The display is :$DISPLAY_NUM, the profile is $PROFILE, the extension is $EXT.
EOF
