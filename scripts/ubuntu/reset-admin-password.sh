#!/usr/bin/env bash
# AATM — reset the admin password without reinstalling (Ubuntu).
#
# The new password is generated locally (secrets.token_urlsafe(18)) unless AATM_NEW_PASSWORD is
# exported. It is printed once, stored only as a PBKDF2 hash, forces a password change at the next
# login (must_change_password=true) and bumps token_version so every existing JWT is rejected.
#
# Usage: scripts/ubuntu/reset-admin-password.sh [--email admin@aatm.local] [--no-force-change]
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

EMAIL="${AATM_ADMIN_EMAIL:-}"
FORCE_CHANGE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) EMAIL="${2:-}"; shift 2 ;;
    --no-force-change) FORCE_CHANGE=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  echo "[AATM Reset] ERROR: .venv not found - run scripts/ubuntu/install.sh first." >&2
  exit 1
fi

echo "[AATM Reset] Applying database migrations first (the users table must exist)..."
(cd "$ROOT_DIR/backend" && "$ROOT_DIR/.venv/bin/alembic" upgrade head)

AATM_RESET_EMAIL="$EMAIL" AATM_RESET_FORCE_CHANGE="$FORCE_CHANGE" PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}" \
  "$ROOT_DIR/.venv/bin/python" - <<'PY'
import os
import secrets

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import utcnow
from app.models import AuditLog, User

settings = get_settings()
email = (os.environ.get("AATM_RESET_EMAIL") or settings.DEFAULT_ADMIN_EMAIL).strip().lower()
force_change = os.environ.get("AATM_RESET_FORCE_CHANGE", "1") == "1"
password = (os.environ.get("AATM_NEW_PASSWORD") or "").strip() or secrets.token_urlsafe(18)
if len(password) < 12:
    raise SystemExit("[AATM Reset] ERROR: the new password must be at least 12 characters.")
if password.lower().startswith("change-") or password == email:
    raise SystemExit("[AATM Reset] ERROR: refusing a placeholder password or the email address.")

engine = create_engine(settings.get_sync_database_url(), future=True)
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

print(f"[AATM Reset] Admin account: {email}")
print(f"[AATM Reset] New password (shown once — save it now): {password}")
if force_change:
    print("[AATM Reset] A password change is required at the next login (must_change_password=true).")
print("[AATM Reset] All previously issued tokens are now invalid (token_version bumped).")
PY
