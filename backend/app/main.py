import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

import app.models  # noqa: F401
from app.api.routes import auth, devices, governance, infra, orchestration, platform, projects
from app.browser.manager import browser_manager
from app.core.config import get_settings, validate_runtime_security
from app.core.events import event_bus
from app.core.security import decode_access_token
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.models import User

WS_AUTH_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_security(get_settings())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
    try:
        yield
    finally:
        running_tasks = [orchestration_scheduler_task, self_healing_task]
        if agent_loop_task is not None:
            running_tasks.append(agent_loop_task)
        for task in running_tasks:
            task.cancel()
        await asyncio.gather(*running_tasks, return_exceptions=True)
        await browser_manager.shutdown_all()


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


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "detail": exc.detail,
            "error": {
                "code": f"HTTP_{exc.status_code}",
                "message": str(exc.detail),
                "request_id": str(uuid.uuid4()),
            },
        },
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "aatm-backend",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
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


async def _authenticate_websocket(websocket: WebSocket) -> User | None:
    token = (websocket.query_params.get("token") or "").strip()
    if not token:
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            return None
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
        return None
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        return None
    if not payload or not payload.get("sub"):
        return None

    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.email == str(payload["sub"])))
        user = res.scalar_one_or_none()
        if not user or not user.is_active:
            return None
        return user


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await websocket.accept()
    user = await _authenticate_websocket(websocket)
    if user is None:
        try:
            await websocket.close(code=4401, reason="Unauthorized")
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
