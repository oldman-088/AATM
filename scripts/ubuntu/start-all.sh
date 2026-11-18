#!/usr/bin/env bash
# AATM: start everything with one command (containers, services, Arena windows) and report readiness.
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
PY="${AATM_PYTHON:-$ROOT_DIR/.venv/bin/python}"
START_SCRIPT="${AATM_START_SCRIPT:-$ROOT_DIR/scripts/ubuntu/start.sh}"

ensure_container() {
  local name="$1" state
  state="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)"
  if [[ "$state" != "true" ]]; then
    echo "[AATM] Starting container $name..."
    docker start "$name" >/dev/null || echo "[AATM] WARNING: could not start $name." >&2
  fi
}

if command -v docker >/dev/null 2>&1; then
  ensure_container aatm-redis
  ensure_container aatm-rabbitmq
else
  echo "[AATM] WARNING: docker not found; Redis and RabbitMQ were not started." >&2
fi

"$START_SCRIPT" || { echo "[AATM] ERROR: the services did not start." >&2; "$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" wait-ready; exit 1; }
"$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" open-windows || true
exec "$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" wait-ready
