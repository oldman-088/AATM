import importlib.util
import sys
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "aatm_stack.py"
spec = importlib.util.spec_from_file_location("aatm_stack_test", path)
stack = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = stack
spec.loader.exec_module(stack)


def test_connected_loading_window_is_not_ready(monkeypatch):
    window = stack.Window("p1", "manager", "Manager", "https://arena.ai/agent")
    monkeypatch.setattr(stack, "enabled_windows", lambda: [window])
    monkeypatch.setattr(stack, "_http_ok", lambda _: True)
    monkeypatch.setattr(stack, "_port_open", lambda _: True)
    monkeypatch.setattr(stack, "bridge_profiles", lambda *_: {"p1": {"connected": True, "agent_state": "loading"}})
    ready, total, problems, missing = stack.readiness()
    assert (ready, total) == (0, 1)
    assert any("loading" in problem for problem in problems)
    assert not missing


def test_wait_ready_shows_loading_while_polling(monkeypatch, capsys):
    responses = iter([(0, 1, ["Manager: loading"], []), (1, 1, [], [])])
    monkeypatch.setattr(stack, "readiness", lambda: next(responses))
    monkeypatch.setattr(stack, "_chrome_binary", lambda: None)
    monkeypatch.setattr(stack.time, "sleep", lambda _: None)
    monkeypatch.setattr(stack, "notify", lambda *_: None)
    monkeypatch.setattr(stack, "record_ready", lambda *_: None)
    assert stack.wait_ready(60) == 0
    output = capsys.readouterr().out
    assert "loading" in output
