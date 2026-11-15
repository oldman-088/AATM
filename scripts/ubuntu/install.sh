#!/usr/bin/env bash
# AATM — install backend + frontend dependencies and prepare the database (Ubuntu).
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

echo "[AATM Ubuntu Install] Checking prerequisites..."
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required." >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "ERROR: git is required." >&2; exit 1; }

echo "[AATM Ubuntu Install] Creating Python virtual environment..."
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r backend/requirements.txt

echo "[AATM Ubuntu Install] Installing Playwright Chromium browser..."
.venv/bin/playwright install chromium || echo "Warning: Chromium download skipped (offline or restricted network)."

echo "[AATM Ubuntu Install] Running database migrations..."
(cd backend && ../.venv/bin/alembic upgrade head)

if [[ ! -f .env ]]; then
  echo "[AATM Ubuntu Install] Creating .env from .env.example (review it before production use)."
  cp .env.example .env
  chmod 600 .env
fi

if command -v npm >/dev/null 2>&1; then
  echo "[AATM Ubuntu Install] Installing and building frontend..."
  (cd frontend && npm install && npm run build)
else
  echo "[AATM Ubuntu Install] WARNING: npm not found - skipping the frontend build."
fi

echo "[AATM Ubuntu Install] Verifying the installation..."
scripts/ubuntu/verify.sh --skip-frontend || {
  echo "[AATM Ubuntu Install] ERROR: verification failed. See the output above." >&2
  exit 1
}

echo "[AATM Ubuntu Install] Complete! Run scripts/ubuntu/start.sh to launch."
