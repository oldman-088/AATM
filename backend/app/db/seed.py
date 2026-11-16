from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import KNOWN_DEFAULT_PASSWORDS, get_settings, is_placeholder_password
from app.core.security import hash_password, verify_password
from app.employees.catalog import ensure_employee_workspace
from app.models import (
    Agent,
    AIProvider,
    ApprovalRequest,
    AuditLog,
    BrowserProfile,
    ChatMessage,
    ExecutionDevice,
    GitWorkflowRecord,
    MemoryEntry,
    Notification,
    Project,
    ProviderAccount,
    ProviderSession,
    SystemSetting,
    Task,
    User,
    Userscript,
)


async def seed_initial_data(db: AsyncSession) -> None:
    settings = get_settings()

    # 1. Default Admin User & startup placeholder-password check
    user_res = await db.execute(select(User).where(User.email == settings.DEFAULT_ADMIN_EMAIL))
    if not user_res.scalar_one_or_none():
        db.add(
            User(
                email=settings.DEFAULT_ADMIN_EMAIL,
                full_name="Chief Engineering Director",
                password_hash=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
                role="admin",
                language="en",
                theme="dark",
                must_change_password=True,
                token_version=1,
            )
        )

    # Ensure existing installs whose accounts still use a known default/placeholder
    # password are flagged for mandatory rotation on next login.
    candidates = set(KNOWN_DEFAULT_PASSWORDS)
    if is_placeholder_password(settings.DEFAULT_ADMIN_PASSWORD):
        candidates.add(settings.DEFAULT_ADMIN_PASSWORD)
    unflagged = (
        await db.execute(select(User).where(User.must_change_password.is_(False)))
    ).scalars().all()
    for existing_user in unflagged:
        if any(verify_password(pw, existing_user.password_hash) for pw in candidates):
            existing_user.must_change_password = True

    # 2. Default Project (AATM)
    proj_res = await db.execute(select(Project).where(Project.slug == "aatm-core"))
    project = proj_res.scalar_one_or_none()
    if not project:
        project = Project(
            name="AATM Platform",
            slug="aatm-core",
            description="AI Agent Team Manager — Multi-Agent Engineering Orchestration Platform",
            repo_path=str(settings.REPO_ROOT),
            default_branch="main",
            protected_branches=["main", "master", "production"],
            tech_stack=["FastAPI", "SQLAlchemy", "React", "TypeScript", "Tailwind", "Playwright"],
            config={"max_concurrent_workers": 4, "max_concurrent_browsers": 2},
            status="active",
        )
        db.add(project)
        await db.flush()

    # 3. AI Providers (Division 05)
    providers = [
        AIProvider(
            id="arena-local-bridge",
            name="Arena Local Bridge",
            provider_type="arena_local_bridge",
            endpoint_url=settings.ARENA_BRIDGE_URL,
            credential_ref="env:ARENA_BRIDGE_TOKEN",
            default_model="agent",
            available_models=["agent", "ping"],
            priority=1,
            is_enabled=True,
            health_status="unknown",
            daily_limit_usd=10.0,
        ),
        AIProvider(
            id="arena-agent",
            name="Arena Agent Runtime",
            provider_type="arena_agent",
            endpoint_url="https://arena.ai/agent",
            credential_ref="env:ARENA_SESSION_TOKEN",
            default_model="arena-agent-orchestrator",
            available_models=["arena-agent-orchestrator", "arena-agent-coder"],
            priority=2,
            is_enabled=True,
            health_status="unknown",
            daily_limit_usd=15.0,
        ),
        AIProvider(
            id="anthropic-claude",
            name="Anthropic Claude API",
            provider_type="anthropic_claude",
            endpoint_url="https://api.anthropic.com/v1",
            credential_ref=settings.ANTHROPIC_API_KEY_REF,
            default_model="claude-sonnet-4-5",
            available_models=["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
            priority=3,
            is_enabled=True,
            health_status="unknown",
            daily_limit_usd=settings.DAILY_BUDGET_USD,
        ),
    ]
    for p in providers:
        existing = await db.get(AIProvider, p.id)
        if not existing:
            db.add(p)

    # 4. Browser Profiles (Division 06)
    profiles = [
        BrowserProfile(
            id="profile-backend-docs",
            name="Research & API Docs Profile",
            agent_id="worker-1",
            profile_dir=str(settings.BROWSER_PROFILES_DIR / "profile-backend-docs"),
            mode="background",
            status="ready",
            current_url="https://fastapi.tiangolo.com",
            page_title="FastAPI Documentation",
        ),
        BrowserProfile(
            id="profile-frontend-preview",
            name="Frontend Live Preview & RTL Inspector",
            agent_id="worker-2",
            profile_dir=str(settings.BROWSER_PROFILES_DIR / "profile-frontend-preview"),
            mode="visible",
            status="ready",
            current_url="http://localhost:5173",
            page_title="AATM Dashboard Preview",
        ),
        BrowserProfile(
            id="profile-arena-session",
            name="Arena Web Session Profile",
            agent_id="worker-3",
            profile_dir=str(settings.BROWSER_PROFILES_DIR / "profile-arena-session"),
            mode="background",
            status="ready",
            current_url="https://arena.ai",
            page_title="Arena AI Workspace",
        ),
        BrowserProfile(
            id="profile-qa-e2e",
            name="QA E2E & GitHub Verification Profile",
            agent_id="worker-4",
            profile_dir=str(settings.BROWSER_PROFILES_DIR / "profile-qa-e2e"),
            mode="background",
            status="ready",
            current_url="https://github.com",
            page_title="GitHub PR Verification",
        ),
    ]
    for bp in profiles:
        if not await db.get(BrowserProfile, bp.id):
            db.add(bp)

    # 5. Manager + 4 Specialized Worker Agents (Division 07)
    agents = [
        Agent(
            id="manager-0",
            name="Chief AI Engineering Manager",
            role="manager",
            role_template="ceo_manager",
            specialty="Architecture, Planning, Coordination, Integration & Delivery",
            skills=["strategy", "architecture", "planning", "delegation", "review"],
            divisions=["D01", "D12"],
            permissions=["assign_tasks", "rebalance_workers", "review_code", "request_approval", "orchestrate"],
            status="idle",
            provider_id="arena-agent",
            model_name="arena-agent-orchestrator",
            git_branch="arena/b15bbe5c-aatm",
        ),
        Agent(
            id="worker-1",
            name="Worker 1 — Backend Engineer",
            role="worker",
            role_template="backend_developer",
            specialty="FastAPI, PostgreSQL/SQLAlchemy, Alembic, REST APIs, Auth & Audit",
            skills=["python", "fastapi", "sqlalchemy", "postgresql", "alembic", "rest_api", "auth", "audit"],
            divisions=["D01", "D02", "D09"],
            permissions=["edit_backend", "run_migrations", "run_backend_tests"],
            status="idle",
            provider_id="anthropic-claude",
            model_name="claude-sonnet-4-5",
            browser_profile_id="profile-backend-docs",
            git_branch="feature/backend",
        ),
        Agent(
            id="worker-2",
            name="Worker 2 — Frontend Engineer",
            role="worker",
            role_template="frontend_developer",
            specialty="React, TypeScript, Vite, Tailwind, shadcn/ui, EN/AR/FA & RTL",
            skills=["react", "typescript", "vite", "tailwind", "rtl", "i18n", "accessibility"],
            divisions=["D03", "D04"],
            permissions=["edit_frontend", "run_frontend_tests", "build_ui"],
            status="idle",
            provider_id="anthropic-claude",
            model_name="claude-sonnet-4-5",
            browser_profile_id="profile-frontend-preview",
            git_branch="feature/frontend",
        ),
        Agent(
            id="worker-3",
            name="Worker 3 — AI Provider & Browser Engineer",
            role="worker",
            role_template="ai_engineer",
            specialty="Arena Local Bridge, Claude API, Routing, Playwright & Browser Wall",
            skills=["llm", "arena", "anthropic", "provider_routing", "playwright", "browser_automation"],
            divisions=["D05", "D06"],
            permissions=["configure_providers", "manage_browsers", "run_integration_tests"],
            status="idle",
            provider_id="arena-local-bridge",
            model_name="arena-bridge-default",
            browser_profile_id="profile-arena-session",
            git_branch="feature/ai-browser",
        ),
        Agent(
            id="worker-4",
            name="Worker 4 — Orchestration & QA Engineer",
            role="worker",
            role_template="qa_engineer",
            specialty="DAG Scheduling, Concurrency, GitHub/Worktrees, QA Engine & Self-Healing",
            skills=["dag_scheduling", "concurrency", "github", "worktrees", "qa", "self_healing"],
            divisions=["D07", "D08", "D09", "D10", "D11", "D12"],
            permissions=["schedule_tasks", "manage_worktrees", "run_qa", "recover_workers"],
            status="idle",
            provider_id="arena-agent",
            model_name="arena-agent-coder",
            browser_profile_id="profile-qa-e2e",
            git_branch="feature/orchestration-qa",
        ),
    ]
    for ag in agents:
        existing_agent = await db.get(Agent, ag.id)
        if existing_agent is None:
            db.add(ag)
        else:
            if not existing_agent.role_template and ag.role_template:
                existing_agent.role_template = ag.role_template
            if not existing_agent.skills and ag.skills:
                existing_agent.skills = ag.skills

    # 6. Initial Tasks covering all 12 Divisions
    existing_tasks = await db.execute(select(Task).limit(1))
    if not existing_tasks.scalar_one_or_none():
        division_tasks = [
            ("D01", "System Foundation & Shared Contracts", "worker-1", "completed", "critical", 100),
            ("D02", "FastAPI Core, SQLAlchemy Models, Auth & REST APIs", "worker-1", "completed", "critical", 100),
            ("D03", "Executive Dashboard, Navigation & Management Views", "worker-2", "in_progress", "high", 70),
            ("D04", "Multilingual EN/AR/FA & Full RTL Support", "worker-2", "in_progress", "high", 75),
            ("D05", "AI Provider Adapters (Arena Bridge, Arena Agent, Claude) & Router", "worker-3", "completed", "high", 100),
            ("D06", "Playwright Persistent Browser Profiles & Browser Wall", "worker-3", "completed", "high", 100),
            ("D07", "Manager & 4-Worker Agent Lifecycle & Communication", "worker-4", "completed", "high", 100),
            ("D08", "Dependency DAG Scheduler, Locks, Retries & Recovery", "worker-4", "completed", "critical", 100),
            ("D09", "Multi-Project Builder & 5-Tier Persistent Memory", "worker-1", "completed", "high", 100),
            ("D10", "GitHub Workflows, Worktrees & Protected Branch Guardrails", "worker-4", "completed", "high", 100),
            ("D11", "QA Engine, Security Scanner, Team Intelligence & Self-Healing", "worker-4", "queued", "high", 40),
            ("D12", "Cross-Platform Scripts (Ubuntu & Windows 11) & E2E Delivery", "manager-0", "queued", "critical", 30),
        ]
        for div, title, owner, status, prio, pct in division_tasks:
            db.add(
                Task(
                    project_id=project.id,
                    division_code=div,
                    title=title,
                    description=f"Division {div} core engineering deliverable for AATM.",
                    priority=prio,
                    status=status,
                    assigned_agent_id=owner,
                    progress_percent=pct,
                    idempotency_key=f"seed-{div.lower()}",
                )
            )

    # 7. Seed 5-Tier Memory Entries (Division 09)
    existing_mem = await db.execute(select(MemoryEntry).limit(1))
    if not existing_mem.scalar_one_or_none():
        memories = [
            ("global", None, None, None, "AATM Core Governance Policy", "Protected branches (main, master, production) require Approval Center sign-off before any merge or production deploy.", ["governance", "security", "git"], 10),
            ("project", project.id, None, None, "Dual Database Architecture", "AATM supports PostgreSQL in production and SQLite locally via SQLAlchemy 2.0 + Alembic migrations.", ["database", "architecture"], 9),
            ("agent", project.id, "worker-2", None, "RTL & Multilingual Standard", "All UI views must use Tailwind CSS logical properties (ms-, me-, ps-, pe-) and support English, Arabic, and Persian.", ["i18n", "rtl", "frontend"], 8),
            ("decision", project.id, None, None, "ADR-001: Browser Engine Fallback", "When Chromium binary is missing on host, BrowserManager reports capability status and uses deterministic fallback snapshot rendering.", ["browser", "playwright", "adr"], 9),
            ("task", project.id, "worker-4", None, "Task Scheduler Locking Rule", "Tasks acquire idempotency keys and worker locks before execution to eliminate duplicate worker execution.", ["orchestration", "locks"], 8),
        ]
        for scope, pid, aid, tid, title, content, tags, imp in memories:
            db.add(
                MemoryEntry(
                    scope=scope,
                    project_id=pid,
                    agent_id=aid,
                    task_id=tid,
                    title=title,
                    content=content,
                    tags=tags,
                    importance=imp,
                    created_by="manager-0",
                )
            )

    # 8. Seed Initial Manager Chat & Approval & Git Record
    existing_chat = await db.execute(select(ChatMessage).limit(1))
    if not existing_chat.scalar_one_or_none():
        db.add(
            ChatMessage(
                project_id=project.id,
                sender_type="manager",
                sender_id="manager-0",
                recipient_id="user",
                content="Chief AI Engineering Manager online. All 4 Worker Agents initialized across 12 Divisions. Ready to coordinate parallel execution, monitor Browser Wall, and enforce Approval Center policies.",
                metadata_json={"divisions_active": 12, "workers_online": 4},
            )
        )

    existing_git = await db.execute(select(GitWorkflowRecord).limit(1))
    if not existing_git.scalar_one_or_none():
        db.add(
            GitWorkflowRecord(
                project_id=project.id,
                branch_name="arena/b15bbe5c-aatm",
                worktree_path=str(settings.REPO_ROOT),
                owner_agent_id="manager-0",
                base_branch="main",
                pr_status="open",
                review_status="pending",
                checks_passed=True,
                is_protected_target=True,
            )
        )

    existing_app = await db.execute(select(ApprovalRequest).limit(1))
    if not existing_app.scalar_one_or_none():
        db.add(
            ApprovalRequest(
                project_id=project.id,
                action_type="merge",
                title="Authorize Release Merge to Protected Branch `main`",
                description="Worker 4 requests executive authorization before merging verified feature work into protected branch `main`.",
                requested_by="worker-4",
                estimated_cost_usd=0.0,
                payload={"source_branch": "arena/b15bbe5c-aatm", "target_branch": "main"},
                status="pending",
            )
        )

    existing_notif = await db.execute(select(Notification).limit(1))
    if not existing_notif.scalar_one_or_none():
        db.add(
            Notification(
                category="orchestration",
                severity="info",
                title="AATM Four-Worker Team Ready",
                message="Backend, Frontend, AI/Browser, and Orchestration/QA workers are registered and synchronized.",
                link="/agents",
            )
        )

    # 9. System Settings
    if not await db.get(SystemSetting, "budgets"):
        db.add(
            SystemSetting(
                key="budgets",
                value={"daily_limit_usd": settings.DAILY_BUDGET_USD, "monthly_limit_usd": settings.MONTHLY_BUDGET_USD, "require_approval_above_usd": 5.0},
                description="AI provider spend controls and approval thresholds",
            )
        )
    if not await db.get(SystemSetting, "concurrency"):
        db.add(
            SystemSetting(
                key="concurrency",
                value={"max_workers": settings.MAX_CONCURRENT_WORKERS, "max_browsers": settings.MAX_CONCURRENT_BROWSERS, "stalled_task_minutes": settings.STALLED_TASK_MINUTES},
                description="Hardware-aware concurrency limits",
            )
        )
    if not await db.get(SystemSetting, "autonomy"):
        db.add(
            SystemSetting(
                key="autonomy",
                value={"mode": "semi_autonomous", "prompt_injection_guard": True, "require_approval_for_protected_merge": True},
                description="Global autonomy policy and safety controls",
            )
        )

    # 10. Execution Device, Provider Accounts/Sessions, Userscripts & Workspaces
    if not await db.get(ExecutionDevice, "device-local"):
        import os
        import platform

        db.add(
            ExecutionDevice(
                id="device-local",
                name="Primary AATM Control Node",
                device_type="local",
                host_address="127.0.0.1",
                os_name=platform.system(),
                cpu_cores=os.cpu_count() or 2,
                ram_gb=4.0,
                capabilities=["fastapi", "python", "node", "playwright", "git"],
                status="online",
                max_concurrent_tasks=settings.MAX_CONCURRENT_WORKERS,
            )
        )

    if not await db.get(ProviderAccount, "acct-arena-primary"):
        db.add(
            ProviderAccount(
                id="acct-arena-primary",
                provider_id="arena-agent",
                label="Primary Arena Organization Account",
                account_identifier="aatm-org@arena.ai",
                allocation_mode="hybrid",
                max_employees=20,
                credential_ref="env:ARENA_SESSION_TOKEN",
                status="active",
            )
        )
        await db.flush()

    for aid in ("manager-0", "worker-1", "worker-2", "worker-3", "worker-4"):
        ws = ensure_employee_workspace(aid)
        ag = await db.get(Agent, aid)
        if ag:
            ag.workspace_path = str(ws)
            ag.provider_account_id = ag.provider_account_id or "acct-arena-primary"
            if not ag.provider_session_id:
                sid = f"sess-{aid}"
                if not await db.get(ProviderSession, sid):
                    db.add(
                        ProviderSession(
                            id=sid,
                            account_id="acct-arena-primary",
                            agent_id=aid,
                            project_id=project.id,
                            browser_profile_id=ag.browser_profile_id,
                            status="active",
                        )
                    )
                ag.provider_session_id = sid

    if not await db.get(Userscript, "us-aatm-telemetry"):
        db.add(
            Userscript(
                id="us-aatm-telemetry",
                name="AATM Authorized Page Inspector",
                namespace="aatm.tampermonkey",
                version="1.0.0",
                description="Extracts structured page headings & DOM health diagnostics on authorized localhost/docs pages.",
                match_patterns=["http://localhost:*/*", "https://fastapi.tiangolo.com/*"],
                allowed_roles=["worker", "manager"],
                allowed_agent_ids=["worker-2", "worker-3", "worker-4"],
                code="// ==UserScript==\n// @name AATM Authorized Page Inspector\n// @match http://localhost:*/*\n// ==/UserScript==\nconsole.info('AATM Inspector Ready:', document.title);",
                is_enabled=True,
                audit_status="approved",
            )
        )

    db.add(
        AuditLog(
            actor="system",
            action="system.bootstrap",
            resource_type="platform",
            resource_id="aatm",
            details={"version": settings.APP_VERSION},
            severity="info",
        )
    )
    await db.commit()
