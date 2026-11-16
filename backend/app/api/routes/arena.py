"""Phase 6 API: Profiles Manager (Browser Wall) and the Arena browser connector.

Real-Arena paths return ``not_enabled`` (HTTP 409) unless ARENA_CONNECTOR_ENABLED and
ARENA_AUTOMATION_APPROVED are true and ARENA_APPROVAL_REFERENCE is recorded.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.browser.arena_connector import (
    NOT_ENABLED_MESSAGE,
    SCRIPT_VERSION,
    arena_domains,
    arena_gate,
    bridge_call,
    build_userscript,
    ensure_arena_browser_provider,
    match_patterns,
    mint_token,
    real_arena_enabled,
    register_profile_with_bridge,
)
from app.browser.engines import validate_userscript_policy
from app.browser.manager import browser_manager
from app.core.events import event_bus
from app.db.base import utcnow
from app.db.session import get_db
from app.models import Agent, AgentMessage, AuditLog, BrowserProfile, Task, User, Userscript

router = APIRouter(tags=["arena"], dependencies=[Depends(get_current_user)])

# One distinct color per employee card (infographic: 5 colored profile cards).
EMPLOYEE_COLORS = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#0ea5e9", "#a855f7", "#14b8a6", "#f97316", "#84cc16", "#ec4899"]


def _not_enabled() -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "not_enabled", "message": NOT_ENABLED_MESSAGE})


@router.get("/arena/connector/status")
async def connector_status():
    gate = arena_gate()
    bridge: dict[str, Any] = {"ok": False, "code": "not_enabled"} if not gate["enabled"] else await bridge_call("GET", "/v1/connector/profiles")
    return {
        **gate,
        "status_label": "waiting_for_arena_approval" if not gate["real_enabled"] else "approved",
        "bridge": bridge,
        "run_userscript": "disabled (501): AATM never injects scripts; the human installs the userscript in Tampermonkey.",
    }


def _health(db_status: str, connector: dict[str, Any] | None) -> str:
    status = (connector or {}).get("status") or db_status
    if status == "needs_human":
        return "red"
    if status in ("ready", "busy"):
        return (connector or {}).get("health", "green")
    return "yellow" if status in ("offline", "unknown") else "red"


@router.get("/arena/profiles")
async def browser_wall(db: AsyncSession = Depends(get_db)):
    """Browser Wall data: one card per employee profile with color, status, URL, snapshot, lock and connector."""
    gate = arena_gate()
    bridge_profiles: dict[str, dict[str, Any]] = {}
    if gate["enabled"]:
        res = await bridge_call("GET", "/v1/connector/profiles")
        if res.get("ok"):
            bridge_profiles = {p["profile_id"]: p for p in res.get("profiles", [])}
    agents = list(
        (await db.execute(select(Agent).where(Agent.is_enabled.is_(True)).order_by(Agent.created_at, Agent.id))).scalars()
    )
    cards = []
    for index, agent in enumerate(agents):
        if not agent.browser_profile_id:
            continue
        profile = await db.get(BrowserProfile, agent.browser_profile_id)
        if profile is None:
            continue
        connector = bridge_profiles.get(profile.id)
        cards.append(
            {
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_role": agent.role,
                "agent_status": agent.status,
                "color": EMPLOYEE_COLORS[index % len(EMPLOYEE_COLORS)],
                "profile_id": profile.id,
                "profile_name": profile.name,
                "profile_status": profile.status,
                "mode": profile.mode,
                "current_url": profile.current_url,
                "page_title": profile.page_title,
                "snapshot_available": browser_manager.has_png_snapshot(profile.id),
                "snapshot_url": f"/api/v1/browsers/{profile.id}/snapshot",
                "lock": browser_manager.get_lock_status(profile),
                "connector_status": (connector or {}).get("status") or profile.connector_status,
                "connector": connector,
                "needs_human_reason": (connector or {}).get("needs_human_reason") or profile.needs_human_reason,
                "token_issued": bool(profile.connector_token_hash),
                "health": _health(profile.connector_status, connector),
            }
        )
    return {"gate": gate, "profiles": cards}


async def _profile_or_404(db: AsyncSession, profile_id: str) -> BrowserProfile:
    profile = await db.get(BrowserProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Browser profile not found")
    return profile


@router.post("/arena/profiles/{profile_id}/userscript", dependencies=[Depends(require_role("admin"))])
async def generate_userscript(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Generate (or rotate) the profile's connector userscript for the admin to review and install.

    The token is returned exactly once; only its SHA-256 is stored. The stored copy in the
    ``userscripts`` table has the token redacted and is never executed by AATM.
    """
    gate = arena_gate()
    if not gate["enabled"]:
        raise _not_enabled()
    profile = await _profile_or_404(db, profile_id)
    agent = await db.get(Agent, profile.agent_id) if profile.agent_id else None
    await ensure_arena_browser_provider(db)
    rotated = bool(profile.connector_token_hash)
    token = mint_token(profile)
    script = build_userscript(profile, agent, token)
    patterns = match_patterns()
    ok, reason = validate_userscript_policy(script, patterns)
    if not ok:  # pragma: no cover - the generator is covered by tests; never ship a non-compliant script
        raise HTTPException(status_code=500, detail={"code": "policy_violation", "message": reason})
    redacted = script.replace(token, "<per-profile token shown once to the admin>")
    name = f"aatm-arena-connector-{profile.id}"
    row = (await db.execute(select(Userscript).where(Userscript.name == name))).scalar_one_or_none()
    if row is None:
        row = Userscript(name=name, namespace="aatm.arena.connector", code=redacted)
        db.add(row)
    row.version = SCRIPT_VERSION
    row.code = redacted
    row.description = f"Arena connector for {agent.name if agent else profile.id} (installed manually by a human; never executed by AATM)."
    row.match_patterns = patterns
    row.allowed_roles = ["worker"]
    row.allowed_agent_ids = [profile.agent_id] if profile.agent_id else []
    row.is_enabled = False
    row.audit_status = "connector_generated"
    db.add(
        AuditLog(
            actor=user.email,
            action="arena.userscript_rotated" if rotated else "arena.userscript_generated",
            resource_type="browser_profile",
            resource_id=profile.id,
            details={"mode": gate["mode"], "match_patterns": patterns, "script_version": SCRIPT_VERSION},
        )
    )
    await db.commit()
    registration = await register_profile_with_bridge(profile)
    return {
        "profile_id": profile.id,
        "mode": gate["mode"],
        "script": script,
        "token_shown_once": True,
        "rotated": rotated,
        "match_patterns": patterns,
        "bridge_registration": registration,
        "install_steps": [
            "Open this employee's Chrome profile (Browser Wall → Open for sign-in) and sign in to Arena yourself.",
            "Open Tampermonkey → Create a new script, paste this script, review it, and save.",
            "Reload the Arena page: the card turns green when the connector is ready.",
        ],
    }


@router.post("/arena/profiles/{profile_id}/register", dependencies=[Depends(require_role("admin", "operator"))])
async def register_profile(profile_id: str, db: AsyncSession = Depends(get_db)):
    """Re-send this profile's token hash / origins / limits to the bridge (e.g. after a bridge restart)."""
    if not arena_gate()["enabled"]:
        raise _not_enabled()
    profile = await _profile_or_404(db, profile_id)
    return await register_profile_with_bridge(profile)


@router.post("/arena/profiles/{profile_id}/resume", dependencies=[Depends(require_role("admin", "operator"))])
async def resume_after_human(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """A human resolved the check in the browser: clear needs_human and re-queue the blocked task."""
    profile = await _profile_or_404(db, profile_id)
    bridge = await bridge_call("POST", f"/v1/connector/profiles/{profile_id}/reset") if arena_gate()["enabled"] else {"ok": False, "code": "not_enabled"}
    profile.connector_status = "ready" if bridge.get("ok") and bridge.get("connected") else "offline"
    profile.needs_human_reason = None
    requeued: list[str] = []
    agent = await db.get(Agent, profile.agent_id) if profile.agent_id else None
    if agent is not None:
        if agent.status == "needs_human":
            agent.status = "idle"
        blocked = list((await db.execute(select(Task).where(Task.status == "blocked", Task.assigned_agent_id == agent.id))).scalars())
        for task in blocked:
            if not any(cp.get("event") == "needs_human" for cp in (task.checkpoints or []) if isinstance(cp, dict)):
                continue
            task.status = "queued"
            task.lock_owner = None
            task.checkpoints = list(task.checkpoints or []) + [{"ts": utcnow().isoformat(), "event": "human_resolved", "by": user.email}]
            paused = list((await db.execute(select(AgentMessage).where(AgentMessage.task_id == task.id, AgentMessage.execution_status == "paused"))).scalars())
            for message in paused:
                message.execution_status = "cancelled"
            requeued.append(task.id)
    db.add(AuditLog(actor=user.email, action="arena.human_resolved", resource_type="browser_profile", resource_id=profile.id,
                    details={"requeued_tasks": requeued, "bridge_ok": bool(bridge.get("ok"))}))
    await db.commit()
    await event_bus.emit("arena.resumed", {"profile_id": profile.id, "requeued_tasks": requeued})
    return {"profile_id": profile.id, "connector_status": profile.connector_status, "requeued_tasks": requeued, "bridge": bridge}


@router.post("/arena/profiles/{profile_id}/open-for-sign-in", dependencies=[Depends(require_role("admin", "operator"))])
async def open_for_sign_in(profile_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Open the employee's own profile visibly on the approved Arena domain so the human signs in.

    Real-Arena path: refused with ``not_enabled`` until the Arena approval is recorded.
    """
    if not real_arena_enabled():
        raise _not_enabled()
    profile = await _profile_or_404(db, profile_id)
    url = f"https://{arena_domains()[0]}/"
    launched = await browser_manager.launch_profile(db, profile, owner=f"human-sign-in:{user.email}", mode="visible", url=url)
    db.add(AuditLog(actor=user.email, action="arena.open_for_sign_in", resource_type="browser_profile", resource_id=profile.id, details={"url": url}))
    await db.commit()
    return {"profile_id": launched.id, "status": launched.status, "url": url,
            "next": "Sign in to Arena yourself in the opened window. AATM never types or reads credentials."}
