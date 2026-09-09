#!/usr/bin/env bash
# Restart the collector browser when it has quietly stopped collecting.
#
# The extension schedules itself with chrome.alarms. In Manifest V3 the service worker is torn
# down when idle and the alarm is meant to wake it; observed on this box, after a few hours it
# stops doing that. Collections simply stop, with the browser still signed in, the extension still
# installed and nothing anywhere complaining.
#
# Driving the collection from the server instead would be the tidier fix, and it is not available:
# Chrome refuses to attach a debugger session to an extension target ("Not allowed"), so nothing
# outside the browser can call into the extension. What is available is restarting the browser,
# which fires onStartup, which re-arms the alarm.
#
# So this is deliberately dumb: notice the silence, restart, let the extension do the rest. It
# checks far more often than it acts, and acting costs a few seconds of a browser nobody watches.
set -euo pipefail

DB=/opt/fomoradar/fomo_agent.db
PY=/opt/fomoradar/venv/bin/python
STALE_MIN=${1:-45}

age=$("$PY" - "$DB" <<'EOF'
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
row = c.execute("SELECT MAX(finished_at) FROM runs WHERE kind = 'fomo_ingest'").fetchone()
print(9999 if not row or not row[0] else int((time.time() - row[0]) / 60))
EOF
)

if [ "$age" -lt "$STALE_MIN" ]; then
  echo "fomo collected ${age}m ago, nothing to do"
  exit 0
fi

echo "fomo last collected ${age}m ago (limit ${STALE_MIN}m) - restarting the collector browser"
systemctl restart radar-browser
