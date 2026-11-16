#!/usr/bin/env bash
# AATM — install backend + frontend dependencies and prepare the database (Ubuntu).
set -euo pipefail
# Usage: scripts/ubuntu/install.sh [--sqlite] [--retry-downloads]
#   Default (Phase 5): Postgres + RabbitMQ + Redis via docker compose when Docker is available.
#   --sqlite:            keep the single-file SQLite database (dev only; warning shown above 3 employees).
#   --retry-downloads:   retry slow/failing Chromium and sandbox-image downloads (default: 3 attempts,
#                        with this flag: AATM_DOWNLOAD_RETRIES attempts, default 6, longer timeouts).
# Re-running install.sh never overwrites existing secrets in .env and never re-creates an existing admin.
USE_SQLITE=0
RETRY_DOWNLOADS=0
for arg in "$@"; do
  case "$arg" in
    --sqlite) USE_SQLITE=1 ;;
    --retry-downloads) RETRY_DOWNLOADS=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
RETRIES="${AATM_DOWNLOAD_RETRIES:-3}"
if [[ "$RETRY_DOWNLOADS" == "1" && -z "${AATM_DOWNLOAD_RETRIES:-}" ]]; then
  RETRIES=6
fi
export PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT="${PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT:-180000}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/logs"

with_retries() { # with_retries <label> <command...>
  local label="$1"; shift
  local attempt=1
  while true; do
    if "$@"; then return 0; fi
    if (( attempt >= RETRIES )); then
      echo "[AATM Ubuntu Install] ${label} failed after ${attempt} attempt(s)." >&2
      return 1
    fi
    local backoff=$(( attempt * 5 ))
    echo "[AATM Ubuntu Install] ${label} failed (attempt ${attempt}/${RETRIES}); retrying in ${backoff}s..." >&2
    sleep "$backoff"
    attempt=$(( attempt + 1 ))
  done
}

echo "[AATM Ubuntu Install] Checking prerequisites..."
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required." >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "ERROR: git is required." >&2; exit 1; }

echo "[AATM Ubuntu Install] Creating Python virtual environment..."
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r backend/requirements.txt

echo "[AATM Ubuntu Install] Installing Playwright Chromium browser (timeout ${PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT} ms, ${RETRIES} attempt(s))..."
with_retries "Playwright Chromium download" .venv/bin/python -m playwright install chromium || \
  echo "[AATM Ubuntu Install] WARNING: Chromium could not be downloaded (offline or restricted network). Re-run: scripts/ubuntu/install.sh --retry-downloads"

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
  echo "[AATM Ubuntu Install] Generated fresh secrets in .env (this file is chmod 600 and git-ignored)."
else
  GENERATED_ADMIN_PW=""
  echo "[AATM Ubuntu Install] .env already exists — keeping existing secrets (nothing was overwritten)."
fi

# Always pin the SQLite file to an absolute path under the repository root: a relative URL such as
# "./data/aatm.db" is resolved against the caller's working directory, which made `cd backend` +
# `alembic upgrade head` fail with "unable to open database file".
.venv/bin/python - <<'PY'
from pathlib import Path

root = Path.cwd().resolve()
data = root / "data"
data.mkdir(parents=True, exist_ok=True)
db_file = data / "aatm.db"
env_path = Path(".env")
lines = env_path.read_text(encoding="utf-8").splitlines()
wanted = {
    "DATABASE_URL": f"sqlite+aiosqlite:///{db_file.as_posix()}",
    "SYNC_DATABASE_URL": f"sqlite:///{db_file.as_posix()}",
}
out = []
for line in lines:
    key = line.split("=", 1)[0].strip()
    # Never touch an existing Postgres URL: only pin SQLite file paths to absolute locations.
    if key in wanted and not line.lstrip().startswith("#") and "sqlite" in line:
        out.append(f"{key}={wanted[key]}")
    else:
        out.append(line)
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"[AATM Ubuntu Install] SQLite fallback path pinned: {db_file}")
PY
chmod 600 .env

if [[ "$USE_SQLITE" == "0" ]] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "[AATM Ubuntu Install] Choosing free host ports for Postgres, RabbitMQ and Redis..."
  .venv/bin/python - <<'PY'
import socket
from pathlib import Path


def is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pinned_port(value: str) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 and is_free(port) else None


def ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


env_path = Path(".env")
lines = env_path.read_text(encoding="utf-8").splitlines()
existing = {}
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        existing[key.strip()] = value.strip()

# Preferred Docker defaults, unless the port is already taken by something else on this machine.
ports: dict[str, int] = {}
taken: set = set()
for key, preferred in (
    ("POSTGRES_PORT", 5432),
    ("RABBITMQ_PORT", 5672),
    ("RABBITMQ_MANAGEMENT_PORT", 15672),
    ("REDIS_PORT", 6379),
):
    keep = pinned_port(existing.get(key, ""))
    if keep is not None and keep not in taken:
        ports[key] = keep
        taken.add(keep)
        continue
    port = preferred if preferred not in taken and is_free(preferred) else ephemeral_port()
    while port in taken:
        port = ephemeral_port()
    ports[key] = port
    taken.add(port)

out, seen = [], set()
for line in lines:
    key = line.split("=", 1)[0].strip()
    if key in ports and not line.lstrip().startswith("#"):
        out.append(f"{key}={ports[key]}")
        seen.add(key)
    else:
        out.append(line)
out += [f"{key}={value}" for key, value in ports.items() if key not in seen]
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
for key, value in ports.items():
    print(f"[AATM Ubuntu Install] {key}={value}")
PY
  chmod 600 .env

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
pg_port = values.get("POSTGRES_PORT", "5432")
rmq_port = values.get("RABBITMQ_PORT", "5672")
rds_port = values.get("REDIS_PORT", "6379")


def url(scheme: str, user: str, password: str, rest: str) -> str:
    return scheme + "://" + user + ":" + password + "@" + rest


wanted = {
    "DATABASE_URL": url("postgresql+psycopg", "aatm", pg, f"127.0.0.1:{pg_port}/aatm"),
    "SYNC_DATABASE_URL": url("postgresql+psycopg", "aatm", pg, f"127.0.0.1:{pg_port}/aatm"),
    "RABBITMQ_URL": url("amqp", "aatm", rmq, f"127.0.0.1:{rmq_port}/"),
    "REDIS_URL": url("redis", "", rds, f"127.0.0.1:{rds_port}/0"),
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
print(
    "[AATM Ubuntu Install] Services on localhost: "
    f"Postgres {pg_port}, RabbitMQ {rmq_port} (management UI {values.get('RABBITMQ_MANAGEMENT_PORT', '15672')}), "
    f"Redis {rds_port}"
)
PY
    chmod 600 .env
    echo "[AATM Ubuntu Install] .env now uses Postgres + RabbitMQ + Redis on the ports above."
  else
    echo "[AATM Ubuntu Install] WARNING: docker compose failed; keeping SQLite and in-process queue/coordination fallbacks."
  fi
elif [[ "$USE_SQLITE" == "0" ]]; then
  echo "[AATM Ubuntu Install] WARNING: Docker not available - using SQLite with in-process queue fallbacks."
  echo "  For Postgres + RabbitMQ + Redis install Docker: https://docs.docker.com/engine/install/ubuntu/"
fi

echo "[AATM Ubuntu Install] Running database migrations..."
(set -a; [[ -f .env ]] && . ./.env; set +a; cd backend && ../.venv/bin/alembic upgrade head)

# Report the admin account honestly: an existing user is never re-created or re-hashed here.
PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python - <<'PY'
from sqlalchemy import create_engine, text

from app.core.config import get_settings

settings = get_settings()
try:
    engine = create_engine(settings.get_sync_database_url(), future=True)
    with engine.connect() as connection:
        existing = connection.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
except Exception as exc:  # noqa: BLE001 - reporting only
    print(f"[AATM Ubuntu Install] Could not inspect the users table: {type(exc).__name__}")
else:
    if existing:
        print("[AATM Ubuntu Install] Existing admin kept — the current password was NOT changed.")
        print("  Reset it with: scripts/ubuntu/reset-admin-password.sh")
    else:
        print(f"[AATM Ubuntu Install] Admin account created: {settings.DEFAULT_ADMIN_EMAIL}")
        print("  The password is the one printed above (or the DEFAULT_ADMIN_PASSWORD in .env).")
PY

if command -v docker >/dev/null 2>&1; then
  echo "[AATM Ubuntu Install] Building sandbox image aatm-sandbox:latest (${RETRIES} attempt(s))..."
  with_retries "Sandbox image build" docker build -t aatm-sandbox:latest -f docker/sandbox.Dockerfile . || \
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

echo "[AATM Ubuntu Install] Complete! Run scripts/ubuntu/start.sh to launch (or scripts/ubuntu/restart.sh to replace a stale process)."
