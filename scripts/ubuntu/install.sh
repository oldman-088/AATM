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
  echo "[AATM Ubuntu Install] Generating .env with cryptographically random secrets..."
  GENERATED_ADMIN_PW="$(
    .venv/bin/python - <<'PY'
import secrets
from pathlib import Path

secret_key = secrets.token_hex(32)
admin_pw = secrets.token_urlsafe(18)
bridge_tok = secrets.token_hex(24)
pg_pw = secrets.token_hex(20)

content = Path(".env.example").read_text(encoding="utf-8")
replacements = {
    "SECRET_KEY=change-this-to-a-secure-random-32-plus-char-secret": f"SECRET_KEY={secret_key}",
    "DEFAULT_ADMIN_PASSWORD=change-me-immediately": f"DEFAULT_ADMIN_PASSWORD={admin_pw}",
    "ARENA_BRIDGE_TOKEN=": f"ARENA_BRIDGE_TOKEN={bridge_tok}",
    "POSTGRES_PASSWORD=": f"POSTGRES_PASSWORD={pg_pw}",
}
for old, new in replacements.items():
    content = content.replace(old, new)

Path(".env").write_text(content, encoding="utf-8")
print(admin_pw)
PY
  )"
  chmod 600 .env
  echo "[AATM Ubuntu Install] Initial admin account: admin@aatm.local"
  echo "[AATM Ubuntu Install] Generated admin password (shown once — save it now): ${GENERATED_ADMIN_PW}"
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
