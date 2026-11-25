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

bridge_port_open() {
  "$ROOT_DIR/.venv/bin/python" - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(1.0)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", port)) == 0 else 1)
PY
}

stop_pid_file() { # stop_pid_file <name> <expected pattern>
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
    echo "[AATM] $name: PID $pid is not ours ('$cmdline'); leaving it alone." >&2
    return 0
  fi
  echo "[AATM] $name: stopping stale process (PID $pid)..."
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
}

backend_healthy() {
  curl -fsS --max-time 2 "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1
}


"$ROOT_DIR/scripts/ubuntu/start-infra.sh"

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
# It reads ARENA_BRIDGE_TOKEN and ARENA_CONNECTOR_ENABLED from .env; real Arena stays off
# unless ARENA_CONNECTOR_ENABLED=true.
if [[ "${ARENA_BRIDGE_AUTOSTART:-true}" == "true" ]]; then
  if [[ -f logs/bridge.pid ]] && kill -0 "$(cat logs/bridge.pid)" 2>/dev/null && \
     bridge_port_open "${ARENA_BRIDGE_PORT:-4100}"; then
    echo "[AATM] Bridge already running (PID $(cat logs/bridge.pid))."
  else
    if [[ -f logs/bridge.pid ]]; then
      echo "[AATM] Bridge PID file is stale or the bridge is not listening — restarting it."
      stop_pid_file bridge bridge.server
    fi
    echo "[AATM] Starting local Arena bridge on 127.0.0.1:${ARENA_BRIDGE_PORT:-4100}..."
    (set -a; [[ -f .env ]] && . ./.env; set +a; \
      ARENA_CONNECTOR_STATE_FILE="${ARENA_CONNECTOR_STATE_FILE:-$HOME/.aatm/bridge/connector-profiles.json}" \
      ARENA_BRIDGE_HOST=127.0.0.1 nohup "$ROOT_DIR/.venv/bin/python" -m bridge.server \
      > "$ROOT_DIR/logs/bridge.log" 2>&1 & echo $! > "$ROOT_DIR/logs/bridge.pid")
  fi
fi

# A PID file is not proof of a working backend: the process can be alive but dead-locked, failed
# to migrate, or serving an old build. Health-check first, replace a stale process, and never keep
# reporting "already running" for a backend that cannot answer /api/health.
if backend_healthy; then
  if [[ -f logs/backend.pid ]]; then
    echo "[AATM] Backend already running and healthy (PID $(cat logs/backend.pid))."
  else
    echo "[AATM] A healthy backend is already serving port ${BACKEND_PORT} (not started by this script)."
    echo "       Use scripts/ubuntu/restart.sh to replace it."
  fi
else
  if [[ -f logs/backend.pid ]] && kill -0 "$(cat logs/backend.pid)" 2>/dev/null; then
    echo "[AATM] Backend process exists (PID $(cat logs/backend.pid)) but /api/health fails — replacing it."
  elif command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :${BACKEND_PORT} )" 2>/dev/null | grep -q LISTEN; then
    echo "[AATM] ERROR: port ${BACKEND_PORT} is in use by a process that is not answering /api/health." >&2
    echo "       Free the port or choose another one: BACKEND_PORT=8010 scripts/ubuntu/start.sh" >&2
    exit 1
  fi
  stop_pid_file backend uvicorn
  echo "[AATM] Starting Backend API on 0.0.0.0:${BACKEND_PORT}..."
  # Export the root .env into the backend's environment, exactly like the bridge above: env:VAR
  # credential refs and the backend -> bridge ARENA_BRIDGE_TOKEN are read from the process
  # environment, so both processes must get the same token from the same file.
  (set -a; [[ -f .env ]] && . ./.env; set +a; \
    AATM_AUTO_SNAPSHOT="${AATM_AUTO_SNAPSHOT:-1}" nohup "$ROOT_DIR/.venv/bin/uvicorn" app.main:app \
    --app-dir "$ROOT_DIR/backend" --host 0.0.0.0 --port "$BACKEND_PORT" \
    > "$ROOT_DIR/logs/backend.log" 2>&1 & echo $! > "$ROOT_DIR/logs/backend.pid")
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

if [[ -x frontend/node_modules/.bin/vite ]]; then
  if [[ -f logs/frontend.pid ]] && kill -0 "$(cat logs/frontend.pid)" 2>/dev/null; then
    echo "[AATM] Frontend already running (PID $(cat logs/frontend.pid))."
  elif bridge_port_open "$FRONTEND_PORT"; then
    echo "[AATM] Port ${FRONTEND_PORT} is already serving; not starting another frontend."
  else
    echo "[AATM] Starting Frontend Dev Server on 0.0.0.0:${FRONTEND_PORT}..."
    # Start vite directly so the PID file names the real server and stop.sh can stop it.
    (cd frontend; nohup ./node_modules/.bin/vite --host 0.0.0.0 --port "$FRONTEND_PORT" --strictPort \
      > "$ROOT_DIR/logs/frontend.log" 2>&1 < /dev/null & echo $! > "$ROOT_DIR/logs/frontend.pid")
  fi
else
  echo "[AATM] Frontend skipped (frontend/node_modules is missing)."
fi

# Watchdog (notifications + logs/watchdog.log). It only reloads a disconnected Arena window (E56).
if [[ "${WATCHDOG_AUTOSTART:-true}" == "true" ]]; then
  if [[ -f logs/watchdog.pid ]] && kill -0 "$(cat logs/watchdog.pid)" 2>/dev/null; then
    echo "[AATM] Watchdog already running (PID $(cat logs/watchdog.pid))."
  else
    echo "[AATM] Starting watchdog (log: logs/watchdog.log)..."
    nohup "$ROOT_DIR/scripts/ubuntu/watchdog.sh" >> "$ROOT_DIR/logs/watchdog.out" 2>&1 &
    echo $! > "$ROOT_DIR/logs/watchdog.pid"
  fi
fi

# Optional send-only Telegram alerts (TELEGRAM_BOT_ENABLED=true in .env). No webhook and no open port.
if "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/ubuntu/telegram_alerts.py" --check-enabled; then
  if [[ -f logs/telegram.pid ]] && kill -0 "$(cat logs/telegram.pid)" 2>/dev/null; then
    echo "[AATM] Telegram alerts already running (PID $(cat logs/telegram.pid))."
  else
    echo "[AATM] Starting Telegram alerts (log: logs/telegram.log)..."
    nohup "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/ubuntu/telegram_alerts.py" >> "$ROOT_DIR/logs/telegram.log" 2>&1 < /dev/null &
    echo $! > "$ROOT_DIR/logs/telegram.pid"
  fi
fi

echo "[AATM] Started! UI: http://localhost:${FRONTEND_PORT} | API: http://localhost:${BACKEND_PORT}/api/health"
