#!/usr/bin/env bash
# AATM — start the backend API and the frontend dev server (Ubuntu).
#
# The backend is health-checked before the script reports success, so a failed start is
# detected immediately instead of silently leaving a dead process behind.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"

if [[ ! -x "$ROOT_DIR/.venv/bin/uvicorn" ]]; then
  echo "[AATM] ERROR: .venv not found - run scripts/ubuntu/install.sh first." >&2
  exit 1
fi

mkdir -p logs

echo "[AATM] Running database migrations (alembic upgrade head)..."
(cd "$ROOT_DIR/backend" && "$ROOT_DIR/.venv/bin/alembic" upgrade head)

if [[ "${AATM_SANDBOX_MODE:-docker}" == "docker" ]]; then
  if command -v docker >/dev/null 2>&1; then
    if ! docker image inspect aatm-sandbox:latest >/dev/null 2>&1; then
      echo "[AATM] Building sandbox image aatm-sandbox:latest from docker/sandbox.Dockerfile..."
      docker build -t aatm-sandbox:latest -f docker/sandbox.Dockerfile . || \
        echo "[AATM] WARNING: sandbox image build failed (SANDBOX_UNAVAILABLE; not falling back to process mode)." >&2
    fi
  else
    echo "[AATM] WARNING: docker not found while AATM_SANDBOX_MODE=docker (SANDBOX_UNAVAILABLE)." >&2
  fi
fi

# Local Arena bridge (loopback only): connector WebSocket + mock Arena page + local bridge API.
# It reads ARENA_BRIDGE_TOKEN and the Arena approval flags from .env; real Arena stays off
# unless ARENA_CONNECTOR_ENABLED, ARENA_AUTOMATION_APPROVED and ARENA_APPROVAL_REFERENCE are set.
if [[ "${ARENA_BRIDGE_AUTOSTART:-true}" == "true" ]]; then
  if [[ -f logs/bridge.pid ]] && kill -0 "$(cat logs/bridge.pid)" 2>/dev/null; then
    echo "[AATM] Bridge already running (PID $(cat logs/bridge.pid))."
  else
    echo "[AATM] Starting local Arena bridge on 127.0.0.1:${ARENA_BRIDGE_PORT:-4100}..."
    (set -a; [[ -f .env ]] && . ./.env; set +a; \
      ARENA_BRIDGE_HOST=127.0.0.1 nohup "$ROOT_DIR/.venv/bin/python" -m bridge.server \
      > "$ROOT_DIR/logs/bridge.log" 2>&1 & echo $! > "$ROOT_DIR/logs/bridge.pid")
  fi
fi

if [[ -f logs/backend.pid ]] && kill -0 "$(cat logs/backend.pid)" 2>/dev/null; then
  echo "[AATM] Backend already running (PID $(cat logs/backend.pid))."
else
  echo "[AATM] Starting Backend API on 0.0.0.0:${BACKEND_PORT}..."
  nohup "$ROOT_DIR/.venv/bin/uvicorn" app.main:app \
    --app-dir "$ROOT_DIR/backend" --host 0.0.0.0 --port "$BACKEND_PORT" \
    > "$ROOT_DIR/logs/backend.log" 2>&1 &
  echo $! > "$ROOT_DIR/logs/backend.pid"
fi

echo "[AATM] Waiting for the backend health endpoint..."
backend_ready=0
for _ in $(seq 1 "$HEALTH_RETRIES"); do
  if curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1; then
    backend_ready=1
    break
  fi
  sleep 1
done
if [[ $backend_ready -ne 1 ]]; then
  echo "[AATM] ERROR: backend did not become healthy. Last log lines:" >&2
  tail -n 30 "$ROOT_DIR/logs/backend.log" >&2 || true
  exit 1
fi
echo "[AATM] Backend healthy: http://localhost:${BACKEND_PORT}/api/health"

if command -v npm >/dev/null 2>&1 && [[ -d frontend ]]; then
  if [[ -f logs/frontend.pid ]] && kill -0 "$(cat logs/frontend.pid)" 2>/dev/null; then
    echo "[AATM] Frontend already running (PID $(cat logs/frontend.pid))."
  else
    echo "[AATM] Starting Frontend Dev Server on 0.0.0.0:${FRONTEND_PORT}..."
    (cd frontend && nohup npm run --silent dev -- --port "$FRONTEND_PORT" \
      > "$ROOT_DIR/logs/frontend.log" 2>&1 & echo $! > "$ROOT_DIR/logs/frontend.pid")
  fi
else
  echo "[AATM] Frontend skipped (npm not available or frontend/ missing)."
fi

echo "[AATM] Started! UI: http://localhost:${FRONTEND_PORT} | API: http://localhost:${BACKEND_PORT}/api/health"
