"""GitHub page: the accounts signed in to the GitHub CLI and the repositories the owner works on.

GET    /github/accounts                    accounts (login, active, scopes) — never tokens
GET    /github/accounts/{login}/repos      repositories that account can see
POST   /github/repositories/check          does the repository exist, can the account write to it
GET    /github/repositories                the owner's registry, with the linked project
POST   /github/repositories                add a repository (needs write access); optionally create its project
DELETE /github/repositories/{owner}/{name} remove it from the registry (the project is kept)

Every write is audited. A registered repository joins the GitHub allowlist, and its `gh` commands run as
the chosen account (see app.github_client.accounts).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.async_subprocess import run_exec
from app.db.session import get_db
from app.github_client import accounts as gh_accounts
from app.github_client.client import sanitize_cli_output
from app.models import AuditLog, Project, SystemSetting, User

router = APIRouter(prefix="/github", tags=["github"])

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com[/:]([^/\s]+/[^/\s#?]+?)(?:\.git)?/?(?:[#?].*)?$", re.I)
WRITE_PERMISSIONS = {"ADMIN", "MAINTAIN", "WRITE"}


def parse_repo(value: str | None) -> str:
    """Accept `owner/name`, https://github.com/owner/name(.git) or git@github.com:owner/name.git."""
    text = (value or "").strip()
    m = _URL_RE.match(text) or _URL_RE.match(text.replace("git@github.com:", "github.com/"))
    slug = m.group(1) if m else text
    slug = slug.removesuffix(".git").strip("/")
    if slug.startswith("-") or ".." in slug or not _SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=422, detail="Enter a repository as owner/name or a github.com link.")
    return slug


async def _require_account(login: str) -> dict:
    if not gh_accounts.valid_login(login):
        raise HTTPException(status_code=422, detail="invalid account")
    for account in await gh_accounts.list_accounts():
        if account["login"].lower() == login.lower():
            return account
    raise HTTPException(status_code=404, detail=f"account {login} is not signed in to the GitHub CLI on this machine")


async def _gh_json(args: list[str], account: str) -> tuple[bool, object | str]:
    env = await gh_accounts.env_for_account(account)
    if env is None:
        return False, "could not use this account's GitHub CLI credentials"
    result = await run_exec(["gh", *args], env=env, timeout=30)
    if result.returncode != 0:
        return False, sanitize_cli_output((result.stderr or result.stdout or "").strip())[:500]
    try:
        return True, json.loads(result.stdout or "null")
    except ValueError:
        return False, "unexpected answer from GitHub"


async def check_repository(repo: str, account: str) -> dict:
    ok, data = await _gh_json(
        ["repo", "view", repo, "--json", "nameWithOwner,isPrivate,url,defaultBranchRef,viewerPermission,description,isArchived"],
        account,
    )
    if not ok or not isinstance(data, dict):
        return {"repo": repo, "account": account, "exists": False, "can_write": False, "error": data}
    permission = str(data.get("viewerPermission") or "")
    return {
        "repo": data.get("nameWithOwner") or repo,
        "account": account,
        "exists": True,
        "private": bool(data.get("isPrivate")),
        "archived": bool(data.get("isArchived")),
        "url": data.get("url") or f"https://github.com/{repo}",
        "default_branch": (data.get("defaultBranchRef") or {}).get("name") or "main",
        "description": (data.get("description") or "")[:300],
        "permission": permission,
        "can_write": permission in WRITE_PERMISSIONS and not data.get("isArchived"),
    }


async def _load_registry(db: AsyncSession) -> list[dict]:
    setting = await db.get(SystemSetting, gh_accounts.SETTING_KEY)
    value = setting.value if setting else None
    entries = value.get("repositories", []) if isinstance(value, dict) else []
    return [e for e in entries if isinstance(e, dict) and e.get("repo")]


async def _save_registry(db: AsyncSession, entries: list[dict]) -> None:
    setting = await db.get(SystemSetting, gh_accounts.SETTING_KEY)
    value = {"repositories": entries}
    if setting:
        setting.value = value
    else:
        db.add(SystemSetting(key=gh_accounts.SETTING_KEY, value=value,
                             description="Repositories added on the GitHub page and the account that works on each"))


async def load_registry_into_memory(db: AsyncSession) -> None:
    gh_accounts.set_registry(await _load_registry(db))


def _project_for(projects: list[Project], repo: str) -> Project | None:
    target = repo.lower()
    for p in projects:
        url = (p.repo_url or "").lower().removesuffix(".git").rstrip("/")
        if url.endswith(f"github.com/{target}"):
            return p
    return None


class CheckBody(BaseModel):
    repo: str
    account: str


class AddBody(BaseModel):
    repo: str
    account: str
    create_project: bool = True
    project_name: str | None = Field(default=None, max_length=140)
    description: str = Field(default="", max_length=2000)


@router.get("/accounts")
async def github_accounts(_: User = Depends(get_current_user)):
    return {"accounts": await gh_accounts.list_accounts()}


@router.get("/accounts/{login}/repos")
async def github_account_repos(login: str, limit: int = 100, _: User = Depends(get_current_user)):
    await _require_account(login)
    ok, data = await _gh_json(
        ["repo", "list", login, "--limit", str(max(1, min(limit, 200))),
         "--json", "nameWithOwner,isPrivate,url,updatedAt,description,isArchived"],
        login,
    )
    if not ok:
        return {"available": False, "repositories": [], "error": data}
    repos = [r for r in (data or []) if isinstance(r, dict) and not r.get("isArchived")]
    return {"available": True, "repositories": repos}


@router.post("/repositories/check", dependencies=[Depends(require_role("admin", "operator"))])
async def github_check_repository(body: CheckBody):
    repo = parse_repo(body.repo)
    await _require_account(body.account)
    return await check_repository(repo, body.account)


@router.get("/repositories")
async def github_repositories(_: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    entries = await _load_registry(db)
    projects = list((await db.execute(select(Project))).scalars().all())
    out = []
    for e in entries:
        p = _project_for(projects, e["repo"])
        out.append({**e, "project": {"id": p.id, "name": p.name, "status": p.status} if p else None})
    return {"repositories": out}


@router.post("/repositories", status_code=201)
async def github_add_repository(
    body: AddBody,
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    repo = parse_repo(body.repo)
    await _require_account(body.account)
    check = await check_repository(repo, body.account)
    if not check["exists"]:
        raise HTTPException(status_code=404, detail=f"{body.account} cannot see {repo}: {check.get('error') or 'not found'}")
    if not check["can_write"]:
        raise HTTPException(status_code=409, detail=f"{body.account} cannot push to {repo} (permission: {check.get('permission') or 'none'})")
    repo = check["repo"]

    entries = [e for e in await _load_registry(db) if e["repo"].lower() != repo.lower()]
    entries.append({"repo": repo, "account": body.account, "default_branch": check["default_branch"],
                    "private": check["private"], "added_by": user.email, "added_at": datetime.now(UTC).isoformat()})
    await _save_registry(db, entries)

    project = _project_for(list((await db.execute(select(Project))).scalars().all()), repo)
    created = False
    if body.create_project and project is None:
        name = (body.project_name or repo.split("/", 1)[1]).strip()[:140]
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or re.sub(r"[^a-z0-9-]+", "-", repo.lower())
        clash = (await db.execute(select(Project).where((Project.slug == slug) | (Project.name == name)))).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=409, detail=f"a project named {name} already exists; choose another project name")
        project = Project(name=name, slug=slug, description=body.description or check["description"],
                          repo_url=check["url"], default_branch=check["default_branch"], status="active",
                          config={"worktree_mode": False, "auto_pr": True, "github_account": body.account})
        db.add(project)
        created = True
    db.add(AuditLog(actor=user.email, action="github.repository.add", resource_type="github_repository", resource_id=repo[:64],
                    details={"account": body.account, "project_created": created}))
    await db.commit()
    gh_accounts.set_registry(entries)
    return {"repository": {**entries[-1]}, "check": check,
            "project": {"id": project.id, "name": project.name, "status": project.status} if project else None,
            "project_created": created}


@router.delete("/repositories/{owner}/{name}")
async def github_remove_repository(
    owner: str,
    name: str,
    user: User = Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    repo = parse_repo(f"{owner}/{name}")
    entries = await _load_registry(db)
    kept = [e for e in entries if e["repo"].lower() != repo.lower()]
    if len(kept) == len(entries):
        raise HTTPException(status_code=404, detail="repository is not in the list")
    await _save_registry(db, kept)
    db.add(AuditLog(actor=user.email, action="github.repository.remove", resource_type="github_repository", resource_id=repo[:64],
                    details={}, severity="warning"))
    await db.commit()
    gh_accounts.set_registry(kept)
    return {"removed": repo}
