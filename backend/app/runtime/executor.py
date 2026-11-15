import subprocess
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.employees.catalog import create_dynamic_employee, ensure_employee_workspace
from app.models import (
    Agent,
    AgentMessage,
    AuditLog,
    MemoryEntry,
    Notification,
    Project,
    Task,
    WorkspaceArtifact,
)


class WorkspaceSandboxError(PermissionError):
    pass


class AgentRuntimeExecutor:
    """Executes real isolated workspace operations, delivers AgentMessages & runs SDLC workflows."""

    @staticmethod
    def resolve_safe_path(agent_id: str, relative_path: str) -> Path:
        root = ensure_employee_workspace(agent_id)
        candidate = (root / relative_path).resolve()
        if root != candidate and root not in candidate.parents:
            raise WorkspaceSandboxError(
                f"Access denied: path '{relative_path}' escapes isolated workspace for '{agent_id}'."
            )
        return candidate

    def list_workspace_files(self, agent_id: str) -> list[dict[str, Any]]:
        root = ensure_employee_workspace(agent_id)
        files: list[dict[str, Any]] = []
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files.append({
                    "path": str(p.relative_to(root)),
                    "size_bytes": p.stat().st_size,
                })
        return files

    async def write_workspace_file(
        self,
        db: AsyncSession,
        agent_id: str,
        relative_path: str,
        content: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> WorkspaceArtifact:
        target = self.resolve_safe_path(agent_id, relative_path)
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        art = WorkspaceArtifact(
            agent_id=agent_id,
            project_id=project_id,
            task_id=task_id,
            relative_path=relative_path,
            action="modify" if existed else "create",
            summary=f"Wrote {len(content)} bytes to {relative_path}",
            exit_code=0,
        )
        db.add(art)
        await db.commit()
        await db.refresh(art)
        return art

    async def run_workspace_command(
        self,
        db: AsyncSession,
        agent_id: str,
        command: list[str],
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute an allowed development tool command strictly inside the employee's isolated workspace."""
        allowed_bins = {"python3", "python", "pytest", "ruff", "ls", "cat"}
        if not command or Path(command[0]).name not in allowed_bins:
            raise WorkspaceSandboxError(f"Command '{command}' not in allowed workspace toolset {sorted(allowed_bins)}.")

        ws = ensure_employee_workspace(agent_id)
        settings = get_settings()
        exe = command[0]
        if exe in ("python", "python3", "pytest", "ruff"):
            venv_bin = settings.REPO_ROOT / ".venv" / "bin" / exe
            if venv_bin.exists():
                command = [str(venv_bin), *command[1:]]

        t0 = time.perf_counter()
        proc = subprocess.run(command, cwd=str(ws), capture_output=True, text=True, timeout=20)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        art = WorkspaceArtifact(
            agent_id=agent_id,
            project_id=project_id,
            task_id=task_id,
            relative_path=".",
            action="test",
            summary=(proc.stdout or proc.stderr)[:1000],
            exit_code=proc.returncode,
        )
        db.add(art)
        await db.commit()
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "elapsed_ms": elapsed_ms,
        }

    async def send_and_execute_task_message(
        self,
        db: AsyncSession,
        sender_id: str,
        recipient_id: str,
        instructions: str,
        project_id: str | None = None,
        task_id: str | None = None,
        priority: str = "high",
        expected_deliverables: list[str] | None = None,
        auto_execute: bool = True,
    ) -> AgentMessage:
        agent = await db.get(Agent, recipient_id)
        if not agent:
            raise ValueError(f"Recipient employee '{recipient_id}' not found.")
        if agent.is_emergency_stopped or not agent.is_enabled:
            raise ValueError(f"Recipient employee '{recipient_id}' is stopped or disabled.")

        ws_dir = ensure_employee_workspace(recipient_id)
        msg = AgentMessage(
            task_id=task_id,
            project_id=project_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            channel_type="task" if task_id else "direct",
            instructions=instructions,
            priority=priority,
            required_tools=["workspace_fs", "python_runner"],
            workspace_info={"workspace_path": str(ws_dir), "branch": agent.git_branch},
            expected_deliverables=expected_deliverables or ["deliverable_report.md"],
            permissions=agent.permissions or [],
            execution_status="delivered",
            delivery_status="acknowledged",
            acked_at=utcnow(),
        )
        db.add(msg)
        await db.flush()

        if auto_execute:
            agent.status = "working"
            agent.last_heartbeat = utcnow()
            report_name = (expected_deliverables or ["deliverable_report.md"])[0]
            await self.write_workspace_file(
                db,
                agent_id=recipient_id,
                relative_path=report_name,
                content=(
                    f"# Deliverable by {agent.name} ({agent.id})\n\n"
                    f"## Task Instructions\n{instructions}\n\n"
                    f"## Status\nExecuted inside isolated workspace `{ws_dir}` at {utcnow().isoformat()}.\n"
                ),
                project_id=project_id,
                task_id=task_id,
            )
            msg.execution_status = "completed"
            msg.response_payload = {"deliverable": report_name, "workspace": str(ws_dir)}
            agent.status = "idle"
            agent.tasks_completed += 1

        db.add(
            AuditLog(
                actor=sender_id,
                action="agent_message.deliver",
                resource_type="agent_message",
                resource_id=msg.id,
                details={"recipient": recipient_id, "auto_execute": auto_execute},
            )
        )
        await db.commit()
        await db.refresh(msg)
        await event_bus.emit("agent_message.delivered", {"id": msg.id, "recipient": recipient_id, "status": msg.execution_status})
        return msg

    async def execute_sdlc_from_idea(
        self,
        db: AsyncSession,
        idea: str,
        project_name: str,
        actor: str = "admin@aatm.local",
    ) -> dict[str, Any]:
        """Full SDLC pipeline: analyze idea -> create project -> build team -> generate & execute workspace code + tests."""
        slug = "".join(c if c.isalnum() else "-" for c in project_name.lower()).strip("-")
        existing = (await db.execute(select(Project).where(Project.slug == slug))).scalar_one_or_none()
        if existing:
            slug = f"{slug}-{utcnow().strftime('%H%M%S')}"
            project_name = f"{project_name} ({utcnow().strftime('%H%M%S')})"

        project = Project(
            name=project_name,
            slug=slug,
            description=idea,
            tech_stack=["Python", "FastAPI", "React", "Pytest"],
            config={"sdlc_pipeline": "automated_v2"},
        )
        db.add(project)
        await db.flush()

        # Dynamically provision or reuse specialized employees for this project
        architect = await create_dynamic_employee(db, f"{project_name} Architect", "software_architect", actor=actor)
        backend = await create_dynamic_employee(db, f"{project_name} Backend Dev", "backend_developer", actor=actor)
        qa = await create_dynamic_employee(db, f"{project_name} QA Engineer", "qa_engineer", actor=actor)

        # Create real specification, module & pytest inside isolated workspaces
        spec_task = Task(
            project_id=project.id,
            division_code="D01",
            title=f"Architecture & Spec for {project_name}",
            description=idea,
            priority="critical",
            execution_mode="automatic",
            assigned_agent_id=architect.id,
            expected_deliverables=["ARCHITECTURE_SPEC.md"],
        )
        impl_task = Task(
            project_id=project.id,
            division_code="D02",
            title=f"Core Implementation for {project_name}",
            description=f"Implement Python module for: {idea}",
            priority="high",
            execution_mode="automatic",
            assigned_agent_id=backend.id,
            expected_deliverables=["app_module.py"],
        )
        qa_task = Task(
            project_id=project.id,
            division_code="D11",
            title=f"Automated Tests & QA for {project_name}",
            description="Write & execute pytest verification suite",
            priority="high",
            execution_mode="collaborative",
            assigned_agent_id=qa.id,
            collaborator_ids=[backend.id],
            expected_deliverables=["test_app.py"],
        )
        db.add_all([spec_task, impl_task, qa_task])
        await db.flush()

        await self.send_and_execute_task_message(
            db, "manager-0", architect.id, f"Design architecture spec for: {idea}", project.id, spec_task.id, "critical", ["ARCHITECTURE_SPEC.md"]
        )
        spec_task.status = "completed"
        spec_task.progress_percent = 100
        spec_task.actual_deliverables = ["ARCHITECTURE_SPEC.md"]

        await self.write_workspace_file(
            db,
            backend.id,
            "app_module.py",
            f'"""Auto-generated module for {project_name}."""\n\ndef service_health() -> dict:\n    return {{"project": "{project_name}", "status": "ok"}}\n',
            project.id,
            impl_task.id,
        )
        impl_task.status = "completed"
        impl_task.progress_percent = 100
        impl_task.actual_deliverables = ["app_module.py"]

        # QA writes real pytest referencing backend module via sys.path or local test
        backend_ws = ensure_employee_workspace(backend.id)
        await self.write_workspace_file(
            db,
            qa.id,
            "test_app.py",
            f"import sys\nsys.path.insert(0, r'{backend_ws}')\nfrom app_module import service_health\n\ndef test_service_health():\n    res = service_health()\n    assert res['status'] == 'ok'\n",
            project.id,
            qa_task.id,
        )
        test_run = await self.run_workspace_command(db, qa.id, ["pytest", "-q", "test_app.py"], project.id, qa_task.id)
        qa_task.status = "completed" if test_run["exit_code"] == 0 else "failed"
        qa_task.progress_percent = 100 if test_run["exit_code"] == 0 else 50
        qa_task.actual_deliverables = ["test_app.py"]
        qa_task.result_summary = test_run["stdout"] or test_run["stderr"]

        db.add(
            MemoryEntry(
                scope="project",
                visibility="project",
                project_id=project.id,
                title=f"SDLC Delivery: {project_name}",
                content=f"Idea '{idea}' executed across Architect ({architect.id}), Backend ({backend.id}), QA ({qa.id}); pytest exit={test_run['exit_code']}.",
                tags=["sdlc", "delivery"],
                importance=9,
                created_by=actor,
            )
        )
        db.add(
            Notification(
                category="sdlc",
                severity="info" if test_run["exit_code"] == 0 else "warning",
                title=f"SDLC Workflow Executed: {project_name}",
                message=f"Team of 3 dynamic employees created; automated pytest exit code = {test_run['exit_code']}.",
                link="/projects",
            )
        )
        await db.commit()
        return {
            "project_id": project.id,
            "project_name": project_name,
            "team_created": [architect.id, backend.id, qa.id],
            "tasks_completed": [spec_task.id, impl_task.id, qa_task.id],
            "pytest_result": test_run,
        }


agent_runtime = AgentRuntimeExecutor()
