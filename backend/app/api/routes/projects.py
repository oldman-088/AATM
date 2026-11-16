import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.config import get_settings
from app.db.session import get_db
from app.git_workflows.manager import PROTECTED_BRANCHES, git_workflow_manager
from app.memory.engine import memory_engine
from app.models import AuditLog, GitWorkflowRecord, MemoryEntry, Project, User
from app.schemas.common import GitWorkflowCreate, MemoryCreate, ProjectCreate, ProjectUpdate

router = APIRouter(tags=["projects-memory-git"], dependencies=[Depends(get_current_user)])


def _resolve_repo_candidate(raw_path: str, repo_root: Path) -> Path:
    p = Path(raw_path)
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()


async def _validate_registered_repo_path(repo_path: str | None, db: AsyncSession) -> str:
    settings = get_settings()
    repo_root = settings.REPO_ROOT.resolve()
    if repo_path is None or not repo_path.strip() or repo_path.strip() == ".":
        return str(repo_root)

    candidate = _resolve_repo_candidate(repo_path.strip(), repo_root)
    allowed: set[Path] = {repo_root}
    rows = (await db.execute(select(Project.repo_path))).scalars().all()
    for registered in rows:
        if registered and str(registered).strip():
            allowed.add(_resolve_repo_candidate(str(registered).strip(), repo_root))

    if candidate not in allowed:
        raise HTTPException(
            status_code=403,
            detail="repo_path is not a registered project repository or REPO_ROOT.",
        )
    return str(candidate)


@router.get("/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Project).order_by(Project.created_at.asc()))
    return [
        {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "description": p.description,
            "repo_path": p.repo_path,
            "repo_url": p.repo_url,
            "default_branch": p.default_branch,
            "protected_branches": p.protected_branches,
            "tech_stack": p.tech_stack,
            "config": p.config,
            "status": p.status,
            "created_at": p.created_at,
        }
        for p in res.scalars().all()
    ]


@router.post("/projects", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    slug = re.sub(r"[^a-z0-9-]+", "-", body.name.lower()).strip("-")
    existing = await db.execute(select(Project).where((Project.slug == slug) | (Project.name == body.name)))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Project with this name or slug already exists")
    repo_dir = _resolve_repo_candidate(body.repo_path, get_settings().REPO_ROOT)
    worktree_mode = (
        body.worktree_mode
        if body.worktree_mode is not None
        else ((repo_dir / ".git").exists() and repo_dir != get_settings().REPO_ROOT.resolve())
    )
    proj = Project(
        name=body.name,
        slug=slug,
        description=body.description,
        repo_path=body.repo_path,
        repo_url=body.repo_url,
        default_branch=body.default_branch,
        protected_branches=body.protected_branches,
        tech_stack=body.tech_stack,
        config={"worktree_mode": bool(worktree_mode), "auto_pr": bool(body.auto_pr)},
    )
    db.add(proj)
    db.add(AuditLog(actor=user.email, action="project.create", resource_type="project", resource_id=proj.id, details={"name": proj.name}))
    await db.commit()
    await db.refresh(proj)
    return proj


@router.patch("/projects/{project_id}", dependencies=[Depends(require_role("admin", "operator"))])
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await db.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    cfg = dict(proj.config or {})
    if body.description is not None:
        proj.description = body.description
    if body.repo_url is not None:
        proj.repo_url = body.repo_url
    if body.default_branch is not None:
        proj.default_branch = body.default_branch
    if body.worktree_mode is not None:
        cfg["worktree_mode"] = body.worktree_mode
    if body.auto_pr is not None:
        cfg["auto_pr"] = body.auto_pr
    proj.config = cfg
    db.add(AuditLog(actor=user.email, action="project.update", resource_type="project", resource_id=proj.id, details=body.model_dump(exclude_none=True)))
    await db.commit()
    await db.refresh(proj)
    return proj


@router.get("/memory")
async def search_memory(
    q: str = "",
    scope: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    return await memory_engine.search(db, query=q, scope=scope, project_id=project_id, agent_id=agent_id)


@router.post("/memory", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_memory(
    body: MemoryCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entry = MemoryEntry(
        scope=body.scope,
        visibility=body.visibility,
        project_id=body.project_id,
        agent_id=body.agent_id,
        task_id=body.task_id,
        title=body.title,
        content=body.content,
        tags=body.tags,
        importance=body.importance,
        created_by=user.email,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/git/status")
async def get_git_status(repo_path: str | None = None, db: AsyncSession = Depends(get_db)):
    validated_path = await _validate_registered_repo_path(repo_path, db)
    info = git_workflow_manager.inspect_repo(validated_path)
    recs = list((await db.execute(select(GitWorkflowRecord).order_by(GitWorkflowRecord.created_at.desc()))).scalars().all())
    return {
        "repository": info,
        "workflows": [
            {
                "id": r.id,
                "project_id": r.project_id,
                "branch_name": r.branch_name,
                "worktree_path": r.worktree_path,
                "owner_agent_id": r.owner_agent_id,
                "base_branch": r.base_branch,
                "pr_status": r.pr_status,
                "review_status": r.review_status,
                "checks_passed": r.checks_passed,
                "is_protected_target": r.is_protected_target,
            }
            for r in recs
        ],
    }


@router.post("/git/workflows", status_code=201, dependencies=[Depends(require_role("admin", "operator"))])
async def create_git_workflow(
    body: GitWorkflowCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rec = GitWorkflowRecord(
        project_id=body.project_id,
        branch_name=body.branch_name,
        base_branch=body.base_branch,
        owner_agent_id=body.owner_agent_id,
        worktree_path=body.worktree_path,
        is_protected_target=body.base_branch.lower() in PROTECTED_BRANCHES,
    )
    db.add(rec)
    db.add(AuditLog(actor=user.email, action="git.workflow_create", resource_type="git_workflow", resource_id=rec.id, details={"branch": rec.branch_name}))
    await db.commit()
    await db.refresh(rec)
    return rec


@router.post("/git/workflows/{workflow_id}/merge", dependencies=[Depends(require_role("admin", "operator"))])
async def request_merge(
    workflow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rec = await db.get(GitWorkflowRecord, workflow_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Workflow record not found")
    return await git_workflow_manager.request_protected_merge(db, rec, user.email)
