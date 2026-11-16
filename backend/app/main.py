import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.api.routes import (
    arena,
    auth,
    dashboard,
    devices,
    governance,
    infra,
    orchestration,
    platform,
    projects,
    reviews,
    scaling,
)
from app.browser.manager import browser_manager
from app.core.config import get_settings, validate_runtime_security
from app.core.events import event_bus
from app.core.security import decode_access_token
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, get_db
from app.models import User
from app.providers.router import get_model_configuration_status
from app.runtime.sandbox import get_sandbox_status

WS_AUTH_TIMEOUT_SECONDS = 5.0


logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_security(get_settings())
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)

    from app.runtime.background_scheduler import BackgroundScheduler

    background_scheduler = BackgroundScheduler()
    app.state.background_scheduler = background_scheduler
    orchestration_scheduler_task = asyncio.create_task(
        background_scheduler.run_orchestration_forever(), name="aatm-orchestration-scheduler"
    )
    self_healing_task = asyncio.create_task(
        background_scheduler.run_self_healing_forever(), name="aatm-self-healing"
    )

    agent_loop_task: asyncio.Task[None] | None = None
    if get_settings().AGENT_LOOP_ENABLED:
        from app.runtime.agent_loop import AgentLoop

        loop = AgentLoop()
        app.state.agent_loop = loop
        agent_loop_task = asyncio.create_task(loop.run_forever(), name="aatm-agent-loop")
    from app.coordination import get_coordinator

    fanout_task: asyncio.Task[None] | None = None
    try:
        fanout_task = await get_coordinator().subscribe_events(event_bus.deliver_remote)
    except Exception as exc:  # noqa: BLE001 - Redis optional; single-process delivery still works
        logger.warning("Redis event fan-out unavailable (%s); using in-process events only", type(exc).__name__)
    try:
        yield
    finally:
        running_tasks = [orchestration_scheduler_task, self_healing_task]
        if agent_loop_task is not None:
            running_tasks.append(agent_loop_task)
        if fanout_task is not None:
            running_tasks.append(fanout_task)
        for task in running_tasks:
            task.cancel()
        await asyncio.gather(*running_tasks, return_exceptions=True)
        await browser_manager.shutdown_all()
        from app.queue import get_wake_bus

        for closer in (get_wake_bus().stop, get_coordinator().close):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


settings = get_settings()
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self' ws: wss:",
    )
    if request.url.path.startswith("/api/v1/auth"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    payload: dict = {
        "detail": exc.detail,
        "error": {
            "code": f"HTTP_{exc.status_code}",
            "message": str(exc.detail),
            "request_id": str(uuid.uuid4()),
        },
    }
    if isinstance(exc.detail, dict) and exc.detail.get("code"):
        code = str(exc.detail["code"])
        payload["code"] = code
        payload["error"]["code"] = code
        if "message" in exc.detail:
            payload["error"]["message"] = str(exc.detail["message"])
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content=payload,
    )


@app.get("/api/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        model_status = await get_model_configuration_status(db)
    except Exception:
        model_status = {
            "status": "NO_MODEL_CONFIGURED",
            "configured": False,
            "configured_providers": [],
            "diagnostic_only_providers": [],
            "reason": "Provider table unavailable.",
        }
    return {
        "status": "ok",
        "service": "aatm-backend",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "sandbox": await get_sandbox_status(),
        "model_status": model_status,
    }


API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(projects.router, prefix=API_PREFIX)
app.include_router(orchestration.router, prefix=API_PREFIX)
app.include_router(infra.router, prefix=API_PREFIX)
app.include_router(governance.router, prefix=API_PREFIX)
app.include_router(platform.router, prefix=API_PREFIX)
app.include_router(platform.public_router, prefix=API_PREFIX)
app.include_router(devices.router, prefix=API_PREFIX)
app.include_router(reviews.router, prefix=API_PREFIX)
app.include_router(scaling.router, prefix=API_PREFIX)
app.include_router(arena.router, prefix=API_PREFIX)
app.include_router(dashboard.router, prefix=API_PREFIX)


async def _authenticate_websocket(websocket: WebSocket) -> tuple[User | None, int, str]:
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return None, 4401, "Unauthorized"

    token = ""
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                token = str(parsed.get("token") or "").strip()
        except json.JSONDecodeError:
            token = ""
    else:
        token = stripped

    if not token:
        return None, 4401, "Unauthorized"
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        return None, 4401, "Unauthorized"
    if not payload or not payload.get("sub"):
        return None, 4401, "Unauthorized"

    raw_version = payload.get("token_version")
    if raw_version is None or isinstance(raw_version, bool):
        return None, 4401, "Unauthorized"
    try:
        token_version = int(raw_version)
    except (TypeError, ValueError):
        return None, 4401, "Unauthorized"

    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.email == str(payload["sub"])))
        user = res.scalar_one_or_none()
        if not user or not user.is_active or int(user.token_version) != token_version:
            return None, 4401, "Unauthorized"
        if user.must_change_password:
            return None, 4403, "PASSWORD_CHANGE_REQUIRED"
        return user, 0, ""


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await websocket.accept()
    user, close_code, close_reason = await _authenticate_websocket(websocket)
    if user is None:
        try:
            await websocket.close(code=close_code, reason=close_reason)
        except Exception:
            pass
        return

    await event_bus.connect(websocket, accept=False)
    await websocket.send_text(json.dumps({"type": "auth.ok", "actor": user.email}))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await event_bus.disconnect(websocket)


# Serve built frontend in production if present
DIST_DIR = Path(settings.REPO_ROOT) / "frontend" / "dist"
if DIST_DIR.exists():
    assets_dir = DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("ws/"):
            raise HTTPException(status_code=404, detail="Not found")
        target = DIST_DIR / full_path
        if target.is_file():
            return FileResponse(target)
        return FileResponse(DIST_DIR / "index.html")
