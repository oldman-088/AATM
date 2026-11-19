import importlib.util
import json
import stat
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "admin_login.py"


def test_login_stores_only_token_with_expiry_and_private_permissions(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("admin_login_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.getpass, "getpass", lambda _: "private-password")
    monkeypatch.setattr(module, "request_login", lambda *_: {"access_token": "test-token", "user": {"role": "admin"}})
    monkeypatch.setattr(module, "token_expiry", lambda _: 4102444800)
    target = tmp_path / "token.json"
    module.login("admin@example.invalid", "http://127.0.0.1:8000", target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text()) == {"access_token": "test-token", "expires_at": 4102444800}
    assert "private-password" not in target.read_text()


def test_reset_informs_env_was_not_updated():
    assert ".env was NOT updated" in (path.parent / "reset-admin-password.sh").read_text()
