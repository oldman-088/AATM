import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings, is_placeholder_password
from app.core.rate_limit import shared_login_rate_limiter as login_rate_limiter
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


def _is_trusted_proxy(peer_host: str, trusted_proxies: set[str]) -> bool:
    if not peer_host or not trusted_proxies:
        return False
    if peer_host in trusted_proxies:
        return True
    try:
        peer_addr = ipaddress.ip_address(peer_host)
    except ValueError:
        return False
    for entry in trusted_proxies:
        try:
            if "/" in entry:
                if peer_addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif peer_addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _client_ip(request: Request) -> str:
    peer_host = request.client.host if request.client and request.client.host else "unknown"
    trusted_proxies = get_settings().get_trusted_proxies()
    if _is_trusted_proxy(peer_host, trusted_proxies):
        forwarded_raw = request.headers.get("x-forwarded-for", "")
        hops = [part.strip() for part in forwarded_raw.split(",") if part.strip()]
        # Walk right-to-left skipping trusted proxy hops to pick the leftmost client-facing hop
        # appended by our edge, falling back to the first hop if all are untrusted.
        for hop in reversed(hops):
            if not _is_trusted_proxy(hop, trusted_proxies):
                return hop
        if hops:
            return hops[0]
    return peer_host


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = _client_ip(request)
    email_key = body.email.strip().lower()
    ip_email_key = f"ip_email:{client_ip}:{email_key}"
    email_only_key = f"email:{email_key}"

    allowed_ip_email, retry_ip_email = await login_rate_limiter.check_allowed(ip_email_key)
    allowed_email, retry_email = (
        await login_rate_limiter.check_allowed(email_only_key) if email_key else (True, 0)
    )
    if not allowed_ip_email or not allowed_email:
        retry_after = max(retry_ip_email, retry_email)
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
        failures_ip_email = await login_rate_limiter.record_failure(ip_email_key)
        failures_email = await login_rate_limiter.record_failure(email_only_key) if email_key else 0
        db.add(
            AuditLog(
                actor=email_key or "anonymous",
                action="auth.login_failed",
                severity="warning",
                resource_type="user",
                resource_id=user.id if user else None,
                details={"ip": client_ip, "attempts": max(failures_ip_email, failures_email)},
            )
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    await login_rate_limiter.reset_key(ip_email_key)
    if email_key:
        await login_rate_limiter.reset_key(email_only_key)
    token = create_access_token(
        subject=user.email,
        role=user.role,
        token_version=user.token_version,
    )
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


@router.post("/logout")
async def logout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user.token_version = int(user.token_version or 1) + 1
    db.add(
        AuditLog(
            actor=user.email,
            action="auth.logout",
            resource_type="user",
            resource_id=user.id,
            details={"token_version": user.token_version},
        )
    )
    await db.commit()
    return {"ok": True}


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
    if body.new_password.strip().lower() == user.email.strip().lower():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password must not match your email address")
    settings = get_settings()
    if is_placeholder_password(body.new_password) or body.new_password == settings.DEFAULT_ADMIN_PASSWORD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Default or placeholder password is not allowed")

    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.token_version = int(user.token_version or 1) + 1
    new_token = create_access_token(
        subject=user.email,
        role=user.role,
        token_version=user.token_version,
    )
    db.add(
        AuditLog(
            actor=user.email,
            action="auth.password_changed",
            resource_type="user",
            resource_id=user.id,
            details={"token_version": user.token_version},
        )
    )
    await db.commit()
    await db.refresh(user)
    return {
        "ok": True,
        "access_token": new_token,
        "token_type": "bearer",
        "user": _serialize_user(user),
    }
