"""manager-0 on Arena Agent mode (R7): the manager's own Arena session, driven like the workers'."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
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
    chained_dependencies,
    extract_manager_json,
    validate_manager_plan,
    validate_manager_review,
)
from app.runtime.agent_protocol import redact_secrets

logger = logging.getLogger(__name__)

MANAGER_PROVIDERS = ("arena_agent", "anthropic_claude", "openai_compatible", "demo")
MANAGER_PROVIDER_SETTING = "manager_provider"

NOT_CONNECTED_AR = "المدير غير متصل بـ Arena — افتح ملف Chrome الخاص بالمدير"
PAGE_HIDDEN_AR = ("نافذة المدير في Chrome مصغّرة أو مخفية، فلا تظهر خانة الكتابة. أظهر النافذة على الشاشة "
                  "وسيُرسل الطلب تلقائيًا.")
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
# E52: after this many identical connection errors in a row the owner sees the cause and gets one alert.
REPEATED_ERROR_ALERT = 3
MAX_STATUS_QUESTION_CHARS = 200  # a long brief that mentions "status" is a goal, not a question
# E37: a busy manager window is a transient state, not a failed attempt. Wait for it to become idle
# (configurable timeout) before submitting; only real attempts count and get a backoff.
BUSY_WAIT_TIMEOUT_SECONDS = 300.0
BUSY_POLL_SECONDS = 2.0
REAL_RETRY_BACKOFF_SECONDS = 2.0
# E23: a review prompt never carries the whole diff; it carries a per-file summary plus risky hunks only.
REVIEW_FILES_PER_PART = 40
REVIEW_PROMPT_MAX_CHARS = 16_000


def _env_seconds(name: str, default: float) -> float:
    """A positive float from the environment (AATM_MANAGER_BUSY_WAIT_SECONDS, ...), else ``default``."""
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _waiting_for_manager_text(kind: str) -> str:
    """What the owner sees while the manager's Arena window is busy (E37)."""
    if kind == "review":
        return "المراجعة تنتظر المدير — نافذة Arena مشغولة الآن، وستُرسل تلقائيًا عند خلوّها."
    return "بانتظار المدير — نافذة Arena مشغولة الآن، وسيُرسل الطلب تلقائيًا عند خلوّها."

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


# A negation shortly before a forbidden match means the text forbids the behaviour, not asks for it
# (for example "do not solve CAPTCHAs"). Only the words directly before the match count, so a real
# request that merely mentions a negation elsewhere is still refused.
_NEGATION = re.compile(
    r"\b(never|not|no|don'?t|do not|must not|mustn'?t|avoid|refuse|refuses|forbid|forbidden|cannot|can'?t)\b|(?:^|\s)(لا|لن|ليس|ممنوع|تجنب)(?:\s|$)",
    re.I,
)
_NEGATION_WINDOW = 25


def _negated(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - _NEGATION_WINDOW):start]
    return bool(_NEGATION.search(before))


def policy_violation(text: str) -> str | None:
    """The reason key when the text asks for something AATM will not plan (no tasks are created), else None."""
    text = text or ""
    for pattern, reason in _POLICY_RULES:
        for match in pattern.finditer(text):
            if not _negated(text, match.start(), match.end()):
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


# E50: "test_reports_*.py" loses its asterisk when Markdown renders it, leaving "test_reports_.py".
_LOST_GLOB_RE = re.compile(r"(?<![\w*])([\w/.-]*[^\W_]_\.(?:py|tsx?|jsx?|md|ya?ml|json))(?![\w*])")


def plan_warnings(tasks: list[dict[str, Any]]) -> list[str]:
    """Warn when the plan is a chain, not parallel work, or names a file whose glob asterisk was lost (E50)."""
    count = len(tasks)
    warnings = [
        f"{n} of {count} tasks depend on task {dep} ({tasks[dep]['title']}). "
        "Check that each of them really needs that output; independent tasks should run in parallel."
        for dep, n in chained_dependencies(tasks)
    ]
    for index, task in enumerate(tasks):
        for name in sorted(set(_LOST_GLOB_RE.findall(f"{task.get('title', '')} {task.get('description', '')}"))):
            warnings.append(f"task {index} ({task['title']}) names {name}, which looks like a pattern whose * was lost; "
                            "name the exact file.")
    return warnings


# A frontend and a backend task of one plan must name the same contract file (E29). Sides are read from the
# title first, then the description; a task that matches neither (or both) is left out of the check.
_FRONTEND_RE = re.compile(
    r"\b(frontend|front-end|ui|react|component|page|screen|css|dashboard|form|button)\b|واجهة|الواجهة", re.IGNORECASE
)
_BACKEND_RE = re.compile(
    r"\b(backend|back-end|api|endpoints?|server|fastapi|database|migrations?|routes?|schema|service)\b|خلفية|الخلفية|خادم",
    re.IGNORECASE,
)
_CONTRACT_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:md|ya?ml|json))(?![\w/-])", re.IGNORECASE)
NO_SHARED_CONTRACT_REASON = (
    "no shared contract: the frontend and backend tasks do not both name one contract file (for example "
    "docs/API.md) that a single task creates and owns. Name the same file in both task descriptions, or merge the two tasks."
)


def _integration_side(task: dict[str, Any]) -> str | None:
    for text in (str(task.get("title") or ""), str(task.get("description") or "")):
        front = bool(_FRONTEND_RE.search(text))
        back = bool(_BACKEND_RE.search(text))
        if front != back:
            return "frontend" if front else "backend"
    return None


def _named_contract_files(task: dict[str, Any]) -> set[str]:
    text = str(task.get("description") or "")
    return {match.group(1).lower().lstrip("./") for match in _CONTRACT_PATH_RE.finditer(text)}


def missing_shared_contract(tasks: list[dict[str, Any]]) -> bool:
    """True when the plan has a frontend and a backend task but no contract file that both descriptions name."""
    frontends = [t for t in tasks if _integration_side(t) == "frontend"]
    backends = [t for t in tasks if _integration_side(t) == "backend"]
    if not frontends or not backends:
        return False
    return not any(_named_contract_files(f) & _named_contract_files(b) for f in frontends for b in backends)


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


def _review_pr_mismatch(job: ManagerJob, verdict: dict[str, Any]) -> str | None:
    """E48: a verdict that names another pull request is never posted (the reply belongs to an older review)."""
    expected = (job.context or {}).get("pr_number")
    stated = verdict.get("pr_number")
    if not expected or not stated or int(stated) == int(expected):
        return None
    return f"the review reply names PR #{stated}, but this review is for PR #{expected}; nothing was posted"


def _json_retry_prompt(kind: str) -> str:
    """The second chance for a reply without JSON: the retry carries an empty JSON template to fill (E33)."""
    template = ('{"pr_number": 0, "verdict": "", "summary_ar": "", "comments": []}' if kind == "review"
                else '{"summary_ar": "", "tasks": []}')
    return (MANAGER_JSON_RETRY + "\n\nFill in this exact template (replace the empty values) and reply with only "
            "the JSON block:\n```json\n" + template + "\n```")


_VERDICT_FIELD_RE = re.compile(
    r"""["']?verdict["']?\s*[:=]\s*["']?(approve|approved|request_changes|request changes|changes_requested)["']?""",
    re.IGNORECASE,
)
_SUMMARY_FIELD_RE = re.compile(r"""["']?summary_ar["']?\s*[:=]\s*["'](.+?)["']\s*$""", re.MULTILINE)
_REQUEST_MARKERS = (
    "request_changes", "request changes", "changes_requested", "changes requested", "disapprove",
    "cannot approve", "can't approve", "not approved", "do not approve",
    "تعديلات مطلوبة", "أطلب تعديلات", "اطلب تعديلات", "أرجو تعديل", "يحتاج تعديل", "لا أوافق", "غير موافق",
    "مرفوض", "أرفض", "لا أنصح بالدمج",
)
_APPROVE_MARKERS = ("approve", "approved", "approval", "lgtm", "موافق", "أوافق", "معتمد", "اعتماد", "أنصح بالدمج")


def review_verdict_from_text(text: str) -> dict[str, Any] | None:
    """E33: the manager answered a review in prose. Extract its verdict and reasons from the text.

    Called only after ``extract_manager_json`` found nothing (and after the JSON-template retry). This never
    invents a verdict: when the text names neither approval nor requested changes, it returns ``None`` and
    the job still goes to a human.
    """
    body = (text or "").strip()
    if not body:
        return None
    lowered = body.lower()
    verdict: str | None = None
    match = _VERDICT_FIELD_RE.search(body)
    if match:
        verdict = "approve" if match.group(1).lower().startswith("approve") else "request_changes"
    elif any(marker in lowered for marker in _REQUEST_MARKERS):
        verdict = "request_changes"
    elif any(marker in lowered for marker in _APPROVE_MARKERS):
        verdict = "approve"
    if verdict is None:
        return None
    summary_match = _SUMMARY_FIELD_RE.search(body)
    summary = summary_match.group(1).strip() if summary_match else body
    comments: list[str] = []
    if verdict == "request_changes":
        for raw in body.splitlines():
            line = raw.strip().lstrip("*-•").strip()
            if line and not line.startswith(("```", "{", "}")) and (len(line) > 20 or line[:1].isdigit()):
                comments.append(line[:2_000])
        comments = comments[:30] or [body[:2_000]]
    return {"verdict": verdict, "summary_ar": summary[:5_000], "comments": comments}


def _combine_review_verdicts(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """One verdict from the manager's verdicts on the parts of a large review (no verdict is invented)."""
    comments: list[str] = []
    summaries: list[str] = []
    for verdict in verdicts:
        comments += list(verdict.get("comments") or [])
        summary = str(verdict.get("summary_ar") or "").strip()
        if summary and summary not in summaries:
            summaries.append(summary)
    return {
        "verdict": "request_changes" if any(v.get("verdict") == "request_changes" for v in verdicts) else "approve",
        "summary_ar": "\n".join(summaries)[:5_000],
        "comments": comments[:30],
    }


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
    # Plan-shape reasons are not size rules: auto-approve does not hide them from the owner.
    for dep, n in chained_dependencies(tasks):
        reasons.append(
            f"Dependency chain: {n} of {len(tasks)} tasks depend on task {dep} ({tasks[dep]['title']}). "
            "Most tasks must run in parallel; the owner must confirm that each one really needs that output."
        )
    if missing_shared_contract(tasks):
        reasons.append(NO_SHARED_CONTRACT_REASON)
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
        # E37: how long a busy manager window may keep a job waiting, and the pause between real attempts.
        self.busy_wait_seconds = _env_seconds("AATM_MANAGER_BUSY_WAIT_SECONDS", BUSY_WAIT_TIMEOUT_SECONDS)
        self.busy_poll_seconds = BUSY_POLL_SECONDS
        self.retry_backoff_seconds = _env_seconds("AATM_MANAGER_RETRY_BACKOFF_SECONDS", REAL_RETRY_BACKOFF_SECONDS)
        self._backoff_until: dict[str, float] = {}
        self._review_prompt_parts: dict[str, list[str]] = {}
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
        # E37: back off between real attempts only. A busy window never reaches this counter.
        delay = self._backoff_until.pop(job.id, 0.0) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
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
                if parsed is None and await self._answer_empty_tasks(db, job, reply):
                    return {"job_id": job.id, "status": job.status, "empty_tasks": True}
                if parsed is None:
                    job.json_retries += 1
                    await db.commit()
                    # E33: the retry carries an empty JSON template, so the manager sees the exact shape.
                    reply = await self._exchange(db, session, job, _json_retry_prompt(job.kind))
                    parsed, error = await self._parse(db, job, reply)
                    if parsed is None and await self._answer_empty_tasks(db, job, reply):
                        return {"job_id": job.id, "status": job.status, "empty_tasks": True}
                    if parsed is None and job.kind == "review":
                        # E33: still no JSON: read the verdict and the reasons from the manager's prose.
                        prose = review_verdict_from_text(reply)
                        if prose is not None:
                            parsed, error = validate_manager_review(prose)
                if parsed is None:
                    await self._needs_human(db, job, f"invalid JSON after one retry: {error}", raw=reply)
                    return {"job_id": job.id, "status": job.status, "error": error}
                if job.kind == "review" and (mismatch := _review_pr_mismatch(job, parsed)):
                    await self._needs_human(db, job, mismatch, raw=reply)
                    return {"job_id": job.id, "status": job.status, "error": "wrong_pull_request"}
                if job.kind == "review":
                    # E23: a large review is split into parts; combine the manager's own verdicts.
                    parts = self._review_prompt_parts.get(job.id) or []
                    if len(parts) > 1:
                        combined = await self._review_remaining_parts(db, session, job, parsed, parts[1:])
                        if combined is None:
                            return {"job_id": job.id, "status": job.status}
                        parsed = combined
                await self._apply(db, job, parsed, raw=reply)
            else:
                await self._finish(db, job, result={"chars": len(reply)}, raw=reply)
                await self._reply(db, job, reply or "(لم يرسل المدير أي رد)")
            return {"job_id": job.id, "status": job.status}
        except ManagerArenaError as exc:
            if exc.code in {"stalled", "needs_human"}:
                await self._needs_human(db, job, exc.message, raw=None)
            elif exc.code == "busy_timeout":
                await self._requeue_waiting_for_manager(db, job)
            else:
                await self._connection_error(db, job, exc.code, exc.message)
            return {"job_id": job.id, "status": job.status, "error": exc.code}
        except ArenaAgentError as exc:
            if exc.code == "busy":
                await self._requeue_waiting_for_manager(db, job)
            elif exc.code == "needs_human":
                await self._needs_human(db, job, f"Arena page needs a human: {exc.message}", raw=None)
            else:
                await self._connection_error(db, job, exc.code, f"{exc.code}: {exc.message}")
            return {"job_id": job.id, "status": job.status, "error": exc.code}
        finally:
            await self._set_manager_status(db, "idle")
            await db.commit()

    async def _connection_error(self, db: AsyncSession, job: ManagerJob, code: str, error: str) -> None:
        """The job waits for the window. E52: when the same error repeats, the owner sees the cause and is alerted once."""
        self._backoff_until[job.id] = time.monotonic() + self._backoff_seconds(job)
        context = dict(job.context or {})
        same = int(context.get("same_error_count", 0)) + 1 if context.get("last_error_code") == code else 1
        job.context = {**context, "last_error_code": code, "same_error_count": same}
        job.status = "waiting_connection"
        job.error = error[:500]
        text = NOT_CONNECTED_AR
        if code == "page_hidden":
            # E51: the window is minimised or hidden and draws no composer. Only the owner can show it; say so at once.
            text = PAGE_HIDDEN_AR
            if same == 1:
                db.add(Notification(category="arena", severity="critical", title="نافذة المدير مخفية",
                                    message=PAGE_HIDDEN_AR, link="/browsers"))
        elif same >= REPEATED_ERROR_ALERT:
            text = f"{NOT_CONNECTED_AR}\n\nالخطأ نفسه تكرر {same} مرات، والسبب: {error[:300]}"
            if same == REPEATED_ERROR_ALERT:
                db.add(Notification(category="arena", severity="critical", title="المدير يفشل بالخطأ نفسه مرارًا",
                                    message=f"{job.kind}: {error[:300]}", link="/browsers"))
        await db.commit()
        await self._reply(db, job, text, waiting=True, readiness=code, repeated_error=same)

    def _backoff_seconds(self, job: ManagerJob) -> float:
        """Pause before the next real attempt (E37); busy waits are free and never reach this."""
        return min(60.0, self.retry_backoff_seconds * max(1, job.attempts))

    async def _requeue_waiting_for_manager(self, db: AsyncSession, job: ManagerJob) -> None:
        """E37: the window is still busy. Put the job back in the queue without spending an attempt."""
        job.attempts = max(0, job.attempts - 1)  # a busy window is not an attempt
        job.status = "queued"
        self._backoff_until[job.id] = time.monotonic() + self.busy_poll_seconds  # no hot loop on a long busy wait
        await db.commit()
        await self._show_waiting_for_manager(db, job)

    async def _review_remaining_parts(self, db: AsyncSession, session: ArenaSession, job: ManagerJob,
                                      first: dict[str, Any], prompts: list[str]) -> dict[str, Any] | None:
        """E23: review the other parts of a large diff and combine the manager's verdicts on all of them."""
        verdicts = [first]
        for offset, prompt in enumerate(prompts, start=2):
            reply = await self._exchange(db, session, job, prompt, kind="review")
            parsed, error = validate_manager_review(extract_manager_json(reply))
            if parsed is None:
                prose = review_verdict_from_text(reply)
                if prose is not None:
                    parsed, error = validate_manager_review(prose)
            if parsed is None:
                await self._needs_human(db, job, f"review part {offset} had no verdict: {error}", raw=reply)
                return None
            if mismatch := _review_pr_mismatch(job, parsed):
                await self._needs_human(db, job, f"review part {offset}: {mismatch}", raw=reply)
                return None
            verdicts.append(parsed)
        return _combine_review_verdicts(verdicts)

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
        try:
            reply = await self._exchange(db, session, job, context, kind="context")
        except ManagerArenaError:
            # The context never reached the page (for example a busy timeout): do not keep a session that
            # believes it has a context it never saw; the next attempt starts a fresh one.
            await close_session(db, session, phase="closed", reason="context was not sent")
            raise
        append_event(session, "context_sent", f"{len(context)} chars", reply_chars=len(reply), team=team_ids)
        await db.commit()
        return session

    async def _show_waiting_for_manager(self, db: AsyncSession, job: ManagerJob) -> None:
        """E37: replace the failure look with «the review is waiting for the manager»."""
        text = _waiting_for_manager_text(job.kind)
        first_time = (job.error or "") != text
        job.error = text
        await db.commit()
        if first_time and job.kind == "review":  # a review has no chat reply: tell the owner explicitly
            db.add(Notification(category="manager", severity="info", title="المراجعة تنتظر المدير",
                                message=text, link="/approvals"))
            await db.commit()
        await self._reply(db, job, text, pending=True, waiting_for_manager=True)
        await event_bus.emit("manager.activity", {**(_job_dict(job) or {}), "waiting_for_manager": True})

    async def _submit_when_idle(self, db: AsyncSession, session: ArenaSession, job: ManagerJob, text: str,
                                first_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """E37: wait for a busy manager window to become idle, then submit once.

        A busy window is transient: the job waits here (up to ``busy_wait_seconds``) and no attempt is
        spent. A submit that still answers ``busy`` is retried inside the same wait. Returns the page state
        right before the successful submit.
        """
        from app.providers.adapters.arena_agent import ArenaAgentError

        deadline = time.monotonic() + self.busy_wait_seconds
        waiting = False
        while True:
            state = first_state if first_state is not None else await self.adapter.state(session.profile_id)
            first_state = None  # only the first attempt uses the state the caller already read
            current = str(state.get("agent_state") or ("offline" if not state.get("connected") else "")) or "offline"
            if state.get("status") == "needs_human" or current == "needs_human":
                raise ManagerArenaError(
                    "needs_human", f"Arena page needs a human: {state.get('needs_human_reason') or ''}".strip())
            if current != "generating":  # only a generating page refuses a submit with "busy"
                try:
                    await self.adapter.submit(session.profile_id, text, source="manager")
                    return state
                except ArenaAgentError as exc:
                    if exc.code != "busy":
                        raise
            if not waiting:
                waiting = True
                await self._show_waiting_for_manager(db, job)
            if time.monotonic() >= deadline:
                raise ManagerArenaError("busy_timeout", "the manager's Arena window is still busy")
            await asyncio.sleep(self.busy_poll_seconds)

    async def _exchange(self, db: AsyncSession, session: ArenaSession, job: ManagerJob, text: str, *, kind: str | None = None,
                        resume: bool = False) -> str:
        """Submit one prompt (shared submit path), follow the page until it is quiet, read the reply."""
        from app.providers.adapters.arena_agent import TOOL_LIMIT_CONTINUE, ArenaAgentError, keep_working_allowed

        before: dict[str, Any] = {}
        before_since = ""
        before_state = ""
        if resume:  # after a restart the prompt may already be on the page: then only the reply is awaited
            before = await self.adapter.state(session.profile_id)
            before_since = str(before.get("agent_state_since") or "")
            before_state = str(before.get("agent_state") or "")
        expects_json = kind != "context" and job.kind in {"plan", "review"}
        pressed_on: str | None = None
        if not resume:
            pre_state = await self.adapter.state(session.profile_id)  # one read, as on main, before the mark
            mark = (await self.adapter.transcript_mark(session.profile_id)).get("mark")
            append_event(session, "submit_started", mark, job_id=job.id, exchange=kind or job.kind)
            await db.commit()
            try:
                before = await self._submit_when_idle(db, session, job, text, pre_state)
            except ArenaAgentError as exc:
                append_event(session, "submit_failed", f"{exc.code}: {exc.message}", job_id=job.id)
                await db.commit()
                raise
            before_since = str(before.get("agent_state_since") or "")
            before_state = str(before.get("agent_state") or "")
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
            prompts = await self._review_prompts(db, job, project, team)
            return prompts[0] if prompts else None
        return None

    async def _review_prompts(self, db: AsyncSession, job: ManagerJob, project: Project | None,
                              team: list[dict[str, Any]]) -> list[str]:
        """E23: the review prompt as one or more parts; each part stays under the strict size cap."""
        from app.orchestration.arena_runner import arena_runner

        review = await db.get(Task, job.review_task_id) if job.review_task_id else None
        parent = await db.get(Task, review.parent_task_id) if review and review.parent_task_id else None
        if review is None or parent is None:
            return []
        material = await arena_runner.review_material(db, parent)
        session = await session_for_task(db, parent.id)
        changes = await arena_runner._compare(project, session) if session is not None else {"ok": False}
        files = list(changes.get("files", [])) if changes.get("ok") else []
        diff = "\n".join(f"--- {f['filename']}\n{f.get('patch', '')}" for f in files)
        parts = max(1, -(-len(files) // REVIEW_FILES_PER_PART)) if files else 1
        prompts = [
            build_manager_review_prompt(
                parent, acceptance=task_acceptance_criteria(parent, project),
                report=(session.report if session else "") or "", diff=diff, pr_url=parent.pr_url,
                round_number=parent.review_round, project=project, team=team,
                discrepancies=(material or {}).get("mechanical_discrepancies"),
                files=files, part=index, parts=parts, max_chars=REVIEW_PROMPT_MAX_CHARS,
            )
            for index in range(parts)
        ]
        self._review_prompt_parts[job.id] = prompts
        job.context = {**(job.context or {}), "parent_task_id": parent.id, "pr_url": parent.pr_url,
                       "pr_number": parent.pr_number,
                       "changed_files": [f["filename"] for f in files][:100], "diff_chars": len(diff),
                       "review_parts": len(prompts), "prompt_chars": sum(len(p) for p in prompts)}
        return prompts

    async def _answer_empty_tasks(self, db: AsyncSession, job: ManagerJob, reply: str) -> bool:
        """E26: ``tasks: []`` is the manager's own answer (a refusal or a clarifying question), not broken JSON.

        Show ``summary_ar`` to the owner exactly as written, create nothing and finish the job. A human is
        asked only when the reply has neither tasks nor a summary.
        """
        if job.kind != "plan":
            return False
        data = extract_manager_json(reply)
        if not isinstance(data, dict) or data.get("tasks") != []:
            return False
        summary = data.get("summary_ar")
        if not isinstance(summary, str) or not summary.strip():
            return False
        ambiguous = data.get("ambiguous") is True
        await self._finish(db, job, result={"empty_tasks": True, "clarification": ambiguous}, raw=reply)
        await self._reply(db, job, summary.strip(), clarification=ambiguous, empty_tasks=True)
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
        self._backoff_until.pop(job.id, None)
        self._review_prompt_parts.pop(job.id, None)
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
            # E45: the plan waits only while an owner approval for this request is still open.
            gated, rejected = await self._gate_state(db, job, gated)
            if rejected:
                await self._finish(db, job, status="failed", error="owner rejected the high-risk approval", raw=raw)
                await self._reply(db, job, "رفض المالك الموافقة على الطلب عالي الخطورة، فلم تُنشأ أي مهام.", failed=True)
                return
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

    @staticmethod
    async def _gate_approvals(db: AsyncSession, chat_message_id: str | None) -> list[ApprovalRequest]:
        """The owner approvals the high-risk gate opened for one Manager Chat message."""
        if not chat_message_id:
            return []
        rows = (await db.execute(select(ApprovalRequest).where(ApprovalRequest.requested_by == MANAGER_AGENT_ID))).scalars()
        return [a for a in rows if (a.payload or {}).get("chat_message_id") == chat_message_id]

    async def _gate_state(self, db: AsyncSession, job: ManagerJob, gated: list[str]) -> tuple[list[str], bool]:
        """(categories still waiting for the owner, whether the owner rejected one). Unlinked categories keep waiting."""
        approvals = await self._gate_approvals(db, job.chat_message_id)
        if any(a.status == "rejected" for a in approvals):
            return gated, True
        approved = {a.action_type for a in approvals if a.status == "approved"}
        return [c for c in gated if c not in approved], False

    async def release_deferred_plan(self, db: AsyncSession, approval: ApprovalRequest) -> dict[str, Any] | None:
        """E45: when the owner decides a gate approval, a plan that was waiting for it goes on (or is cancelled)."""
        chat_message_id = (approval.payload or {}).get("chat_message_id")
        if approval.requested_by != MANAGER_AGENT_ID or not chat_message_id:
            return None
        job = (await db.execute(
            select(ManagerJob).where(ManagerJob.chat_message_id == chat_message_id, ManagerJob.kind == "plan")
            .order_by(ManagerJob.created_at.desc()))).scalars().first()
        result = (job.result or {}) if job is not None else {}
        if job is None or job.status != "done" or not result.get("deferred") or not isinstance(result.get("plan"), dict):
            return None  # no plan yet: _apply_plan checks the approvals when the manager's reply arrives
        waiting, rejected = await self._gate_state(db, job, list(result["deferred"]))
        if rejected:
            job.result = {k: v for k, v in result.items() if k != "deferred"}
            await self._finish(db, job, status="failed", error="owner rejected the high-risk approval")
            await self._reply(db, job, "رفض المالك الموافقة على الطلب عالي الخطورة، فلم تُنشأ أي مهام.", failed=True)
            return {"job_id": job.id, "released": False, "rejected": True}
        if waiting:
            return {"job_id": job.id, "released": False, "waiting": waiting}
        plan = result["plan"]
        job.result = {**{k: v for k, v in result.items() if k != "deferred"}, "released_by_approval": approval.id}
        job.context = {**(job.context or {}), "gated_categories": []}
        await self._apply_plan(db, job, plan, raw=job.raw_reply or "")
        return {"job_id": job.id, "released": True, "status": job.status}

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
