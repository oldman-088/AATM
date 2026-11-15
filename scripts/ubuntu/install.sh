#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

echo "[AATM Ubuntu Install] Creating Python virtual environment..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r backend/requirements.txt

echo "[AATM Ubuntu Install] Installing Playwright Chromium browser..."
.venv/bin/playwright install chromium || echo "Warning: Chromium download skipped (offline or restricted network)."

echo "[AATM Ubuntu Install] Running database migrations..."
(cd backend && ../.venv/bin/alembic upgrade head)

echo "[AATM Ubuntu Install] Installing and building frontend..."
(cd frontend && npm install && npm run build)

echo "[AATM Ubuntu Install] Complete! Run scripts/ubuntu/start.sh to launch."
