"""GitHub page: add any repository of any signed-in account; it joins the allowlist and runs as that account."""

# ruff: noqa: F811 - pytest fixtures are re-exported from other test modules
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_e14_project_picker import _client, chat_app  # noqa: F401 - fixture

from app.api.routes import github_repos
from app.core.async_subprocess import CommandResult
from app.github_client import accounts as gh_accounts
from app.github_client.client import github_client
from app.models import AuditLog, Project

ACCOUNTS = [{"host": "github.com", "login": "alice", "active": True, "scopes": ["repo"]},
            {"host": "github.com", "login": "bob", "active": False, "scopes": ["repo"]}]


def _check(repo: str, account: str, permission: str = "WRITE") -> dict:
    return {"repo": repo, "account": account, "exists": True, "private": True, "archived": False,
            "url": f"https://github.com/{repo}", "default_branch": "trunk", "description": "demo",
            "permission": permission, "can_write": permission in {"ADMIN", "MAINTAIN", "WRITE"}}


@pytest.fixture(autouse=True)
def _fake_github(monkeypatch):
    async def accounts():
        return ACCOUNTS

    async def check(repo, account):
        return _check(repo, account, "READ" if repo.endswith("/readonly") else "WRITE")

    monkeypatch.setattr(gh_accounts, "list_accounts", accounts)
    monkeypatch.setattr(github_repos, "check_repository", check)
    gh_accounts.set_registry([])
    yield
    gh_accounts.set_registry([])


@pytest.mark.parametrize("raw", ["acme/widgets", "https://github.com/acme/widgets", "https://github.com/acme/widgets.git",
                                 "git@github.com:acme/widgets.git", "github.com/acme/widgets/"])
def test_parse_repo_accepts_slugs_and_links(raw) -> None:
    assert github_repos.parse_repo(raw) == "acme/widgets"


@pytest.mark.parametrize("raw", ["", "acme", "../etc/passwd", "-x/y", "https://gitlab.com/a/b c"])
def test_parse_repo_rejects_garbage(raw) -> None:
    with pytest.raises(HTTPException):
        github_repos.parse_repo(raw)


def test_repo_detected_in_gh_arguments() -> None:
    assert gh_accounts.repo_in_args(["pr", "merge", "5", "--repo", "acme/widgets"]) == "acme/widgets"
    assert gh_accounts.repo_in_args(["api", "repos/acme/widgets/commits/main/check-runs"]) == "acme/widgets"
    assert gh_accounts.repo_in_args(["auth", "status"]) is None


@pytest.mark.asyncio
async def test_registered_repo_runs_as_its_account(monkeypatch) -> None:
    calls = []

    async def fake_exec(argv, **kwargs):
        calls.append(argv)
        return CommandResult(returncode=0, stdout="tok-bob\n", stderr="")

    monkeypatch.setattr(gh_accounts, "run_exec", fake_exec)
    gh_accounts._token_cache.clear()
    gh_accounts.set_registry([{"repo": "Acme/Widgets", "account": "bob"}])
    env = await gh_accounts.env_for_gh_args(["pr", "view", "1", "--repo", "acme/widgets"])
    assert env is not None and env["GH_TOKEN"] == "tok-bob"
    assert calls == [["gh", "auth", "token", "--user", "bob"]]
    assert await gh_accounts.env_for_gh_args(["pr", "view", "1", "--repo", "other/repo"]) is None
    gh_accounts._token_cache.clear()


@pytest.mark.asyncio
async def test_adding_a_repository_creates_its_project_and_allows_it(chat_app) -> None:
    assert not github_client.validate_repo("acme/widgets")[0]
    async with await _client(chat_app) as client:
        response = await client.post("/api/v1/github/repositories",
                                     json={"repo": "https://github.com/acme/widgets", "account": "bob"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["project_created"] and body["repository"]["account"] == "bob"
        listed = (await client.get("/api/v1/github/repositories")).json()["repositories"]
    assert listed[0]["repo"] == "acme/widgets" and listed[0]["project"]["name"] == "widgets"
    assert github_client.validate_repo("acme/widgets")[0]
    assert gh_accounts.account_for("acme/widgets") == "bob"
    async with chat_app() as db:
        project = (await db.execute(select(Project).where(Project.name == "widgets"))).scalar_one()
        assert project.repo_url == "https://github.com/acme/widgets" and project.default_branch == "trunk"
        assert project.status == "active" and project.config["github_account"] == "bob"
        assert (await db.execute(select(AuditLog).where(AuditLog.action == "github.repository.add"))).scalars().first()


@pytest.mark.asyncio
async def test_repository_without_write_access_or_unknown_account_is_refused(chat_app) -> None:
    async with await _client(chat_app) as client:
        no_write = await client.post("/api/v1/github/repositories", json={"repo": "acme/readonly", "account": "alice"})
        stranger = await client.post("/api/v1/github/repositories", json={"repo": "acme/widgets", "account": "mallory"})
    assert no_write.status_code == 409 and "cannot push" in no_write.json()["detail"]
    assert stranger.status_code == 404
    assert gh_accounts.registered_repos() == set()


@pytest.mark.asyncio
async def test_removing_a_repository_keeps_its_project(chat_app) -> None:
    async with await _client(chat_app) as client:
        assert (await client.post("/api/v1/github/repositories", json={"repo": "acme/widgets", "account": "alice"})).status_code == 201
        assert (await client.delete("/api/v1/github/repositories/acme/widgets")).status_code == 200
        assert (await client.delete("/api/v1/github/repositories/acme/widgets")).status_code == 404
    assert gh_accounts.registered_repos() == set()
    async with chat_app() as db:
        assert (await db.execute(select(Project).where(Project.name == "widgets"))).scalar_one_or_none() is not None
