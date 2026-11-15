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

from app.core.config import get_settings
from app.db.base import Base
from app.db.seed import seed_initial_data
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models import Task
from app.orchestration.replacement import EMERGENCY_STOP_MARKER
from app.qa.engine import FORBIDDEN_ARTIFACT_RULES, qa_engine
from app.runtime.worker_daemon import WorkerConfig, WorkerDaemon


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
async def project_id(client: AsyncClient):
    projects = (await client.get("/api/v1/projects")).json()
    return projects[0]["id"]


async def _cancel_all_queued(client: AsyncClient, auth_headers: dict) -> None:
    """Clear pre-existing queued work so a test can own the queue deterministically."""
    for task in (await client.get("/api/v1/tasks?status=queued")).json():
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
    in_progress = {t["id"] for t in (await client.get("/api/v1/tasks?status=in_progress")).json()}

    # The dependent task must NOT be dispatched before its dependency completed,
    # even though it carries a higher priority weight.
    assert root["id"] in in_progress
    assert child["id"] not in in_progress

    # A dispatched task holds an exclusive lock.
    locked = [t for t in (await client.get("/api/v1/tasks?status=in_progress")).json() if t["lock_owner"]]
    assert locked, "dispatched task must hold a lock"

    # Completing the dependency unlocks the child for the next cycle.
    await client.patch(f"/api/v1/tasks/{root['id']}", headers=auth_headers, json={"status": "completed"})
    await client.patch(f"/api/v1/tasks/{child['id']}", headers=auth_headers, json={"status": "queued"})
    await client.post("/api/v1/orchestration/cycle", headers=auth_headers)
    after = {t["id"] for t in (await client.get("/api/v1/tasks?status=in_progress")).json()}
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

    files = (await client.get("/api/v1/workspaces/worker-1/files")).json()["files"]
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
    messages = (await client.get("/api/v1/messages")).json()
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
    worker1_files = [f["path"] for f in (await client.get("/api/v1/workspaces/worker-1/files")).json()["files"]]
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
    agents = {a["id"]: a for a in (await client.get("/api/v1/agents")).json()}
    assert agents[employee["id"]]["status"] == "offline"
    tasks = {t["id"]: t for t in (await client.get("/api/v1/tasks")).json()}
    for task_id in data["transferred_tasks"]:
        assert tasks[task_id]["assigned_agent_id"] == data["replacement_employee"]

    # The handoff files are really present in the replacement workspace.
    replacement_files = [
        f["path"] for f in (await client.get(f"/api/v1/workspaces/{data['replacement_employee']}/files")).json()["files"]
    ]
    assert any("e2e_wip.txt" in p for p in replacement_files)


# --------------------------------------------------------------------------------------
# 5. Emergency Stop
# --------------------------------------------------------------------------------------
async def test_e2e_emergency_stop_global_scope_and_recovery(client, auth_headers, project_id):
    await _cancel_all_queued(client, auth_headers)
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
    assert task["id"] in {t["id"] for t in (await client.get("/api/v1/tasks?status=in_progress")).json()}

    stop = await client.post(
        "/api/v1/emergency-stop",
        headers=auth_headers,
        json={"scope": "global", "reason": "E2E safety drill"},
    )
    assert stop.status_code == 200
    assert stop.json()["agents"] >= 1

    # In-flight work is halted, not silently dropped.
    halted = {t["id"]: t for t in (await client.get("/api/v1/tasks?status=blocked")).json()}
    assert task["id"] in halted
    assert halted[task["id"]]["error_message"].startswith(EMERGENCY_STOP_MARKER)

    # Clearing the stop must return the halted work to the queue - otherwise the task is
    # stranded forever, because the scheduler only dispatches "queued" tasks.
    cleared = await client.post("/api/v1/emergency-stop/clear", headers=auth_headers)
    assert cleared.status_code == 200
    assert cleared.json()["resumed_agents"] >= 1
    assert cleared.json()["resumed_tasks"] >= 1

    recovered = (await client.get("/api/v1/tasks")).json()
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
    still_running = [t for t in (await client.get("/api/v1/tasks?status=in_progress")).json() if t["id"] == task["id"]]
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
    before = len((await client.get("/api/v1/approvals?status=pending")).json())
    resp = await client.post(
        "/api/v1/chat",
        headers=auth_headers,
        json={"content": directive, "recipient_id": "manager-0"},
    )
    assert resp.status_code == 201
    actions = resp.json()["manager_reply"]["metadata"]["actions"]
    assert any("Approval Request" in a for a in actions), f"directive not gated: {directive!r}"
    after = len((await client.get("/api/v1/approvals?status=pending")).json())
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
async def test_e2e_multi_device_worker_daemon_round_trip(client, auth_headers, project_id, tmp_path):
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

        # 4. The server marks the task complete and records the artifact.
        stored = next(t for t in (await client.get("/api/v1/tasks")).json() if t["id"] == task["id"])
        assert stored["status"] == "completed"
        assert stored["device_id"] == "e2e-node-01"
        assert "remote_deliverable.md" in (stored["actual_deliverables"] or [])

        # 5. The deliverable really exists inside the remote node's isolated workspace.
        remote_ws = tmp_path / "e2e-node-01"
        assert (remote_ws / "remote_deliverable.md").exists()

        # 6. The daemon's workspace containment holds on the remote node too.
        from app.runtime.sandbox import WorkspaceSandboxError

        with pytest.raises(WorkspaceSandboxError):
            daemon.resolve_path("../../etc/passwd")

        # 7. The device registry reflects the node.
        devices = {d["id"]: d for d in (await client.get("/api/v1/devices")).json()}
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

    stored = next(t for t in (await client.get("/api/v1/tasks")).json() if t["id"] == task["id"])
    assert stored["status"] == "queued", "a task owned by a dead node must be requeued"
