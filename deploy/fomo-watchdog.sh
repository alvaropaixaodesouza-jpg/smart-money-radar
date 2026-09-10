#!/usr/bin/env bash
# Make the collector collect when it has quietly stopped.
#
# The extension schedules itself with chrome.alarms. In Manifest V3 the service worker is torn down
# when idle and the alarm is meant to wake it; observed on this box, after a few hours it stops
# doing that. Collections simply stop, with the browser still signed in, the extension still
# installed and nothing anywhere complaining.
#
# This used to answer that by restarting the browser and trusting onStartup to re-arm the alarm.
# Measured on 2026-09-10, that does not work: five hours of restarting every ten minutes produced
# no collection at all, while one poke down the page bridge collected everything waiting. A browser
# that is signed in and holding a live token does not need to be killed — it needs to be asked.
#
# So the escalation is cheapest first:
#   1. poke the page bridge, which relays into the service worker and runs a collection
#   2. only if the next check still finds silence, restart the browser
# A restart resets the extension's alarm, so restarting on every pass is not merely useless — it
# guarantees the alarm never reaches its period. That was the loop this script was stuck in.
set -euo pipefail

DB=/opt/fomoradar/fomo_agent.db
PY=/opt/fomoradar/venv/bin/python
APP=/opt/fomoradar/app
STALE_MIN=${1:-160}   # the collector runs every 2h; 160 is one missed pass
# How stale before a poke is judged to have failed and the browser is restarted instead.
HARD_MIN=${2:-300}   # two missed passes, and a poke has not helped

age=$("$PY" - "$DB" <<'EOF'
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
# A delivery that brought nothing is not a collection: the extension posts whatever it managed
# to fetch, so a signed-out browser or a broken collect() still writes a run row. Requiring a
# leaderboard in the stats is what stops this watchdog guarding an empty pipe.
row = c.execute("SELECT MAX(finished_at) FROM runs WHERE kind = 'fomo_ingest' "
                "AND error IS NULL AND stats_json LIKE '%\"24h\":%'").fetchone()
print(9999 if not row or not row[0] else int((time.time() - row[0]) / 60))
EOF
)

if [ "$age" -lt "$STALE_MIN" ]; then
  echo "fomo collected ${age}m ago, nothing to do"
  exit 0
fi

# Back off between pokes. This runs every ten minutes, and a collection that fails for a reason a
# poke cannot fix - fomo returning 403 to every request, say - would otherwise be answered by six
# forced collections an hour, each pulling three leaderboards and two dozen wallet calls. That is
# more traffic than the normal schedule, aimed at an endpoint that is already refusing us, and it
# is how a soft rate limit becomes a long one. Measured once, on 2026-09-10, by doing exactly that.
STAMP=/opt/fomoradar/ext/.last-poke
POKE_GAP_MIN=${3:-20}
last=$(( ( $(date +%s) - $(stat -c %Y "$STAMP" 2>/dev/null || echo 0) ) / 60 ))

if [ "$age" -lt "$HARD_MIN" ]; then
  if [ "$last" -lt "$POKE_GAP_MIN" ]; then
    echo "fomo last collected ${age}m ago, but poked ${last}m ago - waiting rather than pushing"
    exit 0
  fi
  echo "fomo last collected ${age}m ago (limit ${STALE_MIN}m) - poking the collector"
  touch "$STAMP"
  cd "$APP" && timeout 60 "$PY" -m fomo_agent.cli browser --collect && exit 0
  echo "the poke did not go through; falling back to a restart"
fi

echo "fomo last collected ${age}m ago (hard limit ${HARD_MIN}m) - restarting the collector browser"
systemctl restart radar-browser
