#!/usr/bin/env bash
# AATM — restart the backend, bridge and frontend with the current code (Ubuntu).
#
# Use this after pulling changes or whenever `start.sh` reported "already running" while the
# API was in fact unhealthy. It stops every process started by start.sh (verifying the PID's
# command line first) and starts a fresh set, then waits for /api/health.
#
# Usage: scripts/ubuntu/restart.sh [--keep-frontend] [--keep-bridge]
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

KEEP_FRONTEND=0
KEEP_BRIDGE=0
for arg in "$@"; do
  case "$arg" in
    --keep-frontend) KEEP_FRONTEND=1 ;;
    --keep-bridge) KEEP_BRIDGE=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

if [[ ! -x "$ROOT_DIR/.venv/bin/uvicorn" ]]; then
  echo "[AATM Restart] ERROR: .venv not found - run scripts/ubuntu/install.sh first." >&2
  exit 1
fi

stop_service() { # stop_service <name> <expected pattern>
  local name="$1" pattern="$2" pid_file="$ROOT_DIR/logs/$1.pid"
  [[ -f "$pid_file" ]] || return 0
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  rm -f "$pid_file"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  local cmdline
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ -n "$cmdline" ]] && ! printf '%s' "$cmdline" | grep -q -- "$pattern"; then
    echo "[AATM Restart] $name: PID $pid is not ours; leaving it alone." >&2
    return 0
  fi
  echo "[AATM Restart] Stopping $name (PID $pid)..."
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 24); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  rm -f "$pid_file"
}

stop_service backend uvicorn
[[ "$KEEP_BRIDGE" == "0" ]] && stop_service bridge bridge.server
[[ "$KEEP_FRONTEND" == "0" ]] && stop_service frontend vite

# A backend that was started outside the PID file (e.g. an old manual uvicorn) would keep the port.
if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :${BACKEND_PORT} )" 2>/dev/null | grep -q LISTEN; then
  if curl -fsS --max-time 2 "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1; then
    echo "[AATM Restart] WARNING: something still answers on port ${BACKEND_PORT} after the stop." >&2
    echo "               If it is not yours, free the port or run: BACKEND_PORT=8010 scripts/ubuntu/restart.sh" >&2
  fi
fi

echo "[AATM Restart] Starting a fresh set of processes..."
"$ROOT_DIR/scripts/ubuntu/start.sh"

echo "[AATM Restart] Done. UI: http://localhost:${FRONTEND_PORT} | API: http://localhost:${BACKEND_PORT}/api/health"
