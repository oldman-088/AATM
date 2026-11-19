"""Arena Agent sessions: per-employee ownership, state tracking and recovery.

Every worker whose browser profile is in ``agent`` mode is an independent Arena Agent worker.
There is no limit on how many profiles, sessions or submissions run at once: the only per-profile
rule is that one browser tab drives one session at a time.

The bridge is the live source of the page state (``ready``, ``generating``, ``idle``,
``keep_working_visible``, ``needs_human``, ``offline``); this module mirrors it into the
``arena_sessions`` table so the dashboard, the orchestrator and the audit log share one view that
survives bridge and backend restarts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.arena_connector import (
    ARENA_MODES,
    arena_gate,
    bridge_call,
    profile_arena_mode,
    register_profile_with_bridge,
)
from app.core.events import event_bus
from app.db.base import utcnow
from app.models import Agent, ArenaSession, AuditLog, BrowserProfile, Task

MAX_SESSION_EVENTS = 100
TERMINAL_PHASES = {"merged", "closed", "failed", "escalated"}


class ArenaSessionConflict(Exception):
    """The profile's open session belongs to another task that is still active."""


def _now() -> datetime:
    return utcnow()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def append_event(session: ArenaSession, kind: str, detail: str | None = None, **extra: Any) -> None:
    entry: dict[str, Any] = {"ts": _now().isoformat(), "kind": kind[:48]}
    if detail:
        entry["detail"] = detail[:300]
    entry.update({k: v for k, v in extra.items() if v is not None})
    session.events = (list(session.events or []) + [entry])[-MAX_SESSION_EVENTS:]
    session.last_activity_at = _now()


async def agent_profile(db: AsyncSession, agent: Agent) -> BrowserProfile | None:
    return await db.get(BrowserProfile, agent.browser_profile_id) if agent.browser_profile_id else None


async def is_arena_agent_worker(db: AsyncSession, agent: Agent) -> bool:
    """True when this employee executes tasks through its own Arena Agent session."""
    if agent.role != "worker" or not agent.is_enabled or not agent.browser_profile_id:
        return False
    if not arena_gate()["enabled"]:
        return False
    profile = await agent_profile(db, agent)
    return profile is not None and profile_arena_mode(profile) == "agent"


async def set_arena_mode(db: AsyncSession, agent: Agent, mode: str, *, actor: str) -> dict[str, Any]:
    """Switch one employee between chat mode and Arena Agent mode (no count limit)."""
    mode = (mode or "").strip().lower()
    if mode not in ARENA_MODES:
        raise ValueError(f"Arena mode must be one of {', '.join(ARENA_MODES)}.")
    profile = await agent_profile(db, agent)
    if profile is None:
        raise ValueError("Employee has no browser profile.")
    previous = profile_arena_mode(profile)
    profile.arena_mode = mode
    db.add(AuditLog(actor=actor, action="arena.mode_changed", resource_type="agent", resource_id=agent.id,
                    details={"profile_id": profile.id, "from": previous, "to": mode}))
    await db.commit()
    registration: dict[str, Any] = {"ok": False, "skipped": True}
    if profile.connector_token_hash and arena_gate()["enabled"]:
        registration = await register_profile_with_bridge(profile)
    await event_bus.emit("arena.mode_changed", {"agent_id": agent.id, "profile_id": profile.id, "mode": mode})
    return {"agent_id": agent.id, "profile_id": profile.id, "mode": mode, "previous": previous, "bridge_registration": registration}


async def sync_profiles_with_bridge(db: AsyncSession) -> list[str]:
    """Register enabled employees' profiles the bridge does not know, e.g. after it restarted.

    A bridge without its state file starts empty. Tokens are not changed, so installed userscripts
    stay valid. Returns the profile ids registered by this call.
    """
    if not arena_gate()["enabled"]:
        return []
    listed = await bridge_call("GET", "/v1/connector/profiles")
    if not listed.get("ok"):
        return []
    known = {str(item.get("profile_id")) for item in listed.get("profiles") or []}
    profiles = (await db.execute(
        select(BrowserProfile).join(Agent, Agent.id == BrowserProfile.agent_id).where(
            Agent.is_enabled.is_(True), BrowserProfile.connector_token_hash.is_not(None),
        )
    )).scalars()
    registered: list[str] = []
    for profile in profiles:
        if profile.id not in known and (await register_profile_with_bridge(profile)).get("ok"):
            registered.append(profile.id)
    return registered


async def open_session_for(db: AsyncSession, profile_id: str) -> ArenaSession | None:
    return (
        await db.execute(
            select(ArenaSession)
            .where(ArenaSession.profile_id == profile_id, ArenaSession.is_open.is_(True))
            .order_by(ArenaSession.created_at.desc())
        )
    ).scalars().first()


async def session_for_task(db: AsyncSession, task_id: str) -> ArenaSession | None:
    return (
        await db.execute(
            select(ArenaSession).where(ArenaSession.task_id == task_id).order_by(ArenaSession.created_at.desc())
        )
    ).scalars().first()


async def close_session(db: AsyncSession, session: ArenaSession, *, phase: str = "closed", reason: str | None = None) -> None:
    session.is_open = False
    session.phase = phase
    session.closed_at = _now()
    append_event(session, f"closed:{phase}", reason)


async def claim_session(db: AsyncSession, agent: Agent, task: Task) -> tuple[ArenaSession, bool]:
    """Return ``(session, needs_new_session)`` for this employee + task.

    * The open session already bound to this task is reused (recovery after a restart).
    * An open session of a finished/merged task is closed: Arena may lock that branch, so the
      next task always starts with ``new_session``.
    * An open session of another active task is a conflict (one tab drives one session).
    """
    if not agent.browser_profile_id:
        raise ValueError("Employee has no browser profile.")
    current = await open_session_for(db, agent.browser_profile_id)
    if current is not None and current.task_id == task.id and current.phase not in TERMINAL_PHASES:
        return current, current.submitted_at is None
    if current is not None:
        other = await db.get(Task, current.task_id) if current.task_id else None
        if other is not None and other.status in {"in_progress", "pr_open", "in_review", "changes_requested"} and current.phase not in TERMINAL_PHASES:
            raise ArenaSessionConflict(f"Profile {agent.browser_profile_id} is still driving task {other.id}.")
        await close_session(db, current, reason="next task starts a new session")
    session = ArenaSession(agent_id=agent.id, profile_id=agent.browser_profile_id, task_id=task.id, phase="new",
                           page_state="offline", events=[])
    append_event(session, "created", f"task {task.id}")
    db.add(session)
    await db.flush()
    return session, True


def apply_bridge_state(session: ArenaSession, summary: dict[str, Any]) -> bool:
    """Mirror the bridge's live profile summary into the session. Returns True on a state change."""
    new_state = str(summary.get("agent_state") or ("offline" if not summary.get("connected") else session.page_state))
    if summary.get("status") == "needs_human":
        new_state = "needs_human"
        session.needs_human_reason = str(summary.get("needs_human_reason") or "human check")[:240]
    changed = new_state != session.page_state
    if changed:
        session.page_state = new_state
        session.last_state_change_at = _parse_iso(summary.get("agent_state_since")) or _now()
        append_event(session, f"state:{new_state}")
    return changed


async def bridge_profile_state(profile_id: str) -> dict[str, Any]:
    res = await bridge_call("GET", f"/v1/connector/profiles/{profile_id}")
    if not res.get("ok"):
        return {"ok": False, "connected": False, "agent_state": "offline", "code": res.get("code"), "message": res.get("message")}
    return res


async def sync_session(db: AsyncSession, session: ArenaSession) -> dict[str, Any]:
    state = await bridge_profile_state(session.profile_id)
    apply_bridge_state(session, state)
    return state


def session_dict(session: ArenaSession | None) -> dict[str, Any] | None:
    if session is None:
        return None
    return {
        "id": session.id,
        "agent_id": session.agent_id,
        "profile_id": session.profile_id,
        "task_id": session.task_id,
        "page_state": session.page_state,
        "phase": session.phase,
        "is_open": session.is_open,
        "kind": session.kind,
        "project_id": session.project_id,
        "exchange_count": session.exchange_count,
        "keep_working_count": session.keep_working_count,
        "follow_up_count": session.follow_up_count,
        "correction_rounds": session.correction_rounds,
        "recovery_count": session.recovery_count,
        "branch_name": session.branch_name,
        "pr_number": session.pr_number,
        "pr_url": session.pr_url,
        "head_sha": session.head_sha,
        "report_available": bool(session.report),
        "needs_human_reason": session.needs_human_reason,
        "error_message": session.error_message,
        "last_state_change_at": session.last_state_change_at.isoformat() if session.last_state_change_at else None,
        "last_activity_at": session.last_activity_at.isoformat() if session.last_activity_at else None,
        "submitted_at": session.submitted_at.isoformat() if session.submitted_at else None,
        "closed_at": session.closed_at.isoformat() if session.closed_at else None,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "events": list(session.events or [])[-20:],
    }


def _profile_row(agent: Agent, profile: BrowserProfile, live: dict[str, Any], session: ArenaSession | None,
                 task: Task | None) -> dict[str, Any]:
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "agent_role": agent.role,
        "agent_status": agent.status,
        "profile_id": profile.id,
        "profile_name": profile.name,
        "arena_mode": profile_arena_mode(profile),
        "token_issued": bool(profile.connector_token_hash),
        "connected": bool(live.get("connected")),
        "connector_status": live.get("status") or profile.connector_status,
        "page_state": live.get("agent_state") or (session.page_state if session else "offline"),
        "needs_human_reason": live.get("needs_human_reason") or profile.needs_human_reason or (session.needs_human_reason if session else None),
        "keep_working_clicks": live.get("keep_working_clicks", 0),
        "last_error": live.get("last_error"),
        "session": session_dict(session),
        "task": {"id": task.id, "title": task.title, "status": task.status} if task else None,
        "human_events": [e for e in (session.events or []) if str(e.get("kind", "")).startswith(("needs_human", "escalated", "merged_outside"))][-10:]
        if session else [],
    }


async def manager_overview_row(db: AsyncSession, bridge: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The manager's Arena card (R7): same fields as a worker card plus the Selector test result and activity."""
    from app.browser.arena_connector import MANAGER_AGENT_ID

    manager = await db.get(Agent, MANAGER_AGENT_ID)
    if manager is None or not manager.browser_profile_id:
        return None
    profile = await agent_profile(db, manager)
    if profile is None:
        return None
    live = dict((bridge or {}).get(profile.id) or {})
    selector: dict[str, Any] | None = None
    if arena_gate()["enabled"] and profile.connector_token_hash:
        detail = await bridge_call("GET", f"/v1/connector/profiles/{profile.id}")
        if detail.get("ok"):
            live = {**live, **{k: v for k, v in detail.items() if k not in {"ok", "recent_events"}}}
            selector = detail.get("selector_report")
    session = await open_session_for(db, profile.id)
    row = _profile_row(manager, profile, live, session, None)
    from app.orchestration.manager_arena import manager_activity

    row.update({
        "is_manager": True,
        "selector_test": {"ran": selector is not None, "ok": bool((selector or {}).get("ok"))},
        "activity": await manager_activity(db),
    })
    return row


async def arena_agent_overview(db: AsyncSession) -> dict[str, Any]:
    """Every enabled worker with a profile, its Arena mode, open session and live bridge state.

    The manager (R7) has its own card under ``manager`` and counts as one Arena profile like any worker.
    """
    gate = arena_gate()
    bridge: dict[str, dict[str, Any]] = {}
    if gate["enabled"]:
        res = await bridge_call("GET", "/v1/connector/profiles")
        if res.get("ok"):
            bridge = {p["profile_id"]: p for p in res.get("profiles", [])}
    agents = list((await db.execute(
        select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True)).order_by(Agent.created_at, Agent.id)
    )).scalars())
    rows = []
    for agent in agents:
        profile = await agent_profile(db, agent)
        if profile is None:
            continue
        session = await open_session_for(db, profile.id)
        task = await db.get(Task, session.task_id) if session and session.task_id else None
        rows.append(_profile_row(agent, profile, bridge.get(profile.id) or {}, session, task))
    manager = await manager_overview_row(db, bridge)
    profiles = rows + ([manager] if manager else [])
    sessions = list((await db.execute(select(ArenaSession))).scalars())
    tasks = [s.task_id for s in sessions if s.task_id]
    task_rows = list((await db.execute(select(Task).where(Task.id.in_(tasks))))) if tasks else []
    statuses = [row[0].status for row in task_rows]
    distribution: dict[str, int] = {}
    for status in statuses:
        distribution[status] = distribution.get(status, 0) + 1
    return {
        "gate": gate,
        "task_distribution": distribution,
        "counts": {
            "profiles": len(profiles),
            "agent_mode_profiles": sum(1 for r in profiles if r["arena_mode"] == "agent"),
            "connected_profiles": sum(1 for r in profiles if r["connected"]),
            "open_sessions": sum(1 for s in sessions if s.is_open),
            "running_tasks": sum(1 for s in statuses if s in {"in_progress", "pr_open", "in_review", "changes_requested"}),
            "completed_tasks": sum(1 for s in statuses if s in {"done", "merged", "completed"}),
            "failed_tasks": sum(1 for s in statuses if s in {"failed", "blocked"}),
            "needs_human": sum(1 for r in profiles if r["page_state"] == "needs_human"),
        },
        "manager": manager,
        "employees": rows,
    }
