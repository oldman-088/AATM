#!/usr/bin/env bash
# AATM watchdog — read-only monitoring. It never restarts, changes or fixes anything; it only
# reports problems as desktop notifications (notify-send) and lines in logs/watchdog.log.
#
#   scripts/ubuntu/watchdog.sh                 # run in the foreground (Ctrl+C to stop)
#   scripts/ubuntu/watchdog.sh --once          # one check cycle, then exit
#   scripts/ubuntu/watchdog.sh --test-notify   # send one test notification
#
# start.sh launches it in the background (PID in logs/watchdog.pid; WATCHDOG_AUTOSTART=false to
# skip) and stop.sh stops it. WATCHDOG_INTERVAL (seconds, default 60) sets the check interval.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$ROOT_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
exec "$PY" "$ROOT_DIR/scripts/ubuntu/watchdog.py" "$@"
