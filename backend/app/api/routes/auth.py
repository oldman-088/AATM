from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.models import AuditLog, User
from app.schemas.common import LoginRequest, TokenResponse, UserPreferencesUpdate

router = APIRouter(prefix="/auth", tags=["auth"])


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "language": user.language,
        "theme": user.theme,
    }


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.email == body.email))
    user = res.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    token = create_access_token(subject=user.email, role=user.role)
    db.add(AuditLog(actor=user.email, action="auth.login", resource_type="user", resource_id=user.id, details={}))
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
