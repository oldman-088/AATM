"""Prompts the manager sends to an Arena Agent session.

Arena Agent works directly on GitHub (its own ``arena/<id>`` branch, commits and pull request), so
every prompt is self-contained: repository, base branch, the task, expected implementation,
acceptance criteria, the project's architecture and conventions, testing and security rules, and
the fixed GitHub workflow block whose ``[AATM-<task_id>]`` title prefix lets AATM find the PR
through the GitHub API.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.security import sanitize_untrusted_content
from app.models import Agent, Project, Task

REPORT_HEADING = "## AATM REPORT"
REPORT_SECTIONS = (
    "Implementation summary",
    "Modified files",
    "Completed requirements",
    "Tests performed",
    "Test results",
    "Known limitations",
    "Outstanding work",
)
_DOC_CHARS = 2_500


GITHUB_BLOCKED_MARKER = "AATM_BLOCKED: GITHUB_ACCESS"
_GITHUB_BLOCKED_LINE = re.compile(rf"^[\s>*`_-]*{re.escape(GITHUB_BLOCKED_MARKER)}\b", re.MULTILINE)


def reports_github_blocked(reply: str) -> bool:
    """True when a line of the agent's reply starts with the marker (the prompt only quotes it mid-sentence)."""
    return bool(_GITHUB_BLOCKED_LINE.search(reply or ""))


def pr_title_prefix(task_id: str) -> str:
    return f"[AATM-{task_id}]"


def github_workflow_block(task_id: str, base_branch: str) -> str:
    return "\n".join([
        "Work on ONE branch.",
        "",
        "Create ONE pull request.",
        "",
        "Push changes after every completed implementation item.",
        "",
        "Do NOT merge the pull request yourself.",
        "",
        f"Put {pr_title_prefix(task_id)} at the beginning of the pull request title.",
        "",
        f"The pull request targets the base branch `{base_branch}`. The project owner reviews and merges it.",
        "",
        "Before you change anything, check that you can read this repository and push to it. If you cannot, stop: "
        f"do not work around it and do not ask for credentials. Reply with one line that begins with {GITHUB_BLOCKED_MARKER} "
        "and then say what is missing.",
    ])


def _read_doc(repo_path: str | None, *names: str) -> str:
    if not repo_path:
        return ""
    root = Path(repo_path)
    for name in names:
        path = root / name
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="ignore")[:_DOC_CHARS].strip()
        except OSError:
            continue
    return ""


def repo_slug(project: Project | None) -> str | None:
    url = (project.repo_url if project else None) or ""
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url.strip())
    return match.group(1) if match else None


def _bullets(items: list[str], empty: str) -> str:
    items = [i for i in (str(x).strip() for x in items or []) if i]
    return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"


def build_task_prompt(task: Task, project: Project | None, agent: Agent | None = None) -> str:
    """The complete, self-contained Arena Agent prompt for one task."""
    base = (project.default_branch if project else None) or "main"
    slug = repo_slug(project) or (project.repo_url if project and project.repo_url else "the project repository")
    config: dict[str, Any] = (project.config or {}) if project and isinstance(project.config, dict) else {}
    architecture = str(config.get("architecture") or "").strip() or _read_doc(
        project.repo_path if project else None, "docs/ARCHITECTURE.md", "ARCHITECTURE.md", "README.md")
    conventions = str(config.get("conventions") or "").strip() or _read_doc(
        project.repo_path if project else None, "AGENTS.md", "CONTRIBUTING.md", "CLAUDE.md")
    test_command = str(config.get("test_command") or "").strip()
    deliverables = list(task.expected_deliverables or [])
    # R7: criteria the manager wrote for this task in its plan come first, then the project's own.
    acceptance = [str(c) for cp in (task.checkpoints or []) if isinstance(cp, dict) and isinstance(cp.get("acceptance_criteria"), list)
                  for c in cp["acceptance_criteria"] if str(c).strip()]
    acceptance += list(config.get("acceptance_criteria") or []) if isinstance(config.get("acceptance_criteria"), list) else []
    acceptance += [
        "Every expected deliverable exists and works as described.",
        "Existing tests still pass and new behaviour has meaningful automated tests.",
        f"The pull request title starts with {pr_title_prefix(task.id)} and targets `{base}`.",
        "The final report below matches the actual diff exactly.",
    ]
    description = sanitize_untrusted_content(task.description or "", source=f"task:{task.id}")["quarantined_text"]
    sections = [
        f"# AATM task {pr_title_prefix(task.id)}: {task.title}",
        "",
        f"Repository: {slug}",
        f"Base branch: {base}",
        f"Task ID: {task.id}",
        f"Priority: {task.priority}" + (f" · Employee: {agent.name}" if agent else ""),
        "",
        "## Task description",
        description or task.title,
        "",
        "## Expected implementation",
        _bullets(deliverables, "Implement the task described above in the existing codebase."),
        "",
        "## Acceptance criteria",
        _bullets(acceptance, ""),
        "",
        "## Relevant project architecture",
        architecture or f"Tech stack: {', '.join(project.tech_stack) if project and project.tech_stack else 'see the repository'}.",
        "",
        "## Existing coding conventions",
        conventions or "Follow the style, structure and naming already used in the repository.",
        "",
        "## Testing requirements",
        "- Add or update automated tests for every change.",
        f"- Run the test suite{f' (`{test_command}`)' if test_command else ''} and the linters before you finish.",
        "- Report the exact commands you ran and their real results.",
        "",
        "## Security requirements",
        "- Never commit secrets, API keys, tokens, passwords, cookies, .env files or local databases.",
        "- Validate untrusted input; do not weaken authentication, authorization or existing security checks.",
        "- Do not add CAPTCHA bypass, anti-bot evasion or unauthorized scraping.",
        "",
        "## Rules",
        "- Do NOT implement fake, placeholder or non-functional features, and do not claim work you did not do.",
        "- Preserve all existing functionality; do not delete or rewrite working code without a reason stated in the report.",
        "- Read the existing code before you change it.",
        "",
        "## GitHub workflow requirements (mandatory)",
        github_workflow_block(task.id, base),
        "",
        "## Final report",
        report_instructions(),
    ]
    return "\n".join(sections).strip() + "\n"


def report_instructions() -> str:
    return "\n".join([
        f"When you are completely finished, reply with a report that starts with the heading `{REPORT_HEADING}` and",
        "contains these sections:",
        *[f"- {name}" for name in REPORT_SECTIONS],
        "Include the pull request URL. List only files you actually changed and tests you actually ran.",
    ])


def build_report_request(task: Task) -> str:
    return "\n".join([
        f"Please send the final report for {pr_title_prefix(task.id)} now.",
        "",
        report_instructions(),
    ])


def build_correction_prompt(task: Task, *, review_comments: str, discrepancies: list[str], round_number: int, pr_url: str | None) -> str:
    """Follow-up message for the same Arena session after the reviewer compared report and diff."""
    safe_comments = sanitize_untrusted_content(review_comments or "", source=f"review:{task.id}")["quarantined_text"]
    return "\n".join([
        f"# Corrections for {pr_title_prefix(task.id)} (review round {round_number})",
        "",
        f"An AATM reviewer compared your report with the actual GitHub diff{f' of {pr_url}' if pr_url else ''}.",
        "Apply the corrections on the SAME branch and the SAME pull request. Do not open a new pull request and do not merge.",
        "",
        "## Discrepancies found between your report and the code",
        _bullets(discrepancies, "No mechanical discrepancies; see the reviewer's comments."),
        "",
        "## Reviewer comments",
        safe_comments or "(none)",
        "",
        "Push the fixes, then send an updated report.",
        "",
        report_instructions(),
    ])


def report_is_complete(text: str) -> bool:
    lowered = (text or "").lower()
    if REPORT_HEADING.lower() not in lowered:
        return False
    return sum(1 for name in REPORT_SECTIONS if name.lower() in lowered) >= 4


_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w/-])")


def report_section(text: str, name: str) -> str:
    match = re.search(rf"(?ims)^\W*{re.escape(name)}\W*$\n?(.*?)(?=^\W*(?:{'|'.join(map(re.escape, REPORT_SECTIONS))})\W*$|\Z)", text or "")
    return match.group(1).strip() if match else ""


def compare_report_to_changes(report: str, changed_files: list[str], *, commits: int | None = None) -> list[str]:
    """Mechanical checks of the agent's report against the real diff (the reviewer judges the rest)."""
    issues: list[str] = []
    changed = {f.strip() for f in changed_files if f and f.strip()}
    if not report:
        return ["The agent did not provide a final report."]
    if not report_is_complete(report):
        missing = [n for n in REPORT_SECTIONS if n.lower() not in report.lower()]
        issues.append("The report is missing sections: " + ", ".join(missing) + ".")
    listed = report_section(report, "Modified files") or report
    claimed = {m.group(1) for m in _PATH_RE.finditer(listed) if "/" in m.group(1) or "." in m.group(1)}
    claimed = {c for c in claimed if not c.startswith(("http", "www.")) and not re.fullmatch(r"\d+(\.\d+)+", c)}
    if changed:
        not_changed = sorted(c for c in claimed if c not in changed and not any(f.endswith("/" + c) or f == c for f in changed))
        if not_changed:
            issues.append("Files listed in the report but not changed in the pull request: " + ", ".join(not_changed[:20]) + ".")
        unreported = sorted(f for f in changed if f not in claimed and f.rsplit("/", 1)[-1] not in claimed)
        if unreported and report_section(report, "Modified files"):
            issues.append("Files changed in the pull request but missing from the report: " + ", ".join(unreported[:20]) + ".")
        tests_claimed = re.search(r"(?i)\b(\d+\s+passed|tests? (?:pass|passed)|all tests pass)", report_section(report, "Test results") or report)
        has_tests = any(re.search(r"(?i)(^|/)(tests?/|test_|.*_test\.|.*\.test\.|.*\.spec\.)", f) for f in changed)
        if tests_claimed and not has_tests:
            issues.append("The report claims passing tests, but the pull request changes no test files.")
    elif claimed:
        issues.append("The report lists modified files, but the pull request has no changes.")
    if commits == 0:
        issues.append("The pull request has no commits ahead of the base branch.")
    return issues


# =========================================================================== R7: manager prompts
# manager-0 runs in its own long-lived Arena Agent session. Every prompt starts with a
# ``[AATM-MANAGER:<KIND>]`` line, restates the manager's role, the team and the GitHub rules, and —
# for planning and review — the exact JSON block AATM parses. AATM never invents a plan or a verdict:
# an unparseable reply gets one retry, then the job is handed to a human.

MANAGER_JSON_RETRY = "Reply again with only the JSON block, no other text."
MAX_PLAN_TASKS = 10
MAX_REVIEW_DIFF_CHARS = 24_000
DIFF_TRUNCATED_MARKER = "[AATM: DIFF TRUNCATED — {shown} of {total} characters shown; read the full diff on GitHub: {url}]"

PLAN_FORMAT = (
    '{"summary_ar": "...", "tasks": [\n'
    '  {"title": "...", "assignee": "worker-1", "description": "...",\n'
    '   "acceptance_criteria": ["..."], "depends_on": []}\n'
    "]}"
)
REVIEW_FORMAT = '{"verdict": "approve" | "request_changes", "summary_ar": "...", "comments": ["..."]}'


def manager_marker(kind: str) -> str:
    return f"[AATM-MANAGER:{kind.upper()}]"


def _team_lines(team: list[dict[str, Any]]) -> str:
    if not team:
        return "- (no enabled workers)"
    return "\n".join(
        f"- {m['id']} — {m.get('name') or m['id']}: {m.get('specialty') or 'general'}"
        + (f" (skills: {', '.join(m['skills'][:8])})" if m.get("skills") else "")
        for m in team
    )


def manager_role_block(team: list[dict[str, Any]], project: Project | None) -> str:
    base = (project.default_branch if project else None) or "main"
    return "\n".join([
        "You are manager-0, the engineering manager of the AATM team. You plan, assign, review and report;",
        "you do not write the code yourself. Write every human-facing text in Arabic.",
        "",
        f"Project: {project.name if project else '(no project selected)'} · Repository: {repo_slug(project) or 'not set'} · Base branch: {base}",
        "",
        "## Team (assign only these ids)",
        _team_lines(team),
        "",
        "## GitHub rules",
        "- Each worker works in its own Arena Agent session, on ONE arena/* branch, and opens ONE pull request",
        "  whose title starts with [AATM-<task_id>].",
        "- Nobody merges by themselves: not the workers, not you. The project owner approves every merge in AATM.",
        "- Base every judgement on the real diff and the acceptance criteria, never on claims in a report.",
    ])


def build_manager_context_prompt(project: Project | None, team: list[dict[str, Any]], open_tasks: list[dict[str, Any]],
                                 open_prs: list[dict[str, Any]]) -> str:
    """First message of every new manager session: who you are and where the project stands."""
    tasks = [f"- {t['id']}: {t['title']} ({t['status']}, {t.get('assignee') or 'unassigned'})" for t in open_tasks[:30]]
    prs = [f"- PR #{p.get('number') or '?'} {p.get('title') or ''} ({p.get('status') or 'open'}) {p.get('url') or ''}".rstrip()
           for p in open_prs[:20]]
    return "\n".join([
        manager_marker("context"),
        manager_role_block(team, project),
        "",
        "## Open tasks",
        "\n".join(tasks) or "- none",
        "",
        "## Open pull requests",
        "\n".join(prs) or "- none",
        "",
        "This is context for the next messages. Reply with one short Arabic sentence confirming you are ready.",
    ])


def build_manager_chat_prompt(message: str, project: Project | None, team: list[dict[str, Any]]) -> str:
    safe = sanitize_untrusted_content(message or "", source="owner_chat")["quarantined_text"]
    return "\n".join([
        manager_marker("chat"),
        manager_role_block(team, project),
        "",
        "## Message from the project owner",
        safe,
        "",
        "Answer the owner directly, in Arabic. Do not claim that work was done unless it is in the project data.",
    ])


def build_manager_plan_prompt(goal: str, project: Project | None, team: list[dict[str, Any]],
                              open_tasks: list[dict[str, Any]]) -> str:
    safe = sanitize_untrusted_content(goal or "", source="owner_goal")["quarantined_text"]
    tasks = [f"- {t['title']} ({t['status']}, {t.get('assignee') or 'unassigned'})" for t in open_tasks[:30]]
    return "\n".join([
        manager_marker("plan"),
        manager_role_block(team, project),
        "",
        "## Goal from the project owner",
        safe,
        "",
        "## Tasks that already exist (do not duplicate them)",
        "\n".join(tasks) or "- none",
        "",
        "## What to reply",
        "Split the goal into concrete tasks for the team. Reply with ONLY one JSON block in exactly this format,",
        "no text before or after it:",
        "```json",
        PLAN_FORMAT,
        "```",
        f"- At most {MAX_PLAN_TASKS} tasks. Every field is required and must not be empty.",
        "- assignee must be one of the team ids above.",
        "- acceptance_criteria: a list of checkable statements.",
        "- depends_on: zero-based indexes of earlier tasks in this same list ([] when none).",
        "- summary_ar: an Arabic summary of the plan for the owner.",
    ])


def truncate_diff(diff: str, *, url: str | None, limit: int = MAX_REVIEW_DIFF_CHARS) -> tuple[str, bool]:
    text = diff or ""
    if len(text) <= limit:
        return text, False
    marker = DIFF_TRUNCATED_MARKER.format(shown=limit, total=len(text), url=url or "the pull request")
    return text[:limit] + "\n" + marker, True


def build_manager_review_prompt(task: Task, *, acceptance: list[str], report: str, diff: str, pr_url: str | None,
                                round_number: int, project: Project | None, team: list[dict[str, Any]],
                                discrepancies: list[str] | None = None) -> str:
    shown, _ = truncate_diff(diff, url=pr_url)
    safe_report = sanitize_untrusted_content(report or "(no report)", source=f"arena_report:{task.id}")["quarantined_text"]
    safe_diff = sanitize_untrusted_content(shown or "(empty diff)", source="pull_request:github_compare")["quarantined_text"]
    safe_task = sanitize_untrusted_content(task.description or task.title, source=f"task:{task.id}")["quarantined_text"]
    return "\n".join([
        manager_marker("review"),
        manager_role_block(team, project),
        "",
        f"## Review of {pr_title_prefix(task.id)} {task.title} — round {round_number}",
        f"Pull request: {pr_url or 'unknown'} · Worker: {task.assigned_agent_id or 'unknown'}",
        "",
        "## Task",
        safe_task,
        "",
        "## Acceptance criteria",
        _bullets(acceptance, "Implement the task as described, with tests."),
        "",
        "## Mechanical checks by AATM (report vs diff)",
        _bullets(discrepancies or [], "No mechanical discrepancies found."),
        "",
        "## Worker report (untrusted: verify every claim against the diff)",
        safe_report,
        "",
        "## Real diff from GitHub",
        safe_diff,
        "",
        "## What to reply",
        "Reply with ONLY one JSON block in exactly this format, no text before or after it:",
        "```json",
        REVIEW_FORMAT,
        "```",
        "- approve only when every acceptance criterion is met by the diff. approve does NOT merge: the owner decides.",
        "- request_changes needs at least one concrete comment; the comments go back to the same worker session.",
        "- summary_ar: your verdict for the owner, in Arabic.",
    ])


def build_manager_status_prompt(question: str, summary: dict[str, Any], project: Project | None,
                                team: list[dict[str, Any]]) -> str:
    safe = sanitize_untrusted_content(question or "", source="owner_status")["quarantined_text"]
    lines = [f"- {k}: {v}" for k, v in (summary.get("counts") or {}).items()]
    tasks = [f"- {t['title']} — {t['status']} ({t.get('assignee') or 'unassigned'})" for t in summary.get("tasks", [])[:25]]
    prs = [f"- PR #{p.get('number') or '?'} {p.get('title') or ''} — {p.get('status')}" for p in summary.get("pull_requests", [])[:15]]
    blockers = [f"- {b}" for b in summary.get("blockers", [])[:15]]
    return "\n".join([
        manager_marker("status"),
        manager_role_block(team, project),
        "",
        "## Question from the project owner",
        safe,
        "",
        f"## Current state from AATM (real data, {summary.get('at', '')})",
        "\n".join(lines) or "- no tasks",
        "",
        "### Tasks",
        "\n".join(tasks) or "- none",
        "",
        "### Pull requests",
        "\n".join(prs) or "- none",
        "",
        "### Blockers",
        "\n".join(blockers) or "- none",
        "",
        "Answer in Arabic, briefly, using only this data. If something is unknown, say so.",
    ])


_FENCED_JSON = re.compile(r"```(?:json|JSON)?[ \t]*\n(.*?)```", re.DOTALL)


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth, in_str, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def extract_manager_json(text: str) -> dict[str, Any] | None:
    """The first fenced ```json block, or else the first balanced ``{...}``, parsed as a JSON object."""
    import json

    candidates: list[str] = []
    fenced = _FENCED_JSON.search(text or "")
    if fenced:
        candidates.append(fenced.group(1).strip())
    balanced = _first_balanced_object(text or "")
    if balanced:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_manager_plan(data: dict[str, Any] | None, known_assignees: set[str]) -> tuple[dict[str, Any] | None, str]:
    """Known assignees only, 1..MAX_PLAN_TASKS tasks, no empty fields, depends_on = earlier indexes."""
    if not isinstance(data, dict):
        return None, "The reply contains no JSON object."
    if not _nonempty_str(data.get("summary_ar")):
        return None, "summary_ar is missing or empty."
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None, "tasks must be a non-empty list."
    if len(tasks) > MAX_PLAN_TASKS:
        return None, f"The plan has {len(tasks)} tasks; the maximum is {MAX_PLAN_TASKS}."
    clean: list[dict[str, Any]] = []
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            return None, f"Task {i} is not an object."
        for key in ("title", "assignee", "description"):
            if not _nonempty_str(task.get(key)):
                return None, f"Task {i}: {key} is missing or empty."
        if task["assignee"].strip() not in known_assignees:
            return None, f"Task {i}: unknown assignee '{task['assignee']}'."
        criteria = task.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria or not all(_nonempty_str(c) for c in criteria):
            return None, f"Task {i}: acceptance_criteria must be a non-empty list of non-empty strings."
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, int) and not isinstance(d, bool) for d in deps):
            return None, f"Task {i}: depends_on must be a list of task indexes."
        if any(d < 0 or d >= i for d in deps) or len(set(deps)) != len(deps):
            return None, f"Task {i}: depends_on may only reference earlier tasks in this plan, once each."
        clean.append({
            "title": task["title"].strip()[:240],
            "assignee": task["assignee"].strip(),
            "description": task["description"].strip()[:8_000],
            "acceptance_criteria": [c.strip()[:500] for c in criteria][:30],
            "depends_on": list(deps),
        })
    return {"summary_ar": data["summary_ar"].strip()[:5_000], "tasks": clean}, ""


def validate_manager_review(data: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(data, dict):
        return None, "The reply contains no JSON object."
    verdict = data.get("verdict")
    if verdict not in {"approve", "request_changes"}:
        return None, "verdict must be 'approve' or 'request_changes'."
    if not _nonempty_str(data.get("summary_ar")):
        return None, "summary_ar is missing or empty."
    comments = data.get("comments", [])
    if not isinstance(comments, list) or not all(_nonempty_str(c) for c in comments):
        return None, "comments must be a list of non-empty strings."
    if verdict == "request_changes" and not comments:
        return None, "request_changes needs at least one comment."
    return {"verdict": verdict, "summary_ar": data["summary_ar"].strip()[:5_000],
            "comments": [c.strip()[:2_000] for c in comments][:30]}, ""
