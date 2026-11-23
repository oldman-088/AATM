import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Agent, AuditLog, BrowserProfile, ProviderAccount, ProviderSession
from app.runtime.sandbox import WorkspaceSandboxError, resolve_contained_path

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
    """Create one direct-child workspace without following symlinks outside the configured root."""
    if (
        not isinstance(agent_id, str)
        or not agent_id.strip()
        or agent_id in {".", ".."}
        or "/" in agent_id
        or "\\" in agent_id
        or "\x00" in agent_id
        or PureWindowsPath(agent_id).drive
    ):
        raise WorkspaceSandboxError("Employee workspace identifiers must be simple path components.")

    root = Path(get_settings().WORKSPACES_DIR).resolve()
    requested = root / agent_id
    if requested.is_symlink():
        raise WorkspaceSandboxError("Employee workspace roots may not be symbolic links.")
    workspace = resolve_contained_path(root, agent_id)
    if workspace.parent != root:
        raise WorkspaceSandboxError("Employee workspaces must be direct children of the configured root.")
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


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
                credential_ref=None if "arena" in provider_id else "env:ANTHROPIC_API_KEY",
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
    model_name: str | None = None,
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
                # Phase 6: one persistent profile per employee under BROWSER_PROFILES_DIR/<agent_id>,
                # visible so the human signs in to Arena manually (AATM never handles credentials).
                profile_dir=str(settings.BROWSER_PROFILES_DIR / emp_id),
                mode="visible",
                status="ready",
                arena_mode="agent",  # every Arena employee runs in Agent mode (the model default is "chat")
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
        model_name=model_name or ("arena-agent-coder" if "arena" in provider_id else "claude-sonnet-4-5"),
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


def _normalize_tokens(items: list[str] | None, *, upper: bool = False) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items or []:
        cleaned = str(raw).strip()
        if not cleaned:
            continue
        norm = cleaned.upper() if upper else cleaned.lower()
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


async def analyze_team_skill_gaps(
    db: AsyncSession,
    required_skills: list[str],
    divisions: list[str] | None = None,
) -> dict[str, Any]:
    """Compare active AI employees' skills/divisions against project requirements and rank matching ROLE_TEMPLATES."""
    norm_skills = _normalize_tokens(required_skills, upper=False)
    norm_divisions = _normalize_tokens(divisions, upper=True)

    active_res = await db.execute(
        select(Agent).where(
            Agent.is_enabled.is_(True),
            Agent.is_emergency_stopped.is_(False),
            Agent.status != "offline",
        )
    )
    active_agents = list(active_res.scalars().all())

    team_skills: set[str] = set()
    team_divisions: set[str] = set()
    for ag in active_agents:
        for s in ag.skills or []:
            team_skills.add(str(s).strip().lower())
        for d in ag.divisions or []:
            team_divisions.add(str(d).strip().upper())

    covered_skills = [s for s in norm_skills if s in team_skills]
    missing_skills = [s for s in norm_skills if s not in team_skills]
    missing_divisions = [d for d in norm_divisions if d not in team_divisions]

    # Greedily select minimal ROLE_TEMPLATES maximizing coverage of remaining missing skills & divisions
    uncovered_skills = set(missing_skills)
    uncovered_divs = set(missing_divisions)
    used_template_ids: set[str] = set()
    recommended_templates: list[dict[str, Any]] = []

    while uncovered_skills or uncovered_divs:
        best_template: dict[str, Any] | None = None
        best_key: tuple[int, int] = (0, 0)
        best_matched_skills: list[str] = []
        best_matched_divs: list[str] = []

        for tmpl in ROLE_TEMPLATES:
            if tmpl["id"] in used_template_ids:
                continue
            t_skills = {s.lower() for s in tmpl["skills"]}
            t_divs = {d.upper() for d in tmpl["divisions"]}
            matched_s = sorted(uncovered_skills & t_skills)
            matched_d = sorted(uncovered_divs & t_divs)
            if not matched_s and not matched_d:
                continue
            rank_key = (len(matched_s), len(matched_d))
            if rank_key > best_key:
                best_key = rank_key
                best_template = tmpl
                best_matched_skills = matched_s
                best_matched_divs = matched_d

        if best_template is None:
            break

        used_template_ids.add(best_template["id"])
        uncovered_skills -= set(best_matched_skills)
        uncovered_divs -= set(best_matched_divs)
        recommended_templates.append(
            {
                "template_id": best_template["id"],
                "title": best_template["title"],
                "matched_skills": best_matched_skills,
                "matched_divisions": best_matched_divs,
                "skills": list(best_template["skills"]),
                "divisions": list(best_template["divisions"]),
                "permissions": list(best_template["permissions"]),
            }
        )

    coverage_ratio = 1.0 if not norm_skills else round(len(covered_skills) / len(norm_skills), 4)
    return {
        "required_skills": norm_skills,
        "required_divisions": norm_divisions,
        "active_employee_count": len(active_agents),
        "covered_skills": covered_skills,
        "missing_skills": missing_skills,
        "missing_divisions": missing_divisions,
        "coverage_ratio": coverage_ratio,
        "recommended_templates": recommended_templates,
        "unmatched_skills": sorted(uncovered_skills),
    }


async def provision_missing_specialists(
    db: AsyncSession,
    required_skills: list[str],
    *,
    divisions: list[str] | None = None,
    provider_id: str = "arena-agent",
    allocation_mode: str = "hybrid",
    preferred_account_id: str | None = None,
    create_browser_profile: bool = False,
    max_new_employees: int = 5,
    device_id: str = "device-local",
    actor: str = "manager-0",
) -> dict[str, Any]:
    """Analyze skill gaps and dynamically provision missing specialist employees from ROLE_TEMPLATES (or custom)."""
    before = await analyze_team_skill_gaps(db, required_skills, divisions=divisions)
    limit = max(0, int(max_new_employees))
    provisioned: list[dict[str, Any]] = []

    for rec in before["recommended_templates"]:
        if len(provisioned) >= limit:
            break
        emp = await create_dynamic_employee(
            db,
            name=f"{rec['title']} (Auto-Provisioned)",
            role_template_id=rec["template_id"],
            specialty=rec["title"],
            skills=list(rec["skills"]),
            divisions=list(rec["divisions"]),
            permissions=list(rec["permissions"]),
            provider_id=provider_id,
            allocation_mode=allocation_mode,
            preferred_account_id=preferred_account_id,
            create_browser_profile=create_browser_profile,
            device_id=device_id,
            actor=actor,
        )
        provisioned.append(
            {
                "id": emp.id,
                "name": emp.name,
                "role_template": emp.role_template,
                "specialty": emp.specialty,
                "skills": list(emp.skills or []),
                "divisions": list(emp.divisions or []),
                "provider_account_id": emp.provider_account_id,
                "provider_session_id": emp.provider_session_id,
            }
        )

    unmatched = list(before["unmatched_skills"])
    if unmatched and len(provisioned) < limit:
        custom_divs = before["missing_divisions"] or before["required_divisions"] or ["D01"]
        emp = await create_dynamic_employee(
            db,
            name=f"Custom Specialist ({', '.join(unmatched[:2])})",
            role_template_id=None,
            specialty=f"Custom Specialist: {', '.join(unmatched)}",
            skills=unmatched,
            divisions=custom_divs,
            permissions=["workspace_exec", "read_memory"],
            provider_id=provider_id,
            allocation_mode=allocation_mode,
            preferred_account_id=preferred_account_id,
            create_browser_profile=create_browser_profile,
            device_id=device_id,
            actor=actor,
        )
        provisioned.append(
            {
                "id": emp.id,
                "name": emp.name,
                "role_template": emp.role_template,
                "specialty": emp.specialty,
                "skills": list(emp.skills or []),
                "divisions": list(emp.divisions or []),
                "provider_account_id": emp.provider_account_id,
                "provider_session_id": emp.provider_session_id,
            }
        )

    after = await analyze_team_skill_gaps(db, required_skills, divisions=divisions)
    return {
        "before": before,
        "provisioned_employees": provisioned,
        "provisioned_count": len(provisioned),
        "after": after,
    }
