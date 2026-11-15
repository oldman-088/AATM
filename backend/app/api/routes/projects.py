import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.git_workflows.manager import PROTECTED_BRANCHES, git_workflow_manager
from app.memory.engine import memory_engine
from app.models import AuditLog, GitWorkflowRecord, MemoryEntry, Project, User
from app.schemas.common import GitWorkflowCreate, MemoryCreate, ProjectCreate

router = APIRouter(tags=["projects-memory-git"])


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
            "status": p.status,
            "created_at": p.created_at,
        }
        for p in res.scalars().all()
    ]


@router.post("/projects", status_code=201)
async def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    slug = re.sub(r"[^a-z0-9-]+", "-", body.name.lower()).strip("-")
    existing = await db.execute(select(Project).where((Project.slug == slug) | (Project.name == body.name)))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Project with this name or slug already exists")
    proj = Project(
        name=body.name,
        slug=slug,
        description=body.description,
        repo_path=body.repo_path,
        repo_url=body.repo_url,
        default_branch=body.default_branch,
        protected_branches=body.protected_branches,
        tech_stack=body.tech_stack,
    )
    db.add(proj)
    db.add(AuditLog(actor=user.email, action="project.create", resource_type="project", resource_id=proj.id, details={"name": proj.name}))
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


@router.post("/memory", status_code=201)
async def create_memory(
    body: MemoryCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entry = MemoryEntry(
        scope=body.scope,
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
    info = git_workflow_manager.inspect_repo(repo_path)
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


@router.post("/git/workflows", status_code=201)
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


@router.post("/git/workflows/{workflow_id}/merge")
async def request_merge(
    workflow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rec = await db.get(GitWorkflowRecord, workflow_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Workflow record not found")
    return await git_workflow_manager.request_protected_merge(db, rec, user.email)
