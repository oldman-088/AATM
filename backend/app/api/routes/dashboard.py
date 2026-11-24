"""Phase 7 dashboard data: home counters, 8-step project timeline, employee cards,
live agent log backlog and the errors & recovery panel.

Everything here is computed from real rows (tasks, PRs, usage logs, audit logs, profiles);
nothing is mocked. Steps that have no evidence yet are reported as ``pending``.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.routes.arena import EMPLOYEE_COLORS
from app.core.config import get_settings
from app.db.session import get_db
from app.models import (
    Agent,
    AIProvider,
    ApprovalRequest,
    AuditLog,
    BrowserProfile,
    ChatMessage,
    ManagerJob,
    Project,
    ProviderUsageLog,
    Task,
    WorkspaceArtifact,
)

router = APIRouter(tags=["dashboard"], dependencies=[Depends(get_current_user)])

OPEN_PR_STATUSES = {"pr_open", "in_review", "changes_requested", "approved"}
EXECUTED_STATUSES = {"pr_open", "in_review", "changes_requested", "approved", "merged", "done", "completed"}
FINISHED_STATUSES = {"merged", "done", "completed"}

# The infographic's 8 steps, in order.
TIMELINE_STEPS = (
    "request",
    "analyze_split",
    "distribute",
    "execute",
    "collect",
    "pull_request",
    "review_merge",
    "completed",
)


def _cost(row: ProviderUsageLog) -> float:
    return float(row.confirmed_cost_usd if row.confirmed_cost_usd is not None else row.estimated_cost_usd or 0.0)


def _start_of_today() -> datetime:
    return datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)


@router.get("/dashboard/home")
async def dashboard_home(db: AsyncSession = Depends(get_db)):
    """Architecture strip + live counters (active employees, tasks per status, open PRs, cost vs budget)."""
    agents = list((await db.execute(select(Agent).where(Agent.is_enabled.is_(True)))).scalars())
    status_rows = (await db.execute(select(Task.status, func.count()).group_by(Task.status))).all()
    tasks_by_status = {status: count for status, count in status_rows}
    open_prs = (
        await db.execute(
            select(func.count()).select_from(Task).where(Task.pr_number.is_not(None), Task.status.in_(OPEN_PR_STATUSES))
        )
    ).scalar_one()
    usage_today = list((await db.execute(select(ProviderUsageLog).where(ProviderUsageLog.created_at >= _start_of_today()))).scalars())
    profiles = list((await db.execute(select(BrowserProfile))).scalars())
    settings = get_settings()
    workers = [a for a in agents if a.role in ("worker", "reviewer")]
    cost_today = round(sum(_cost(u) for u in usage_today), 4)
    return {
        "architecture": [
            {"id": "dashboard", "count": None},
            {"id": "orchestrator", "count": sum(1 for a in agents if a.role == "manager")},
            {"id": "profiles", "count": len([p for p in profiles if p.agent_id])},
            {"id": "results", "count": open_prs + sum(tasks_by_status.get(s, 0) for s in FINISHED_STATUSES)},
        ],
        "counters": {
            "active_employees": sum(1 for a in workers if a.status == "working"),
            "total_employees": len(workers),
            "tasks_by_status": tasks_by_status,
            "open_prs": open_prs,
            "cost_today_usd": cost_today,
            "daily_budget_usd": float(settings.DAILY_BUDGET_USD),
            "budget_used_percent": round(100 * cost_today / settings.DAILY_BUDGET_USD, 1) if settings.DAILY_BUDGET_USD else None,
            "needs_human": sum(1 for p in profiles if p.connector_status == "needs_human"),
        },
    }


# P1 "Needs you now": everything that waits for the owner, in one list. Each item names the screen (tab) to open.
ATTENTION_LIMIT = 50


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.get("/dashboard/attention")
async def dashboard_attention(db: AsyncSession = Depends(get_db)):
    """Pending approvals, plan drafts, manager jobs and windows that need a human, blocked tasks (newest first)."""
    items: list[dict[str, Any]] = []
    for row in (await db.execute(select(ApprovalRequest).where(ApprovalRequest.status == "pending"))).scalars():
        items.append({"kind": "approval", "id": row.id, "title": row.title, "detail": row.action_type,
                      "tab": "approvals", "at": _iso(row.created_at)})
    jobs = (await db.execute(select(ManagerJob).where(ManagerJob.status.in_(("draft", "needs_human", "waiting_connection"))))).scalars()
    for job in jobs:
        repeated = int((job.context or {}).get("same_error_count", 0))
        if job.status == "waiting_connection" and repeated < 3 and (job.context or {}).get("last_error_code") != "page_hidden":
            continue  # a short wait for the window is normal; only a stuck one needs the owner (E52, E51)
        kind = {"draft": "plan_draft", "needs_human": "manager_needs_human"}.get(job.status, "manager_stuck")
        items.append({"kind": kind, "id": job.id, "title": (job.request_text or job.kind)[:160], "detail": job.error,
                      "tab": "approvals" if job.kind == "review" and job.status == "needs_human" else "chat",
                      "at": _iso(job.updated_at or job.created_at)})
    for profile in (await db.execute(select(BrowserProfile).where(BrowserProfile.connector_status == "needs_human"))).scalars():
        items.append({"kind": "window_needs_human", "id": profile.id, "title": profile.name or profile.id,
                      "detail": profile.needs_human_reason, "tab": "browsers",
                      "at": _iso(profile.last_heartbeat or profile.created_at)})
    from app.runtime.loop.common import repo_access_denied

    for project in (await db.execute(select(Project))).scalars():
        denied = repo_access_denied(project)
        if denied:  # E43: the Arena GitHub app cannot see this repository on these accounts
            entries = (project.config or {}).get("repo_access_denied") or {}
            repo = next((e.get("repo") for e in entries.values() if e and e.get("repo")), None) or project.name
            latest = max((str((entries.get(a) or {}).get("at") or "") for a in denied), default="")
            items.append({"kind": "repo_access", "id": project.id, "title": repo, "detail": ", ".join(sorted(denied)),
                          "tab": "browsers", "at": latest or None})
    for task in (await db.execute(select(Task).where(Task.status == "blocked"))).scalars():
        items.append({"kind": "task_blocked", "id": task.id, "title": task.title, "detail": task.error_message,
                      "tab": "tasks", "at": _iso(task.updated_at or task.created_at)})
    items.sort(key=lambda item: item["at"] or "", reverse=True)
    return {"count": len(items), "items": items[:ATTENTION_LIMIT]}


def _step(state: str, done: int = 0, total: int = 0, detail: str | None = None) -> dict[str, Any]:
    return {"state": state, "done": done, "total": total, "detail": detail}


def _progress(done: int, total: int) -> str:
    if total and done >= total:
        return "done"
    return "active" if done else "pending"


async def build_timeline(db: AsyncSession, project: Project) -> list[dict[str, Any]]:
    tasks = list((await db.execute(select(Task).where(Task.project_id == project.id))).scalars())
    impl = [t for t in tasks if t.task_type == "implementation" and t.status != "cancelled"]
    reviews = [t for t in tasks if t.task_type == "review"]
    request = (
        await db.execute(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.project_id == project.id, ChatMessage.sender_type == "user")
        )
    ).scalar_one()
    n = len(impl)
    assigned = sum(1 for t in impl if t.assigned_agent_id)
    started = sum(1 for t in impl if t.started_at or t.status in EXECUTED_STATUSES or t.worktree_path)
    executed = sum(1 for t in impl if t.status in EXECUTED_STATUSES)
    collected = sum(1 for t in impl if t.commit_sha or t.result_summary)
    with_pr = sum(1 for t in impl if t.pr_number)
    finished = sum(1 for t in impl if t.status in FINISHED_STATUSES)
    steps = {
        "request": _step("done" if request or n else "pending", request, request),
        "analyze_split": _step("done" if n else "pending", n, n),
        "distribute": _step(_progress(assigned, n), assigned, n),
        # Execution is "done" once every task produced its result; started-but-unfinished is active.
        "execute": _step("done" if n and executed >= n else ("active" if started else "pending"), executed, n),
        "collect": _step(_progress(collected, n), collected, n),
        "pull_request": _step(_progress(with_pr, n), with_pr, n),
        "review_merge": _step(
            "done" if n and finished >= n else ("active" if reviews or finished else "pending"),
            finished,
            n,
            f"{len(reviews)} review task(s)" if reviews else None,
        ),
        "completed": _step("done" if project.completed_at else "pending", 1 if project.completed_at else 0, 1),
    }
    # The first not-yet-done step after a done one is the current step.
    out = []
    for key in TIMELINE_STEPS:
        out.append({"id": key, **steps[key]})
    return out


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    value = (end - start).total_seconds() / 60
    return value if value > 0 else None


async def build_progress(db: AsyncSession, project: Project) -> dict[str, Any]:
    """P3 O4: how much of the project is done and roughly how long the rest takes, from real task timings."""
    tasks = [t for t in (await db.execute(select(Task).where(Task.project_id == project.id))).scalars()
             if t.task_type == "implementation" and t.status != "cancelled"]
    total = len(tasks)
    done = sum(1 for t in tasks if t.status in FINISHED_STATUSES)
    remaining = total - done
    samples = sorted(m for t in tasks if t.status in FINISHED_STATUSES and (m := _minutes(t.started_at, t.completed_at)))
    typical = samples[len(samples) // 2] if samples else None
    team = (project.config or {}).get("team") or []
    workers = (await db.execute(select(func.count()).select_from(Agent).where(
        Agent.role == "worker", Agent.is_enabled.is_(True), *([Agent.id.in_(team)] if team else [])))).scalar_one()
    parallel = max(1, min(remaining, int(workers or 1))) if remaining else 0
    eta = round(-(-remaining // parallel) * typical) if remaining and typical else (0 if total and not remaining else None)
    return {"total": total, "done": done, "remaining": remaining, "percent": round(100 * done / total) if total else 0,
            "typical_task_minutes": round(typical, 1) if typical else None, "parallel_workers": parallel,
            "eta_minutes": eta, "samples": len(samples)}


@router.get("/projects/{project_id}/timeline")
async def project_timeline(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    steps = await build_timeline(db, project)
    current = next((s["id"] for s in steps if s["state"] != "done"), None)
    return {"project_id": project.id, "project_name": project.name, "status": project.status, "steps": steps, "current_step": current,
            "progress": await build_progress(db, project)}


@router.get("/dashboard/daily")
async def dashboard_daily(hours: int = Query(24, ge=1, le=168), db: AsyncSession = Depends(get_db)):
    """P3 O5: what happened in the last day (or ``hours``) and what waits for the owner."""
    from datetime import timedelta

    from app.models import ApprovalRequest, Notification

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = list((await db.execute(select(Task).where(Task.updated_at >= since))).scalars())
    merged = [t for t in recent if t.status in {"merged", "done"} and t.pr_number]
    completed = [t for t in recent if t.status in FINISHED_STATUSES]
    failed = [t for t in recent if t.status in {"failed", "blocked"}]
    pending = (await db.execute(select(func.count()).select_from(ApprovalRequest).where(ApprovalRequest.status == "pending"))).scalar_one()
    critical = (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.severity == "critical", Notification.created_at >= since))).scalar_one()
    return {
        "hours": hours,
        "completed_tasks": len(completed),
        "merged_prs": [{"task_id": t.id, "pr_number": t.pr_number, "title": t.title} for t in merged][:20],
        "failed_or_blocked": [{"task_id": t.id, "title": t.title, "status": t.status, "error": (t.error_message or "")[:200]}
                              for t in failed][:20],
        "pending_approvals": pending,
        "critical_alerts": critical,
    }


@router.get("/dashboard/employees")
async def employee_cards(db: AsyncSession = Depends(get_db)):
    """One colored card per employee: role, model/provider, profile status, current task, last action, cost."""
    agents = list((await db.execute(select(Agent).order_by(Agent.created_at, Agent.id))).scalars())
    costs = dict(
        (
            await db.execute(
                select(ProviderUsageLog.agent_id, func.sum(func.coalesce(ProviderUsageLog.confirmed_cost_usd, ProviderUsageLog.estimated_cost_usd)))
                .group_by(ProviderUsageLog.agent_id)
            )
        ).all()
    )
    cards = []
    for index, agent in enumerate(agents):
        provider = await db.get(AIProvider, agent.provider_id) if agent.provider_id else None
        profile = await db.get(BrowserProfile, agent.browser_profile_id) if agent.browser_profile_id else None
        task = await db.get(Task, agent.current_task_id) if agent.current_task_id else None
        last = (
            await db.execute(
                select(WorkspaceArtifact)
                .where(WorkspaceArtifact.agent_id == agent.id, WorkspaceArtifact.action == "agent_iteration")
                .order_by(WorkspaceArtifact.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        cards.append(
            {
                "id": agent.id,
                "name": agent.name,
                "role": agent.role,
                "status": agent.status,
                "is_enabled": agent.is_enabled,
                "is_emergency_stopped": agent.is_emergency_stopped,
                "color": EMPLOYEE_COLORS[index % len(EMPLOYEE_COLORS)],
                "provider": provider.name if provider else None,
                "provider_id": agent.provider_id,
                "model": agent.model_name or (provider.default_model if provider else None),
                "profile_status": profile.status if profile else None,
                "connector_status": profile.connector_status if profile else None,
                "current_task": {"id": task.id, "title": task.title, "status": task.status, "progress": task.progress_percent} if task else None,
                "last_action": {"summary": last.summary, "at": last.created_at, "failed": bool(last.exit_code)} if last else None,
                "cost_usd": round(float(costs.get(agent.id) or 0.0), 4),
                "tasks_completed": agent.tasks_completed,
                "tasks_failed": agent.tasks_failed,
            }
        )
    return {"employees": cards}


@router.get("/dashboard/agent-log")
async def agent_log(limit: int = Query(default=100, ge=1, le=500), agent_id: str | None = None, db: AsyncSession = Depends(get_db)):
    """Backlog for the live agent log (new iterations then stream over /ws/events as ``agent.iteration``)."""
    stmt = select(WorkspaceArtifact).where(WorkspaceArtifact.action == "agent_iteration")
    if agent_id:
        stmt = stmt.where(WorkspaceArtifact.agent_id == agent_id)
    rows = list((await db.execute(stmt.order_by(WorkspaceArtifact.created_at.desc()).limit(limit))).scalars())
    return {
        "entries": [
            {"agent_id": r.agent_id, "task_id": r.task_id, "summary": r.summary, "failed": bool(r.exit_code), "at": r.created_at}
            for r in reversed(rows)
        ]
    }


@router.get("/dashboard/recovery")
async def recovery_panel(db: AsyncSession = Depends(get_db)):
    """Errors & recovery: failed tasks, retried tasks, replacements and needs_human sessions."""
    failed = list((await db.execute(select(Task).where(Task.status == "failed").order_by(Task.updated_at.desc()).limit(50))).scalars())
    retried = list(
        (await db.execute(select(Task).where(Task.retry_count > 0, Task.status != "failed").order_by(Task.updated_at.desc()).limit(50))).scalars()
    )
    blocked = list((await db.execute(select(Task).where(Task.status == "blocked").order_by(Task.updated_at.desc()).limit(50))).scalars())
    replacements = list(
        (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "employee.replaced").order_by(AuditLog.created_at.desc()).limit(20)
            )
        ).scalars()
    )
    needs_human = list((await db.execute(select(BrowserProfile).where(BrowserProfile.connector_status == "needs_human"))).scalars())

    def task_row(t: Task) -> dict[str, Any]:
        return {"id": t.id, "title": t.title, "status": t.status, "agent_id": t.assigned_agent_id,
                "retry_count": t.retry_count, "max_retries": t.max_retries, "error": (t.error_message or "")[:400]}

    return {
        "failed_tasks": [task_row(t) for t in failed],
        "retried_tasks": [task_row(t) for t in retried],
        "blocked_tasks": [task_row(t) for t in blocked],
        "replacements": [{"id": a.id, "resource_id": a.resource_id, "actor": a.actor, "details": a.details, "at": a.created_at} for a in replacements],
        "needs_human": [{"profile_id": p.id, "agent_id": p.agent_id, "reason": p.needs_human_reason} for p in needs_human],
    }
