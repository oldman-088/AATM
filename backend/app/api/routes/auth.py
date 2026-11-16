from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import login_rate_limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models import AuditLog, User
from app.schemas.common import ChangePasswordRequest, LoginRequest, TokenResponse, UserPreferencesUpdate

router = APIRouter(prefix="/auth", tags=["auth"])


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "language": user.language,
        "theme": user.theme,
        "must_change_password": bool(user.must_change_password),
    }


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = _client_ip(request)
    email_key = body.email.strip().lower()
    rate_key = f"{client_ip}:{email_key}"

    allowed, retry_after = login_rate_limiter.check_allowed(rate_key)
    if not allowed:
        db.add(
            AuditLog(
                actor=email_key or "anonymous",
                action="auth.login_rate_limited",
                severity="warning",
                resource_type="user",
                resource_id=email_key or None,
                details={"ip": client_ip, "retry_after_seconds": retry_after},
            )
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    res = await db.execute(select(User).where(User.email == body.email))
    user = res.scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        failures = login_rate_limiter.record_failure(rate_key)
        db.add(
            AuditLog(
                actor=email_key or "anonymous",
                action="auth.login_failed",
                severity="warning",
                resource_type="user",
                resource_id=user.id if user else None,
                details={"ip": client_ip, "attempts": failures},
            )
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    login_rate_limiter.reset_key(rate_key)
    token = create_access_token(subject=user.email, role=user.role)
    db.add(
        AuditLog(
            actor=user.email,
            action="auth.login",
            resource_type="user",
            resource_id=user.id,
            details={"ip": client_ip},
        )
    )
    await db.commit()
    return TokenResponse(access_token=token, user=_serialize_user(user))


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    return _serialize_user(user)


@router.patch("/preferences")
async def update_preferences(
    body: UserPreferencesUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.language is not None:
        user.language = body.language
    if body.theme is not None:
        user.theme = body.theme
    await db.commit()
    await db.refresh(user)
    return _serialize_user(user)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must differ from current password")
    settings = get_settings()
    if body.new_password == settings.DEFAULT_ADMIN_PASSWORD and body.new_password == "aatm-admin-2026":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Default placeholder password is not allowed")

    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    db.add(
        AuditLog(
            actor=user.email,
            action="auth.password_changed",
            resource_type="user",
            resource_id=user.id,
            details={},
        )
    )
    await db.commit()
    await db.refresh(user)
    return {"ok": True, "user": _serialize_user(user)}
