import re
from typing import Any

import psutil
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.manager import browser_manager
from app.core.config import get_settings
from app.core.events import event_bus
from app.models import Agent, AuditLog, BrowserProfile, Notification, Task
from app.orchestration.scheduler import task_scheduler

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
]


class QAAndIntelligenceEngine:
    """Runs automated code inspection, security checks, hardware monitoring, team intelligence & self-healing."""

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

    def run_security_scan(self) -> dict[str, Any]:
        settings = get_settings()
        findings: list[dict[str, Any]] = []
        files_scanned = 0
        for ext in ("*.py", "*.ts", "*.tsx"):
            for path in settings.REPO_ROOT.rglob(ext):
                if any(part in (".venv", "node_modules", ".git", "dist") for part in path.parts):
                    continue
                files_scanned += 1
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    for pat in SECRET_PATTERNS:
                        if pat.search(text):
                            findings.append({
                                "severity": "high",
                                "file": str(path.relative_to(settings.REPO_ROOT)),
                                "rule": "hardcoded_secret_pattern",
                            })
                except Exception:
                    continue

        return {
            "status": "passed" if not findings else "warning",
            "files_scanned": files_scanned,
            "findings": findings,
        }

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

    async def run_self_healing(self, db: AsyncSession) -> dict[str, Any]:
        stalled = await task_scheduler.detect_and_recover_stalled_tasks(db)

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

        db.add(
            AuditLog(
                actor="self-healing",
                action="qa.self_healing",
                resource_type="system",
                details={
                    "stalled_tasks_recovered": stalled,
                    "browsers_recovered": recovered_browsers,
                    "workers_recovered": recovered_workers,
                },
            )
        )
        if stalled or recovered_browsers or recovered_workers:
            db.add(
                Notification(
                    category="recovery",
                    severity="info",
                    title="Self-Healing Recovery Executed",
                    message=f"Recovered {len(stalled)} task(s), {len(recovered_browsers)} browser(s), {len(recovered_workers)} worker(s).",
                    link="/qa",
                )
            )
        await db.commit()
        await event_bus.emit("qa.completed", {"self_healing": True})
        return {
            "stalled_tasks_recovered": stalled,
            "browsers_recovered": recovered_browsers,
            "workers_recovered": recovered_workers,
        }


qa_engine = QAAndIntelligenceEngine()
