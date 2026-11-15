import json
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import get_settings


class GitHubClient:
    """Octokit-compatible GitHub REST & CLI integration respecting repository permissions and protected branches."""

    @staticmethod
    def _run_gh(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
        root = cwd or get_settings().REPO_ROOT
        try:
            proc = subprocess.run(["gh", *args], cwd=str(root), capture_output=True, text=True, timeout=15)
            return proc.returncode, (proc.stdout or proc.stderr).strip()
        except Exception as exc:
            return 1, str(exc)

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

    def generate_change_summary(self, repo_path: str | None = None) -> dict[str, Any]:
        root = Path(repo_path) if repo_path else get_settings().REPO_ROOT
        try:
            diff_stat = subprocess.run(["git", "diff", "--stat", "HEAD~1..HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
            log_out = subprocess.run(["git", "log", "-n", "5", "--oneline"], cwd=str(root), capture_output=True, text=True, timeout=10)
            return {
                "recent_commits": [ln for ln in log_out.stdout.splitlines() if ln.strip()],
                "diff_summary": diff_stat.stdout.strip() or "No diff from parent commit.",
            }
        except Exception as exc:
            return {"recent_commits": [], "diff_summary": str(exc)}


github_client = GitHubClient()
