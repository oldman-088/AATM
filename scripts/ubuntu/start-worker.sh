#!/usr/bin/env bash
# AATM — start a remote worker daemon on this host (Ubuntu).
#
# The daemon registers with the central AATM server, heartbeats, claims tasks and
# executes them inside an isolated local workspace.
#
# Required environment (or entries in .env):
#   AATM_SERVER_URL    e.g. https://aatm.internal:8000
#   AATM_DEVICE_ID     unique id of this node, e.g. node-ubuntu-01
#   AATM_DEVICE_TOKEN  device token issued by the central server
#
# Optional:
#   AATM_WORKER_WORKSPACE   local workspace root (default: data/remote_workspaces)
#   AATM_WORKER_POLL_SECONDS
#   AATM_WORKER_MAX_CONCURRENT
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${AATM_SERVER_URL:?AATM_SERVER_URL is required (central AATM server URL)}"
: "${AATM_DEVICE_ID:?AATM_DEVICE_ID is required (unique id for this execution node)}"
: "${AATM_DEVICE_TOKEN:?AATM_DEVICE_TOKEN is required (device token issued by the server)}"

PY="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "[AATM Worker] ERROR: $PY not found - run scripts/ubuntu/install.sh on this host first." >&2
  exit 1
fi

mkdir -p logs
WORKSPACE_ROOT="${AATM_WORKER_WORKSPACE:-$ROOT_DIR/data/remote_workspaces}"
LOG_FILE="$ROOT_DIR/logs/worker-${AATM_DEVICE_ID}.log"
PID_FILE="$ROOT_DIR/logs/worker-${AATM_DEVICE_ID}.pid"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[AATM Worker] Already running (PID $OLD_PID)."
    exit 0
  fi
  rm -f "$PID_FILE"
fi

echo "[AATM Worker] Starting daemon for device '$AATM_DEVICE_ID' against $AATM_SERVER_URL..."
PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}" \
AATM_SERVER_URL="$AATM_SERVER_URL" \
AATM_DEVICE_ID="$AATM_DEVICE_ID" \
AATM_DEVICE_TOKEN="$AATM_DEVICE_TOKEN" \
AATM_WORKER_WORKSPACE="$WORKSPACE_ROOT" \
  nohup "$PY" -m app.runtime.worker_daemon \
    --server-url "$AATM_SERVER_URL" \
    --device-id "$AATM_DEVICE_ID" \
    --token "$AATM_DEVICE_TOKEN" \
    --workspace-root "$WORKSPACE_ROOT/$AATM_DEVICE_ID" \
    >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

sleep 2
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  echo "[AATM Worker] Started (PID $PID). Log: $LOG_FILE"
else
  echo "[AATM Worker] ERROR: the daemon exited immediately. Last log lines:" >&2
  tail -n 20 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
