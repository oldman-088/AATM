"""manager-0 on Arena Agent mode (R7): the manager's own Arena session, driven like the workers'."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any

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
    MANAGER_JSON_RETRY_DETAIL,
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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
QUIET_STATES = {"idle", "ready", "pr_ready"}
BUSY_STATES = {"generating", "keep_working_visible", "tool_call_limit"}
OPEN_JOB_STATES = ("queued", "waiting_connection", "running")
TERMINAL_TASK_STATES = {"done", "merged", "completed", "cancelled"}
MAX_RAW_REPLY_CHARS = 20_000
# A plan or review job that keeps failing must stop instead of re-running forever (one job reached 74 attempts).
MAX_JOB_ATTEMPTS = 3
# E37: a busy or disconnected manager window is not a failed attempt; refund it, but never forever.
MAX_ATTEMPT_REFUNDS = 12
MAX_STATUS_QUESTION_CHARS = 200  # a long brief that mentions "status" is a goal, not a question

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


# ------------------------------------------------------------------ E8: plan validation and policy
MAX_TASKS_WITHOUT_DRAFT = 5      # more tasks than this: the owner approves the plan first
MAX_TASKS_ONE_LINE = 3           # a one-line request gets 1-3 tasks at most
CHAIN_SHARE = 0.5                # warn when more than this share of the tasks depend on one task

PLANNING_RULES = (
    "## Planning rules",
    "- Plan independent tasks together. Leave depends_on empty unless a task needs another task's output.",
    "- Add a dependency only when a task needs another task's output. Never chain tasks just to order them.",
    "- Prefer few, larger tasks over many small ones.",
    "- A one-line request gets 1 to 3 tasks at most.",
    "- If the goal is unclear, set \"ambiguous\": true and ask the owner your question in summary_ar.",
    "- Refuse goals about unauthorized scraping, surveillance or profiling of private people, or CAPTCHA / anti-bot bypass: return an empty tasks list and say why in summary_ar.",
)

# (pattern, reason key). Matched case-insensitively against the goal and every planned task.
_POLICY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(scrap(e|es|ed|ing)|crawl(s|ed|ing)?|harvest)\b.{0,80}\b(private|personal|members?[- ]only|login|logged[- ]in|password|e-?mails?|phone numbers?|without (permission|consent|authori[sz]ation)|unauthori[sz]ed|rate[- ]limit)", re.I), "unauthorized_scraping"),
    (re.compile(r"\b(private|personal|members?[- ]only|without (permission|consent)|unauthori[sz]ed)\b.{0,80}\b(scrap(e|es|ed|ing)|crawl(s|ed|ing)?)\b", re.I), "unauthorized_scraping"),
    (re.compile(r"(سحب|استخراج|كشط)\s+(ال)?بيانات.{0,60}(الخاصة|الشخصية|بدون إذن)"), "unauthorized_scraping"),
    (re.compile(r"\b(surveil(l)?ance|spy(ing)? on|stalk(ing)?|covertly (track|monitor))\b", re.I), "surveillance_or_profiling"),
    (re.compile(r"\b(track|monitor|profil(e|ing)|follow|locate|find where)\s+(a |an |the |this |that |my |their )?(specific |private )?(person|people|individual|someone|employee|customer|user|somebody|him|her)\b", re.I), "surveillance_or_profiling"),
    (re.compile(r"\b(solve|solving|bypass|bypassing|defeat|break|crack|circumvent|evade|get past|beat)\b.{0,40}\b(captcha|recaptcha|hcaptcha|anti[- ]?bot|bot[- ]detection)\b", re.I), "captcha_or_antibot_bypass"),
    (re.compile(r"\b(captcha|recaptcha|hcaptcha|anti[- ]?bot)\b.{0,40}\b(bypass|solve|defeat|break|circumvent|evade|get past)\b", re.I), "captcha_or_antibot_bypass"),
    (re.compile(r"(تجاوز|كسر|اكسر|تخطي)\s+(ال)?(كابتشا|حماية|الحماية)"), "captcha_or_antibot_bypass"),
)

POLICY_TEXT_AR = {
    "unauthorized_scraping": "هذا الطلب يطلب جمع بيانات خاصة أو سحبها دون إذن، وهذا مرفوض.",
    "surveillance_or_profiling": "هذا الطلب يطلب مراقبة أشخاص محددين أو تتبعهم أو تنميط هويتهم، وهذا مرفوض.",
    "captcha_or_antibot_bypass": "هذا الطلب يطلب تجاوز CAPTCHA أو حماية مضادة للبوتات، وهذا مرفوض.",
}
POLICY_TEXT_EN = {
    "unauthorized_scraping": "Refused: unauthorized scraping of private data.",
    "surveillance_or_profiling": "Refused: surveillance or profiling of private people.",
    "captcha_or_antibot_bypass": "Refused: CAPTCHA or anti-bot bypass.",
}


def policy_violation(text: str) -> str | None:
    """The reason key when the text asks for something AATM will not plan (no tasks are created), else None."""
    for pattern, reason in _POLICY_RULES:
        if pattern.search(text or ""):
            return reason
    return None


def validate_plan_graph(tasks: list[dict[str, Any]]) -> list[str]:
    """Reject unknown dependencies, self-dependencies and cycles. An empty list means the graph is valid."""
    count = len(tasks)
    errors: list[str] = []
    for index, task in enumerate(tasks):
        for dep in task.get("depends_on") or []:
            if not isinstance(dep, int) or isinstance(dep, bool) or dep < 0 or dep >= count:
                errors.append(f"Task {index} depends on unknown task {dep}.")
            elif dep == index:
                errors.append(f"Task {index} depends on itself.")
    if errors:
        return errors
    state = [0] * count  # 0 = unvisited, 1 = on the stack, 2 = done

    def _has_cycle(index: int) -> bool:
        if state[index] == 1:
            return True
        if state[index] == 2:
            return False
        state[index] = 1
        if any(_has_cycle(dep) for dep in tasks[index].get("depends_on") or []):
            return True
        state[index] = 2
        return False

    if any(_has_cycle(index) for index in range(count)):
        errors.append("The plan has a circular dependency between its tasks.")
    return errors


def plan_warnings(tasks: list[dict[str, Any]]) -> list[str]:
    """Warn when more than half of the tasks depend on one task: the plan is a chain, not parallel work."""
    count = len(tasks)
    dependents: dict[int, int] = {}
    for task in tasks:
        for dep in set(task.get("depends_on") or []):
            dependents[dep] = dependents.get(dep, 0) + 1
    warnings: list[str] = []
    for dep, n in sorted(dependents.items()):
        if n > count * CHAIN_SHARE:
            warnings.append(
                f"{n} of {count} tasks depend on task {dep} ({tasks[dep]['title']}). "
                "Check that each of them really needs that output; independent tasks should run in parallel."
            )
    return warnings


def _context_matches_team(session: ArenaSession, team_ids: list[str]) -> bool:
    """True when the team in the session's last context message is today's team. Sessions started before the
    team was recorded have no ``team`` entry and are left alone."""
    for event in reversed(session.events or []):
        if event.get("kind") == "context_sent":
            sent = event.get("team")
            return not isinstance(sent, list) or sorted(map(str, sent)) == sorted(team_ids)
    return True


def clarification_question(data: Any) -> str | None:
    """The manager's question when it answers a vague goal with ``{"ambiguous": true, "tasks": []}``.

    That is a valid answer, not broken JSON: the owner must see the question. Anything else (no question,
    a flag with tasks, no flag) is not a clarification and keeps the strict plan validation.
    """
    if not isinstance(data, dict) or data.get("ambiguous") is not True:
        return None
    question = data.get("summary_ar")
    if not isinstance(question, str) or not question.strip() or data.get("tasks"):
        return None
    return question.strip()


def plan_draft_reasons(tasks: list[dict[str, Any]], goal: str, ambiguous: bool, *, auto_approve: bool = False) -> list[str]:
    """Why this plan needs the owner's approval before its tasks are created (empty: create directly).

    ``auto_approve`` (the owner's ARENA_MANAGER_AUTO_APPROVE_PLANS choice) skips the size rules only; an
    ambiguous goal still waits for confirmation."""
    reasons: list[str] = []
    if not auto_approve:
        if len(tasks) > MAX_TASKS_WITHOUT_DRAFT:
            reasons.append(f"The plan has {len(tasks)} tasks; more than {MAX_TASKS_WITHOUT_DRAFT} needs your approval.")
        stripped = (goal or "").strip()
        one_line = bool(stripped) and "\n" not in stripped and len(stripped) <= 160
        if one_line and len(tasks) > MAX_TASKS_ONE_LINE:
            reasons.append(f"A one-line request should give 1 to {MAX_TASKS_ONE_LINE} tasks; this plan has {len(tasks)}.")
    if ambiguous:
        reasons.append("The goal is ambiguous; confirm it before any task is created.")
    return reasons


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
    if len(text) <= MAX_STATUS_QUESTION_CHARS and _STATUS_RE.search(text):
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
        self._task_lock: tuple[Any, asyncio.Lock] | None = None
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

    async def _team(self, db: AsyncSession, project_id: str | None = None) -> list[dict[str, Any]]:
        """The workers the manager may plan for: every enabled worker, or the project's own team when
        ``project.config["team"]`` lists worker ids (unknown or disabled ids are ignored)."""
        workers = list((await db.execute(
            select(Agent).where(Agent.role == "worker", Agent.is_enabled.is_(True), Agent.is_emergency_stopped.is_(False))
            .order_by(Agent.created_at, Agent.id)
        )).scalars())
        project = await db.get(Project, project_id) if project_id else None
        team_ids = (project.config or {}).get("team") if project else None
        if isinstance(team_ids, list) and team_ids:
            workers = [w for w in workers if w.id in {str(t) for t in team_ids}]
        return [{"id": w.id, "name": w.name, "specialty": w.specialty, "skills": list(w.skills or []),
                 "divisions": list(w.divisions or [])} for w in workers]

    async def _open_tasks(self, db: AsyncSession, project_id: str | None) -> list[dict[str, Any]]:
        # Never fall back to every project: without a project there is no historical context to show.
        if not project_id:
            return []
        query = select(Task).where(Task.status.not_in(TERMINAL_TASK_STATES), Task.task_type == "implementation",
                                   Task.project_id == project_id)
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
        refusal = policy_violation(text or content)
        if kind == "plan" and refusal:
            return await self._refuse(db, job, refusal)
        if kind == "plan" and (not project_id or await db.get(Project, project_id) is None):
            await self._finish(db, job, status="failed", error="no project selected")
            message = await self._reply(db, job, "اختر مشروعًا أولًا: الخطة تُنشئ مهام داخل مشروع، ولم يُرسل شيء إلى المدير.",
                                        failed=True)
            assert message is not None
            return message
        project = await db.get(Project, project_id) if project_id else None
        if kind == "plan" and project is not None and project.status in {"completed", "disabled"}:
            await self._finish(db, job, status="failed", error=f"project is {project.status}")
            message = await self._reply(db, job, f"المشروع «{project.name}» مكتمل، ولم يُرسل شيء إلى المدير. اختر مشروعًا نشطًا.",
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

    @staticmethod
    def _refund_attempt(job: ManagerJob) -> None:
        """Give back the attempt a busy/disconnected window consumed (bounded by MAX_ATTEMPT_REFUNDS)."""
        context = dict(job.context or {})
        refunds = int(context.get("attempt_refunds", 0))
        if refunds < MAX_ATTEMPT_REFUNDS and job.attempts > 0:
            context["attempt_refunds"] = refunds + 1
            job.context = context
            job.attempts -= 1

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
        if job.attempts >= MAX_JOB_ATTEMPTS:
            await self._finish(db, job, status="failed",
                               error=f"stopped after {job.attempts} attempts to avoid an endless loop")
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
            resume = False
            if (job.context or {}).get("interrupted"):
                job.context = {k: v for k, v in job.context.items() if k != "interrupted"}
                resume = await self._interrupted_prompt_sent(session, job)
                append_event(session, "job_resumed", "waiting for the reply" if resume else "prompt was not sent", job_id=job.id)
                await db.commit()
            if job.kind == "context":
                await self._finish(db, job, result={"session_id": session.id})
                return {"job_id": job.id, "status": job.status}
            if job.kind == "plan":
                existing = await self.tasks_for_request(db, job)
                if existing:
                    await self._finish(db, job, result={**(job.result or {}), "task_ids": [t.id for t in existing],
                                                        "idempotent_retry": True})
                    return {"job_id": job.id, "status": job.status, "idempotent_retry": True}
            prompt = await self._prompt(db, job)
            if prompt is None:
                await self._finish(db, job, status="failed", error="The job's task or project no longer exists.")
                return {"job_id": job.id, "status": job.status}
            reply = await self._exchange(db, session, job, prompt, resume=resume)
            if job.kind in {"plan", "review"}:
                parsed, error = await self._parse(db, job, reply)
                if parsed is None and await self._answer_clarification(db, job, reply):
                    return {"job_id": job.id, "status": job.status, "clarification": True}
                if parsed is None:
                    job.json_retries += 1
                    await db.commit()
                    reply = await self._exchange(db, session, job, MANAGER_JSON_RETRY_DETAIL)
                    parsed, error = await self._parse(db, job, reply)
                    if parsed is None and await self._answer_clarification(db, job, reply):
                        return {"job_id": job.id, "status": job.status, "clarification": True}
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
                self._refund_attempt(job)
                await db.commit()
                await self._reply(db, job, NOT_CONNECTED_AR, waiting=True, readiness=exc.code)
            return {"job_id": job.id, "status": job.status, "error": exc.code}
        except ArenaAgentError as exc:
            if exc.code == "needs_human":
                await self._needs_human(db, job, f"Arena page needs a human: {exc.message}", raw=None)
            else:
                job.status = "waiting_connection"
                job.error = f"{exc.code}: {exc.message}"[:500]
                self._refund_attempt(job)
                await db.commit()
                await self._reply(db, job, NOT_CONNECTED_AR, waiting=True, readiness=exc.code)
            return {"job_id": job.id, "status": job.status, "error": exc.code}
        finally:
            await self._set_manager_status(db, "idle")
            await db.commit()

    # ------------------------------------------------------------------ restart
    async def recover_after_restart(self, db: AsyncSession) -> int:
        """Called once at startup: a job left running goes back to the queue and is checked before sending."""
        jobs = list((await db.execute(select(ManagerJob).where(ManagerJob.status == "running"))).scalars())
        for job in jobs:
            job.status = "queued"
            job.error = None
            job.context = {**(job.context or {}), "interrupted": True}
        if jobs:
            await self._set_manager_status(db, "idle")
        await db.commit()
        return len(jobs)

    async def _interrupted_prompt_sent(self, session: ArenaSession, job: ManagerJob) -> bool:
        from app.providers.adapters.arena_agent import ArenaAgentError

        events = [e for e in session.events or [] if e.get("job_id") == job.id
                  and e.get("kind") in {"submit_started", "submitted", "submit_failed"}]
        if not events or events[-1]["kind"] == "submit_failed":
            return False
        last = events[-1]
        if last["kind"] == "submitted":
            return not str(last.get("detail") or "").startswith("context:")
        if last.get("exchange") == "context":
            return False
        try:
            page = await self.adapter.transcript_mark(session.profile_id)
        except ArenaAgentError:
            return False
        return page.get("state") in BUSY_STATES or page.get("mark") != last.get("detail")

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
        team_ids = [m["id"] for m in await self._team(db, job.project_id)]
        if reason is None and session is not None and not _context_matches_team(session, team_ids):
            # The manager learned its team once, when the session started. A changed team (a worker added,
            # enabled or removed, or the project's team edited) must reach it, or it plans for the old team.
            reason = "the team changed since the manager's context was sent"
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
        context = build_manager_context_prompt(project, await self._team(db, job.project_id), open_tasks, prs)
        reply = await self._exchange(db, session, job, context, kind="context")
        append_event(session, "context_sent", f"{len(context)} chars", reply_chars=len(reply), team=team_ids)
        await db.commit()
        return session

    async def _exchange(self, db: AsyncSession, session: ArenaSession, job: ManagerJob, text: str, *, kind: str | None = None,
                        resume: bool = False) -> str:
        """Submit one prompt (shared submit path), follow the page until it is quiet, read the reply."""
        from app.providers.adapters.arena_agent import TOOL_LIMIT_CONTINUE, ArenaAgentError, keep_working_allowed

        before = await self.adapter.state(session.profile_id)
        before_since = str(before.get("agent_state_since") or "")
        before_state = str(before.get("agent_state") or "")
        expects_json = kind != "context" and job.kind in {"plan", "review"}
        pressed_on: str | None = None
        if not resume:  # after a restart the prompt may already be on the page: then only the reply is awaited
            mark = (await self.adapter.transcript_mark(session.profile_id)).get("mark")
            append_event(session, "submit_started", mark, job_id=job.id, exchange=kind or job.kind)
            await db.commit()
            try:
                await self.adapter.submit(session.profile_id, text, source="manager")
            except ArenaAgentError as exc:
                append_event(session, "submit_failed", f"{exc.code}: {exc.message}", job_id=job.id)
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
        seen_busy = resume
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
                # Arena shows this option after every reply: press it only for an unfinished answer.
                panel_is_new = (current, since) != (before_state, before_since)
                latest = None
                if panel_is_new or time.monotonic() - started > self.start_grace_seconds:
                    try:
                        latest = str((await self.adapter.read_last_reply(session.profile_id)).get("text") or "")
                    except ArenaAgentError as exc:
                        append_event(session, "read_reply_failed", f"{exc.code}: {exc.message}")
                        await db.commit()
                if latest is not None and latest.strip():
                    if not expects_json or extract_manager_json(latest) is not None:
                        break
                    if latest == pressed_on:  # the last press changed nothing: let the JSON retry handle it
                        append_event(session, "keep_working_no_progress", f"#{session.keep_working_count}")
                        await db.commit()
                        break
                    if keep_working_allowed(session.keep_working_count):
                        clicked = await self.adapter.keep_working(session.profile_id)
                        if clicked.get("clicked"):
                            pressed_on = latest
                            session.keep_working_count += 1
                            append_event(session, "keep_working", f"#{session.keep_working_count}")
                            await db.commit()
                    else:
                        append_event(session, "keep_working_limit", f"limit {session.keep_working_count} reached")
                        await db.commit()
                        break
            elif current == "tool_call_limit":
                if not keep_working_allowed(session.keep_working_count):
                    reason = f"Arena tool-call limit: all {session.keep_working_count} automatic continues were used"
                    await self._session_needs_human(db, session, reason)
                    raise ManagerArenaError("needs_human", reason)
                await self.adapter.submit(session.profile_id, TOOL_LIMIT_CONTINUE, source="manager")
                session.keep_working_count += 1
                append_event(session, "tool_limit_continue", f"#{session.keep_working_count}")
                await db.commit()
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
        team = await self._team(db, job.project_id)
        if job.kind == "chat":
            return build_manager_chat_prompt(job.request_text, project, team)
        if job.kind == "status":
            summary = await self.status_summary(db, job.project_id)
            job.context = {**(job.context or {}), "status_summary": summary}
            return build_manager_status_prompt(job.request_text, summary, project, team)
        if job.kind == "plan":
            if project is None:
                return None
            prompt = build_manager_plan_prompt(job.request_text, project, team, await self._open_tasks(db, job.project_id))
            return prompt + "\n" + "\n".join(PLANNING_RULES)
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

    async def _answer_clarification(self, db: AsyncSession, job: ManagerJob, reply: str) -> bool:
        """A plan job answered with a clarifying question: show it to the owner, create nothing, finish the job."""
        if job.kind != "plan":
            return False
        question = clarification_question(extract_manager_json(reply))
        if question is None:
            return False
        await self._finish(db, job, result={"clarification": True}, raw=reply)
        await self._reply(db, job, question, clarification=True)
        return True

    async def _parse(self, db: AsyncSession, job: ManagerJob, reply: str) -> tuple[dict[str, Any] | None, str]:
        data = extract_manager_json(reply)
        if job.kind == "plan":
            known = {m["id"] for m in await self._team(db, job.project_id)}
            plan, error = validate_manager_plan(data, known)
            if plan is not None and isinstance(data, dict) and data.get("ambiguous") is True:
                plan["ambiguous"] = True
            return plan, error
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
        """Validate a model plan before any task exists: policy, graph, then draft-or-create (E8)."""
        gated = list((job.context or {}).get("gated_categories") or [])
        if gated:
            await self._finish(db, job, result={"plan": plan, "deferred": gated}, raw=raw)
            await self._reply(db, job, plan["summary_ar"] + "\n\nلم تُنشأ المهام: طلب عالي الخطورة ينتظر موافقة المالك.", plan=plan,
                              deferred=gated)
            return
        planned_text = " ".join([job.request_text or ""] + [f"{t['title']} {t['description']}" for t in plan["tasks"]])
        refusal = policy_violation(planned_text)
        if refusal:
            await self._refuse(db, job, refusal, raw=raw)
            return
        errors = validate_plan_graph(plan["tasks"])
        if errors:
            await self._finish(db, job, status="failed", error="invalid plan: " + " ".join(errors)[:400], raw=raw)
            await self._reply(db, job, "الخطة مرفوضة لأن ترتيب مهامها غير صالح؛ لم تُنشأ أي مهام. " + " ".join(errors),
                              failed=True, plan_errors=errors)
            return
        warnings = plan_warnings(plan["tasks"])
        reasons = plan_draft_reasons(plan["tasks"], job.request_text or "", bool(plan.get("ambiguous")),
                                     auto_approve=get_settings().ARENA_MANAGER_AUTO_APPROVE_PLANS)
        if reasons:
            await self._make_draft(db, job, plan, raw=raw, warnings=warnings, reasons=reasons)
            return
        await self._create_plan_tasks(db, job, plan, raw=raw, warnings=warnings)

    async def _refuse(self, db: AsyncSession, job: ManagerJob, reason: str, *, raw: str | None = None) -> ChatMessage:
        """A goal the policy forbids: no tasks, the job fails with the reason, and the owner sees why."""
        await self._finish(db, job, status="failed", error=f"policy: {reason}", raw=raw)
        message = await self._reply(
            db, job,
            f"{POLICY_TEXT_AR[reason]} {POLICY_TEXT_EN[reason]} لم تُنشأ أي مهام.",
            failed=True, policy=reason,
        )
        assert message is not None
        return message

    async def _make_draft(self, db: AsyncSession, job: ManagerJob, plan: dict[str, Any], *, raw: str | None,
                          warnings: list[str], reasons: list[str]) -> None:
        """Keep the plan as a draft: nothing is created until the owner approves, edits or rejects it."""
        job.result = {**(job.result or {}), "draft": plan, "warnings": warnings, "draft_reasons": reasons}
        await self._finish(db, job, status="draft", raw=raw)
        lines = [plan["summary_ar"], "", "مسودة خطة بانتظار موافقتك (اعتماد / تعديل / رفض):"]
        lines += [f"- {t['title']} ← {t['assignee']}" for t in plan["tasks"]]
        lines += ["", "السبب: " + (" ".join(reasons) or "عُدّلت المسودة؛ اعتمدها لإنشاء المهام.")]
        lines += [f"تحذير: {w}" for w in warnings]
        await self._reply(db, job, "\n".join(lines), draft=True, draft_reasons=reasons, warnings=warnings, plan=plan)

    async def _create_plan_tasks(self, db: AsyncSession, job: ManagerJob, plan: dict[str, Any], *, raw: str | None,
                                 warnings: list[str]) -> list[Task]:
        from app.orchestration.scheduler import task_scheduler

        async with self._create_lock():
            existing = await self.tasks_for_request(db, job)
            if existing:
                # Idempotent retry: this request already produced its tasks. Never create or dispatch them twice.
                if job.status != "done":
                    await self._finish(db, job, result={**(job.result or {}), "task_ids": [t.id for t in existing],
                                                        "summary_ar": plan.get("summary_ar", ""), "warnings": warnings,
                                                        "idempotent_retry": True}, raw=raw)
                return existing
            tasks = await self._insert_plan_tasks(db, job, plan)
        await self._finish(db, job, result={"task_ids": [t.id for t in tasks], "summary_ar": plan["summary_ar"],
                                            "warnings": warnings}, raw=raw)
        lines = [plan["summary_ar"], ""] + [f"- {t.title} ← {t.assigned_agent_id}" for t in tasks]
        lines += [f"تحذير: {w}" for w in warnings]
        await self._reply(db, job, "\n".join(lines), task_ids=[t.id for t in tasks], plan=plan, warnings=warnings)
        for task in tasks:
            await event_bus.emit("task.created", {"task_id": task.id, "source": "manager_arena"})
        # Dispatch only this request's new tasks through the existing R3 flow; older queued tasks are left alone.
        await task_scheduler.run_orchestration_cycle(db, only_task_ids={t.id for t in tasks})
        return tasks

    def _create_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._task_lock is None or self._task_lock[0] is not loop:
            self._task_lock = (loop, asyncio.Lock())
        return self._task_lock[1]

    async def tasks_for_request(self, db: AsyncSession, job: ManagerJob) -> list[Task]:
        """Tasks already created by this request (the manager job id is the request id), oldest first."""
        rows = (await db.execute(
            select(Task).where(Task.project_id == job.project_id).order_by(Task.created_at.asc())
        )).scalars()
        return [t for t in rows if any(isinstance(c, dict) and (c.get("request_id") or c.get("manager_job_id")) == job.id
                                       for c in (t.checkpoints or []))]

    async def _insert_plan_tasks(self, db: AsyncSession, job: ManagerJob, plan: dict[str, Any]) -> list[Task]:
        team = {m["id"]: m for m in await self._team(db, job.project_id)}
        tasks: list[Task] = []
        for item in plan["tasks"]:
            divisions = team[item["assignee"]].get("divisions") or []
            tasks.append(Task(
                project_id=job.project_id, division_code=divisions[0] if divisions else "D01", title=item["title"],
                description=item["description"], priority="high", execution_mode="manual", status="queued",
                assigned_agent_id=item["assignee"], dependencies=[], expected_deliverables=[],
                checkpoints=[{"ts": _now().isoformat(), "event": "manager_plan", "manager_job_id": job.id,
                              "request_id": job.id, "acceptance_criteria": item["acceptance_criteria"]}],
            ))
        db.add_all(tasks)
        await db.flush()
        for task, item in zip(tasks, plan["tasks"], strict=True):
            task.dependencies = [tasks[i].id for i in item["depends_on"]]
            db.add(AuditLog(actor=MANAGER_AGENT_ID, action="task.create", resource_type="task", resource_id=task.id,
                            details={"title": task.title, "assignee": task.assigned_agent_id, "source": "manager_arena_plan",
                                     "manager_job_id": job.id, "request_id": job.id}))
        await db.commit()
        return tasks

    async def approve_draft(self, db: AsyncSession, job: ManagerJob) -> list[Task]:
        """Owner approved the draft: create its tasks now (the same checks run again first)."""
        if job.status != "draft" or not (job.result or {}).get("draft"):
            raise ValueError("Only a draft plan can be approved.")
        plan = job.result["draft"]
        team_ids = {m["id"] for m in await self._team(db, job.project_id)}
        missing = sorted({t["assignee"] for t in plan["tasks"]} - team_ids)
        if missing:
            raise ValueError(f"Assignees are no longer on the team: {', '.join(missing)}.")
        # Claim the draft atomically so a double click or two tabs cannot approve the same request twice.
        claimed = await db.execute(
            update(ManagerJob).where(ManagerJob.id == job.id, ManagerJob.status == "draft").values(status="running")
        )
        await db.commit()
        if claimed.rowcount != 1:
            raise ValueError("Only a draft plan can be approved.")
        await db.refresh(job)
        try:
            return await self._create_plan_tasks(db, job, plan, raw=job.raw_reply,
                                                 warnings=list((job.result or {}).get("warnings") or []))
        except Exception:
            await db.rollback()
            if not await self.tasks_for_request(db, job):
                job.status = "draft"
                await db.commit()
            raise

    async def edit_draft(self, db: AsyncSession, job: ManagerJob, data: dict[str, Any]) -> ManagerJob:
        """Owner edited the draft: validate the new plan exactly like a model plan; it stays a draft."""
        if job.status != "draft":
            raise ValueError("Only a draft plan can be edited.")
        known = {m["id"] for m in await self._team(db, job.project_id)}
        plan, error = validate_manager_plan(data, known)
        if plan is None:
            raise ValueError(error)
        if data.get("ambiguous") is True:
            plan["ambiguous"] = True
        refusal = policy_violation(" ".join([job.request_text or ""] + [f"{t['title']} {t['description']}" for t in plan["tasks"]]))
        if refusal:
            raise ValueError(POLICY_TEXT_EN[refusal])
        errors = validate_plan_graph(plan["tasks"])
        if errors:
            raise ValueError(" ".join(errors))
        await self._make_draft(db, job, plan, raw=job.raw_reply, warnings=plan_warnings(plan["tasks"]),
                               reasons=plan_draft_reasons(plan["tasks"], job.request_text or "", bool(plan.get("ambiguous"))))
        return job

    async def reject_draft(self, db: AsyncSession, job: ManagerJob, *, actor: str) -> ManagerJob:
        if job.status != "draft":
            raise ValueError("Only a draft plan can be rejected.")
        await self._finish(db, job, status="failed", error=f"draft rejected by {actor}")
        await self._reply(db, job, "تم رفض المسودة؛ لم تُنشأ أي مهام.", failed=True)
        return job

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
