#!/usr/bin/env bash
# AATM — update dependencies, apply migrations and re-verify (Ubuntu).
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x .venv/bin/pip ]]; then
  echo "[AATM Update] ERROR: .venv not found - run scripts/ubuntu/install.sh first." >&2
  exit 1
fi

echo "[AATM Update] Pulling latest dependencies and applying migrations..."
.venv/bin/pip install -r backend/requirements.txt
(cd backend && ../.venv/bin/alembic upgrade head)
if command -v npm >/dev/null 2>&1; then
  (cd frontend && npm install && npm run build)
fi

echo "[AATM Update] Re-running the verification suite..."
scripts/ubuntu/verify.sh || {
  echo "[AATM Update] ERROR: verification failed after the update." >&2
  exit 1
}
echo "[AATM Update] Complete."
