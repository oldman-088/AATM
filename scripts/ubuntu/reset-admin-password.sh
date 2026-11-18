#!/usr/bin/env bash
# AATM — reset the admin password without reinstalling (Ubuntu).
#
# --prompt asks for the new password (hidden, twice). Otherwise AATM_NEW_PASSWORD is used, or a
# password is generated (secrets.token_urlsafe(18)) and printed once; a password you chose is never
# printed. It is stored only as a PBKDF2 hash and token_version is bumped, so every existing JWT is
# rejected. A change is required at the next login unless --no-force-change is given.
#
# The last line is always "[AATM Reset] SUCCESS: ..." (exit 0) or "[AATM Reset] FAILED: ..." (exit 1).
# SUCCESS is printed only after the change was read back from the database. Outcomes, without any
# password or hash, are appended to logs/admin-password-reset.log.
#
# Usage: scripts/ubuntu/reset-admin-password.sh [--prompt] [--email admin@aatm.local] [--no-force-change]
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

mkdir -p logs
RESET_LOG="${AATM_RESET_LOG:-$ROOT_DIR/logs/admin-password-reset.log}"
touch "$RESET_LOG" && chmod 600 "$RESET_LOG"
log_line() { printf '%s %s\n' "$(date -Is)" "$*" >> "$RESET_LOG"; }
fail() { echo "[AATM Reset] FAILED: $*" >&2; log_line "FAILED: $*"; exit 1; }

EMAIL="${AATM_ADMIN_EMAIL:-}"
FORCE_CHANGE=1
PROMPT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) EMAIL="${2:-}"; shift 2 ;;
    --no-force-change) FORCE_CHANGE=0; shift ;;
    --prompt) PROMPT=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
log_line "started (prompt=$PROMPT force_change=$FORCE_CHANGE)"

[[ -x "$ROOT_DIR/.venv/bin/python" ]] || fail ".venv not found - run scripts/ubuntu/install.sh first."

if [[ $PROMPT -eq 1 ]]; then
  entered=0
  for _ in 1 2 3; do
    IFS= read -rsp "New AATM admin password (at least 12 characters): " first || fail "no password entered."
    echo
    IFS= read -rsp "Type it again: " second || fail "no password entered."
    echo
    if [[ "$first" == "$second" ]]; then entered=1; break; fi
    echo "[AATM Reset] The two entries differ. Try again."
    log_line "entries differed"
  done
  [[ $entered -eq 1 ]] || fail "the two entries differed 3 times; nothing was changed."
  export AATM_NEW_PASSWORD="$first"
  unset first second
fi

echo "[AATM Reset] Applying database migrations first (the users table must exist)..."
if ! migration_output="$(cd "$ROOT_DIR/backend" && "$ROOT_DIR/.venv/bin/alembic" upgrade head 2>&1)"; then
  printf '%s\n' "$migration_output" | tail -n 5 >&2
  fail "database migrations failed; nothing was changed."
fi

AATM_RESET_LOG="$RESET_LOG" AATM_RESET_ROOT="$ROOT_DIR" AATM_RESET_EMAIL="$EMAIL" \
AATM_RESET_FORCE_CHANGE="$FORCE_CHANGE" PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}" \
  "$ROOT_DIR/.venv/bin/python" - <<'PY'
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.db.base import utcnow
from app.models import AuditLog, User


def log_line(text: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with open(os.environ["AATM_RESET_LOG"], "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {text}\n")


def fail(text: str) -> None:
    print(f"[AATM Reset] FAILED: {text}", file=sys.stderr)
    log_line(f"FAILED: {text}")
    raise SystemExit(1)


def backend_database_files() -> set[str] | None:
    """The .db files the running backend has open, or None when that cannot be determined."""
    try:
        pid = int((Path(os.environ["AATM_RESET_ROOT"]) / "logs" / "backend.pid").read_text().strip())
        if b"uvicorn" not in Path(f"/proc/{pid}/cmdline").read_bytes():
            return None
        targets = {os.path.realpath(fd) for fd in map(str, Path(f"/proc/{pid}/fd").iterdir())}
    except (OSError, ValueError):
        return None
    return {target for target in targets if target.endswith(".db")} or None


def describe(exc: Exception) -> str:
    # SQLAlchemy's own message includes the statement parameters (the hash): never show it.
    if isinstance(exc, DBAPIError):
        return f"{type(exc.orig).__name__}: {exc.orig}"
    return type(exc).__name__


settings = get_settings()
email = (os.environ.get("AATM_RESET_EMAIL") or settings.DEFAULT_ADMIN_EMAIL).strip().lower()
force_change = os.environ.get("AATM_RESET_FORCE_CHANGE", "1") == "1"
chosen = os.environ.get("AATM_NEW_PASSWORD") or ""
password = chosen or secrets.token_urlsafe(18)
busy_seconds = float(os.environ.get("AATM_RESET_BUSY_SECONDS") or 60)

if password != password.strip():
    fail("the password must not start or end with a space; nothing was changed.")
if len(password) < 12:
    fail("the new password must be at least 12 characters; nothing was changed.")
if password.lower().startswith("change-") or password.lower() == email:
    fail("refusing a placeholder password or the email address; nothing was changed.")

database_url = settings.get_sync_database_url()
is_sqlite = database_url.startswith("sqlite")
engine = create_engine(database_url, future=True, connect_args={"timeout": busy_seconds} if is_sqlite else {})
target = os.path.realpath(engine.url.database or "") if is_sqlite else (engine.url.host or "database server")
print(f"[AATM Reset] Database: {target}")
log_line(f"database: {target}")
if is_sqlite and not os.environ.get("AATM_RESET_ALLOW_OTHER_DB"):
    backend_files = backend_database_files()
    if backend_files is None:
        print("[AATM Reset] The backend is not running or its database could not be read; continuing.")
    elif target not in backend_files:
        fail(f"the running backend uses {', '.join(sorted(backend_files))}, not {target}; nothing was changed.")
    else:
        print("[AATM Reset] This is the database the running backend has open.")


def apply_reset() -> int:
    with Session(engine) as session:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                full_name="AATM Admin",
                password_hash=hash_password(password),
                role="admin",
                language="ar",
                theme="dark",
                must_change_password=force_change,
                token_version=1,
            )
            session.add(user)
            action = "user.created.admin_password_reset"
        else:
            user.password_hash = hash_password(password)
            user.is_active = True
            user.must_change_password = force_change
            user.token_version = int(user.token_version or 1) + 1
            action = "user.admin_password_reset"
        expected_version = int(user.token_version)
        session.add(
            AuditLog(
                actor="system:reset-admin-password",
                action=action,
                resource_type="user",
                resource_id=email,
                details={"token_version_bumped": True, "must_change_password": force_change},
                severity="warning",
                created_at=utcnow(),
            )
        )
        session.commit()
    return expected_version


try:
    for attempt in range(1, 4):
        try:
            version = apply_reset()
            break
        except OperationalError as exc:
            text = str(exc.orig).lower()
            if attempt == 3 or not ("locked" in text or "busy" in text):
                raise
            print(f"[AATM Reset] The database is busy; retrying ({attempt}/3)...")
            time.sleep(1)

    # Proof, from a new connection: the stored row really holds this password and version.
    engine.dispose()
    with Session(engine) as session:
        stored = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        verified = (
            stored is not None
            and stored.is_active
            and int(stored.token_version) == version
            and verify_password(password, stored.password_hash)
        )
except Exception as exc:  # noqa: BLE001 - the operator gets one clear line, never a traceback with parameters
    fail(f"the database was not updated ({describe(exc)}).")

if not verified:
    fail("the change could not be confirmed when read back from the database.")

if not chosen:
    print(f"[AATM Reset] New password (shown once — save it now): {password}")
if force_change:
    print("[AATM Reset] A password change is required at the next login (must_change_password=true).")
print("[AATM Reset] All previously issued tokens are now invalid (token_version bumped).")
print(f"[AATM Reset] SUCCESS: password changed for {email} (token_version is now {version}, confirmed in the database).")
log_line(f"SUCCESS: {email} token_version={version}")
PY
exit $?
