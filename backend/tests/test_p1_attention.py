"""P1: "Needs you now" lists everything that waits for the owner, each with the screen to open."""

# ruff: noqa: F811 - pytest fixtures are re-exported from test_e_dashboard
from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from test_e_dashboard import _client_headers, env  # noqa: F401 - fixture

from app.main import app
from app.models import ApprovalRequest, BrowserProfile, ManagerJob, Project, Task


async def _attention(factory) -> dict:
    headers = await _client_headers(factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/dashboard/attention", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_every_kind_of_waiting_item_is_listed_with_its_screen(env):
    before = {i["id"] for i in (await _attention(env))["items"]}
    async with env() as db:
        project = Project(name="Shop", slug="shop-attention", default_branch="main")
        db.add(project)
        await db.flush()
        rows = [
            ApprovalRequest(action_type="merge", title="Merge or push into a protected branch", description="d",
                            requested_by="manager-0", status="pending"),
            ManagerJob(kind="plan", status="draft", request_text="Build reports"),
            ManagerJob(kind="review", status="needs_human", request_text="review", error="invalid JSON"),
            ManagerJob(kind="plan", status="waiting_connection", request_text="hidden",
                       context={"last_error_code": "page_hidden", "same_error_count": 1}),
            ManagerJob(kind="plan", status="waiting_connection", request_text="short wait",
                       context={"last_error_code": "composer_not_found", "same_error_count": 1}),
            ManagerJob(kind="plan", status="done", request_text="finished"),
            BrowserProfile(id="p-human", name="worker-3", profile_dir="/tmp/p", connector_status="needs_human",
                           needs_human_reason="human check"),
            Task(project_id=project.id, title="Stuck task", description="d", status="blocked", error_message="session closed"),
        ]
        db.add_all(rows)
        await db.commit()
        ids = {r.id: r for r in rows}
    data = await _attention(env)
    new = {i["id"]: i for i in data["items"] if i["id"] not in before}
    by_title = {i["title"]: i for i in new.values()}
    assert by_title["Merge or push into a protected branch"]["tab"] == "approvals"
    assert by_title["Build reports"]["kind"] == "plan_draft" and by_title["Build reports"]["tab"] == "chat"
    assert by_title["review"]["kind"] == "manager_needs_human" and by_title["review"]["tab"] == "approvals"
    assert by_title["hidden"]["kind"] == "manager_stuck"
    assert by_title["worker-3"]["tab"] == "browsers" and by_title["worker-3"]["detail"] == "human check"
    assert by_title["Stuck task"]["tab"] == "tasks"
    assert "short wait" not in by_title and "finished" not in by_title
    assert len(new) == 6 and data["count"] >= 6
    assert set(new) <= set(ids)


async def test_a_repository_the_arena_app_cannot_see_is_listed(env):
    from app.db.base import utcnow
    from app.models import Project

    async with env() as db:
        db.add(Project(name="Private", slug="private-attention", default_branch="main",
                       config={"repo_access_denied": {"worker-2": {"at": utcnow().isoformat(), "repo": "acme/private"}}}))
        await db.commit()
    item = next(i for i in (await _attention(env))["items"] if i["kind"] == "repo_access")
    assert item["title"] == "acme/private" and item["detail"] == "worker-2" and item["tab"] == "browsers"
