
import pytest

from app.core.config import get_settings
from app.employees.catalog import ensure_employee_workspace
from app.orchestration.replacement import (
    MAX_HANDOFF_FILE_BYTES,
    copy_workspace_handoff,
)
from app.runtime.sandbox import WorkspaceSandboxError, is_sensitive_workspace_path


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".git/config",
        ".ssh/id_rsa",
        "project/secrets/service.json",
        "nested/credentials/token.txt",
        "server.PEM",
        "client.key",
        "C:\\Users\\agent\\.aws\\credentials",
        "",
        "/etc/passwd",
        "../.env",
    ],
)
def test_is_sensitive_workspace_path_rejects_secrets_and_absolute_paths(path):
    assert is_sensitive_workspace_path(path)


@pytest.mark.parametrize("path", ["src/main.py", "docs/README.md", "tests/test_safe.py"])
def test_is_sensitive_workspace_path_allows_regular_project_files(path):
    assert not is_sensitive_workspace_path(path)


def test_ensure_employee_workspace_rejects_escaping_ids_and_symlink_roots(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "agent-link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(get_settings(), "WORKSPACES_DIR", root)

    workspace = ensure_employee_workspace("agent-1")
    assert workspace == (root / "agent-1").resolve()
    assert workspace.is_dir()

    for invalid_id in ("../outside", r"..\outside", "C:\\outside", "nested/name", "nul\x00id", "agent-link"):
        with pytest.raises(WorkspaceSandboxError):
            ensure_employee_workspace(invalid_id)
    assert not (outside / "outside").exists()


def test_copy_workspace_handoff_copies_only_bounded_non_sensitive_files(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspaces"
    source = workspace_root / "worker-a"
    source.mkdir(parents=True)
    (source / "src").mkdir()
    (source / "src" / "module.py").write_text("answer = 42\n", encoding="utf-8")
    (source / "README.md").write_text("handoff notes\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("private repository metadata\n", encoding="utf-8")
    (source / "secrets").mkdir()
    (source / "secrets" / "token.txt").write_text("do-not-copy\n", encoding="utf-8")
    (source / "oversized.bin").write_bytes(b"x" * (MAX_HANDOFF_FILE_BYTES + 1))

    external = tmp_path / "outside.txt"
    external.write_text("outside workspace", encoding="utf-8")
    (source / "linked.txt").symlink_to(external)
    monkeypatch.setattr(get_settings(), "WORKSPACES_DIR", workspace_root)

    handoff_name, copied = copy_workspace_handoff("worker-a", "worker-b")

    assert handoff_name == "handoff_from_worker-a"
    assert set(copied) == {"README.md", "src/module.py"}
    recipient = workspace_root / "worker-b" / handoff_name
    assert (recipient / "README.md").read_text(encoding="utf-8") == "handoff notes\n"
    assert (recipient / "src" / "module.py").read_text(encoding="utf-8") == "answer = 42\n"
    assert not (recipient / ".env").exists()
    assert not (recipient / ".git").exists()
    assert not (recipient / "secrets").exists()
    assert not (recipient / "oversized.bin").exists()
    assert not (recipient / "linked.txt").exists()


def test_copy_workspace_handoff_rejects_unsafe_ids_and_does_not_copy_to_self(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "WORKSPACES_DIR", tmp_path / "workspaces")

    assert copy_workspace_handoff("worker-a", "worker-a") == (None, [])
    for source_id, recipient_id in (("../outside", "worker-b"), ("worker-a", "../outside")):
        with pytest.raises(WorkspaceSandboxError):
            copy_workspace_handoff(source_id, recipient_id)
