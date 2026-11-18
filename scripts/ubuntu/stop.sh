#!/usr/bin/env bash
# AATM — stop the backend and frontend processes started by start.sh.
#
# The PID files are validated against the expected process command line before any
# signal is sent, so a recycled PID belonging to an unrelated process is never killed.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
STOP_FAILED=0

stop_service() {
  local name="$1"
  local pattern="$2"
  local pid_file="$ROOT_DIR/logs/${name}.pid"

  if [[ ! -f "$pid_file" ]]; then
    echo "[AATM] $name: no PID file, nothing to stop."
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" ]] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    echo "[AATM] $name: invalid PID file, removing."
    rm -f "$pid_file"
    return 0
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[AATM] $name: not running (stale PID $pid), removing PID file."
    rm -f "$pid_file"
    return 0
  fi

  # Verify the PID really belongs to our process before signalling it.
  local cmdline
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ -n "$cmdline" ]] && ! printf '%s' "$cmdline" | grep -q -- "$pattern"; then
    echo "[AATM] $name: PID $pid does not look like our process ('$cmdline'); skipping." >&2
    rm -f "$pid_file"
    return 0
  fi

  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 120); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "[AATM] $name: still shutting down after 30 seconds (PID $pid); it was not force-killed." >&2
    STOP_FAILED=1
    return 0
  fi
  echo "[AATM] $name: stopped (PID $pid)."
  rm -f "$pid_file"
}

stop_service watchdog "watchdog.py"
stop_service backend "uvicorn"
stop_service frontend "vite"
stop_service bridge "bridge.server"

# Remote worker daemons started via start-worker.sh.
for pid_file in "$ROOT_DIR"/logs/worker-*.pid; do
  [[ -e "$pid_file" ]] || continue
  name="$(basename "$pid_file" .pid)"
  stop_service "$name" "worker_daemon"
done
exit "$STOP_FAILED"
