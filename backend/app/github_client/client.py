import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.core.config import get_settings

PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master", "production", "prod"})

DEFAULT_ALLOWED_REPOS: frozenset[str] = frozenset(
    {
        "oldman-088/aatm",
        "aatm/aatm-core",
        "example/project",
    }
)

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,199}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BRANCH_PREFIXES = (
    "feature/",
    "feat/",
    "fix/",
    "bugfix/",
    "hotfix/",
    "release/",
    "chore/",
    "docs/",
    "refactor/",
    "test/",
    "ci/",
    "arena/",
    "refs/",
)

_SECRET_SCRUB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}\b"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), "[REDACTED]"),
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*['\"]?(?:bearer|token)\s+)[^\s'\"]+"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.=]{12,}"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(gh_token|github_token|access_token|auth_token|api_key|client_secret|password|passwd|secret|token)"
            r"\s*([=:])\s*['\"]?([^\s'\";,]{6,})['\"]?"
        ),
        r"\1\2[REDACTED]",
    ),
]

_SENSITIVE_ENV_VARS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "ARENA_SESSION_TOKEN",
    "ARENA_BRIDGE_TOKEN",
)


def sanitize_cli_output(text: str | None) -> str:
    """Scrub credentials, tokens, and URL userinfo from CLI stdout/stderr or error messages."""
    if not text:
        return ""
    cleaned = str(text)
    for env_name in _SENSITIVE_ENV_VARS:
        val = os.environ.get(env_name)
        if val and len(val.strip()) >= 6:
            cleaned = cleaned.replace(val.strip(), "[REDACTED]")
    for pattern, replacement in _SECRET_SCRUB_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def normalize_branch_name(branch: str | None) -> str:
    """Strip whitespace and optional refs/heads/ prefix from a branch name."""
    raw = (branch or "").strip()
    if raw.startswith("refs/heads/"):
        raw = raw[len("refs/heads/") :]
    return raw.strip()


def is_protected_branch(branch: str | None, protected_branches: Iterable[str] | None = None) -> bool:
    """Return True if `branch` matches a protected branch name (main, master, production, prod)."""
    normalized = normalize_branch_name(branch).lower()
    if not normalized:
        return False
    protected = {b.strip().lower() for b in (protected_branches or PROTECTED_BRANCHES) if b and b.strip()}
    return normalized in protected


def validate_branch_name(branch: str | None) -> tuple[bool, str]:
    """Validate git ref naming rules and block option-injection or traversal patterns."""
    cleaned = normalize_branch_name(branch)
    if not cleaned:
        return False, "Branch name is required."
    if cleaned.startswith("-") or cleaned.startswith("/") or cleaned.endswith("/") or cleaned.endswith(".lock"):
        return False, f"Invalid branch name '{sanitize_cli_output(cleaned)}'."
    if ".." in cleaned or "@{" in cleaned or "//" in cleaned:
        return False, f"Invalid branch name '{sanitize_cli_output(cleaned)}'."
    if any(ch in cleaned for ch in ("~", "^", ":", "?", "*", "[", "\\")) or any(ch.isspace() for ch in cleaned):
        return False, f"Invalid branch name '{sanitize_cli_output(cleaned)}'."
    if not _BRANCH_NAME_RE.fullmatch(cleaned):
        return False, f"Invalid branch name '{sanitize_cli_output(cleaned)}'."
    return True, cleaned


def verify_approval(
    approval: Any | None,
    *,
    target_branch: str | None = None,
    repo: str | None = None,
) -> tuple[bool, str, str | None]:
    """Validate that an ApprovalRequest (model or dict) is present and approved."""
    protected = is_protected_branch(target_branch) if target_branch else False
    if approval is None:
        if protected:
            return (
                False,
                f"Target branch '{target_branch}' is protected. An approved ApprovalRequest is required.",
                None,
            )
        return False, "An approved ApprovalRequest is required for GitHub write operations.", None

    if isinstance(approval, dict):
        status = str(approval.get("status", "")).strip().lower()
        approval_id = approval.get("id") or approval.get("approval_id")
        payload = approval.get("payload") or {}
    else:
        status = str(getattr(approval, "status", "")).strip().lower()
        approval_id = getattr(approval, "id", None)
        payload = getattr(approval, "payload", None) or {}

    resolved_id = str(approval_id) if approval_id else None
    if status != "approved":
        if protected:
            return (
                False,
                f"Target branch '{target_branch}' is protected and ApprovalRequest status is '{status or 'unknown'}' (must be 'approved').",
                resolved_id,
            )
        return (
            False,
            f"ApprovalRequest status is '{status or 'unknown'}' (must be 'approved').",
            resolved_id,
        )

    if isinstance(payload, dict):
        if target_branch:
            approved_target = (
                payload.get("target_branch")
                or payload.get("target")
                or payload.get("base_branch")
                or payload.get("base")
            )
            if approved_target and normalize_branch_name(str(approved_target)).lower() != normalize_branch_name(target_branch).lower():
                return (
                    False,
                    f"ApprovalRequest target branch '{approved_target}' does not match '{target_branch}'.",
                    resolved_id,
                )
        if repo:
            approved_repo = payload.get("repo")
            if approved_repo and str(approved_repo).strip().lower() != repo.strip().lower():
                return (
                    False,
                    f"ApprovalRequest repository '{approved_repo}' does not match '{repo}'.",
                    resolved_id,
                )

    return True, "Approved", resolved_id


class GitHubClient:
    """Octokit-compatible GitHub REST & CLI integration respecting repository permissions and protected branches."""

    def __init__(self, allowed_repos: Iterable[str] | None = None) -> None:
        self._allowed_repos: set[str] | None = (
            {r.strip().lower() for r in allowed_repos if r and r.strip()}
            if allowed_repos is not None
            else None
        )

    @property
    def allowed_repos(self) -> set[str]:
        if self._allowed_repos is not None:
            return set(self._allowed_repos)
        env_repos = os.environ.get("AATM_ALLOWED_GITHUB_REPOS", "")
        if env_repos.strip():
            return {r.strip().lower() for r in env_repos.split(",") if r.strip()}
        return set(DEFAULT_ALLOWED_REPOS)

    @allowed_repos.setter
    def allowed_repos(self, repos: Iterable[str] | None) -> None:
        self._allowed_repos = (
            {r.strip().lower() for r in repos if r and r.strip()}
            if repos is not None
            else None
        )

    @staticmethod
    def sanitize_output(text: str | None) -> str:
        return sanitize_cli_output(text)

    def validate_repo(
        self,
        repo: str | None,
        allowed_repos: Iterable[str] | None = None,
    ) -> tuple[bool, str]:
        """Validate `owner/name` repository format and check against the repository allowlist."""
        cleaned = (repo or "").strip()
        if not cleaned:
            return False, "Repository slug in 'owner/name' format is required."
        if cleaned.startswith("-") or ".." in cleaned or not _REPO_SLUG_RE.fullmatch(cleaned):
            return False, f"Invalid repository slug '{sanitize_cli_output(cleaned)}'. Expected 'owner/name'."
        owner, name = cleaned.split("/", 1)
        if owner in {".", ".."} or name in {".", ".."}:
            return False, f"Invalid repository slug '{sanitize_cli_output(cleaned)}'."

        allowlist = (
            {r.strip().lower() for r in allowed_repos if r and r.strip()}
            if allowed_repos is not None
            else self.allowed_repos
        )
        if allowlist and cleaned.lower() not in allowlist:
            return False, f"Repository '{sanitize_cli_output(cleaned)}' is not in the allowed repository allowlist."
        return True, cleaned

    @staticmethod
    def _run_gh(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
        root = cwd or get_settings().REPO_ROOT
        try:
            proc = subprocess.run(["gh", *args], cwd=str(root), capture_output=True, text=True, timeout=15)
            raw_out = (proc.stdout or proc.stderr or "").strip()
            return proc.returncode, sanitize_cli_output(raw_out)
        except Exception as exc:
            return 1, sanitize_cli_output(str(exc))

    def get_auth_status(self) -> dict[str, Any]:
        rc, out = self._run_gh(["auth", "status"])
        return {"authenticated": rc == 0, "details": out}

    def list_repositories(self, limit: int = 10) -> dict[str, Any]:
        rc, out = self._run_gh(["repo", "list", "--limit", str(limit), "--json", "nameWithOwner,isPrivate,url,updatedAt"])
        if rc != 0:
            return {"available": False, "repositories": [], "error": out}
        try:
            return {"available": True, "repositories": json.loads(out)}
        except Exception:
            return {"available": False, "repositories": [], "error": out}

    def list_pull_requests(self, repo: str | None = None, limit: int = 10) -> dict[str, Any]:
        if repo is not None:
            ok_repo, repo_or_err = self.validate_repo(repo)
            if not ok_repo:
                return {"available": False, "pull_requests": [], "error": repo_or_err}
            repo = repo_or_err
        args = ["pr", "list", "--limit", str(limit), "--json", "number,title,state,headRefName,baseRefName,url"]
        if repo:
            args.extend(["--repo", repo])
        rc, out = self._run_gh(args)
        if rc != 0:
            return {"available": False, "pull_requests": [], "error": out}
        try:
            return {"available": True, "pull_requests": json.loads(out)}
        except Exception:
            return {"available": False, "pull_requests": [], "error": out}

    def list_issues(self, repo: str | None = None, limit: int = 10) -> dict[str, Any]:
        if repo is not None:
            ok_repo, repo_or_err = self.validate_repo(repo)
            if not ok_repo:
                return {"available": False, "issues": [], "error": repo_or_err}
            repo = repo_or_err
        args = ["issue", "list", "--limit", str(limit), "--json", "number,title,state,url,labels"]
        if repo:
            args.extend(["--repo", repo])
        rc, out = self._run_gh(args)
        if rc != 0:
            return {"available": False, "issues": [], "error": out}
        try:
            return {"available": True, "issues": json.loads(out)}
        except Exception:
            return {"available": False, "issues": [], "error": out}

    def create_branch(
        self,
        branch_name: str,
        base_branch: str = "main",
        repo: str = "oldman-088/AATM",
        approval: Any | None = None,
        *,
        source_sha: str | None = None,
        allowed_repos: Iterable[str] | None = None,
        protected_branches: Iterable[str] | None = None,
        require_approval: bool = False,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Create a GitHub branch via `gh api`, enforcing repo allowlist and protected-branch approval guardrails."""
        # Support positional (repo, branch_name[, base_branch]) when first arg is an allowed repo slug
        effective_allow = (
            {r.strip().lower() for r in allowed_repos if r and r.strip()}
            if allowed_repos is not None
            else self.allowed_repos
        )
        if (
            "/" in (branch_name or "")
            and not (branch_name or "").lower().startswith(_BRANCH_PREFIXES)
            and (branch_name or "").strip().lower() in effective_allow
            and repo == "oldman-088/AATM"
        ):
            repo, branch_name, base_branch = branch_name, base_branch, "main"

        ok_repo, repo_or_err = self.validate_repo(repo, allowed_repos=allowed_repos)
        if not ok_repo:
            return {
                "ok": False,
                "allowed": False,
                "repo": sanitize_cli_output(repo),
                "branch_name": sanitize_cli_output(branch_name),
                "error": repo_or_err,
                "reason": repo_or_err,
            }
        repo = repo_or_err

        ok_branch, clean_branch = validate_branch_name(branch_name)
        if not ok_branch:
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "branch_name": sanitize_cli_output(branch_name),
                "error": clean_branch,
                "reason": clean_branch,
            }

        ok_base, clean_base = validate_branch_name(base_branch)
        if not ok_base:
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "branch_name": clean_branch,
                "base_branch": sanitize_cli_output(base_branch),
                "error": clean_base,
                "reason": clean_base,
            }

        protected_target = is_protected_branch(clean_branch, protected_branches)
        approval_id: str | None = None
        if protected_target or require_approval or approval is not None:
            approved, reason, approval_id = verify_approval(
                approval,
                target_branch=clean_branch if protected_target else None,
                repo=repo,
            )
            if not approved:
                return {
                    "ok": False,
                    "allowed": False,
                    "repo": repo,
                    "branch_name": clean_branch,
                    "base_branch": clean_base,
                    "is_protected_target": protected_target,
                    "approval_id": approval_id,
                    "error": reason,
                    "reason": reason,
                }

        sha = (source_sha or "").strip()
        if sha:
            if not _SHA_RE.fullmatch(sha):
                msg = "Invalid source commit SHA."
                return {
                    "ok": False,
                    "allowed": False,
                    "repo": repo,
                    "branch_name": clean_branch,
                    "error": msg,
                    "reason": msg,
                }
        else:
            rc_ref, ref_out = self._run_gh(
                ["api", f"repos/{repo}/git/ref/heads/{clean_base}", "--jq", ".object.sha"],
                cwd=cwd,
            )
            if rc_ref != 0:
                return {
                    "ok": False,
                    "allowed": True,
                    "repo": repo,
                    "branch_name": clean_branch,
                    "base_branch": clean_base,
                    "is_protected_target": protected_target,
                    "approval_id": approval_id,
                    "error": ref_out,
                    "reason": ref_out,
                }
            sha = ref_out.strip()
            if sha.startswith("{"):
                try:
                    parsed_ref = json.loads(sha)
                    sha = str(
                        parsed_ref.get("object", {}).get("sha")
                        or parsed_ref.get("sha")
                        or "0000000000000000000000000000000000000000"
                    )
                except Exception:
                    sha = "0000000000000000000000000000000000000000"
            elif not _SHA_RE.fullmatch(sha):
                sha = "0000000000000000000000000000000000000000"

        rc, out = self._run_gh(
            [
                "api",
                f"repos/{repo}/git/refs",
                "-X",
                "POST",
                "-f",
                f"ref=refs/heads/{clean_branch}",
                "-f",
                f"sha={sha}",
            ],
            cwd=cwd,
        )
        if rc != 0:
            return {
                "ok": False,
                "allowed": True,
                "repo": repo,
                "branch_name": clean_branch,
                "base_branch": clean_base,
                "is_protected_target": protected_target,
                "approval_id": approval_id,
                "error": out,
                "reason": out,
            }

        return {
            "ok": True,
            "allowed": True,
            "repo": repo,
            "branch_name": clean_branch,
            "base_branch": clean_base,
            "ref": f"refs/heads/{clean_branch}",
            "sha": sha,
            "is_protected_target": protected_target,
            "approval_id": approval_id,
            "output": out,
        }

    def create_pull_request(
        self,
        repo: str,
        title: str,
        head: str,
        base: str = "main",
        body: str = "",
        *,
        draft: bool = False,
        approval: Any | None = None,
        allowed_repos: Iterable[str] | None = None,
        protected_branches: Iterable[str] | None = None,
        require_approval: bool = True,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Create a pull request via `gh pr create` with repo allowlist and protected-branch approval enforcement."""
        ok_repo, repo_or_err = self.validate_repo(repo, allowed_repos=allowed_repos)
        if not ok_repo:
            return {
                "ok": False,
                "allowed": False,
                "repo": sanitize_cli_output(repo),
                "head": sanitize_cli_output(head),
                "base": sanitize_cli_output(base),
                "error": repo_or_err,
                "reason": repo_or_err,
            }
        repo = repo_or_err

        clean_title = (title or "").strip()
        if not clean_title:
            msg = "Pull request title is required."
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "error": msg,
                "reason": msg,
            }

        ok_head, clean_head = validate_branch_name(head)
        if not ok_head:
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "head": sanitize_cli_output(head),
                "base": sanitize_cli_output(base),
                "error": clean_head,
                "reason": clean_head,
            }

        ok_base, clean_base = validate_branch_name(base)
        if not ok_base:
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "head": clean_head,
                "base": sanitize_cli_output(base),
                "error": clean_base,
                "reason": clean_base,
            }

        if clean_head.lower() == clean_base.lower():
            msg = "Head branch and base branch must differ."
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "head": clean_head,
                "base": clean_base,
                "error": msg,
                "reason": msg,
            }

        protected_target = is_protected_branch(clean_base, protected_branches)
        approval_id: str | None = None
        if protected_target or require_approval or approval is not None:
            approved, reason, approval_id = verify_approval(
                approval,
                target_branch=clean_base if protected_target else None,
                repo=repo,
            )
            if not approved:
                return {
                    "ok": False,
                    "allowed": False,
                    "repo": repo,
                    "head": clean_head,
                    "base": clean_base,
                    "is_protected_target": protected_target,
                    "approval_id": approval_id,
                    "error": reason,
                    "reason": reason,
                }

        args = [
            "pr",
            "create",
            "--repo",
            repo,
            "--title",
            clean_title,
            "--body",
            body or "",
            "--head",
            clean_head,
            "--base",
            clean_base,
        ]
        if draft:
            args.append("--draft")

        rc, out = self._run_gh(args, cwd=cwd)
        if rc != 0:
            return {
                "ok": False,
                "allowed": True,
                "repo": repo,
                "head": clean_head,
                "base": clean_base,
                "is_protected_target": protected_target,
                "approval_id": approval_id,
                "error": out,
                "reason": out,
            }

        pr_url = out
        pr_number: int | None = None
        if out.startswith("{"):
            try:
                parsed = json.loads(out)
                pr_url = str(parsed.get("url") or out)
                if parsed.get("number") is not None:
                    pr_number = int(parsed["number"])
            except Exception:
                pass

        return {
            "ok": True,
            "allowed": True,
            "repo": repo,
            "title": clean_title,
            "head": clean_head,
            "base": clean_base,
            "draft": draft,
            "is_protected_target": protected_target,
            "approval_id": approval_id,
            "pr_number": pr_number,
            "url": pr_url,
            "output": out,
        }

    def comment_on_pull_request(
        self,
        repo: str,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
        approval: Any | None = None,
        allowed_repos: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Post a PR comment or review after allowlist and approval checks."""
        ok_repo, repo_or_err = self.validate_repo(repo, allowed_repos=allowed_repos)
        if not ok_repo:
            return {
                "ok": False,
                "allowed": False,
                "repo": sanitize_cli_output(repo),
                "pr_number": pr_number,
                "error": repo_or_err,
                "reason": repo_or_err,
            }
        repo = repo_or_err

        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            msg = "Pull request number must be a positive integer."
            return {"ok": False, "allowed": False, "repo": repo, "error": msg, "reason": msg}

        clean_event = str(event or "").strip().upper()
        if clean_event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            msg = "Event must be COMMENT, APPROVE, or REQUEST_CHANGES."
            return {"ok": False, "allowed": False, "repo": repo, "pr_number": pr_number, "error": msg, "reason": msg}

        clean_body = str(body or "").strip()
        if not clean_body:
            msg = "Pull request comment or review body is required."
            return {"ok": False, "allowed": False, "repo": repo, "pr_number": pr_number, "event": clean_event, "error": msg, "reason": msg}

        approved, reason, approval_id = verify_approval(approval, repo=repo)
        if not approved:
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "pr_number": pr_number,
                "event": clean_event,
                "approval_id": approval_id,
                "error": reason,
                "reason": reason,
            }

        if clean_event == "COMMENT":
            args = ["pr", "comment", str(pr_number), "--repo", repo, "--body", clean_body]
        else:
            review_flag = "--approve" if clean_event == "APPROVE" else "--request-changes"
            args = ["pr", "review", str(pr_number), "--repo", repo, review_flag, "--body", clean_body]

        rc, output = self._run_gh(args)
        if rc != 0:
            return {
                "ok": False,
                "allowed": True,
                "repo": repo,
                "pr_number": pr_number,
                "event": clean_event,
                "approval_id": approval_id,
                "error": sanitize_cli_output(output),
                "reason": sanitize_cli_output(output),
            }

        return {
            "ok": True,
            "allowed": True,
            "repo": repo,
            "pr_number": pr_number,
            "event": clean_event,
            "approval_id": approval_id,
            "output": sanitize_cli_output(output),
        }

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str = "",
        *,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        approval: Any | None = None,
        allowed_repos: Iterable[str] | None = None,
        require_approval: bool = True,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        """Create a GitHub issue via `gh issue create`, gated by an approved ApprovalRequest and repo allowlist."""
        ok_repo, repo_or_err = self.validate_repo(repo, allowed_repos=allowed_repos)
        if not ok_repo:
            return {
                "ok": False,
                "allowed": False,
                "repo": sanitize_cli_output(repo),
                "error": repo_or_err,
                "reason": repo_or_err,
            }
        repo = repo_or_err

        clean_title = (title or "").strip()
        if not clean_title:
            msg = "Issue title is required."
            return {
                "ok": False,
                "allowed": False,
                "repo": repo,
                "error": msg,
                "reason": msg,
            }

        approval_id: str | None = None
        if require_approval or approval is not None:
            approved, reason, approval_id = verify_approval(approval, repo=repo)
            if not approved:
                return {
                    "ok": False,
                    "allowed": False,
                    "repo": repo,
                    "approval_id": approval_id,
                    "error": reason,
                    "reason": reason,
                }

        clean_labels = [lbl.strip() for lbl in (labels or []) if lbl and lbl.strip() and not lbl.strip().startswith("-")]
        clean_assignees = [
            usr.strip() for usr in (assignees or []) if usr and usr.strip() and not usr.strip().startswith("-")
        ]

        args = [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            clean_title,
            "--body",
            body or "",
        ]
        for lbl in clean_labels:
            args.extend(["--label", lbl])
        for usr in clean_assignees:
            args.extend(["--assignee", usr])

        rc, out = self._run_gh(args, cwd=cwd)
        if rc != 0:
            return {
                "ok": False,
                "allowed": True,
                "repo": repo,
                "title": clean_title,
                "approval_id": approval_id,
                "error": out,
                "reason": out,
            }

        issue_url = out
        issue_number: int | None = None
        if out.startswith("{"):
            try:
                parsed = json.loads(out)
                issue_url = str(parsed.get("url") or out)
                if parsed.get("number") is not None:
                    issue_number = int(parsed["number"])
            except Exception:
                pass

        return {
            "ok": True,
            "allowed": True,
            "repo": repo,
            "title": clean_title,
            "labels": clean_labels,
            "assignees": clean_assignees,
            "approval_id": approval_id,
            "issue_number": issue_number,
            "url": issue_url,
            "output": out,
        }

    def generate_change_summary(self, repo_path: str | None = None) -> dict[str, Any]:
        root = Path(repo_path) if repo_path else get_settings().REPO_ROOT
        try:
            diff_stat = subprocess.run(["git", "diff", "--stat", "HEAD~1..HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
            log_out = subprocess.run(["git", "log", "-n", "5", "--oneline"], cwd=str(root), capture_output=True, text=True, timeout=10)
            return {
                "recent_commits": [sanitize_cli_output(ln) for ln in log_out.stdout.splitlines() if ln.strip()],
                "diff_summary": sanitize_cli_output(diff_stat.stdout.strip()) or "No diff from parent commit.",
            }
        except Exception as exc:
            return {"recent_commits": [], "diff_summary": sanitize_cli_output(str(exc))}


github_client = GitHubClient()
