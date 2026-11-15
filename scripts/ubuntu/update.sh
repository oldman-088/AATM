#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

echo "[AATM Update] Pulling latest dependencies and applying migrations..."
.venv/bin/pip install -r backend/requirements.txt
(cd backend && ../.venv/bin/alembic upgrade head)
(cd frontend && npm install && npm run build)
echo "[AATM Update] Complete."
