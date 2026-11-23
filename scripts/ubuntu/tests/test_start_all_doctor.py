"""E17 wiring: every ``start-all`` ends with one doctor pass — reported, never applied.

start-all.sh is run by hand, by systemd after a boot, and by the retry wrapper; the check has to
happen in that one place so it cannot be skipped. These tests drive the real script with a stub
python and a stub start script, so no service, port or database is involved.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
START_ALL = ROOT / "scripts/ubuntu/start-all.sh"


@pytest.fixture
def harness(tmp_path):
    """A logging stub for the interpreter and one for start.sh; both record their calls in order."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"

    py = bin_dir / "python"
    py.write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\nexit 0\n', encoding="utf-8")
    py.chmod(0o755)

    start = bin_dir / "start.sh"
    start.write_text(f'#!/usr/bin/env bash\necho "start.sh" >> {calls}\nexit 0\n', encoding="utf-8")
    start.chmod(0o755)

    env = {**os.environ, "AATM_PYTHON": str(py), "AATM_START_SCRIPT": str(start),
           "PATH": f"{bin_dir}:/usr/bin:/bin"}
    return tmp_path, calls, env


def run_start_all(env) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(START_ALL)], cwd=ROOT, env=env, capture_output=True, text=True,
                          check=False)


def test_the_doctor_runs_once_after_readiness(harness):
    """start -> open windows -> wait-ready -> doctor. In that order, because findings mean nothing first."""
    _, calls, env = harness
    result = run_start_all(env)
    assert result.returncode == 0, result.stderr
    logged = calls.read_text().splitlines()
    assert any(line.endswith("open-windows") for line in logged)
    doctor_lines = [i for i, line in enumerate(logged) if "aatm_doctor.py" in line]
    ready = [i for i, line in enumerate(logged) if "wait-ready" in line]
    assert len(doctor_lines) == 1, logged
    assert ready and doctor_lines[0] > ready[-1], logged
    assert "aatm_doctor.py --log" in logged[doctor_lines[0]]


def test_the_boot_pass_never_repairs(harness):
    """No --fix in the boot path: nobody is reading the screen when the machine comes back."""
    _, calls, env = harness
    run_start_all(env)
    assert "--fix" not in calls.read_text()


def test_a_failed_start_still_leaves_a_doctor_record(harness):
    """The report matters most when the start failed, and it must not change the exit status."""
    _, calls, env = harness
    Path(env["AATM_START_SCRIPT"]).write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "start.sh failed" >> {calls}
            exit 1
        """), encoding="utf-8")
    result = run_start_all(env)
    assert result.returncode == 1
    logged = calls.read_text().splitlines()
    assert "start.sh failed" in logged[0]
    assert any("aatm_doctor.py" in line for line in logged), logged
    assert "لم تبدأ الخدمات" in result.stdout or "لم تبدأ الخدمات" in result.stderr


def test_the_pass_can_be_turned_off_for_one_run(harness):
    """A documented escape hatch, so a broken check can never block a start."""
    _, calls, env = harness
    result = run_start_all({**env, "AATM_DOCTOR_ON_START": "0"})
    assert result.returncode == 0
    assert "aatm_doctor.py" not in calls.read_text()


def test_the_log_line_is_optional(harness):
    """AATM_DOCTOR_ON_START_LOG=0 keeps the report on the console and out of logs/doctor.log."""
    _, calls, env = harness
    run_start_all({**env, "AATM_DOCTOR_ON_START_LOG": "0"})
    lines = [entry for entry in calls.read_text().splitlines() if "aatm_doctor.py" in entry]
    assert lines == [f"{ROOT}/scripts/ubuntu/aatm_doctor.py"], lines  # present, just without --log
    assert "--log" not in lines[0]


def test_the_doctor_itself_is_the_only_place_that_writes(harness):
    """start-all must not grow its own copy of the checks: it only calls the entry point."""
    source = START_ALL.read_text(encoding="utf-8")
    assert "aatm_doctor.py" in source
    for name in ("worker_working_without_task", "window_on_wrong_repo", "sqlite3"):
        assert name not in source, f"start-all.sh must not contain check logic ({name})"
