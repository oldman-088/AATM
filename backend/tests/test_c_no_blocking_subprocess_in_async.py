"""Item C / Phase 5 rule 5: no blocking subprocess (or sleep) inside async code paths.

Every async path uses ``app.core.async_subprocess.run_exec`` (asyncio.create_subprocess_exec).
The only remaining ``subprocess.run`` is the synchronous secret-scan CLI path
(``python -m app.qa.engine``), which async callers reach via ``run_security_scan_async``
(index via async subprocess, file walk in a worker thread).
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCAN_ROOTS = [BACKEND / "app", BACKEND.parent / "bridge"]
BLOCKING = {("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"), ("subprocess", "check_output"),
            ("subprocess", "check_call"), ("time", "sleep"), ("os", "system")}
# run_security_scan is synchronous by design and only runs in a worker thread (run_security_scan_async uses
# asyncio.to_thread); _is_git_ignored is its helper for the gitignore check on local files.
SYNC_ALLOWLIST = {
    ("app/qa/engine.py", "_tracked_files"),
    ("app/qa/engine.py", "run_security_scan"),
    ("app/qa/engine.py", "_is_git_ignored"),
}


def _py_files():
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def _blocking_calls(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
            if (sub.func.value.id, sub.func.attr) in BLOCKING:
                yield sub


def test_no_blocking_subprocess_or_sleep_inside_async_def():
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for call in _blocking_calls(node):
                    offenders.append(f"{path.relative_to(BACKEND.parent)}:{call.lineno} in async {node.name}")
    assert offenders == []


def test_sync_subprocess_only_in_allowlisted_cli_path():
    found = set()
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(BACKEND).as_posix() if BACKEND in path.parents else path.name
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if any(True for _ in _blocking_calls(node)):
                    found.add((rel, node.name))
    assert found <= SYNC_ALLOWLIST, found - SYNC_ALLOWLIST


async def test_run_exec_times_out_and_kills_child(tmp_path: Path):
    import sys
    import time

    from app.core.async_subprocess import run_exec

    started = time.monotonic()
    result = await run_exec([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=0.3)
    assert result.timed_out is True and result.returncode == 124
    assert time.monotonic() - started < 5
    ok = await run_exec([sys.executable, "-c", "print('hi')"], cwd=tmp_path, timeout=10)
    assert ok.returncode == 0 and ok.stdout.strip() == "hi"
    missing = await run_exec(["definitely-not-a-binary-aatm"], timeout=5)
    assert missing.returncode == 127


async def test_security_scan_async_matches_sync_scan():
    from app.qa.engine import qa_engine

    async_scan = await qa_engine.run_security_scan_async()
    sync_scan = qa_engine.run_security_scan()
    assert async_scan["status"] == sync_scan["status"]
    assert async_scan["git_index_scanned"] is True
    assert async_scan["files_scanned"] == sync_scan["files_scanned"]
