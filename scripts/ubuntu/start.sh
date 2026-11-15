#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs
echo "[AATM] Starting Backend API on 0.0.0.0:8000..."
nohup "$ROOT_DIR/.venv/bin/uvicorn" app.main:app --app-dir "$ROOT_DIR/backend" --host 0.0.0.0 --port 8000 > "$ROOT_DIR/logs/backend.log" 2>&1 &
echo $! > "$ROOT_DIR/logs/backend.pid"

echo "[AATM] Starting Frontend Dev Server on 0.0.0.0:5173..."
(cd "$ROOT_DIR/frontend" && nohup npm run dev > "$ROOT_DIR/logs/frontend.log" 2>&1 & echo $! > "$ROOT_DIR/logs/frontend.pid")

echo "[AATM] Started! UI: http://localhost:5173 | API: http://localhost:8000/api/health"
