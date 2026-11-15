import re
import subprocess
from pathlib import Path
from typing import Any

import psutil
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.manager import browser_manager
from app.core.config import get_settings
from app.core.events import event_bus
from app.models import Agent, AuditLog, BrowserProfile, Notification, Task
from app.orchestration.scheduler import task_scheduler

# --------------------------------------------------------------------------------------
# Static secret scanning (Division 11 - QA / Security)
# --------------------------------------------------------------------------------------
# High-confidence token formats. A match on any of these is always reported, because the
# value cannot plausibly be a placeholder.
HIGH_CONFIDENCE_SECRET_RULES: list[tuple[str, str, re.Pattern[str], bool]] = [
    ("anthropic_api_key", "critical", re.compile(r"sk-ant-(?:api\d{2}-)?[A-Za-z0-9_\-]{16,}"), False),
    ("openai_project_key", "critical", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}"), False),
    ("openai_legacy_key", "critical", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), False),
    ("aws_access_key_id", "critical", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"), False),
    ("github_token", "critical", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"), False),
    ("github_fine_grained_pat", "critical", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), False),
    ("gitlab_pat", "critical", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), False),
    ("slack_token", "critical", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), False),
    ("stripe_secret_key", "critical", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), False),
    ("google_api_key", "critical", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), False),
    ("sendgrid_api_key", "critical", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"), False),
    ("npm_access_token", "critical", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), False),
    ("twilio_api_key", "high", re.compile(r"\bSK[0-9a-fA-F]{32}\b"), False),
    ("discord_webhook", "high", re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+"), False),
    ("private_key_block", "critical", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), False),
    ("json_web_token", "high", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), False),
    ("authorization_bearer", "high", re.compile(r"(?i)\bauthorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9_\-\.=]{20,}"), False),
    ("basic_auth_url", "high", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]{2,}:([^\s/@]{2,})@"), True),
]

# Generic "a secret was assigned a literal value" heuristics. These can legitimately hit a
# documented development placeholder, so they are matched against an allowlist below.
GENERIC_SECRET_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("hardcoded_password", "high", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*(?::\s*str\s*)?[=:]\s*['\"]([^'\"\s]{6,})['\"]")),
    ("hardcoded_secret", "high", re.compile(r"(?i)\b(?:secret|api[_\-]?key|access[_\-]?key|auth[_\-]?token|access[_\-]?token|client[_\-]?secret|private[_\-]?key)\s*(?::\s*str\s*)?[=:]\s*['\"]([^'\"\s]{8,})['\"]")),
    ("hardcoded_aws_secret_key", "critical", re.compile(r"(?i)\baws_secret_access_key\s*(?::\s*str\s*)?[=:]\s*['\"]?([A-Za-z0-9/+=]{40})")),
]

# Values that are documented, intentional development placeholders. They are still surfaced
# (as "suppressed") so an operator can see them, but they do not fail the scan.
DOCUMENTED_PLACEHOLDER_VALUES: list[re.Pattern[str]] = [
    re.compile(r"^aatm[_-](?:admin|dev[_-]?secret|password|secret)"),
    re.compile(r"^change-(?:me-immediately|this-to-a-secure)", re.IGNORECASE),
    re.compile(r"^(?:your|my)[-_]?(?:secret|key|token|password)", re.IGNORECASE),
    re.compile(r"^<[^>]*>$"),
    re.compile(r"^\$\{[^}]*\}$"),
    re.compile(r"^(?:\*{3,}|x{3,})$", re.IGNORECASE),
    re.compile(r"^(?:placeholder|example|redacted|dummy|todo|changeme)", re.IGNORECASE),
    re.compile(r"EXAMPLE", re.IGNORECASE),
]

# File extensions that can realistically carry credentials. The previous scanner only
# covered .py/.ts/.tsx, which missed .env, shell scripts, PowerShell and YAML.
SCAN_EXTENSIONS: tuple[str, ...] = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".env", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".txt", ".md", ".sql", ".html", ".xml",
)

# Directories that hold generated/local state and must never be scanned or committed.
EXCLUDED_SCAN_DIRS: frozenset[str] = frozenset(
    {
        ".venv", "venv", "env", "node_modules", ".git", ".hg", ".svn", "dist", "build",
        "out", "coverage", ".next", ".nuxt", ".cache", ".pytest_cache", ".ruff_cache",
        ".mypy_cache", "__pycache__", ".tox", ".idea", ".vscode",
        "data", "worktrees", "backups", "logs",
    }
)

# Files that are templates by definition: generic literal rules are skipped for them,
# but high-confidence token formats are still scanned.
TEMPLATE_FILE_MARKERS: tuple[str, ...] = (".example", ".sample", ".template", ".dist", ".tpl")

# Artifacts that must never be committed (Manager hard rule: no secrets, no .env, no
# private browser profiles).
FORBIDDEN_ARTIFACT_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("env_file_committed", "critical", re.compile(r"(?:^|/)\.env$")),
    ("private_key_file_committed", "critical", re.compile(r"(?i)\.(?:pem|key|p12|pfx|jks|keystore)$")),
    ("browser_profile_committed", "critical", re.compile(r"(?:^|/)browser_profiles?/")),
    ("local_database_committed", "high", re.compile(r"(?i)\.(?:db|sqlite|sqlite3)$")),
    ("credential_dump_committed", "critical", re.compile(r"(?:^|/)(?:credentials|secrets?)\.(?:json|ya?ml|txt|env)$")),
]

# Backwards-compatible alias retained for callers that imported the old constant.
SECRET_PATTERNS = [pat for _rid, _sev, pat, _allow in HIGH_CONFIDENCE_SECRET_RULES]


class QAAndIntelligenceEngine:
    """Runs automated code inspection, security checks, hardware monitoring, team intelligence & self-healing."""

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _is_template_file(path: Path) -> bool:
        name = path.name.lower()
        return any(marker in name for marker in TEMPLATE_FILE_MARKERS)

    @staticmethod
    def _is_placeholder_value(value: str) -> bool:
        return any(pat.search(value.strip()) for pat in DOCUMENTED_PLACEHOLDER_VALUES)

    @staticmethod
    def _looks_like_secret(value: str) -> bool:
        """Heuristic: does this literal look like a credential rather than a UI string?

        Keeps rules such as an i18n label (`password: 'Password'`) out of the findings,
        while still catching a genuine mixed-case + digit + symbol credential literal.
        """
        v = (value or "").strip()
        if len(v) < 8 or " " in v:
            return False
        letters = sum(c.isalpha() for c in v)
        digits = sum(c.isdigit() for c in v)
        symbols = sum(not c.isalnum() for c in v)
        if letters and digits and symbols:
            return True
        if letters and digits and len(v) >= 16:
            return True
        if letters and symbols and len(v) >= 16:
            return True
        return False

    @staticmethod
    def _line_of(text: str, offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    @staticmethod
    def _tracked_files(root: Path) -> list[str] | None:
        """List files tracked by git, or None when git/index is unavailable."""
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                return None
            return [p for p in proc.stdout.split("\0") if p]
        except Exception:
            return None

    # ---------------------------------------------------------------- telemetry
    def get_system_resources(self) -> dict[str, Any]:
        settings = get_settings()
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(settings.REPO_ROOT))
        cpu = psutil.cpu_percent(interval=0.05)
        return {
            "cpu_percent": round(cpu, 1),
            "cpu_count": psutil.cpu_count(logical=True) or 1,
            "ram_total_gb": round(vm.total / (1024**3), 2),
            "ram_used_gb": round(vm.used / (1024**3), 2),
            "ram_percent": round(vm.percent, 1),
            "disk_total_gb": round(disk.total / (1024**3), 2),
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "disk_percent": round(disk.percent, 1),
        }

    # ---------------------------------------------------------------- secret scanning
    def run_security_scan(self, repo_root: Path | str | None = None) -> dict[str, Any]:
        """Scan the working tree and the git index for committed secrets and forbidden artifacts.

        Findings never contain the matched secret value - only the file, line and rule id -
        so that the scan output is safe to store in the audit trail.
        """
        settings = get_settings()
        root = Path(repo_root).resolve() if repo_root else settings.REPO_ROOT
        findings: list[dict[str, Any]] = []
        suppressed: list[dict[str, Any]] = []
        files_scanned = 0
        scanned_paths: set[str] = set()

        def _record(rule: str, severity: str, rel: str, line: int, suppressed_reason: str | None) -> None:
            entry = {"severity": severity, "file": rel, "rule": rule, "line": line}
            if suppressed_reason:
                entry["status"] = "suppressed"
                entry["suppression_reason"] = suppressed_reason
                suppressed.append(entry)
            else:
                entry["status"] = "reported"
                findings.append(entry)

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel_path = path.relative_to(root)
                rel = str(rel_path)
            except ValueError:
                continue
            if any(part in EXCLUDED_SCAN_DIRS for part in rel_path.parts):
                continue
            name = path.name.lower()
            # Dotfiles such as ".env" / ".env.production" have no extension suffix.
            is_env_file = name == ".env" or name.startswith(".env.")
            if not is_env_file and not any(name.endswith(ext) for ext in SCAN_EXTENSIONS):
                continue

            files_scanned += 1
            scanned_paths.add(rel)

            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            is_template = self._is_template_file(path)

            for rule_id, severity, pat, allow_placeholder in HIGH_CONFIDENCE_SECRET_RULES:
                for match in pat.finditer(text):
                    value = match.group(1) if match.groups() else ""
                    if (
                        allow_placeholder
                        and value
                        and self._is_placeholder_value(value)
                    ):
                        _record(
                            rule_id,
                            severity,
                            rel,
                            self._line_of(text, match.start()),
                            "documented development placeholder value",
                        )
                    else:
                        _record(rule_id, severity, rel, self._line_of(text, match.start()), None)

            for rule_id, severity, pat in GENERIC_SECRET_RULES:
                if is_template:
                    continue
                for match in pat.finditer(text):
                    value = match.group(1) if match.groups() else ""
                    if not self._looks_like_secret(value):
                        # Not credential-shaped (for example an i18n UI label).
                        continue
                    if value and self._is_placeholder_value(value):
                        _record(
                            rule_id,
                            severity,
                            rel,
                            self._line_of(text, match.start()),
                            "documented development placeholder value",
                        )
                    else:
                        _record(rule_id, severity, rel, self._line_of(text, match.start()), None)

        # --- forbidden artifacts, evaluated against the git index (what would be committed)
        tracked = self._tracked_files(root)
        git_index_available = tracked is not None
        artifact_candidates = tracked if tracked is not None else sorted(scanned_paths)
        forbidden_artifacts: list[dict[str, Any]] = []
        for rel in artifact_candidates:
            for rule_id, severity, pat in FORBIDDEN_ARTIFACT_RULES:
                if pat.search(rel):
                    forbidden_artifacts.append(
                        {"severity": severity, "file": rel, "rule": rule_id, "status": "reported"}
                    )
                    break

        findings.extend(forbidden_artifacts)
        status = "passed" if not findings else "warning"

        return {
            "status": status,
            "files_scanned": files_scanned,
            "findings": findings,
            "suppressed_findings": suppressed,
            "forbidden_artifacts": forbidden_artifacts,
            "git_index_scanned": git_index_available,
            "rules_evaluated": len(HIGH_CONFIDENCE_SECRET_RULES) + len(GENERIC_SECRET_RULES),
        }

    # ---------------------------------------------------------------- intelligence
    async def compute_team_intelligence(self, db: AsyncSession) -> dict[str, Any]:
        agents = list((await db.execute(select(Agent))).scalars().all())
        tasks = list((await db.execute(select(Task))).scalars().all())

        total_tasks = len(tasks)
        completed = sum(1 for t in tasks if t.status == "completed")
        failed = sum(1 for t in tasks if t.status == "failed")
        in_progress = sum(1 for t in tasks if t.status == "in_progress")
        blocked = sum(1 for t in tasks if t.status == "blocked")

        workers = [a for a in agents if a.role == "worker"]
        idle_workers = [w.id for w in workers if w.status == "idle"]
        recommendations: list[str] = []
        if idle_workers and any(t.status == "queued" for t in tasks):
            recommendations.append(f"{len(idle_workers)} worker(s) idle with queued tasks available — run orchestration cycle.")
        if failed > 0:
            recommendations.append(f"{failed} failed task(s) detected — review errors or trigger self-healing recovery.")
        if blocked > 0:
            recommendations.append(f"{blocked} blocked task(s) detected — verify emergency-stop clearance or requeue manually.")
        if not recommendations:
            recommendations.append("All four workers balanced; no bottlenecks detected.")

        return {
            "total_tasks": total_tasks,
            "completed_tasks": completed,
            "in_progress_tasks": in_progress,
            "failed_tasks": failed,
            "blocked_tasks": blocked,
            "completion_rate": round((completed / total_tasks) * 100, 1) if total_tasks else 0.0,
            "worker_count": len(workers),
            "idle_workers": idle_workers,
            "recommendations": recommendations,
            "resources": self.get_system_resources(),
        }

    # ---------------------------------------------------------------- self healing
    async def run_self_healing(
        self,
        db: AsyncSession,
        *,
        repo_path: Path | str | None = None,
        worktrees_root: Path | str | None = None,
    ) -> dict[str, Any]:
        stalled = await task_scheduler.detect_and_recover_stalled_tasks(db)

        # Recover tasks that were blocked by an Emergency Stop. Without this they stay
        # stranded forever: the scheduler only dispatches "queued" tasks and the stalled
        # sweep only inspects "in_progress" tasks.
        from app.api.routes.devices import sweep_offline_devices
        from app.git_workflows.manager import git_workflow_manager
        from app.orchestration.replacement import replacement_service

        requeued = await replacement_service.resume_emergency_stopped_tasks(db, actor="self_healing")

        # Reclaim work owned by execution nodes that stopped reporting.
        device_sweep = await sweep_offline_devices(db, actor="self_healing")

        # Recover crashed browser profiles
        bp_res = await db.execute(select(BrowserProfile).where(BrowserProfile.status == "crashed"))
        recovered_browsers: list[str] = []
        for bp in bp_res.scalars().all():
            await browser_manager.simulate_crash_and_recover(db, bp, recover=True)
            recovered_browsers.append(bp.id)

        # Reset error-state workers if no active task
        ag_res = await db.execute(select(Agent).where(Agent.status == "error"))
        recovered_workers: list[str] = []
        for ag in ag_res.scalars().all():
            ag.status = "idle"
            ag.current_task_id = None
            recovered_workers.append(ag.id)

        # Prune orphaned git worktrees belonging to missing, offline, replaced, or disabled agents.
        settings = get_settings()
        effective_repo = Path(repo_path).resolve() if repo_path else settings.REPO_ROOT
        wt_dir = (
            Path(worktrees_root).resolve()
            if worktrees_root
            else (
                (effective_repo / "data" / "worktrees").resolve()
                if repo_path
                else (settings.DATA_DIR / "worktrees").resolve()
            )
        )
        all_agents = {
            ag.id: ag for ag in (await db.execute(select(Agent))).scalars().all()
        }
        pruned_worktrees: list[str] = []
        if wt_dir.is_dir():
            for child in sorted(wt_dir.iterdir()):
                if not child.is_dir():
                    continue
                candidate_id = child.name
                ag = all_agents.get(candidate_id)
                is_orphaned = (
                    ag is None
                    or not ag.is_enabled
                    or ag.status in ("offline", "replaced", "disabled")
                )
                if not is_orphaned:
                    continue
                try:
                    rm_res = await git_workflow_manager.remove_agent_worktree(
                        candidate_id,
                        db=db,
                        repo_path=effective_repo,
                        worktrees_root=wt_dir,
                        actor="self_healing",
                    )
                    if rm_res.get("ok") or not child.exists():
                        pruned_worktrees.append(candidate_id)
                except Exception:
                    continue

        db.add(
            AuditLog(
                actor="self_healing",
                action="qa.self_healing",
                resource_type="system",
                details={
                    "stalled_tasks_recovered": stalled,
                    "emergency_stopped_tasks_requeued": requeued,
                    "offline_devices": device_sweep["offline_devices"],
                    "device_tasks_requeued": device_sweep["requeued_tasks"],
                    "browsers_recovered": recovered_browsers,
                    "workers_recovered": recovered_workers,
                    "pruned_worktrees": pruned_worktrees,
                },
            )
        )
        if (
            stalled
            or requeued
            or device_sweep["requeued_tasks"]
            or recovered_browsers
            or recovered_workers
            or pruned_worktrees
        ):
            db.add(
                Notification(
                    category="recovery",
                    severity="info",
                    title="Self-Healing Recovery Executed",
                    message=(
                        f"Recovered {len(stalled)} stalled task(s), requeued {len(requeued)} "
                        f"emergency-stopped task(s), {len(device_sweep['requeued_tasks'])} "
                        f"offline-device task(s), {len(recovered_browsers)} browser(s), "
                        f"{len(recovered_workers)} worker(s), pruned {len(pruned_worktrees)} worktree(s)."
                    ),
                    link="/qa",
                )
            )
        await db.commit()
        await event_bus.emit("qa.completed", {"self_healing": True})
        return {
            "stalled_tasks_recovered": stalled,
            "emergency_stopped_tasks_requeued": requeued,
            "offline_devices": device_sweep["offline_devices"],
            "device_tasks_requeued": device_sweep["requeued_tasks"],
            "browsers_recovered": recovered_browsers,
            "workers_recovered": recovered_workers,
            "pruned_worktrees": pruned_worktrees,
        }


qa_engine = QAAndIntelligenceEngine()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m app.qa.engine [--repo-root DIR] [--json]`.

    Exit code 0 when the scan is clean, 1 when findings are reported, 2 on error.
    Used by the platform verification scripts (`scripts/ubuntu/verify.sh`,
    `scripts/windows/verify.ps1`) and usable as a pre-commit / CI gate.
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="AATM static secret scanner")
    parser.add_argument("--repo-root", default=None, help="repository root to scan (default: configured REPO_ROOT)")
    parser.add_argument("--json", action="store_true", help="emit the raw scan report as JSON")
    parser.add_argument(
        "--fail-on-suppressed",
        action="store_true",
        help="also fail when only suppressed (documented placeholder) findings exist",
    )
    args = parser.parse_args(argv)

    if args.repo_root:
        from app.core.config import get_settings

        settings = get_settings()
        settings.REPO_ROOT = Path(args.repo_root).resolve()

    scan = qa_engine.run_security_scan()
    findings = scan.get("findings", [])
    suppressed = scan.get("suppressed_findings", [])

    if args.json:
        print(_json.dumps(scan, indent=2, default=str))
    else:
        print(f"AATM secret scan: {scan['files_scanned']} files, {scan['rules_evaluated']} rules")
        for finding in findings:
            print(f"  [{finding['severity']}] {finding['file']}:{finding.get('line')} {finding['rule']}")
        for finding in suppressed:
            print(
                f"  [{finding['severity']}] {finding['file']}:{finding.get('line')} {finding['rule']} "
                f"(suppressed: {finding['suppression_reason']})"
            )
        print(f"  status: {scan['status']} ({len(findings)} finding(s), {len(suppressed)} suppressed)")

    if findings:
        return 1
    if args.fail_on_suppressed and suppressed:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
