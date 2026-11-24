"""E51: Arena windows keep rendering when they are covered, in the background or minimised."""

import importlib.util
import sys
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "aatm_stack.py"
spec = importlib.util.spec_from_file_location("aatm_stack_flags_test", path)
stack = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = stack
spec.loader.exec_module(stack)


def test_chrome_command_keeps_background_windows_rendering(tmp_path):
    command = stack.chrome_command("chrome", tmp_path / "manager", "Manager", "https://arena.ai/agent")
    for flag in ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                 "--disable-background-timer-throttling"):
        assert flag in command
    assert command[0] == "chrome" and command[-1] == "https://arena.ai/agent"
    assert f"--user-data-dir={tmp_path / 'manager'}" in command


def test_launch_uses_the_same_command(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(stack, "profiles_home", lambda: tmp_path)
    monkeypatch.setattr(stack.subprocess, "Popen", lambda args, **_: seen.append(args))
    stack._launch("chrome", stack.Window("p1", "manager", "Manager", "https://arena.ai/agent"), "about:blank")
    assert seen and "--disable-renderer-backgrounding" in seen[0] and seen[0][-1] == "about:blank"
