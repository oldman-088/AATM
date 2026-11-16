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
    acceptance = list(config.get("acceptance_criteria") or []) if isinstance(config.get("acceptance_criteria"), list) else []
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
