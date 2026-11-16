"""End-to-end verification suite for the six Manager verification areas.

Covers:
  1. DAG scheduling          (dependency order, cycles, idempotency, locks, priority, retries)
  2. Agent Messaging delivery (delivery, acknowledgement, auto-execution, deliverables)
  3. Isolated workspace execution (traversal, absolute paths, symlinks, tool allowlist)
  4. Smart replacement        (task handover, workspace file transfer, candidate scoring)
  5. Emergency Stop           (global + employee scope, and recovery of halted work)
  6. Secret scan              (coverage of file types, generic secrets, forbidden artifacts)

Plus the governance gate (protected-branch / high-risk approval routing) and the
multi-device worker daemon round trip.
"""

import shutil
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.routes import devices as device_routes
from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import ExecutionDevice, Task
from app.orchestration.replacement import EMERGENCY_STOP_MARKER
from app.providers.base import ProviderResponse
from app.qa.engine import FORBIDDEN_ARTIFACT_RULES, qa_engine
from app.runtime.worker_daemon import WorkerConfig, WorkerDaemon


@pytest.fixture(scope="session", autouse=True)
async def _preserve_alembic_version_stamp():
    yield
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    backend_dir = Path(__file__).resolve().parents[1]
    alembic_cfg = Config(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    head_rev = ScriptDirectory.from_config(alembic_cfg).get_current_head()

    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        if head_rev:
            await conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": head_rev},
            )


@pytest.fixture(scope="module")
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://e2e") as ac:
        yield ac


@pytest.fixture(scope="module")
async def auth_headers(client: AsyncClient):
    settings = get_settings()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.DEFAULT_ADMIN_EMAIL, "password": settings.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
async def project_id(client: AsyncClient, auth_headers: dict):
    projects = (await client.get("/api/v1/projects", headers=auth_headers)).json()
    return projects[0]["id"]


async def _cancel_all_queued(client: AsyncClient, auth_headers: dict) -> None:
    """Clear pre-existing queued work so a test can own the queue deterministically."""
    for task in (await client.get("/api/v1/tasks?status=queued", headers=auth_headers)).json():
        await client.patch(f"/api/v1/tasks/{task['id']}", headers=auth_headers, json={"status": "cancelled"})


# --------------------------------------------------------------------------------------
# 1. DAG scheduling
# --------------------------------------------------------------------------------------
async def test_e2e_dag_dependency_ordering_and_locks(client, auth_headers, project_id):
    await _cancel_all_queued(client, auth_headers)

    root = (
        await client.post(
            "/api/v1/tasks",
            headers=auth_headers,
            json={
                "project_id": project_id,
                "division_code": "D08",
                "title": "DAG root task",
                "priority": "low",
                "execution_mode": "automatic",
            },
        )
    ).json()
    child = (
        await client.post(
            "/api/v1/tasks",
            headers=auth_headers,
            json={
                "project_id": project_id,
                "division_code": "D08",
                "title": "DAG dependent task",
                "priority": "critical",
                "execution_mode": "automatic",
                "dependencies": [root["id"]],
            },
        )
    ).json()
    assert child["dependencies"] == [root["id"]]

    # A dependency on a task that does not exist must be rejected.
    ghost = await client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "division_code": "D08",
            "title": "DAG ghost dependency",
            "dependencies": ["task-that-does-not-exist"],
        },
    )
    assert ghost.status_code == 400

    await client.post("/api/v1/orchestration/cycle", headers=auth_headers)
    in_progress = {t["id"] for t in (await client.get("/api/v1/tasks?status=in_progress", headers=auth_headers)).json()}

    # The dependent task must NOT be dispatched before its dependency completed,
    # even though it carries a higher priority weight.
    assert root["id"] in in_progress
    assert child["id"] not in in_progress

    # A dispatched task holds an exclusive lock.
    locked = [t for t in (await client.get("/api/v1/tasks?status=in_progress", headers=auth_headers)).json() if t["lock_owner"]]
    assert locked, "dispatched task must hold a lock"

    # Completing the dependency unlocks the child for the next cycle.
    await client.patch(f"/api/v1/tasks/{root['id']}", headers=auth_headers, json={"status": "completed"})
    await client.patch(f"/api/v1/tasks/{child['id']}", headers=auth_headers, json={"status": "queued"})
    await client.post("/api/v1/orchestration/cycle", headers=auth_headers)
    after = {t["id"] for t in (await client.get("/api/v1/tasks?status=in_progress", headers=auth_headers)).json()}
    assert child["id"] in after


async def test_e2e_dag_idempotency_and_retry(client, auth_headers, project_id):
    key = "e2e-idempotency-key-1"
    first = (
        await client.post(
            "/api/v1/tasks",
            headers=auth_headers,
            json={
                "project_id": project_id,
                "division_code": "D08",
                "title": "Idempotent task",
                "idempotency_key": key,
            },
        )
    ).json()
    second = (
        await client.post(
            "/api/v1/tasks",
            headers=auth_headers,
            json={
                "project_id": project_id,
                "division_code": "D08",
                "title": "Idempotent task submitted twice",
                "idempotency_key": key,
            },
        )
    ).json()
    assert first["id"] == second["id"], "the same idempotency key must not create a second task"

    # A failure below the retry budget requeues the task and counts the attempt.
    failed = (
        await client.patch(
            f"/api/v1/tasks/{first['id']}",
            headers=auth_headers,
            json={"status": "failed", "error_message": "e2e induced failure"},
        )
    ).json()
    assert failed["status"] == "queued"
    assert failed["retry_count"] == 1


# --------------------------------------------------------------------------------------
# 2. Agent Messaging delivery
# --------------------------------------------------------------------------------------
async def test_e2e_agent_messaging_delivery(client, auth_headers):
    resp = await client.post(
        "/api/v1/messages",
        headers=auth_headers,
        json={
            "sender_id": "manager-0",
            "recipient_id": "worker-1",
            "instructions": "Produce the E2E verification summary.",
            "expected_deliverables": ["e2e_verification.md"],
            "auto_execute": True,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["execution_status"] == "completed"
    assert body["delivery_status"] == "acknowledged"
    assert body["acked_at"] is not None

    files = (await client.get("/api/v1/workspaces/worker-1/files", headers=auth_headers)).json()["files"]
    assert any(f["path"] == "e2e_verification.md" for f in files)

    # A message for an unknown recipient is a client error, not a 500.
    unknown = await client.post(
        "/api/v1/messages",
        headers=auth_headers,
        json={
            "sender_id": "manager-0",
            "recipient_id": "employee-does-not-exist",
            "instructions": "This recipient does not exist.",
        },
    )
    assert unknown.status_code == 400

    # The message bus records the delivery for audit.
    messages = (await client.get("/api/v1/messages", headers=auth_headers)).json()
    assert any(m["recipient_id"] == "worker-1" for m in messages)


# --------------------------------------------------------------------------------------
# 3. Isolated workspace execution
# --------------------------------------------------------------------------------------
async def test_e2e_workspace_isolation(client, auth_headers):
    settings = get_settings()

    for path in (
        "../../etc/passwd",
        "/etc/passwd",
        "a/b/../../../../../../etc/shadow",
        "./../../data/other-employee/secret.txt",
    ):
        resp = await client.post(
            "/api/v1/workspaces/worker-1/files",
            headers=auth_headers,
            json={"relative_path": path, "content": "escape attempt"},
        )
        assert resp.status_code == 403, f"path traversal not blocked for {path!r}"

    # A symlink that points outside the workspace must not become an escape hatch.
    workspace = Path(settings.WORKSPACES_DIR) / "worker-1"
    link = workspace / "e2e_outside_link"
    link.unlink(missing_ok=True)
    link.symlink_to("/etc")
    try:
        resp = await client.post(
            "/api/v1/workspaces/worker-1/files",
            headers=auth_headers,
            json={"relative_path": "e2e_outside_link/e2e_probe", "content": "escape attempt"},
        )
        assert resp.status_code == 403, "symlink escape not blocked"
    finally:
        link.unlink(missing_ok=True)

    # Only allowlisted developer tools may run.
    denied = await client.post(
        "/api/v1/workspaces/worker-1/exec",
        headers=auth_headers,
        json={"command": ["bash", "-c", "echo escape"]},
    )
    assert denied.status_code == 403

    allowed = await client.post(
        "/api/v1/workspaces/worker-1/exec",
        headers=auth_headers,
        json={"command": ["ls", "-la"]},
    )
    assert allowed.status_code == 200
    assert allowed.json()["exit_code"] == 0

    # One employee's workspace is invisible to another.
    await client.post(
        "/api/v1/workspaces/worker-2/files",
        headers=auth_headers,
        json={"relative_path": "private_note.txt", "content": "worker-2 only"},
    )
    worker1_files = [f["path"] for f in (await client.get("/api/v1/workspaces/worker-1/files", headers=auth_headers)).json()["files"]]
    assert "private_note.txt" not in worker1_files


# --------------------------------------------------------------------------------------
# 4. Smart replacement
# --------------------------------------------------------------------------------------
async def test_e2e_smart_replacement(client, auth_headers, project_id):
    employee = (
        await client.post(
            "/api/v1/employees",
            headers=auth_headers,
            json={
                "name": "E2E Replacement Target",
                "role_template_id": "backend_developer",
                "create_browser_profile": False,
            },
        )
    ).json()
    await client.post(
        f"/api/v1/workspaces/{employee['id']}/files",
        headers=auth_headers,
        json={"relative_path": "e2e_wip.txt", "content": "in-progress work state"},
    )
    await client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "division_code": "D07",
            "title": "E2E handoff task",
            "execution_mode": "manual",
            "assigned_agent_id": employee["id"],
        },
    )

    replacement = await client.post(f"/api/v1/employees/{employee['id']}/replace", headers=auth_headers)
    assert replacement.status_code == 200
    data = replacement.json()
    assert data["replaced_employee"] == employee["id"]
    assert data["replacement_employee"] != employee["id"]
    assert len(data["transferred_tasks"]) >= 1
    assert "e2e_wip.txt" in data["transferred_files"]

    # The failing employee is taken offline and the work now belongs to the replacement.
    agents = {a["id"]: a for a in (await client.get("/api/v1/agents", headers=auth_headers)).json()}
    assert agents[employee["id"]]["status"] == "offline"
    tasks = {t["id"]: t for t in (await client.get("/api/v1/tasks", headers=auth_headers)).json()}
    for task_id in data["transferred_tasks"]:
        assert tasks[task_id]["assigned_agent_id"] == data["replacement_employee"]

    # The handoff files are really present in the replacement workspace.
    replacement_files = [
        f["path"] for f in (await client.get(f"/api/v1/workspaces/{data['replacement_employee']}/files", headers=auth_headers)).json()["files"]
    ]
    assert any("e2e_wip.txt" in p for p in replacement_files)


# --------------------------------------------------------------------------------------
# 5. Emergency Stop
# --------------------------------------------------------------------------------------
async def test_e2e_emergency_stop_global_scope_and_recovery(client, auth_headers, project_id):
    await _cancel_all_queued(client, auth_headers)
    for stale in (await client.get("/api/v1/tasks?status=in_progress", headers=auth_headers)).json():
        await client.patch(f"/api/v1/tasks/{stale['id']}", headers=auth_headers, json={"status": "cancelled"})
    for agent in (await client.get("/api/v1/agents", headers=auth_headers)).json():
        if agent["role"] == "worker" and agent["status"] != "idle":
            await client.post(f"/api/v1/agents/{agent['id']}/restart", headers=auth_headers)
    task = (
        await client.post(
            "/api/v1/tasks",
            headers=auth_headers,
            json={
                "project_id": project_id,
                "division_code": "D11",
                "title": "E2E emergency stop task",
                "execution_mode": "automatic",
            },
        )
    ).json()
    await client.post("/api/v1/orchestration/cycle", headers=auth_headers)
    assert task["id"] in {t["id"] for t in (await client.get("/api/v1/tasks?status=in_progress", headers=auth_headers)).json()}

    stop = await client.post(
        "/api/v1/emergency-stop",
        headers=auth_headers,
        json={"scope": "global", "reason": "E2E safety drill"},
    )
    assert stop.status_code == 200
    assert stop.json()["agents"] >= 1

    # In-flight work is halted, not silently dropped.
    halted = {t["id"]: t for t in (await client.get("/api/v1/tasks?status=blocked", headers=auth_headers)).json()}
    assert task["id"] in halted
    assert halted[task["id"]]["error_message"].startswith(EMERGENCY_STOP_MARKER)

    # Clearing the stop must return the halted work to the queue - otherwise the task is
    # stranded forever, because the scheduler only dispatches "queued" tasks.
    cleared = await client.post("/api/v1/emergency-stop/clear", headers=auth_headers)
    assert cleared.status_code == 200
    assert cleared.json()["resumed_agents"] >= 1
    assert cleared.json()["resumed_tasks"] >= 1

    recovered = (await client.get("/api/v1/tasks", headers=auth_headers)).json()
    still_blocked = [t for t in recovered if t["status"] == "blocked"]
    assert not still_blocked, f"emergency-stopped tasks must not stay orphaned: {still_blocked}"
    assert next(t for t in recovered if t["id"] == task["id"])["status"] == "queued"


async def test_e2e_emergency_stop_employee_scope_halts_own_task(client, auth_headers, project_id):
    await _cancel_all_queued(client, auth_headers)
    task = (
        await client.post(
            "/api/v1/tasks",
            headers=auth_headers,
            json={
                "project_id": project_id,
                "division_code": "D11",
                "title": "E2E scoped stop task",
                "execution_mode": "automatic",
            },
        )
    ).json()

    # Force a deterministic in-progress state owned by worker-1.
    async with AsyncSessionLocal() as db:
        row = await db.get(Task, task["id"])
        row.status = "in_progress"
        row.assigned_agent_id = "worker-1"
        row.lock_owner = "worker-1"
        await db.commit()

    stop = await client.post(
        "/api/v1/emergency-stop",
        headers=auth_headers,
        json={"scope": "employee", "target_id": "worker-1", "reason": "E2E scoped drill"},
    )
    assert stop.status_code == 200
    assert stop.json()["agents"] == 1
    assert stop.json()["tasks"] == 1, "an employee-scoped stop must halt that employee's active task"

    # The task must no longer be left running with a lock owned by a halted agent.
    still_running = [t for t in (await client.get("/api/v1/tasks?status=in_progress", headers=auth_headers)).json() if t["id"] == task["id"]]
    assert not still_running, "employee-scoped emergency stop stranded an in-progress task"

    await client.post("/api/v1/emergency-stop/clear", headers=auth_headers)


# --------------------------------------------------------------------------------------
# 6. Secret scan
# --------------------------------------------------------------------------------------
def _sample_secret(kind: str) -> str:
    """Build a credential-shaped sample for a detection rule.

    The samples are assembled at runtime rather than pasted into the test source, so
    that this test file itself never contains a literal that the scanner would flag
    (and so no scanner blind-spot has to be opened up for test fixtures).
    """
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    filler = "".join(alphabet[i % len(alphabet)] for i in range(48))
    builders = {
        "anthropic": lambda: "sk-ant-" + "api03-" + filler[:24],
        "github": lambda: "ghp_" + filler[:36],
        "aws": lambda: "AKIA" + filler[:16].upper(),
        "slack": lambda: "xox" + "b-" + filler[:12] + "-" + filler[:12],
        "openai": lambda: "sk-pro" + "j-" + filler[:32],
        "password": lambda: "S3cret" + "!Pa" + "ssw0rd",
    }
    return builders[kind]()


def _pem_header(algorithm: str = "RSA ") -> str:
    """A PEM header assembled from fragments so this file holds no literal key block."""
    return "-----BEGIN " + algorithm + "PRIVATE KEY-----"


def _password_assignment() -> str:
    """A `password = '...'` assignment assembled from fragments."""
    key = "pass" + "word"
    return f"{key} = '{_sample_secret('password')}'\n"


async def test_e2e_secret_scan_detects_secrets_across_file_types(client, auth_headers):
    settings = get_settings()
    probe_dir = Path(settings.REPO_ROOT) / "e2e_secret_probe"
    probe_dir.mkdir(exist_ok=True)
    try:
        (probe_dir / "leak.py").write_text(f"KEY = '{_sample_secret('anthropic')}'\n")
        (probe_dir / "leak.sh").write_text(f"export GH_TOKEN={_sample_secret('github')}\n")
        (probe_dir / "leak.env").write_text(f"ANTHROPIC_API_KEY={_sample_secret('anthropic')}\n")
        (probe_dir / "leak.yaml").write_text(f"token: {_sample_secret('github')}\n")
        (probe_dir / "leak.json").write_text('{"aws_key": "%s"}\n' % _sample_secret("aws"))
        (probe_dir / "leak.txt").write_text(f"slack={_sample_secret('slack')}\n")
        (probe_dir / "generic.py").write_text(
            _password_assignment()
            + _pem_header()
            + "\n"
            + f"openai = '{_sample_secret('openai')}'\n"
        )

        scan = qa_engine.run_security_scan()
        found = {f["file"] for f in scan["findings"]}
        for name in ("leak.py", "leak.sh", "leak.env", "leak.yaml", "leak.json", "leak.txt", "generic.py"):
            assert any(f.endswith(name) for f in found), f"secret scanner missed {name}: {sorted(found)}"

        assert scan["status"] == "warning"
        # Findings must never echo the secret value itself.
        assert _sample_secret("password") not in str(scan["findings"])
        assert _sample_secret("anthropic") not in str(scan["findings"])
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    # A clean tree scans clean again.
    clean = qa_engine.run_security_scan()
    assert clean["status"] == "passed", clean["findings"]


async def test_e2e_secret_scan_flags_forbidden_committed_artifacts(client, auth_headers, tmp_path):
    """A committed .env / private key / browser profile must be reported."""
    root = tmp_path / "repo"
    (root / "browser_profiles" / "prof").mkdir(parents=True)
    (root / "browser_profiles" / "prof" / "Cookies").write_text("session=abc")
    (root / ".env").write_text("SECRET=1")
    (root / "server.pem").write_text(_pem_header(""))
    (root / "aatm.db").write_text("sqlite")
    (root / "safe.py").write_text("x = 1\n")

    import subprocess

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)

    found = qa_engine._tracked_files(root)
    assert found is not None
    artifacts = []
    for rel in found:
        for rule_id, _sev, pat in FORBIDDEN_ARTIFACT_RULES:
            if pat.search(rel):
                artifacts.append(rule_id)
                break
    assert "env_file_committed" in artifacts
    assert "private_key_file_committed" in artifacts
    assert "browser_profile_committed" in artifacts
    assert "local_database_committed" in artifacts


# --------------------------------------------------------------------------------------
# Governance gate: high-risk directives must be routed to the Approval Center
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "directive",
    [
        "merge main",
        "merge master into main",
        "merge to production",
        "push to main",
        "force-push to master",
        "deploy prod",
        "delete all data",
        "increase budget",
        "rotate credentials",
    ],
)
async def test_e2e_high_risk_directives_are_gated(client, auth_headers, directive):
    before = len((await client.get("/api/v1/approvals?status=pending", headers=auth_headers)).json())
    resp = await client.post(
        "/api/v1/chat",
        headers=auth_headers,
        json={"content": directive, "recipient_id": "manager-0"},
    )
    assert resp.status_code == 201
    actions = resp.json()["manager_reply"]["metadata"]["actions"]
    assert any("Approval Request" in a for a in actions), f"directive not gated: {directive!r}"
    after = len((await client.get("/api/v1/approvals?status=pending", headers=auth_headers)).json())
    assert after > before


@pytest.mark.parametrize(
    "directive",
    ["merge the feature branch", "build a dashboard for all users", "fix the password reset flow"],
)
async def test_e2e_benign_directives_are_not_gated(client, auth_headers, directive):
    resp = await client.post(
        "/api/v1/chat",
        headers=auth_headers,
        json={"content": directive, "recipient_id": "manager-0"},
    )
    assert resp.status_code == 201
    actions = resp.json()["manager_reply"]["metadata"]["actions"]
    assert not any("Approval Request" in a for a in actions), f"benign directive gated: {directive!r}"


# --------------------------------------------------------------------------------------
# Multi-device worker daemon round trip
# --------------------------------------------------------------------------------------
async def test_e2e_multi_device_worker_daemon_round_trip(client, auth_headers, project_id, tmp_path, monkeypatch):
    model_actions = iter(
        [
            '{"action":"write","path":"remote_deliverable.md","content":"# Remote task\\n\\nCompleted by the isolated worker.\\n"}',
            '{"action":"done","summary":"Created and verified the remote deliverable.","deliverables":["remote_deliverable.md"]}',
        ]
    )
    model_requests = []
    reserved_slot_counts: list[int] = []

    async def mock_model_turn(db, request, *, preferred_provider_id=None):
        model_requests.append((request, preferred_provider_id))
        device = await db.get(ExecutionDevice, "e2e-node-01")
        if device is not None:
            reserved_slot_counts.append(device.active_tasks)
        return ProviderResponse(
            provider_id="arena-agent",
            model="arena-agent-coder",
            content=next(model_actions),
            input_tokens=10,
            output_tokens=10,
            estimated_cost_usd=0.0,
            latency_ms=0.0,
            success=True,
        )

    monkeypatch.setattr(device_routes.provider_router, "route_and_execute", mock_model_turn)
    await _cancel_all_queued(client, auth_headers)

    # 1. An operator registers the execution node and receives its device token.
    reg = await client.post(
        "/api/v1/devices",
        headers=auth_headers,
        json={
            "id": "e2e-node-01",
            "name": "E2E Remote Node",
                "device_type": "remote_server",
                "host_address": "10.0.0.99",
                "os_name": "Ubuntu 24.04",
                "max_concurrent_tasks": 1,
            },
    )
    assert reg.status_code == 201
    device_token = reg.json()["device_token"]
    assert device_token

    # 2. The daemon authenticates. A wrong token must be rejected.
    daemon_transport = ASGITransport(app=app)
    async with AsyncClient(transport=daemon_transport, base_url="http://e2e-node") as daemon_client:
        bad = WorkerDaemon(
            WorkerConfig(device_id="e2e-node-01", token="wrong-token", workspace_root=tmp_path / "bad"),
            client=daemon_client,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await bad.register()

        # 3. The daemon registers, heartbeats, claims a task, executes it and reports back.
        daemon = WorkerDaemon(
            WorkerConfig(
                device_id="e2e-node-01",
                device_name="E2E Remote Node",
                token=device_token,
                workspace_root=tmp_path / "e2e-node-01",
                poll_seconds=0.01,
                max_concurrent=1,
            ),
            client=daemon_client,
        )
        registration = await daemon.register()
        assert registration["bound_agent_id"].startswith("dev-e2e-node-01")

        task = (
            await client.post(
                "/api/v1/tasks",
                headers=auth_headers,
                json={
                    "project_id": project_id,
                    "division_code": "D12",
                    "title": "E2E remote execution task",
                    "description": "Execute this on the remote node.",
                    "expected_deliverables": ["remote_deliverable.md"],
                },
            )
        ).json()

        executed = await daemon.run_once()
        assert executed is not None, "the daemon must claim an eligible task"
        assert executed["id"] == task["id"]
        assert len(model_requests) == 2
        assert all(request.task_id == task["id"] for request, _ in model_requests)
        assert all(provider_id == "arena-agent" for _, provider_id in model_requests)
        assert reserved_slot_counts == [1, 1]

        # 4. The server marks the task complete and records the artifact.
        stored = next(t for t in (await client.get("/api/v1/tasks", headers=auth_headers)).json() if t["id"] == task["id"])
        assert stored["status"] == "completed"
        assert stored["device_id"] == "e2e-node-01"
        assert "remote_deliverable.md" in (stored["actual_deliverables"] or [])
        async with AsyncSessionLocal() as db:
            device_record = await db.get(ExecutionDevice, "e2e-node-01")
            assert device_record is not None
            assert device_record.active_tasks == 0

        # 5. The deliverable really exists inside the remote node's isolated workspace.
        remote_ws = tmp_path / "e2e-node-01"
        assert (remote_ws / "remote_deliverable.md").exists()

        # 6. The daemon's workspace containment holds on the remote node too.
        from app.runtime.sandbox import WorkspaceSandboxError

        with pytest.raises(WorkspaceSandboxError):
            daemon.resolve_path("../../etc/passwd")

        # 7. The device registry reflects the node.
        devices = {d["id"]: d for d in (await client.get("/api/v1/devices", headers=auth_headers)).json()}
        assert devices["e2e-node-01"]["status"] == "online"


async def test_e2e_offline_device_tasks_are_reclaimed(client, auth_headers, project_id, tmp_path):
    """A node that stops reporting must not strand the task it was running."""
    await _cancel_all_queued(client, auth_headers)

    reg = await client.post(
        "/api/v1/devices",
        headers=auth_headers,
        json={
            "id": "e2e-node-offline",
            "name": "E2E Offline Node",
            "device_type": "remote_server",
            "host_address": "10.0.0.100",
        },
    )
    token = reg.json()["device_token"]

    daemon_transport = ASGITransport(app=app)
    async with AsyncClient(transport=daemon_transport, base_url="http://e2e-node") as daemon_client:
        daemon = WorkerDaemon(
            WorkerConfig(
                device_id="e2e-node-offline",
                token=token,
                workspace_root=tmp_path / "offline-node",
            ),
            client=daemon_client,
        )
        await daemon.register()
        task = (
            await client.post(
                "/api/v1/tasks",
                headers=auth_headers,
                json={
                    "project_id": project_id,
                    "division_code": "D12",
                    "title": "E2E stranded task",
                },
            )
        ).json()
        claimed = await daemon.claim()
        assert claimed is not None
        assert claimed["id"] == task["id"]

    # Simulate a host that died mid-task: stale heartbeat, task still in_progress.
    async with AsyncSessionLocal() as db:
        from datetime import timedelta

        from app.db.base import utcnow
        from app.models import ExecutionDevice

        device = await db.get(ExecutionDevice, "e2e-node-offline")
        device.last_heartbeat = utcnow() - timedelta(minutes=30)
        row = await db.get(Task, task["id"])
        row.status = "in_progress"
        await db.commit()

    heal = await client.post("/api/v1/qa/self-heal", headers=auth_headers)
    assert heal.status_code == 200
    assert "e2e-node-offline" in heal.json()["offline_devices"]

    stored = next(t for t in (await client.get("/api/v1/tasks", headers=auth_headers)).json() if t["id"] == task["id"])
    assert stored["status"] == "queued", "a task owned by a dead node must be requeued"


# --------------------------------------------------------------------------------------
# 7. Approval-gated GitHub write helpers (create_branch, create_pull_request, create_issue)
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("protected_branch", ["main", "master", "production"])
async def test_e2e_github_write_protected_branch_refusal_without_approval(monkeypatch, project_id, protected_branch):
    import subprocess as _subprocess

    from app.git_workflows.manager import git_workflow_manager
    from app.github_client.client import github_client
    from app.models import ApprovalRequest

    calls: list[list[str]] = []

    def _forbid_subprocess(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.github_client.client.subprocess.run", _forbid_subprocess)

    pending_approval = ApprovalRequest(
        id=f"app-pending-{protected_branch}",
        project_id=project_id,
        action_type="merge",
        title=f"Pending approval for {protected_branch}",
        description="Not yet approved.",
        requested_by="worker-4",
        status="pending",
    )

    # 1. Protected branch creation without approval or with pending approval must be refused.
    for candidate_approval in (None, pending_approval):
        br_client = github_client.create_branch(
            branch_name=protected_branch,
            base_branch="main",
            repo="oldman-088/AATM",
            approval=candidate_approval,
        )
        assert br_client["allowed"] is False
        assert br_client["ok"] is False
        assert br_client["is_protected_target"] is True
        assert protected_branch in br_client["reason"]

        br_mgr = git_workflow_manager.create_branch(
            branch_name=protected_branch,
            base_branch="main",
            repo="oldman-088/AATM",
            approval=candidate_approval,
        )
        assert br_mgr["allowed"] is False
        assert br_mgr["ok"] is False
        assert br_mgr["is_protected_target"] is True

        # 2. Pull request targeting a protected base branch without approved ApprovalRequest must be refused.
        pr_client = github_client.create_pull_request(
            repo="oldman-088/AATM",
            title=f"Target {protected_branch} PR",
            head="feature/sprint-v2-1",
            base=protected_branch,
            approval=candidate_approval,
        )
        assert pr_client["allowed"] is False
        assert pr_client["ok"] is False
        assert pr_client["is_protected_target"] is True
        assert protected_branch in pr_client["reason"]

        pr_mgr = git_workflow_manager.create_pull_request(
            repo="oldman-088/AATM",
            title=f"Target {protected_branch} PR",
            head="feature/sprint-v2-1",
            base=protected_branch,
            approval=candidate_approval,
        )
        assert pr_mgr["allowed"] is False
        assert pr_mgr["ok"] is False

    assert calls == [], "gh CLI must never be invoked when protected-branch guardrails refuse the write"


async def test_e2e_github_write_repo_allowlist_and_validation(monkeypatch, project_id):
    import subprocess as _subprocess

    from app.github_client.client import github_client
    from app.models import ApprovalRequest

    calls: list[list[str]] = []

    def _forbid_subprocess(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.github_client.client.subprocess.run", _forbid_subprocess)

    approved = ApprovalRequest(
        id="app-approved-allowlist",
        project_id=project_id,
        action_type="merge",
        title="Approved request",
        description="Approved",
        requested_by="worker-4",
        status="approved",
    )

    for bad_repo in ("untrusted-org/rogue-repo", "not-a-repo-slug", "../escape/repo", "-flag/injection"):
        assert github_client.create_branch("feature/x", repo=bad_repo, approval=approved)["allowed"] is False
        assert (
            github_client.create_pull_request(
                repo=bad_repo,
                title="PR",
                head="feature/x",
                base="main",
                approval=approved,
            )["allowed"]
            is False
        )
        assert github_client.create_issue(repo=bad_repo, title="Issue", approval=approved)["allowed"] is False

    for bad_branch in ("../../etc/passwd", "-f", "branch..bad", "branch.lock"):
        assert github_client.create_branch(bad_branch, repo="oldman-088/AATM", approval=approved)["allowed"] is False

    assert calls == [], "disallowed or invalid repos/branches must be rejected before invoking gh"


async def test_e2e_github_write_approval_gated_branch_pr_and_issue_execution(monkeypatch, project_id):
    import subprocess as _subprocess

    from app.git_workflows.manager import git_workflow_manager
    from app.github_client.client import github_client, sanitize_cli_output
    from app.models import ApprovalRequest

    calls: list[list[str]] = []

    def _mock_subprocess_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["gh", "api"] and "--jq" in cmd:
            return _subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="9d63854af0a7226a9d1e0114a59af3f1d87f21af\n",
                stderr="",
            )
        if cmd[:2] == ["gh", "api"] and "POST" in cmd:
            return _subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"ref": "refs/heads/feature/v2-1", "object": {"sha": "9d63854af0a7226a9d1e0114a59af3f1d87f21af"}}',
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "create"]:
            return _subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="https://github.com/oldman-088/AATM/pull/42\n",
                stderr="",
            )
        if cmd[:3] == ["gh", "issue", "create"]:
            return _subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="https://github.com/oldman-088/AATM/issues/17\n",
                stderr="",
            )
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.github_client.client.subprocess.run", _mock_subprocess_run)

    approved = ApprovalRequest(
        id="app-e2e-gh-approved-01",
        project_id=project_id,
        action_type="merge",
        title="Approve Sprint v2.1 GitHub PR & Issue",
        description="Executive approval for GitHub branch, PR, and issue creation.",
        requested_by="worker-4",
        status="approved",
    )

    # 1. Issue creation without approval is refused; with approval it executes `gh issue create`.
    unapproved_issue = git_workflow_manager.create_issue(
        repo="oldman-088/AATM",
        title="Unapproved issue attempt",
        body="Should be blocked",
        approval=None,
    )
    assert unapproved_issue["allowed"] is False
    assert unapproved_issue["ok"] is False
    assert calls == []

    issue_res = git_workflow_manager.create_issue(
        repo="oldman-088/AATM",
        title="Track Sprint v2.1 Verification",
        body="Automated QA verification issue.",
        labels=["qa", "sprint-v2.1"],
        assignees=["worker-4"],
        approval=approved,
    )
    assert issue_res["allowed"] is True
    assert issue_res["ok"] is True
    assert issue_res["approval_id"] == "app-e2e-gh-approved-01"
    assert issue_res["url"] == "https://github.com/oldman-088/AATM/issues/17"
    assert any(c[:3] == ["gh", "issue", "create"] for c in calls)

    # 2. Branch creation (feature branch + protected branch with approval) wraps `gh api`.
    br_res = git_workflow_manager.create_branch(
        branch_name="feature/v2-1",
        base_branch="main",
        repo="oldman-088/AATM",
        approval=approved,
    )
    assert br_res["allowed"] is True
    assert br_res["ok"] is True
    assert br_res["ref"] == "refs/heads/feature/v2-1"
    assert br_res["sha"] == "9d63854af0a7226a9d1e0114a59af3f1d87f21af"

    # 3. Approval-gated PR targeting protected branch `main` executes `gh pr create` once approved.
    pr_res = await git_workflow_manager.create_pull_request(
        repo="oldman-088/AATM",
        title="feat(v2.1): approval-gated GitHub write helpers",
        head="feature/v2-1",
        base="main",
        body="Implements approval-gated branch, PR, and issue helpers.",
        draft=True,
        approval=approved,
    )
    assert pr_res["allowed"] is True
    assert pr_res["ok"] is True
    assert pr_res["is_protected_target"] is True
    assert pr_res["approval_id"] == "app-e2e-gh-approved-01"
    assert pr_res["url"] == "https://github.com/oldman-088/AATM/pull/42"
    pr_cmd = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--repo" in pr_cmd and "oldman-088/AATM" in pr_cmd
    assert "--base" in pr_cmd and "main" in pr_cmd
    assert "--head" in pr_cmd and "feature/v2-1" in pr_cmd
    assert "--draft" in pr_cmd

    # 4. CLI failure output is sanitized so credentials/tokens never leak.
    gh_secret = _sample_secret("github")
    ant_secret = _sample_secret("anthropic")

    def _mock_leaking_error(cmd, *args, **kwargs):
        auth_url = "https://" + "bot:" + gh_secret + "@" + "github.com/oldman-088/AATM.git"
        return _subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr=f"HTTP 401: token {gh_secret} rejected for {auth_url} (key={ant_secret})",
        )

    monkeypatch.setattr("app.github_client.client.subprocess.run", _mock_leaking_error)
    err_res = github_client.create_pull_request(
        repo="oldman-088/AATM",
        title="PR with error",
        head="feature/v2-1",
        base="main",
        approval=approved,
    )
    assert err_res["ok"] is False
    assert gh_secret not in err_res["error"]
    assert ant_secret not in err_res["error"]
    assert "[REDACTED]" in err_res["error"]
    assert gh_secret not in sanitize_cli_output(f"Authorization: Bearer {gh_secret}")


# --------------------------------------------------------------------------------------
# 8. Increment 5: CI/Templates, Security Policy, Device Revocation & Bridge Cleanup
# --------------------------------------------------------------------------------------
async def test_e2e_github_ci_templates_and_security_policy_scan_clean():
    settings = get_settings()
    root = Path(settings.REPO_ROOT)

    ci_workflow = root / ".github" / "workflows" / "ci.yml"
    pr_template = root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    issue_template = root / ".github" / "ISSUE_TEMPLATE" / "task.md"
    security_policy = root / "docs" / "SECURITY_POLICY.md"

    for artifact in (ci_workflow, pr_template, issue_template, security_policy):
        assert artifact.is_file(), f"Required governance artifact missing: {artifact}"
        assert artifact.stat().st_size > 100, f"Governance artifact unexpectedly empty: {artifact}"

    ci_text = ci_workflow.read_text(encoding="utf-8")
    for required_gate in (
        "alembic upgrade head",
        "ruff check backend",
        "ruff check --config backend/pyproject.toml bridge",
        "pytest -v",
        "bridge/tests/test_bridge.py",
        "app.qa.engine",
        "npm run lint",
        "npm test",
        "npm run build",
    ):
        assert required_gate in ci_text, f"CI workflow missing gate: {required_gate}"

    pr_text = pr_template.read_text(encoding="utf-8")
    issue_text = issue_template.read_text(encoding="utf-8")
    assert "AATM_MASTER_SPECIFICATION.md" in pr_text
    assert "AGENTS.md" in pr_text
    assert "AATM_MASTER_SPECIFICATION.md" in issue_text
    assert "AGENTS.md" in issue_text

    sec_text = security_policy.read_text(encoding="utf-8")
    for topic in (
        "Workspace Isolation",
        "validate_credential_ref",
        "sanitize_untrusted_content",
        "ApprovalRequest",
        "token_version",
        "Emergency Stop",
    ):
        assert topic in sec_text, f"SECURITY_POLICY.md missing section/topic: {topic}"

    scan = qa_engine.run_security_scan()
    assert scan["status"] == "passed", f"Secret scanner reported findings: {scan['findings']}"
    assert scan["findings"] == []


async def test_e2e_device_token_revocation_halts_worker_daemon_on_401(
    client, auth_headers, project_id, tmp_path
):
    await _cancel_all_queued(client, auth_headers)

    reg = await client.post(
        "/api/v1/devices",
        headers=auth_headers,
        json={
            "id": "e2e-node-revoke-01",
            "name": "E2E Revocation Node",
            "device_type": "remote_server",
            "host_address": "10.0.0.201",
            "os_name": "Ubuntu 24.04",
        },
    )
    assert reg.status_code == 201
    token_v1 = reg.json()["device_token"]

    daemon_transport = ASGITransport(app=app)
    async with AsyncClient(transport=daemon_transport, base_url="http://e2e-node") as daemon_client:
        daemon = WorkerDaemon(
            WorkerConfig(
                device_id="e2e-node-revoke-01",
                device_name="E2E Revocation Node",
                token=token_v1,
                workspace_root=tmp_path / "e2e-node-revoke-01",
                poll_seconds=0.001,
            ),
            client=daemon_client,
        )
        await daemon.register()
        hb_ok = await daemon.heartbeat()
        assert hb_ok["status"] == "online"
        assert daemon.token_revoked is False

        # Operator revokes the device token; token_version increments and v1 becomes invalid.
        rev = await client.post("/api/v1/devices/e2e-node-revoke-01/revoke", headers=auth_headers)
        assert rev.status_code == 200
        token_v2 = rev.json()["device_token"]
        assert token_v2 != token_v1
        assert rev.json()["token_version"] >= 2

        # Daemon with revoked v1 token must detect HTTP 401 on heartbeat/poll and stop polling cleanly.
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await daemon.run_once()
        assert exc_info.value.response.status_code == 401
        assert daemon.token_revoked is True
        assert daemon.stop_reason is not None and "401" in daemon.stop_reason

        # Starting run_forever with a revoked token also halts immediately without spinning 5 cycles.
        daemon_loop = WorkerDaemon(
            WorkerConfig(
                device_id="e2e-node-revoke-01",
                device_name="E2E Revocation Node",
                token=token_v1,
                workspace_root=tmp_path / "e2e-node-revoke-01",
                poll_seconds=0.001,
            ),
            client=daemon_client,
        )
        await daemon_loop.run_forever(max_cycles=5)
        assert daemon_loop.token_revoked is True
        assert daemon_loop.stop_reason is not None and "401" in daemon_loop.stop_reason
        assert daemon_loop.cycles_completed <= 1

    # A fresh daemon configured with the rotated v2 token is accepted again.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://e2e-node") as daemon_client_v2:
        daemon_v2 = WorkerDaemon(
            WorkerConfig(
                device_id="e2e-node-revoke-01",
                device_name="E2E Revocation Node",
                token=token_v2,
                workspace_root=tmp_path / "e2e-node-revoke-01",
                poll_seconds=0.001,
            ),
            client=daemon_client_v2,
        )
        hb_v2 = await daemon_v2.heartbeat()
        assert hb_v2["status"] == "online"
        assert daemon_v2.token_revoked is False


async def test_e2e_smart_replacement_bridge_session_cleanup(
    client, auth_headers, project_id, monkeypatch
):
    from app.models import ProviderSession
    from app.providers.adapters import ArenaLocalBridgeAdapter

    deleted_calls: list[tuple[str, str | None]] = []

    async def _mock_delete_session_ok(self, session_id: str, account_id: str | None = None):
        deleted_calls.append((session_id, account_id))
        return {
            "deleted": True,
            "session_id": session_id,
            "account_id": account_id,
            "status": "deleted",
            "bridge_reachable": True,
        }

    monkeypatch.setattr(ArenaLocalBridgeAdapter, "delete_session", _mock_delete_session_ok)

    emp_bridge = (
        await client.post(
            "/api/v1/employees",
            headers=auth_headers,
            json={
                "name": "E2E Bridge Employee",
                "role_template_id": "backend_developer",
                "provider_id": "arena-local-bridge",
                "allocation_mode": "one_per_employee",
                "create_browser_profile": False,
            },
        )
    ).json()
    old_session_id = emp_bridge["provider_session_id"]
    assert old_session_id

    rep = await client.post(f"/api/v1/employees/{emp_bridge['id']}/replace", headers=auth_headers)
    assert rep.status_code == 200
    rep_body = rep.json()
    assert rep_body["bridge_session_cleanup"] is not None
    assert rep_body["bridge_session_cleanup"]["deleted"] is True
    assert rep_body["bridge_session_cleanup"]["session_id"] == old_session_id
    assert any(sid == old_session_id for sid, _ in deleted_calls)

    async with AsyncSessionLocal() as db:
        closed_sess = await db.get(ProviderSession, old_session_id)
        assert closed_sess is not None and closed_sess.status == "closed"

    # When the local bridge is offline/unreachable, replacement must still succeed gracefully.
    async def _mock_delete_session_offline(self, session_id: str, account_id: str | None = None):
        raise RuntimeError("Bridge connection refused")

    monkeypatch.setattr(ArenaLocalBridgeAdapter, "delete_session", _mock_delete_session_offline)

    emp_offline = (
        await client.post(
            "/api/v1/employees",
            headers=auth_headers,
            json={
                "name": "E2E Offline Bridge Employee",
                "role_template_id": "backend_developer",
                "provider_id": "arena-local-bridge",
                "allocation_mode": "one_per_employee",
                "create_browser_profile": False,
            },
        )
    ).json()
    rep_offline = await client.post(f"/api/v1/employees/{emp_offline['id']}/replace", headers=auth_headers)
    assert rep_offline.status_code == 200
    cleanup_offline = rep_offline.json()["bridge_session_cleanup"]
    assert cleanup_offline is not None
    assert cleanup_offline["deleted"] is False
    assert cleanup_offline["bridge_reachable"] is False


# --------------------------------------------------------------------------------------
# 9. Round 2: Isolated Per-Agent Git Worktrees & Approval Payload Compatibility
# --------------------------------------------------------------------------------------
async def test_e2e_agent_git_worktree_lifecycle_isolation_and_hygiene(tmp_path):
    import subprocess as _subprocess

    from sqlalchemy import select

    from app.git_workflows.manager import git_workflow_manager
    from app.models import AuditLog

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _subprocess.run(["git", "-C", str(repo), "config", "user.email", "qa@aatm.local"], check=True)
    _subprocess.run(["git", "-C", str(repo), "config", "user.name", "AATM QA"], check=True)
    (repo / "README.md").write_text("# Temporary Repo\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    _subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial commit"], check=True, capture_output=True)

    # 1. Path traversal / unsafe agent_id values must be rejected.
    for bad_agent in ("../../etc/passwd", "../escape", "/abs/path", "-flag", "worker/sub", ""):
        res_bad = git_workflow_manager.create_agent_worktree(bad_agent, "feature/test", repo_path=repo)
        assert res_bad["allowed"] is False
        assert res_bad["ok"] is False
        rm_bad = git_workflow_manager.remove_agent_worktree(bad_agent, repo_path=repo)
        assert rm_bad["allowed"] is False
        assert rm_bad["ok"] is False

    # 2. Invalid branch names and unapproved protected branches must be rejected.
    for bad_branch in ("../../bad", "-f", "branch.lock", "main", "master", "production"):
        res_br = git_workflow_manager.create_agent_worktree("worker-4", bad_branch, repo_path=repo)
        assert res_br["allowed"] is False
        assert res_br["ok"] is False

    # 3. Create isolated worktrees for worker-4 and worker-1 with DB audit logging.
    async with AsyncSessionLocal() as db:
        wt_w4 = await git_workflow_manager.create_agent_worktree(
            "worker-4",
            "feature/w4-worktree",
            base_ref="HEAD",
            db=db,
            repo_path=repo,
        )
        assert wt_w4["allowed"] is True
        assert wt_w4["ok"] is True
        w4_path = Path(wt_w4["worktree_path"])
        assert w4_path == (repo / "data" / "worktrees" / "worker-4").resolve()
        assert w4_path.is_dir()

        wt_w1 = await git_workflow_manager.create_agent_worktree(
            "worker-1",
            "feature/w1-worktree",
            base_ref="HEAD",
            db=db,
            repo_path=repo,
        )
        assert wt_w1["allowed"] is True
        assert wt_w1["ok"] is True
        w1_path = Path(wt_w1["worktree_path"])
        assert w1_path == (repo / "data" / "worktrees" / "worker-1").resolve()

        # Verify file isolation across worktrees and main repo.
        (w4_path / "w4_only.py").write_text(f"TOKEN = '{_sample_secret('github')}'\n", encoding="utf-8")
        assert not (repo / "w4_only.py").exists()
        assert not (w1_path / "w4_only.py").exists()

        # 4. Conflict prevention: duplicate agent worktree or duplicate checked-out branch is refused.
        dup_agent = git_workflow_manager.create_agent_worktree("worker-4", "feature/another", repo_path=repo)
        assert dup_agent["allowed"] is False
        assert dup_agent["ok"] is False

        dup_branch = git_workflow_manager.create_agent_worktree("worker-2", "feature/w4-worktree", repo_path=repo)
        assert dup_branch["allowed"] is False
        assert dup_branch["ok"] is False

        # 5. QA Engine excludes transient local data/worktrees/ runtime files while flagging committed forbidden artifacts.
        scan_clean = qa_engine.run_security_scan(repo_root=repo)
        assert scan_clean["status"] == "passed", scan_clean["findings"]

        (repo / ".env").write_text("S=1\n", encoding="utf-8")
        _subprocess.run(["git", "-C", str(repo), "add", "-f", ".env"], check=True)
        scan_flagged = qa_engine.run_security_scan(repo_root=repo)
        assert scan_flagged["status"] == "warning"
        assert any(f["rule"] == "env_file_committed" for f in scan_flagged["findings"])
        _subprocess.run(["git", "-C", str(repo), "rm", "-f", ".env"], check=True, capture_output=True)

        # 6. Clean teardown of worktrees and verification of AuditLog records.
        rm_w4 = await git_workflow_manager.remove_agent_worktree("worker-4", db=db, repo_path=repo)
        assert rm_w4["ok"] is True
        assert rm_w4["removed"] is True
        assert not w4_path.exists()

        rm_w1 = await git_workflow_manager.remove_agent_worktree("worker-1", db=db, repo_path=repo)
        assert rm_w1["ok"] is True
        assert not w1_path.exists()

        logs = (
            await db.execute(select(AuditLog).where(AuditLog.resource_type == "git_worktree"))
        ).scalars().all()
        actions = {lg.action for lg in logs}
        assert "git.worktree_create" in actions
        assert "git.worktree_remove" in actions


async def test_e2e_github_verify_approval_target_branch_and_base_payload_compatibility(
    monkeypatch, project_id
):
    import subprocess as _subprocess

    from app.git_workflows.manager import git_workflow_manager
    from app.github_client.client import verify_approval
    from app.models import ApprovalRequest

    monkeypatch.setattr(
        "app.github_client.client.subprocess.run",
        lambda cmd, *a, **kw: _subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="https://github.com/oldman-088/AATM/pull/99\n",
            stderr="",
        ),
    )

    for payload_key in ("target_branch", "base", "base_branch", "target"):
        approval = ApprovalRequest(
            id=f"app-compat-{payload_key}",
            project_id=project_id,
            action_type="merge",
            title=f"Approve PR via {payload_key}",
            description="Payload compatibility verification",
            requested_by="worker-4",
            payload={payload_key: "main", "repo": "oldman-088/AATM"},
            status="approved",
        )
        ok, _reason, resolved_id = verify_approval(approval, target_branch="main", repo="oldman-088/AATM")
        assert ok is True
        assert resolved_id == f"app-compat-{payload_key}"

        pr_ok = git_workflow_manager.create_pull_request(
            repo="oldman-088/AATM",
            title="Compatible approval payload PR",
            head="feature/compat-check",
            base="main",
            approval=approval,
        )
        assert pr_ok["allowed"] is True
        assert pr_ok["ok"] is True

        # Using an approval scoped to `main` for `production` must be rejected.
        pr_mismatch = git_workflow_manager.create_pull_request(
            repo="oldman-088/AATM",
            title="Mismatched protected target PR",
            head="feature/compat-check",
            base="production",
            approval=approval,
        )
        assert pr_mismatch["allowed"] is False
        assert pr_mismatch["ok"] is False
        assert "production" in pr_mismatch["reason"]


# ---------------------------------------------------------------------------
# 23. Round 3: QA Self-Healing Orphaned Worktree Pruning & POST /github/branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_e2e_self_healing_prunes_orphaned_worktrees_and_preserves_active(
    client, auth_headers, tmp_path, monkeypatch
):
    """Verify `qa_engine.run_self_healing(db)` (via `POST /api/v1/qa/self-heal`)
    automatically prunes git worktrees under `data/worktrees/` whose `agent_id`
    either belongs to an `offline` / `replaced` employee or does not exist in
    `agents`, while preserving active agents' worktrees.
    """
    import subprocess as _subprocess

    from app.core.config import get_settings
    from app.git_workflows.manager import git_workflow_manager

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    _subprocess.run(
        ["git", "config", "user.email", "aatm-test@aatm.local"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    _subprocess.run(
        ["git", "config", "user.name", "AATM Test"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    (repo_dir / "README.md").write_text("# Self-Healing Worktree Pruning Test\n", encoding="utf-8")
    _subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
    _subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )

    # Create an employee and then replace it so the original employee transitions to status="offline".
    emp_resp = await client.post(
        "/api/v1/employees",
        headers=auth_headers,
        json={
            "name": "Transient Worktree Worker",
            "title": "Transient Engineer",
            "role": "backend",
            "department": "Engineering",
            "system_prompt": "Test worker",
        },
    )
    assert emp_resp.status_code == 201, emp_resp.text
    offline_emp_id = emp_resp.json()["id"]

    replace_resp = await client.post(
        f"/api/v1/employees/{offline_emp_id}/replace",
        headers=auth_headers,
        json={"reason": "Replaced before self-healing sweep"},
    )
    assert replace_resp.status_code == 200, replace_resp.text
    replacement_emp_id = replace_resp.json()["replacement_employee"]

    # Create 3 isolated worktrees under repo_dir / "data" / "worktrees":
    # 1) Active employee (replacement_emp_id) -> must be preserved
    # 2) Replaced/offline employee (offline_emp_id) -> must be pruned
    # 3) Non-existent ghost agent ("ghost-orphan-worker") -> must be pruned
    wt_active = await git_workflow_manager.create_agent_worktree(
        replacement_emp_id,
        "feature/active-worker",
        base_ref="main",
        repo_path=repo_dir,
    )
    wt_offline = await git_workflow_manager.create_agent_worktree(
        offline_emp_id,
        "feature/offline-worker",
        base_ref="main",
        repo_path=repo_dir,
    )
    wt_ghost = await git_workflow_manager.create_agent_worktree(
        "ghost-orphan-worker",
        "feature/ghost-worker",
        base_ref="main",
        repo_path=repo_dir,
    )
    assert wt_active["ok"] is True
    assert wt_offline["ok"] is True
    assert wt_ghost["ok"] is True

    active_path = Path(wt_active["worktree_path"])
    offline_path = Path(wt_offline["worktree_path"])
    ghost_path = Path(wt_ghost["worktree_path"])
    assert active_path.is_dir()
    assert offline_path.is_dir()
    assert ghost_path.is_dir()

    settings = get_settings()
    monkeypatch.setattr(settings, "REPO_ROOT", repo_dir)
    monkeypatch.setattr(settings, "DATA_DIR", repo_dir / "data")

    heal_resp = await client.post("/api/v1/qa/self-heal", headers=auth_headers)
    assert heal_resp.status_code == 200, heal_resp.text
    heal_data = heal_resp.json()
    assert "pruned_worktrees" in heal_data
    assert offline_emp_id in heal_data["pruned_worktrees"]
    assert "ghost-orphan-worker" in heal_data["pruned_worktrees"]
    assert replacement_emp_id not in heal_data["pruned_worktrees"]

    # Orphaned worktrees were removed from disk; active employee's worktree remains intact.
    assert not offline_path.exists()
    assert not ghost_path.exists()
    assert active_path.is_dir()

    # Clean up remaining active worktree
    cleanup = await git_workflow_manager.remove_agent_worktree(
        replacement_emp_id, repo_path=repo_dir
    )
    assert cleanup["ok"] is True


@pytest.mark.asyncio
async def test_e2e_github_branches_endpoint_enforces_branch_approval_and_payload_match(
    client, auth_headers, project_id, monkeypatch
):
    """Verify `POST /api/v1/github/branches` enforces `action_type="branch"` approval
    and exact payload matching (`repo`, `branch_name`, `from_branch`), rejecting
    missing/wrong-type/pending/mismatched requests with 403 and succeeding (201)
    with a mocked `gh` client once approved.
    """
    import subprocess as _subprocess

    gh_calls: list[list[str]] = []

    def _fake_gh_run(cmd, *args, **kwargs):
        gh_calls.append(list(cmd))
        joined = " ".join(cmd)
        if "git/ref/heads/" in joined:
            return _subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="deadbeefcafebabe1234567890\n", stderr=""
            )
        if "git/refs" in joined:
            return _subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"ref": "refs/heads/feature/round3-verified"}\n',
                stderr="",
            )
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="{}\n", stderr="")

    monkeypatch.setattr("app.github_client.client.subprocess.run", _fake_gh_run)

    # 1. Missing approval_id -> 403
    missing_resp = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": "nonexistent-approval-id",
            "repo": "oldman-088/AATM",
            "branch_name": "feature/round3-verified",
            "from_branch": "main",
        },
    )
    assert missing_resp.status_code == 403

    # 2. Approval with wrong action_type ("merge" instead of "branch"), even when approved -> 403
    wrong_type_create = await client.post(
        "/api/v1/approvals",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "action_type": "merge",
            "title": "Merge approval misused for branch create",
            "description": "Should be rejected by /github/branches",
            "requested_by": "worker-4",
            "payload": {
                "repo": "oldman-088/AATM",
                "branch_name": "feature/round3-verified",
                "from_branch": "main",
            },
        },
    )
    assert wrong_type_create.status_code == 201
    wrong_type_id = wrong_type_create.json()["id"]
    await client.post(
        f"/api/v1/approvals/{wrong_type_id}/decide",
        headers=auth_headers,
        json={"decision": "approved", "review_note": "Approved merge"},
    )
    wrong_type_resp = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": wrong_type_id,
            "repo": "oldman-088/AATM",
            "branch_name": "feature/round3-verified",
            "from_branch": "main",
        },
    )
    assert wrong_type_resp.status_code == 403

    # 3. Create valid `action_type="branch"` approval; while still pending -> 403
    branch_app_create = await client.post(
        "/api/v1/approvals",
        headers=auth_headers,
        json={
            "project_id": project_id,
            "action_type": "branch",
            "title": "Create branch feature/round3-verified from main",
            "description": "Round 3 governed branch creation test",
            "requested_by": "worker-4",
            "payload": {
                "repo": "oldman-088/AATM",
                "branch_name": "feature/round3-verified",
                "from_branch": "main",
            },
        },
    )
    assert branch_app_create.status_code == 201
    branch_app_id = branch_app_create.json()["id"]

    pending_resp = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": branch_app_id,
            "repo": "oldman-088/AATM",
            "branch_name": "feature/round3-verified",
            "from_branch": "main",
        },
    )
    assert pending_resp.status_code == 403

    # Approve the branch creation request
    decide_resp = await client.post(
        f"/api/v1/approvals/{branch_app_id}/decide",
        headers=auth_headers,
        json={"decision": "approved", "review_note": "Approved branch creation"},
    )
    assert decide_resp.status_code == 200

    # 4. Payload mismatch checks (mismatched branch_name, from_branch, or repo) -> 403
    mismatch_branch = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": branch_app_id,
            "repo": "oldman-088/AATM",
            "branch_name": "feature/tampered-branch",
            "from_branch": "main",
        },
    )
    assert mismatch_branch.status_code == 403

    mismatch_base = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": branch_app_id,
            "repo": "oldman-088/AATM",
            "branch_name": "feature/round3-verified",
            "from_branch": "develop",
        },
    )
    assert mismatch_base.status_code == 403

    # Ensure zero `gh` calls occurred during all rejected attempts
    assert gh_calls == []

    # 5. Matching approved request -> 201 Created
    ok_resp = await client.post(
        "/api/v1/github/branches",
        headers=auth_headers,
        json={
            "approval_id": branch_app_id,
            "repo": "oldman-088/AATM",
            "branch_name": "feature/round3-verified",
            "from_branch": "main",
        },
    )
    assert ok_resp.status_code == 201, ok_resp.text
    ok_data = ok_resp.json()
    assert ok_data["ok"] is True
    assert ok_data["allowed"] is True
    assert ok_data["branch_name"] == "feature/round3-verified"
    assert ok_data["base_branch"] == "main"
    assert ok_data["ref"] == "refs/heads/feature/round3-verified"
    assert ok_data["approval_id"] == branch_app_id
    assert len(gh_calls) == 2


# ---------------------------------------------------------------------------
# 24. Round 4: PostgreSQL URL Normalization, Alembic Offline SQL DDL & Hardened Config
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_e2e_postgresql_url_normalization_and_alembic_sql_ddl_compilation(monkeypatch):
    """Verify PostgreSQL URL normalization (`postgres://`, `postgresql://`,
    `postgresql+asyncpg://`, `postgresql+psycopg2://` -> `postgresql+psycopg://`),
    async engine construction via `_make_engine()`, and Alembic offline (`sql=True`)
    DDL compilation across the entire migration chain on PostgreSQL dialect.
    """
    import io

    from alembic.config import Config as AlembicConfig

    from alembic import command
    from app.core.config import (
        Settings,
        normalize_async_database_url,
        normalize_database_url,
        normalize_sync_database_url,
    )
    from app.db.session import _make_engine

    expected_pg = "postgresql+psycopg://aatm@localhost:5432/aatm"
    for raw_pg in (
        "postgres://aatm@localhost:5432/aatm",
        "postgresql://aatm@localhost:5432/aatm",
        "postgresql+asyncpg://aatm@localhost:5432/aatm",
        "postgresql+psycopg2://aatm@localhost:5432/aatm",
        "postgresql+psycopg://aatm@localhost:5432/aatm",
    ):
        assert normalize_async_database_url(raw_pg) == expected_pg
        assert normalize_sync_database_url(raw_pg) == expected_pg
        assert normalize_database_url(raw_pg, async_driver=True) == expected_pg
        assert normalize_database_url(raw_pg, async_driver=False) == expected_pg

    assert (
        normalize_async_database_url("sqlite:///./data/aatm.db")
        == "sqlite+aiosqlite:///./data/aatm.db"
    )
    assert (
        normalize_sync_database_url("sqlite+aiosqlite:///./data/aatm.db")
        == "sqlite:///./data/aatm.db"
    )

    pg_settings = Settings(DATABASE_URL="postgres://aatm@localhost:5432/aatm")
    assert pg_settings.get_async_database_url() == expected_pg
    assert pg_settings.get_sync_database_url() == expected_pg

    pg_engine = _make_engine("postgres://aatm@localhost:5432/aatm")
    try:
        assert pg_engine.dialect.name == "postgresql"
        assert pg_engine.dialect.driver == "psycopg"
    finally:
        await pg_engine.dispose()

    # Compile full Alembic migration chain in offline `sql=True` mode using PostgreSQL dialect.
    backend_dir = Path(__file__).resolve().parents[1]
    alembic_cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", "postgres://aatm@localhost:5432/aatm")
    ddl_buffer = io.StringIO()
    alembic_cfg.attributes["output_buffer"] = ddl_buffer

    command.upgrade(alembic_cfg, "head", sql=True)
    compiled_ddl = ddl_buffer.getvalue()

    assert "CREATE TABLE agents" in compiled_ddl
    assert "CREATE TABLE ai_providers" in compiled_ddl
    assert "CREATE TABLE agent_messages" in compiled_ddl
    assert "CREATE TABLE execution_devices" in compiled_ddl
    assert "ALTER TABLE execution_devices ADD COLUMN token_version" in compiled_ddl
    assert "ALTER TABLE agents ADD COLUMN reports_to" in compiled_ddl
    assert "a56f5804a0ab" in compiled_ddl
    assert "ee92483cefa3" in compiled_ddl
    assert "6c3ab8f4d2e1" in compiled_ddl
    assert "629b77662bf2" in compiled_ddl


def test_e2e_round4_settings_env_and_hardened_docker_compose_worker():
    """Verify `GITHUB_WEBHOOK_SECRET` and `AATM_SANDBOX_MODE` in `Settings` and
    `.env.example`, and verify the hardened `worker` container definition in
    `docker-compose.yml`.
    """
    from app.core.config import ROOT_DIR, Settings

    s = Settings(
        GITHUB_WEBHOOK_SECRET="whsec-test-only",
        AATM_SANDBOX_MODE="docker",
    )
    assert s.GITHUB_WEBHOOK_SECRET == "whsec-test-only"
    assert s.AATM_GITHUB_WEBHOOK_SECRET == "whsec-test-only"
    assert s.AATM_SANDBOX_MODE == "docker"
    assert s.SANDBOX_MODE == "docker"

    env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    assert "GITHUB_WEBHOOK_SECRET=" in env_example
    assert "AATM_SANDBOX_MODE=docker" in env_example

    compose_text = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "postgres:" in compose_text
    assert "worker:" in compose_text
    assert "read_only: true" in compose_text
    assert "no-new-privileges:true" in compose_text
    assert "cap_drop:" in compose_text
    assert "pids_limit: 64" in compose_text
    assert "AATM_SANDBOX_MODE" in compose_text
