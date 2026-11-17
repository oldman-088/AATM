"""manager-0 on Arena Agent mode (R7): the manager's own Arena session, driven like the workers'."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.arena_connector import (
    MANAGER_AGENT_ID,
    MANAGER_PROFILE_ID,
    NOT_ENABLED_MESSAGE,
    arena_gate,
    bridge_call,
)
from app.browser.arena_sessions import append_event, close_session, session_for_task
from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.models import (
    Agent,
    ApprovalRequest,
    ArenaSession,
    AuditLog,
    BrowserProfile,
    ChatMessage,
    ManagerJob,
    Notification,
    Project,
    SystemSetting,
    Task,
)
from app.orchestration.arena_prompts import (
    MANAGER_JSON_RETRY,
    build_manager_chat_prompt,
    build_manager_context_prompt,
    build_manager_plan_prompt,
    build_manager_review_prompt,
    build_manager_status_prompt,
    extract_manager_json,
    validate_manager_plan,
    validate_manager_review,
)
from app.runtime.agent_protocol import redact_secrets

logger = logging.getLogger(__name__)

MANAGER_PROVIDERS = ("arena_agent", "anthropic_claude", "openai_compatible", "demo")
MANAGER_PROVIDER_SETTING = "manager_provider"

NOT_CONNECTED_AR = "المدير غير متصل بـ Arena — افتح ملف Chrome الخاص بالمدير"
READINESS_AR = {
    "not_enabled": "موصّل Arena متوقف (ARENA_CONNECTOR_ENABLED=false) — لا يمكن تشغيل المدير عبر Arena.",
    "bridge_unreachable": "الجسر المحلي لـ Arena غير متاح — شغّل الجسر ثم أعد الفحص.",
    "not_registered": "لم يُولَّد سكربت ملف المدير بعد — افتح بطاقة المدير في تبويب المتصفحات واضغط «توليد السكربت».",
    "not_agent_mode": "ملف المدير ليس في وضع وكيل Arena.",
    "offline": NOT_CONNECTED_AR,
    "needs_human": "ملف المدير يحتاج تدخلًا بشريًا: ",
    "selector_not_run": "المدير متصل، لكن لم يُشغَّل «اختبار المحددات» على ملفه بعد — شغّله من بطاقة المدير.",
    "selector_failed": "فشل «اختبار المحددات» على ملف المدير — اطلب من المالك فحص صفحة Arena قبل المتابعة.",
    "ready": "المدير متصل بـ Arena واختبار المحددات ناجح.",
}


async def manager_provider_mode(db: AsyncSession) -> str:
    """Effective manager runtime: AATM_MANAGER_PROVIDER, else the wizard's choice, else '' (legacy)."""
    configured = (get_settings().AATM_MANAGER_PROVIDER or "").strip()
    if configured:
        return configured
    row = await db.get(SystemSetting, MANAGER_PROVIDER_SETTING)
    value = str((row.value or {}).get("provider_type") or "") if row is not None else ""
    return value if value in MANAGER_PROVIDERS else ""


async def manager_readiness(profile_id: str = MANAGER_PROFILE_ID) -> dict[str, Any]:
    """Is the manager's Arena profile usable? Connected, not blocked, agent mode, Selector test passed.

    Read-only: it asks the bridge for the profile's live state and stored Selector test report; it
    never sends anything to the Arena page.
    """
    base: dict[str, Any] = {"profile_id": profile_id, "connected": False, "selector_ok": False, "selector_ran": False}

    def verdict(code: str, **extra: Any) -> dict[str, Any]:
        reason = READINESS_AR[code] + (str(extra.pop("detail", "")) if code == "needs_human" else "")
        return {**base, **extra, "ready": code == "ready", "code": code, "reason_ar": reason}

    if not arena_gate()["enabled"]:
        return verdict("not_enabled", detail_en=NOT_ENABLED_MESSAGE)
    res = await bridge_call("GET", f"/v1/connector/profiles/{profile_id}")
    if not res.get("ok"):
        code = str(res.get("code") or "")
        if code == "not_registered":
            return verdict("not_registered")
        if code == "bridge_unreachable" or code.startswith("http_"):
            return verdict("bridge_unreachable", detail_en=str(res.get("message") or code))
        return verdict("offline", detail_en=str(res.get("message") or code))
    selector = res.get("selector_report") or None
    state = {"connected": bool(res.get("connected")), "selector_ran": selector is not None,
             "selector_ok": bool((selector or {}).get("ok")), "agent_state": res.get("agent_state")}
    if res.get("status") == "needs_human" or res.get("agent_state") == "needs_human":
        return verdict("needs_human", detail=str(res.get("needs_human_reason") or ""), **state)
    if not res.get("connected"):
        return verdict("offline", **state)
    if res.get("mode") != "agent":
        return verdict("not_agent_mode", **state)
    if selector is None:
        return verdict("selector_not_run", **state)
    if not state["selector_ok"]:
        return verdict("selector_failed", **state)
    return verdict("ready", **state)


# =========================================================================== runtime
STALL_MINUTES = 20
QUIET_STATES = {"idle", "ready"}
BUSY_STATES = {"generating", "keep_working_visible"}
OPEN_JOB_STATES = ("queued", "waiting_connection", "running")
TERMINAL_TASK_STATES = {"done", "merged", "completed", "cancelled"}
MAX_RAW_REPLY_CHARS = 20_000

_STATUS_RE = re.compile(
    r"(وين\s*وصلت|وين\s*صرتو|شو\s*صار|إلى\s*أين\s*وصل|الى\s*اين\s*وصل|ما\s*(?:هو\s*)?(?:الوضع|وضع)|حالة\s*المشروع|آخر\s*المستجدات|"
    r"\bstatus\b|\bprogress\b|where\s+are\s+we|کجا\s*هستیم|وضعیت|چه\s*خبر)",
    re.IGNORECASE,
)
_PREFIXES = {"/chat": "chat", "/goal": "goal", "/plan": "goal", "/status": "status"}


class ManagerArenaError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def classify_owner_message(content: str, intent: str | None = None) -> tuple[str, str]:
    """(kind, text): status questions -> status, other questions -> chat, everything else is a goal -> plan.

    ``/chat``, ``/goal`` (or ``/plan``) and ``/status`` at the start of the message, or an explicit
    ``intent``, override the heuristic.
    """
    text = (content or "").strip()
    head = text.split(maxsplit=1)
    if head and head[0].lower() in _PREFIXES:
        return _PREFIXES[head[0].lower()].replace("goal", "plan"), (head[1] if len(head) > 1 else "").strip()
    if intent in {"chat", "status"}:
        return intent, text
    if intent == "goal":
        return "plan", text
    if _STATUS_RE.search(text):
        return "status", text
    if text.endswith(("?", "؟")):
        return "chat", text
    return "plan", text


def _now() -> datetime:
    return utcnow()


def _job_dict(job: ManagerJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {"job_id": job.id, "kind": job.kind, "status": job.status, "project_id": job.project_id,
            "updated_at": (job.updated_at or job.created_at).isoformat() if (job.updated_at or job.created_at) else None,
            "error": job.error}


async def manager_activity(db: AsyncSession) -> dict[str, Any] | None:
    """What the manager is doing now: the running job, else the oldest waiting/queued one, else the last."""
    for statuses in (("running",), ("waiting_connection", "queued")):
        job = (await db.execute(
            select(ManagerJob).where(ManagerJob.status.in_(statuses)).order_by(ManagerJob.created_at.asc())
        )).scalars().first()
        if job is not None:
            return _job_dict(job)
    last = (await db.execute(select(ManagerJob).order_by(ManagerJob.updated_at.desc()))).scalars().first()
    if last is None:
        return None
    return {**(_job_dict(last) or {}), "status": "idle" if last.status == "done" else last.status, "last_kind": last.kind}


class ManagerArenaRuntime:
    """Sends every manager-0 job to the manager's Arena session and acts on the reply.

    * Every prompt goes through ``ArenaAgentAdapter.submit`` -> ``arena_submit_queue`` (the same
      submit path as the workers); on top of that, one manager prompt is in flight at a time.
    * One long-lived manager session per project; a new session (with a short context prompt) when
      the owner asks, the project changes, or after ``ARENA_MANAGER_MAX_EXCHANGES`` exchanges.
    * Plans and verdicts are parsed from the reply; one "JSON only" retry, then "needs human" with
      the raw reply. Nothing is invented and the manager never merges.
    """

    def __init__(self, adapter: Any = None, session_factory: Any = None) -> None:
        from app.providers.adapters.arena_agent import ArenaAgentAdapter

        self.adapter = adapter or ArenaAgentAdapter()
        self._session_factory = session_factory
        self.poll_seconds = 1.0
        self.stall_minutes = float(STALL_MINUTES)
        # Grace period after a submit during which the old reply is not mistaken for the new one.
        self.start_grace_seconds = 20.0
        self._lock: tuple[Any, asyncio.Lock] | None = None
        self._drain_task: asyncio.Task | None = None
        self.profile_id = MANAGER_PROFILE_ID

    # ------------------------------------------------------------------ plumbing
    @property
    def session_factory(self) -> Any:
        if self._session_factory is None:
            from app.db.session import AsyncSessionLocal

            return AsyncSessionLocal
        return self._session_factory

    @session_factory.setter
    def session_factory(self, value: Any) -> None:
        self._session_factory = value

    def _in_flight_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock[0] is not loop:
            self._lock = (loop, asyncio.Lock())
        return self._lock[1]

    async def _profile_id(self, db: AsyncSession) -> str:
        manager = await db.get(Agent, MANAGER_AGENT_ID)
        return (manager.browser_profile_id if manager and manager.browser_profile_id else None) or MANAGER_PROFILE_ID

    async def _team(self, db: AsyncSession) -> list[dict[str, Any]]:
        workers = (await db.execute(
            select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True), Agent.is_emergency_stopped.is_(False))
            .order_by(Agent.created_at, Agent.id)
        )).scalars()
        return [{"id": w.id, "name": w.name, "specialty": w.specialty, "skills": list(w.skills or []),
                 "divisions": list(w.divisions or [])} for w in workers]

    async def _open_tasks(self, db: AsyncSession, project_id: str | None) -> list[dict[str, Any]]:
        query = select(Task).where(Task.status.not_in(TERMINAL_TASK_STATES), Task.task_type == "implementation")
        if project_id:
            query = query.where(Task.project_id == project_id)
        rows = (await db.execute(query.order_by(Task.created_at.asc()).limit(50))).scalars()
        return [{"id": t.id, "title": t.title, "status": t.status, "assignee": t.assigned_agent_id,
                 "pr_url": t.pr_url, "pr_number": t.pr_number} for t in rows]

    async def status_summary(self, db: AsyncSession, project_id: str | None) -> dict[str, Any]:
        """Short, real state of the project for status questions: tasks, PRs, blockers."""
        query = select(Task).where(Task.task_type == "implementation")
        if project_id:
            query = query.where(Task.project_id == project_id)
        tasks = list((await db.execute(query.order_by(Task.updated_at.desc()).limit(60))).scalars())
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.status] = counts.get(t.status, 0) + 1
        blockers = [f"{t.title}: {t.status}" + (f" — {t.error_message[:120]}" if t.error_message else "")
                    for t in tasks if t.status in {"blocked", "failed"}]
        sessions = (await db.execute(select(ArenaSession).where(ArenaSession.is_open.is_(True),
                                                                ArenaSession.phase == "needs_human"))).scalars()
        blockers += [f"Arena session of {s.agent_id} needs a human: {s.needs_human_reason or ''}".strip() for s in sessions]
        approvals = (await db.execute(select(ApprovalRequest).where(ApprovalRequest.status == "pending"))).scalars()
        blockers += [f"Waiting for the owner's approval: {a.title}" for a in approvals
                     if not project_id or a.project_id in {None, project_id}]
        return {
            "at": _now().isoformat(timespec="minutes"),
            "counts": counts,
            "tasks": [{"title": t.title, "status": t.status, "assignee": t.assigned_agent_id} for t in tasks[:25]],
            "pull_requests": [{"number": t.pr_number, "title": t.title, "status": t.pr_status or t.status, "url": t.pr_url}
                              for t in tasks if t.pr_url][:15],
            "blockers": blockers[:15],
        }

    # ------------------------------------------------------------------ console messages
    async def _reply(self, db: AsyncSession, job: ManagerJob, content: str, **metadata: Any) -> ChatMessage | None:
        """Create or update the manager's console reply for this job and tell the live views."""
        if job.kind in {"review", "context"} and not job.reply_message_id:
            return None
        message = await db.get(ChatMessage, job.reply_message_id) if job.reply_message_id else None
        meta = {"runtime": "arena_agent", "manager_job_id": job.id, "job_kind": job.kind, "job_status": job.status, **metadata}
        if message is None:
            message = ChatMessage(project_id=job.project_id, sender_type="manager", sender_id=MANAGER_AGENT_ID,
                                  recipient_id="user", content=content, metadata_json=meta)
            db.add(message)
            await db.flush()
            job.reply_message_id = message.id
        else:
            message.content = content
            message.metadata_json = {**(message.metadata_json or {}), **meta}
        await db.commit()
        await event_bus.emit("chat.message", {"id": message.id, "sender": MANAGER_AGENT_ID, "content": message.content,
                                              "manager_job_id": job.id, "status": job.status})
        return message

    async def _stream(self, job: ManagerJob, text: str) -> None:
        """Show the manager's reply on its robot screen (Command Bridge) through the live code events."""
        from app.runtime.code_stream import build_write_frame, code_stream

        frame = build_write_frame(agent_id=MANAGER_AGENT_ID, task_id=job.review_task_id, path=f"manager/{job.kind}.md",
                                  content=text, action="review" if job.kind == "review" else "write")
        if frame is not None:
            frame["source"] = "manager_arena"
            await code_stream.publish(frame)

    # ------------------------------------------------------------------ entry points
    async def enqueue(self, db: AsyncSession, kind: str, *, project_id: str | None, text: str = "",
                      chat_message_id: str | None = None, review_task_id: str | None = None,
                      context: dict[str, Any] | None = None) -> ManagerJob:
        job = ManagerJob(kind=kind, project_id=project_id, request_text=text or "", chat_message_id=chat_message_id,
                         review_task_id=review_task_id, context=dict(context or {}), status="queued")
        db.add(job)
        await db.flush()
        db.add(AuditLog(actor=MANAGER_AGENT_ID, action=f"manager.job_{kind}", resource_type="manager_job", resource_id=job.id,
                        details={"project_id": project_id, "review_task_id": review_task_id}))
        await db.commit()
        await event_bus.emit("manager.activity", _job_dict(job) or {})
        return job

    async def handle_owner_message(self, db: AsyncSession, project_id: str | None, content: str, *,
                                   user_message_id: str | None = None, intent: str | None = None,
                                   gated_categories: set[str] | None = None) -> ChatMessage:
        """Command Console -> a manager job. Replies at once: 'working on it' or 'not connected'."""
        kind, text = classify_owner_message(content, intent)
        job = await self.enqueue(db, kind, project_id=project_id, text=text or content, chat_message_id=user_message_id,
                                 context={"gated_categories": sorted(gated_categories or [])})
        if kind == "plan" and (not project_id or await db.get(Project, project_id) is None):
            await self._finish(db, job, status="failed", error="no project selected")
            message = await self._reply(db, job, "اختر مشروعًا أولًا: الخطة تُنشئ مهام داخل مشروع، ولم يُرسل شيء إلى المدير.",
                                        failed=True)
            assert message is not None
            return message
        ready = await manager_readiness(await self._profile_id(db))
        if not ready["connected"] or ready["code"] in {"not_enabled", "not_registered", "bridge_unreachable", "needs_human",
                                                       "not_agent_mode", "offline"}:
            job.status = "waiting_connection"
            job.error = ready["reason_ar"]
            message = await self._reply(db, job, NOT_CONNECTED_AR if ready["code"] in {"offline", "not_registered"} else ready["reason_ar"],
                                        waiting=True, readiness=ready["code"])
            await self._notify_waiting(db, job, ready)
            assert message is not None
            return message
        message = await self._reply(db, job, "المدير يكتب الرد عبر Arena…", pending=True)
        assert message is not None
        self.kick()
        return message

    async def request_new_session(self, db: AsyncSession, project_id: str | None, *, actor: str) -> ManagerJob:
        """Owner pressed New session: close the open manager session and start a fresh one with context."""
        session = await self._open_session(db)
        if session is not None:
            await close_session(db, session, phase="closed", reason=f"new session requested by {actor}")
        job = await self.enqueue(db, "context", project_id=project_id or (session.project_id if session else None),
                                 context={"requested_by": actor, "force_new": True})
        self.kick()
        return job

    def kick(self) -> None:
        """Run queued jobs in the background (one at a time); a no-op while a drain is running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._drain_task
        if task is not None and not task.done() and task.get_loop() is loop:
            return
        self._drain_task = loop.create_task(self.drain())

    async def wait_idle(self) -> None:
        task = self._drain_task
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def tick(self, db: AsyncSession) -> dict[str, Any]:
        """Orchestration cycle: review jobs for manager reviews, release waiting jobs once connected."""
        created = await self._enqueue_reviews(db)
        waiting = list((await db.execute(select(ManagerJob).where(ManagerJob.status == "waiting_connection"))).scalars())
        released = 0
        if waiting:
            ready = await manager_readiness(await self._profile_id(db))
            if ready["connected"] and ready["code"] not in {"needs_human", "not_agent_mode"}:
                for job in waiting:
                    job.status = "queued"
                    job.error = None
                    released += 1
                await db.commit()
        queued = (await db.execute(select(ManagerJob.id).where(ManagerJob.status == "queued"))).first() is not None
        if queued:
            self.kick()
        return {"reviews_enqueued": created, "released": released, "queued": queued}

    async def _enqueue_reviews(self, db: AsyncSession) -> int:
        reviews = list((await db.execute(select(Task).where(
            Task.task_type == "review", Task.assigned_agent_id == MANAGER_AGENT_ID, Task.status == "queued",
        ))).scalars())
        created = 0
        for review in reviews:
            exists = (await db.execute(select(ManagerJob.id).where(ManagerJob.review_task_id == review.id))).first()
            if exists is not None:
                continue
            review.status = "in_progress"
            review.started_at = review.started_at or _now()
            await self.enqueue(db, "review", project_id=review.project_id, review_task_id=review.id)
            created += 1
        return created

    async def _notify_waiting(self, db: AsyncSession, job: ManagerJob, ready: dict[str, Any]) -> None:
        db.add(Notification(category="arena", severity="warning", title="المدير غير متصل بـ Arena",
                            message=f"{ready['reason_ar']} (job {job.kind})", link="/browsers"))
        await db.commit()
        await event_bus.emit("manager.activity", _job_dict(job) or {})

    # ------------------------------------------------------------------ draining
    async def drain(self) -> list[dict[str, Any]]:
        """Process queued jobs in order, one manager prompt in flight at a time."""
        results: list[dict[str, Any]] = []
        async with self._in_flight_lock():
            while True:
                async with self.session_factory() as db:
                    job = (await db.execute(
                        select(ManagerJob).where(ManagerJob.status == "queued").order_by(ManagerJob.created_at.asc())
                    )).scalars().first()
                    if job is None:
                        break
                    try:
                        results.append(await self._run_job(db, job))
                    except Exception as exc:  # noqa: BLE001 - one broken job must not stop the queue
                        logger.warning("manager job %s failed: %s", job.id, type(exc).__name__, exc_info=True)
                        await db.rollback()
                        job = await db.get(ManagerJob, job.id)
                        if job is not None:
                            job.status = "failed"
                            job.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                            job.finished_at = _now()
                            await db.commit()
                            await self._reply(db, job, f"تعذّر تنفيذ طلب المدير ({type(exc).__name__}).", failed=True)
                        results.append({"job_id": getattr(job, "id", None), "status": "failed"})
        return results

    async def _set_manager_status(self, db: AsyncSession, status: str) -> None:
        manager = await db.get(Agent, MANAGER_AGENT_ID)
        if manager is not None and manager.status != status:
            manager.status = status
            manager.last_heartbeat = _now()

    async def _run_job(self, db: AsyncSession, job: ManagerJob) -> dict[str, Any]:
        from app.providers.adapters.arena_agent import ArenaAgentError

        profile_id = await self._profile_id(db)
        ready = await manager_readiness(profile_id)
        if not ready["connected"] or ready["code"] in {"needs_human", "not_agent_mode"}:
            job.status = "waiting_connection"
            job.error = ready["reason_ar"]
            await db.commit()
            await self._reply(db, job, NOT_CONNECTED_AR if ready["code"] == "offline" else ready["reason_ar"], waiting=True,
                              readiness=ready["code"])
            await self._notify_waiting(db, job, ready)
            return {"job_id": job.id, "status": job.status}
        job.status = "running"
        job.attempts += 1
        job.started_at = _now()
        await self._set_manager_status(db, "working")
        await db.commit()
        await event_bus.emit("manager.activity", _job_dict(job) or {})
        try:
            session = await self._ensure_session(db, job, profile_id)
            job.session_id = session.id
            if job.kind == "context":
                await self._finish(db, job, result={"session_id": session.id})
                return {"job_id": job.id, "status": job.status}
            prompt = await self._prompt(db, job)
            if prompt is None:
                await self._finish(db, job, status="failed", error="The job's task or project no longer exists.")
                return {"job_id": job.id, "status": job.status}
            reply = await self._exchange(db, session, job, prompt)
            if job.kind in {"plan", "review"}:
                parsed, error = await self._parse(db, job, reply)
                if parsed is None:
                    job.json_retries += 1
                    await db.commit()
                    reply = await self._exchange(db, session, job, MANAGER_JSON_RETRY)
                    parsed, error = await self._parse(db, job, reply)
                if parsed is None:
                    await self._needs_human(db, job, f"invalid JSON after one retry: {error}", raw=reply)
                    return {"job_id": job.id, "status": job.status, "error": error}
                await self._apply(db, job, parsed, raw=reply)
            else:
                await self._finish(db, job, result={"chars": len(reply)}, raw=reply)
                await self._reply(db, job, reply or "(لم يرسل المدير أي رد)")
            return {"job_id": job.id, "status": job.status}
        except ManagerArenaError as exc:
            if exc.code in {"stalled", "needs_human"}:
                await self._needs_human(db, job, exc.message, raw=None)
            else:
                job.status = "waiting_connection"
                job.error = exc.message
                await db.commit()
                await self._reply(db, job, NOT_CONNECTED_AR, waiting=True, readiness=exc.code)
            return {"job_id": job.id, "status": job.status, "error": exc.code}
        except ArenaAgentError as exc:
            if exc.code == "needs_human":
                await self._needs_human(db, job, f"Arena page needs a human: {exc.message}", raw=None)
            else:
                job.status = "waiting_connection"
                job.error = f"{exc.code}: {exc.message}"[:500]
                await db.commit()
                await self._reply(db, job, NOT_CONNECTED_AR, waiting=True, readiness=exc.code)
            return {"job_id": job.id, "status": job.status, "error": exc.code}
        finally:
            await self._set_manager_status(db, "idle")
            await db.commit()

    # ------------------------------------------------------------------ session
    async def _open_session(self, db: AsyncSession) -> ArenaSession | None:
        return (await db.execute(
            select(ArenaSession).where(ArenaSession.kind == "manager", ArenaSession.is_open.is_(True))
            .order_by(ArenaSession.created_at.desc())
        )).scalars().first()

    async def _ensure_session(self, db: AsyncSession, job: ManagerJob, profile_id: str) -> ArenaSession:
        session = await self._open_session(db)
        limit = get_settings().ARENA_MANAGER_MAX_EXCHANGES
        reason = None
        if session is None:
            reason = "no open manager session"
        elif job.project_id and session.project_id and session.project_id != job.project_id:
            reason = f"project changed to {job.project_id}"
        elif limit and session.exchange_count >= limit:
            reason = f"{session.exchange_count} exchanges reached ARENA_MANAGER_MAX_EXCHANGES={limit}"
        if session is not None and reason is None:
            if session.phase == "needs_human":  # a human resolved the page (the bridge says connected again)
                session.phase = "manager_ready"
                session.needs_human_reason = None
                append_event(session, "monitoring_resumed")
            if job.project_id and not session.project_id:
                session.project_id = job.project_id
            return session
        if session is not None:
            await close_session(db, session, phase="closed", reason=reason)
        await self.adapter.new_session(profile_id)
        session = ArenaSession(kind="manager", agent_id=MANAGER_AGENT_ID, profile_id=profile_id, project_id=job.project_id,
                               phase="manager_ready", page_state="ready", events=[])
        append_event(session, "created", reason)
        db.add(session)
        await db.commit()
        await event_bus.emit("arena.session", {"session_id": session.id, "agent_id": MANAGER_AGENT_ID, "phase": session.phase,
                                               "kind": "manager"})
        project = await db.get(Project, job.project_id) if job.project_id else None
        open_tasks = await self._open_tasks(db, job.project_id)
        prs = [{"number": t["pr_number"], "title": t["title"], "status": t["status"], "url": t["pr_url"]} for t in open_tasks if t["pr_url"]]
        context = build_manager_context_prompt(project, await self._team(db), open_tasks, prs)
        reply = await self._exchange(db, session, job, context, kind="context")
        append_event(session, "context_sent", f"{len(context)} chars", reply_chars=len(reply))
        await db.commit()
        return session

    async def _exchange(self, db: AsyncSession, session: ArenaSession, job: ManagerJob, text: str, *, kind: str | None = None) -> str:
        """Submit one prompt (shared submit path), follow the page until it is quiet, read the reply."""
        from app.providers.adapters.arena_agent import ArenaAgentError, keep_working_allowed

        before = await self.adapter.state(session.profile_id)
        before_since = str(before.get("agent_state_since") or "")
        try:
            await self.adapter.submit(session.profile_id, text, source="manager")
        except ArenaAgentError as exc:
            append_event(session, "submit_failed", f"{exc.code}: {exc.message}")
            await db.commit()
            raise
        session.exchange_count += 1
        session.submitted_at = _now()
        session.last_state_change_at = session.submitted_at
        session.phase = "manager_busy"
        append_event(session, "submitted", f"{kind or job.kind}: {len(text)} chars", job_id=job.id)
        await db.commit()
        await event_bus.emit("manager.activity", {**(_job_dict(job) or {}), "exchange": session.exchange_count})

        started = time.monotonic()
        last_change = time.monotonic()
        last_seen = (str(before.get("agent_state") or ""), before_since)
        seen_busy = False
        while True:
            state = await self.adapter.state(session.profile_id)
            current = str(state.get("agent_state") or ("offline" if not state.get("connected") else "")) or "offline"
            since = str(state.get("agent_state_since") or "")
            if (current, since) != last_seen:
                last_seen = (current, since)
                last_change = time.monotonic()
                if current != session.page_state:
                    session.page_state = current
                    session.last_state_change_at = _now()
                    append_event(session, f"state:{current}")
                    await db.commit()
            if state.get("status") == "needs_human" or current == "needs_human":
                await self._session_needs_human(db, session, str(state.get("needs_human_reason") or "human check on the Arena page"))
                raise ManagerArenaError("needs_human", f"Arena page needs a human: {state.get('needs_human_reason') or ''}".strip())
            if current in BUSY_STATES:
                seen_busy = True
            if current == "keep_working_visible":
                if keep_working_allowed(session.keep_working_count):
                    clicked = await self.adapter.keep_working(session.profile_id)
                    if clicked.get("clicked"):
                        session.keep_working_count += 1
                        append_event(session, "keep_working", f"#{session.keep_working_count}")
                        await db.commit()
                elif seen_busy:
                    append_event(session, "keep_working_limit", f"limit {session.keep_working_count} reached")
                    await db.commit()
                    break
            elif current in QUIET_STATES and (seen_busy or (since and since != before_since)
                                              or time.monotonic() - started > self.start_grace_seconds):
                break
            if time.monotonic() - last_change > self.stall_minutes * 60:
                reason = f"stalled: no Arena activity for {self.stall_minutes:g} minutes"
                await self._session_needs_human(db, session, reason)
                raise ManagerArenaError("stalled", reason)
            if current == "offline" and time.monotonic() - last_change > max(30.0, self.poll_seconds * 30):
                raise ManagerArenaError("offline", "The manager's Arena page went offline.")
            await asyncio.sleep(self.poll_seconds)

        reply = await self.adapter.read_last_reply(session.profile_id)
        text_out = redact_secrets(str(reply.get("text") or ""))[:MAX_RAW_REPLY_CHARS]
        session.phase = "manager_ready"
        session.last_state_change_at = _now()
        append_event(session, "reply", f"{len(text_out)} chars", job_id=job.id)
        await db.commit()
        await self._stream(job, text_out)
        return text_out

    async def _session_needs_human(self, db: AsyncSession, session: ArenaSession, reason: str) -> None:
        session.phase = "needs_human"
        session.needs_human_reason = reason[:240]
        append_event(session, "needs_human", reason)
        profile = await db.get(BrowserProfile, session.profile_id)
        if profile is not None:
            profile.connector_status = "needs_human"
            profile.needs_human_reason = reason[:240]
        db.add(Notification(category="arena", severity="critical", title="جلسة المدير في Arena تحتاجك",
                            message=f"{reason}. افتح ملف Chrome الخاص بالمدير، عالج الصفحة، ثم اضغط «استئناف».", link="/browsers"))
        db.add(AuditLog(actor=MANAGER_AGENT_ID, action="arena.needs_human", resource_type="browser_profile",
                        resource_id=session.profile_id, details={"reason": reason[:240], "session_id": session.id, "kind": "manager"},
                        severity="warning"))
        await db.commit()
        await event_bus.emit("arena.needs_human", {"agent_id": MANAGER_AGENT_ID, "profile_id": session.profile_id, "reason": reason})

    # ------------------------------------------------------------------ prompts / parsing
    async def _prompt(self, db: AsyncSession, job: ManagerJob) -> str | None:
        project = await db.get(Project, job.project_id) if job.project_id else None
        team = await self._team(db)
        if job.kind == "chat":
            return build_manager_chat_prompt(job.request_text, project, team)
        if job.kind == "status":
            summary = await self.status_summary(db, job.project_id)
            job.context = {**(job.context or {}), "status_summary": summary}
            return build_manager_status_prompt(job.request_text, summary, project, team)
        if job.kind == "plan":
            if project is None:
                return None
            return build_manager_plan_prompt(job.request_text, project, team, await self._open_tasks(db, job.project_id))
        if job.kind == "review":
            return await self._review_prompt(db, job, project, team)
        return None

    async def _review_prompt(self, db: AsyncSession, job: ManagerJob, project: Project | None, team: list[dict[str, Any]]) -> str | None:
        from app.orchestration.arena_runner import arena_runner

        review = await db.get(Task, job.review_task_id) if job.review_task_id else None
        parent = await db.get(Task, review.parent_task_id) if review and review.parent_task_id else None
        if review is None or parent is None:
            return None
        material = await arena_runner.review_material(db, parent)
        files = material.get("changed_files", []) if material else []
        session = await session_for_task(db, parent.id)
        changes = await arena_runner._compare(project, session) if session is not None else {"ok": False}
        diff = "\n".join(f"--- {f['filename']}\n{f.get('patch', '')}" for f in changes.get("files", [])) if changes.get("ok") else ""
        job.context = {**(job.context or {}), "parent_task_id": parent.id, "pr_url": parent.pr_url,
                       "changed_files": [f["filename"] for f in files][:100], "diff_chars": len(diff)}
        return build_manager_review_prompt(
            parent, acceptance=task_acceptance_criteria(parent, project), report=(session.report if session else "") or "",
            diff=diff, pr_url=parent.pr_url, round_number=parent.review_round, project=project, team=team,
            discrepancies=(material or {}).get("mechanical_discrepancies"),
        )

    async def _parse(self, db: AsyncSession, job: ManagerJob, reply: str) -> tuple[dict[str, Any] | None, str]:
        data = extract_manager_json(reply)
        if job.kind == "plan":
            known = {m["id"] for m in await self._team(db)}
            return validate_manager_plan(data, known)
        return validate_manager_review(data)

    # ------------------------------------------------------------------ outcomes
    async def _finish(self, db: AsyncSession, job: ManagerJob, *, status: str = "done", result: dict[str, Any] | None = None,
                      raw: str | None = None, error: str | None = None) -> None:
        job.status = status
        job.result = {**(job.result or {}), **(result or {})}
        if raw is not None:
            job.raw_reply = raw[:MAX_RAW_REPLY_CHARS]
        job.error = error
        job.finished_at = _now()
        await db.commit()
        await event_bus.emit("manager.activity", _job_dict(job) or {})

    async def _needs_human(self, db: AsyncSession, job: ManagerJob, reason: str, *, raw: str | None) -> None:
        await self._finish(db, job, status="needs_human", raw=raw, error=reason[:500])
        db.add(Notification(category="manager", severity="critical", title="طلب المدير يحتاج تدخلًا بشريًا",
                            message=f"{job.kind}: {reason[:300]}", link="/chat" if job.kind != "review" else "/approvals"))
        if job.review_task_id:
            review = await db.get(Task, job.review_task_id)
            if review is not None:
                review.status = "blocked"
                review.error_message = f"Manager review needs a human: {reason}"[:500]
                review.checkpoints = list(review.checkpoints or []) + [
                    {"ts": _now().isoformat(), "event": "manager_review_needs_human", "manager_job_id": job.id}]
        await db.commit()
        text = ("تعذّر على المدير إرسال رد صالح، لذا لم يُنشأ أي شيء ولم تُخترع خطة أو حكم. الطلب يحتاج تدخلًا بشريًا."
                if raw is not None else f"جلسة المدير في Arena تحتاج تدخلًا بشريًا: {reason}")
        await self._reply(db, job, text, needs_human=True, raw_reply=(raw or "")[:4_000] or None)

    async def _apply(self, db: AsyncSession, job: ManagerJob, parsed: dict[str, Any], *, raw: str) -> None:
        if job.kind == "plan":
            await self._apply_plan(db, job, parsed, raw=raw)
        else:
            await self._apply_review(db, job, parsed, raw=raw)

    async def _apply_plan(self, db: AsyncSession, job: ManagerJob, plan: dict[str, Any], *, raw: str) -> None:
        from app.orchestration.scheduler import task_scheduler

        gated = list((job.context or {}).get("gated_categories") or [])
        if gated:
            await self._finish(db, job, result={"plan": plan, "deferred": gated}, raw=raw)
            await self._reply(db, job, plan["summary_ar"] + "\n\nلم تُنشأ المهام: طلب عالي الخطورة ينتظر موافقة المالك.", plan=plan,
                              deferred=gated)
            return
        team = {m["id"]: m for m in await self._team(db)}
        tasks: list[Task] = []
        for item in plan["tasks"]:
            divisions = team[item["assignee"]].get("divisions") or []
            tasks.append(Task(
                project_id=job.project_id, division_code=divisions[0] if divisions else "D01", title=item["title"],
                description=item["description"], priority="high", execution_mode="manual", status="queued",
                assigned_agent_id=item["assignee"], dependencies=[], expected_deliverables=[],
                checkpoints=[{"ts": _now().isoformat(), "event": "manager_plan", "manager_job_id": job.id,
                              "acceptance_criteria": item["acceptance_criteria"]}],
            ))
        db.add_all(tasks)
        await db.flush()
        for task, item in zip(tasks, plan["tasks"], strict=True):
            task.dependencies = [tasks[i].id for i in item["depends_on"]]
            db.add(AuditLog(actor=MANAGER_AGENT_ID, action="task.create", resource_type="task", resource_id=task.id,
                            details={"title": task.title, "assignee": task.assigned_agent_id, "source": "manager_arena_plan",
                                     "manager_job_id": job.id}))
        await db.commit()
        await self._finish(db, job, result={"task_ids": [t.id for t in tasks], "summary_ar": plan["summary_ar"]}, raw=raw)
        lines = [plan["summary_ar"], ""] + [f"- {t.title} ← {t.assigned_agent_id}" for t in tasks]
        await self._reply(db, job, "\n".join(lines), task_ids=[t.id for t in tasks], plan=plan)
        for task in tasks:
            await event_bus.emit("task.created", {"task_id": task.id, "source": "manager_arena"})
        # Dispatch to the workers' Arena sessions through the existing R3 flow.
        await task_scheduler.run_orchestration_cycle(db)

    async def _apply_review(self, db: AsyncSession, job: ManagerJob, verdict: dict[str, Any], *, raw: str) -> None:
        from app.orchestration.review import review_service
        from app.orchestration.scheduler import task_scheduler

        review = await db.get(Task, job.review_task_id) if job.review_task_id else None
        manager = await db.get(Agent, MANAGER_AGENT_ID)
        if review is None or manager is None:
            await self._finish(db, job, status="failed", error="Review task missing.", raw=raw)
            return
        body = "\n".join([verdict["summary_ar"], *[f"- {c}" for c in verdict["comments"]]])
        event = "REQUEST_CHANGES" if verdict["verdict"] == "request_changes" else "COMMENT"
        outcome = await review_service.submit_review(db, review, manager, event=event, body=body)
        review = await db.get(Task, job.review_task_id)
        if review is not None and review.status not in {"completed", "done"}:
            await task_scheduler.complete_task(db, review, summary=f"Manager verdict: {verdict['verdict']}", target_status="completed")
        await self._finish(db, job, result={"verdict": verdict, "outcome": {k: v for k, v in outcome.items() if k != "github"}}, raw=raw)
        from app.runtime.code_stream import build_review_frame, code_stream

        frame = build_review_frame(agent_id=MANAGER_AGENT_ID, task_id=review.parent_task_id if review else None, body=body, event=event)
        if frame is not None:
            await code_stream.publish(frame)
        await event_bus.emit("manager.review", {"review_task_id": job.review_task_id, "verdict": verdict["verdict"],
                                                "follow_up_task_id": outcome.get("follow_up_task_id"),
                                                "merge_approval_id": outcome.get("merge_approval_id")})


def task_acceptance_criteria(task: Task, project: Project | None = None) -> list[str]:
    """Acceptance criteria the manager wrote for this task (plan), then the project's own."""
    criteria: list[str] = []
    for cp in task.checkpoints or []:
        if isinstance(cp, dict) and isinstance(cp.get("acceptance_criteria"), list):
            criteria += [str(c) for c in cp["acceptance_criteria"] if str(c).strip()]
    config = (project.config or {}) if project and isinstance(project.config, dict) else {}
    if isinstance(config.get("acceptance_criteria"), list):
        criteria += [str(c) for c in config["acceptance_criteria"] if str(c).strip()]
    return list(dict.fromkeys(criteria))


async def manager_reviews_arena_prs(db: AsyncSession, task: Task) -> bool:
    """True when the manager (not a reviewer employee) reviews this PR: manager on Arena + Arena-built PR."""
    if await manager_provider_mode(db) != "arena_agent":
        return False
    return await session_for_task(db, task.id) is not None


manager_arena_runtime = ManagerArenaRuntime()
