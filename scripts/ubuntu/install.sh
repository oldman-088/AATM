#!/usr/bin/env bash
# AATM — install backend + frontend dependencies and prepare the database (Ubuntu).
set -euo pipefail
# Usage: scripts/ubuntu/install.sh [--sqlite]
#   Default (Phase 5): Postgres + RabbitMQ + Redis via docker compose when Docker is available.
#   --sqlite: keep the single-file SQLite database (dev only; warning shown above 3 employees).
USE_SQLITE=0
for arg in "$@"; do
  [[ "$arg" == "--sqlite" ]] && USE_SQLITE=1
done
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
    "RABBITMQ_PASSWORD=": f"RABBITMQ_PASSWORD={secrets.token_hex(20)}",
    "REDIS_PASSWORD=": f"REDIS_PASSWORD={secrets.token_hex(20)}",
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

if [[ "$USE_SQLITE" == "0" ]] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "[AATM Ubuntu Install] Starting Postgres, RabbitMQ and Redis (docker compose, localhost-only ports)..."
  if docker compose --env-file .env up -d --wait postgres rabbitmq redis; then
    .venv/bin/python - <<'PY'
from pathlib import Path

env_path = Path(".env")
lines = env_path.read_text(encoding="utf-8").splitlines()
values = dict(line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))
pg = values.get("POSTGRES_PASSWORD", "")
rmq = values.get("RABBITMQ_PASSWORD", "")
rds = values.get("REDIS_PASSWORD", "")
def url(scheme: str, user: str, password: str, rest: str) -> str:
    return scheme + "://" + user + ":" + password + "@" + rest


wanted = {
    "DATABASE_URL": url("postgresql+psycopg", "aatm", pg, "127.0.0.1:5432/aatm"),
    "SYNC_DATABASE_URL": url("postgresql+psycopg", "aatm", pg, "127.0.0.1:5432/aatm"),
    "RABBITMQ_URL": url("amqp", "aatm", rmq, "127.0.0.1:5672/"),
    "REDIS_URL": url("redis", "", rds, "127.0.0.1:6379/0"),
}
out, seen = [], set()
for line in lines:
    key = line.split("=", 1)[0].strip()
    if key in wanted and not line.lstrip().startswith("#"):
        out.append(f"{key}={wanted[key]}")
        seen.add(key)
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in wanted.items() if k not in seen]
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
    chmod 600 .env
    echo "[AATM Ubuntu Install] .env now uses Postgres + RabbitMQ + Redis."
  else
    echo "[AATM Ubuntu Install] WARNING: docker compose failed; keeping SQLite and in-process queue/coordination fallbacks."
  fi
elif [[ "$USE_SQLITE" == "0" ]]; then
  echo "[AATM Ubuntu Install] WARNING: Docker not available - using SQLite with in-process queue fallbacks."
  echo "  For Postgres + RabbitMQ + Redis install Docker: https://docs.docker.com/engine/install/ubuntu/"
fi

echo "[AATM Ubuntu Install] Running database migrations..."
(set -a; [[ -f .env ]] && . ./.env; set +a; cd backend && ../.venv/bin/alembic upgrade head)

if command -v docker >/dev/null 2>&1; then
  echo "[AATM Ubuntu Install] Building sandbox image aatm-sandbox:latest..."
  docker build -t aatm-sandbox:latest -f docker/sandbox.Dockerfile . || \
    echo "[AATM Ubuntu Install] WARNING: Docker sandbox image build failed (sandbox will report SANDBOX_UNAVAILABLE until built)."
else
  echo "[AATM Ubuntu Install] WARNING: docker not found - sandbox image aatm-sandbox:latest was not built (SANDBOX_UNAVAILABLE in docker mode)."
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
