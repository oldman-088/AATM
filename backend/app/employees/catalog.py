import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Agent, AuditLog, BrowserProfile, ProviderAccount, ProviderSession

ROLE_TEMPLATES: list[dict[str, Any]] = [
    {"id": "ceo_manager", "title": "CEO / AI Manager", "divisions": ["D01", "D12"], "skills": ["strategy", "architecture", "delegation", "review"], "permissions": ["assign_tasks", "create_employees", "review_code", "request_approval", "orchestrate"]},
    {"id": "project_manager", "title": "Project Manager", "divisions": ["D01", "D08", "D09"], "skills": ["planning", "roadmap", "scheduling", "dependencies"], "permissions": ["assign_tasks", "manage_milestones"]},
    {"id": "business_analyst", "title": "Business Analyst", "divisions": ["D01", "D09"], "skills": ["requirements", "specs", "user_stories", "acceptance_criteria"], "permissions": ["read_memory", "write_specs"]},
    {"id": "software_architect", "title": "Software Architect", "divisions": ["D01", "D02", "D09"], "skills": ["architecture", "contracts", "scalability", "system_design"], "permissions": ["design_architecture", "write_specs", "review_code"]},
    {"id": "backend_developer", "title": "Backend Developer", "divisions": ["D02", "D09"], "skills": ["python", "fastapi", "sqlalchemy", "postgresql", "rest_api"], "permissions": ["edit_backend", "run_migrations", "run_backend_tests", "workspace_exec"]},
    {"id": "frontend_developer", "title": "Frontend Developer", "divisions": ["D03", "D04"], "skills": ["react", "typescript", "vite", "tailwind", "rtl", "i18n"], "permissions": ["edit_frontend", "run_frontend_tests", "build_ui", "workspace_exec"]},
    {"id": "ui_ux_designer", "title": "UI/UX Designer", "divisions": ["D03", "D04"], "skills": ["ux", "design_systems", "accessibility", "rtl_layout"], "permissions": ["edit_frontend", "write_specs"]},
    {"id": "database_engineer", "title": "Database Engineer", "divisions": ["D02", "D09"], "skills": ["postgresql", "sqlite", "alembic", "indexing", "migrations"], "permissions": ["edit_backend", "run_migrations"]},
    {"id": "api_engineer", "title": "API Engineer", "divisions": ["D02", "D05"], "skills": ["openapi", "rest", "websockets", "contracts", "auth"], "permissions": ["edit_backend", "run_backend_tests"]},
    {"id": "ai_engineer", "title": "AI Engineer", "divisions": ["D05", "D07"], "skills": ["llm", "arena", "anthropic", "prompts", "evaluation"], "permissions": ["configure_providers", "run_integration_tests"]},
    {"id": "browser_automation_engineer", "title": "Browser Automation Engineer", "divisions": ["D06"], "skills": ["playwright", "tampermonkey", "browser_use", "dom", "e2e"], "permissions": ["manage_browsers", "manage_userscripts", "run_integration_tests"]},
    {"id": "devops_engineer", "title": "DevOps Engineer", "divisions": ["D10", "D12"], "skills": ["docker", "linux", "windows", "ci_cd", "scripts"], "permissions": ["manage_worktrees", "workspace_exec", "run_scripts"]},
    {"id": "qa_engineer", "title": "QA Engineer", "divisions": ["D11", "D12"], "skills": ["pytest", "vitest", "playwright", "qa", "verification"], "permissions": ["run_qa", "run_backend_tests", "run_frontend_tests", "workspace_exec"]},
    {"id": "security_engineer", "title": "Security Engineer", "divisions": ["D11"], "skills": ["appsec", "rbac", "secret_scanning", "prompt_injection", "audit"], "permissions": ["run_qa", "audit_security"]},
    {"id": "code_reviewer", "title": "Code Reviewer", "divisions": ["D10", "D11"], "skills": ["code_review", "linting", "architecture_compliance"], "permissions": ["review_code", "run_qa"]},
    {"id": "tech_docs_specialist", "title": "Technical Documentation Specialist", "divisions": ["D09", "D12"], "skills": ["documentation", "markdown", "api_docs", "user_guides"], "permissions": ["write_docs", "read_memory"]},
]


def ensure_employee_workspace(agent_id: str) -> Path:
    settings = get_settings()
    ws = (settings.WORKSPACES_DIR / agent_id).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


async def allocate_account_and_session(
    db: AsyncSession,
    agent_id: str,
    provider_id: str,
    allocation_mode: str = "hybrid",
    preferred_account_id: str | None = None,
    browser_profile_id: str | None = None,
) -> tuple[str | None, str | None]:
    account: ProviderAccount | None = None
    if preferred_account_id:
        account = await db.get(ProviderAccount, preferred_account_id)

    if not account:
        res = await db.execute(
            select(ProviderAccount).where(ProviderAccount.provider_id == provider_id, ProviderAccount.status == "active")
        )
        accounts = list(res.scalars().all())
        for acc in accounts:
            assigned_cnt = (
                await db.execute(select(func.count(Agent.id)).where(Agent.provider_account_id == acc.id))
            ).scalar_one()
            if allocation_mode == "one_per_employee" and assigned_cnt == 0:
                account = acc
                break
            if allocation_mode in ("shared", "hybrid") and assigned_cnt < acc.max_employees:
                account = acc
                break

        if not account:
            account = ProviderAccount(
                id=f"acct-{provider_id}-{uuid.uuid4().hex[:6]}",
                provider_id=provider_id,
                label=f"{provider_id} Account ({allocation_mode})",
                account_identifier=f"{agent_id}@{provider_id}.local",
                allocation_mode=allocation_mode,
                max_employees=1 if allocation_mode == "one_per_employee" else 8,
                credential_ref="env:ARENA_SESSION_TOKEN" if "arena" in provider_id else "env:ANTHROPIC_API_KEY",
                status="active",
            )
            db.add(account)
            await db.flush()

    session = ProviderSession(
        id=f"sess-{agent_id}-{uuid.uuid4().hex[:6]}",
        account_id=account.id,
        agent_id=agent_id,
        browser_profile_id=browser_profile_id,
        status="active",
        health_score=100.0,
    )
    db.add(session)
    await db.flush()
    return account.id, session.id


async def create_dynamic_employee(
    db: AsyncSession,
    name: str,
    role_template_id: str | None = None,
    specialty: str | None = None,
    skills: list[str] | None = None,
    divisions: list[str] | None = None,
    permissions: list[str] | None = None,
    provider_id: str = "arena-agent",
    allocation_mode: str = "hybrid",
    preferred_account_id: str | None = None,
    create_browser_profile: bool = True,
    autonomy_mode: str = "semi_autonomous",
    device_id: str = "device-local",
    actor: str = "manager-0",
) -> Agent:
    template = next((t for t in ROLE_TEMPLATES if t["id"] == role_template_id), None)
    emp_id = f"emp-{uuid.uuid4().hex[:8]}"
    ws_path = ensure_employee_workspace(emp_id)

    bp_id: str | None = None
    if create_browser_profile:
        settings = get_settings()
        bp_id = f"profile-{emp_id}"
        db.add(
            BrowserProfile(
                id=bp_id,
                name=f"{name} Browser",
                agent_id=emp_id,
                profile_dir=str(settings.BROWSER_PROFILES_DIR / bp_id),
                mode="background",
                status="ready",
            )
        )

    acct_id, sess_id = await allocate_account_and_session(
        db,
        agent_id=emp_id,
        provider_id=provider_id,
        allocation_mode=allocation_mode,
        preferred_account_id=preferred_account_id,
        browser_profile_id=bp_id,
    )

    agent = Agent(
        id=emp_id,
        name=name,
        role="manager" if role_template_id in ("ceo_manager", "project_manager") else "worker",
        role_template=role_template_id or "custom",
        specialty=specialty or (template["title"] if template else "Custom Specialist"),
        skills=skills if skills is not None else (template["skills"] if template else ["general"]),
        divisions=divisions if divisions is not None else (template["divisions"] if template else ["D01"]),
        permissions=permissions if permissions is not None else (template["permissions"] if template else ["workspace_exec"]),
        status="idle",
        provider_id=provider_id,
        provider_account_id=acct_id,
        provider_session_id=sess_id,
        model_name="arena-agent-coder" if "arena" in provider_id else "claude-sonnet-4-5",
        browser_profile_id=bp_id,
        workspace_path=str(ws_path),
        device_id=device_id,
        autonomy_mode=autonomy_mode,
        git_branch=f"employee/{emp_id}",
    )
    db.add(agent)
    db.add(
        AuditLog(
            actor=actor,
            action="employee.create",
            resource_type="agent",
            resource_id=emp_id,
            details={"name": name, "template": role_template_id, "allocation_mode": allocation_mode},
        )
    )
    await db.commit()
    await db.refresh(agent)
    return agent
