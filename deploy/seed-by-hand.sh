#!/usr/bin/env bash
# Write a waiting session straight into the collector browser's fomo.family tab.
#
# The extension does this on its own; this is the manual path for when it has not yet, and for
# proving the session itself is good. It uses the X clipboard rather than typing, because the
# payload is twenty kilobytes and because a keyboard layout cannot mangle a paste.
#
# The session never appears in a log or on a terminal: it goes from the file to the clipboard to
# the page, and the file is deleted afterwards.
set -euo pipefail

SEED=${1:-/opt/fomoradar/fomo-session.json}
export DISPLAY=:99

[ -f "$SEED" ] || { echo "no session waiting at $SEED"; exit 1; }

# Build a one-liner that restores every key, straight from the JSON. Printed nowhere.
/opt/fomoradar/venv/bin/python - "$SEED" > /tmp/restore.js <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
payload = {"local": d.get("local") or {}, "session": d.get("session") or {},
           "cookies": d.get("cookies") or {}}
js = (
    "(()=>{const p=" + json.dumps(payload, separators=(',', ':')) + ";"
    "for(const[k,v]of Object.entries(p.local))localStorage.setItem(k,v);"
    "for(const[k,v]of Object.entries(p.session))sessionStorage.setItem(k,v);"
    "for(const[k,v]of Object.entries(p.cookies))"
    "document.cookie=k+'='+v+'; path=/; max-age=2592000; samesite=lax';"
    "console.log('restored',Object.keys(p.local).length,'keys');location.reload();})()"
)
sys.stdout.write(js)
PY

WIN=$(xdotool search --onlyvisible --class chrome | head -1)
xdotool windowactivate "$WIN"; sleep 1

# Put the fomo tab in front. Ctrl+1 is the first tab, which is the one the service opens.
xdotool key --clearmodifiers ctrl+1; sleep 3

# Ctrl+Shift+J opens devtools with the console focused, which saves guessing at panel layout.
xdotool key --clearmodifiers ctrl+shift+j; sleep 4

# Chrome refuses pasted code in the console until somebody types this once per profile.
xdotool type --delay 60 'allow pasting'
xdotool key --clearmodifiers Return; sleep 2

xclip -selection clipboard < /tmp/restore.js
xdotool key --clearmodifiers ctrl+v; sleep 2
xdotool key --clearmodifiers Return; sleep 6

xdotool key --clearmodifiers ctrl+shift+j; sleep 1   # close devtools
rm -f /tmp/restore.js "$SEED"
echo "session written and both copies deleted"
