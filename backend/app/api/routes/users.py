"""User administration (admin only): list users and change a user's role or active state.

Password hashes and tokens are never returned. Every change is audited and signs the user out
(token_version + 1), so a new role or a deactivation takes effect at once. The system never
loses its last active admin, and an admin cannot change their own role or access here.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db.session import get_db
from app.models import AuditLog, User

router = APIRouter(prefix="/users", tags=["users"])

ROLES = ("admin", "operator", "viewer")


class UserUpdate(BaseModel):
    role: Literal["admin", "operator", "viewer"] | None = None
    is_active: bool | None = None


def _serialize(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": bool(user.is_active),
        "must_change_password": bool(user.must_change_password),
        "language": user.language,
        "created_at": user.created_at,
    }


@router.get("")
async def list_users(_: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_serialize(u) for u in users]


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdate,
    actor: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.id == actor.id:
        raise HTTPException(status_code=409, detail="you cannot change your own role or access")
    new_role = body.role if body.role is not None else user.role
    new_active = body.is_active if body.is_active is not None else bool(user.is_active)
    if new_role == user.role and new_active == bool(user.is_active):
        return _serialize(user)

    # Backstop: the acting admin is itself an active admin, so with the self-change rule above this only
    # matters if roles or the permission check change later.
    if user.role == "admin" and user.is_active and (new_role != "admin" or not new_active):
        active_admins = (
            await db.execute(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True)))
        ).scalar_one()
        if active_admins <= 1:
            raise HTTPException(status_code=409, detail="the last active admin cannot be demoted or deactivated")

    details = {"from": {"role": user.role, "is_active": bool(user.is_active)}, "to": {"role": new_role, "is_active": new_active}}
    user.role = new_role
    user.is_active = new_active
    user.token_version = int(user.token_version or 1) + 1
    db.add(AuditLog(actor=actor.email, action="user.update", resource_type="user", resource_id=user.id,
                    details=details, severity="warning"))
    await db.commit()
    return _serialize(user)
