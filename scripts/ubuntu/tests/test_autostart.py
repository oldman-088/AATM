"""E5 — the autostart unit: one command to install it, one to remove it, and what it must contain.

systemd is never really started here. The tests stub ``systemctl``/``loginctl`` with scripts that
record their arguments, then assert on the generated unit file, the commands the installer ran, and
the retry behaviour of the script the unit executes.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UNIT_PATH = "config/systemd/user/aatm.service"


def stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    script.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    """HOME, an empty XDG_CONFIG_HOME and a PATH holding only the stubs the tests install."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return {"tmp": tmp_path, "bin": bin_dir,
            "env": {**os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}"}}


def sh(env: dict, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(ROOT / "scripts/ubuntu" / script), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False)


# --------------------------------------------------------------------------------- install: the unit
def test_user_unit_install_and_uninstall(tmp_path):
    """The roundtrip that already existed, kept honest: install writes it, uninstall removes it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run(["bash", "scripts/ubuntu/install-autostart.sh"], cwd=ROOT, env=env, check=True)
    unit = tmp_path / "config/systemd/user/aatm.service"
    text = unit.read_text()
    assert "After=network-online.target" in text
    # the unit runs autostart.sh, which is what runs start-all.sh (retry + doctor pass)
    assert f"{ROOT}/scripts/ubuntu/autostart.sh" in text
    assert "start-all.sh" in (ROOT / "scripts/ubuntu/autostart.sh").read_text()
    assert "WantedBy=default.target" in text
    subprocess.run(["bash", "scripts/ubuntu/uninstall-autostart.sh"], cwd=ROOT, env=env, check=True)
    assert not unit.exists()


def test_unit_waits_for_the_network_and_survives_its_own_failures(env):
    stub(env["bin"], "systemctl", "#!/bin/sh\nexit 0\n")
    stub(env["bin"], "loginctl", "#!/bin/sh\necho 'Linger=yes'\n")
    assert sh(env["env"], "install-autostart.sh").returncode == 0
    text = (Path(env["tmp"]) / UNIT_PATH).read_text()
    assert "Wants=network-online.target" in text and "After=network-online.target" in text
    # a one-shot that starts the stack: retries belong to the script, so systemd must not rate-limit them
    assert "Type=oneshot" in text and "RemainAfterExit=yes" in text
    assert "StartLimitIntervalSec=0" in text
    assert "TimeoutStartSec=infinity" in text
    assert "Environment=AATM_AUTOSTART=1" in text
    assert f"WorkingDirectory={ROOT}\n" in text  # the repo, not the fake HOME
    # systemd does not strip quotes here: a quoted path reads "not absolute" and the unit never loads (E65)
    assert 'WorkingDirectory="' not in text


def test_unit_path_survives_spaces_and_percent_signs(tmp_path):
    """ExecStart is quoted and percent-escaped, or a path with % in it silently breaks the unit."""
    repo = tmp_path / "a repo %x" / "AATM"
    (repo / "scripts/ubuntu").mkdir(parents=True)
    (repo / "scripts/ubuntu/install-autostart.sh").write_text(
        (ROOT / "scripts/ubuntu/install-autostart.sh").read_text(encoding="utf-8"), encoding="utf-8")
    # only the script the unit points at is a stand-in here; the installer itself is the subject
    (repo / "scripts/ubuntu/autostart.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (repo / "scripts/ubuntu/autostart.sh").chmod(0o755)
    stub(tmp_path / "bin", "systemctl", "#!/bin/sh\nexit 0\n")
    stub(tmp_path / "bin", "loginctl", "#!/bin/sh\necho 'Linger=yes'\n")
    env = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(repo / "scripts/ubuntu/install-autostart.sh")],
                            capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "config/systemd/user/aatm.service").read_text()
    escaped = str(repo).replace("%", "%%")
    assert f'ExecStart="{escaped}/scripts/ubuntu/autostart.sh"' in text  # %x must not read as a specifier
    assert f"WorkingDirectory={escaped}\n" in text


def test_install_enables_the_unit_and_turns_linger_on_when_needed(env):
    """The verification the owner asked for: the unit was enabled, and linger makes it a boot start."""
    calls = Path(env["tmp"]) / "calls.log"
    stub(env["bin"], "systemctl", f"""
        #!/bin/sh
        echo "systemctl $*" >> {calls}
        exit 0
    """)
    stub(env["bin"], "loginctl", f"""
        #!/bin/sh
        echo "loginctl $*" >> {calls}
        [ "$2" = "show-user" ] && echo 'Linger=no'
        exit 0
    """)
    result = sh(env["env"], "install-autostart.sh")
    assert result.returncode == 0, result.stderr
    logged = calls.read_text()
    assert "systemctl --user daemon-reload" in logged
    assert "systemctl --user enable aatm.service" in logged
    assert "systemctl --user is-enabled aatm.service" in logged
    assert "loginctl enable-linger" in logged
    assert "linger enabled" in result.stdout
    assert "is enabled" in result.stdout


def test_install_leaves_linger_alone_when_it_is_already_on(env):
    calls = Path(env["tmp"]) / "loginctl.log"
    stub(env["bin"], "systemctl", "#!/bin/sh\nexit 0\n")
    stub(env["bin"], "loginctl", f"#!/bin/sh\necho \"loginctl $*\" >> {calls}\necho 'Linger=yes'\n")
    result = sh(env["env"], "install-autostart.sh")
    assert result.returncode == 0, result.stderr
    assert "linger is already on" in result.stdout
    assert "enable-linger" not in calls.read_text()


def test_no_linger_flag_declines_the_change_it_would_otherwise_make(env):
    calls = Path(env["tmp"]) / "loginctl.log"
    stub(env["bin"], "systemctl", "#!/bin/sh\nexit 0\n")
    stub(env["bin"], "loginctl", f"#!/bin/sh\necho \"loginctl $*\" >> {calls}\necho 'Linger=no'\n")
    result = sh(env["env"], "install-autostart.sh", "--no-linger")
    assert result.returncode == 0, result.stderr
    assert "starts at login, not at boot" in result.stderr  # a degraded install is a warning
    assert "enable-linger" not in calls.read_text()
    # and without the flag the same stub does get asked for it
    assert sh(env["env"], "install-autostart.sh").returncode == 0
    assert "enable-linger" in calls.read_text()


def test_install_reports_clearly_when_there_is_no_user_manager(env):
    """No systemd --user is a supported machine, not a stack trace: say what to do instead."""
    stub(env["bin"], "systemctl", "#!/bin/sh\nexit 1\n")
    result = sh(env["env"], "install-autostart.sh")
    assert result.returncode == 3
    assert "no usable systemd --user manager" in result.stderr
    assert "--desktop" in result.stderr
    assert not (Path(env["tmp"]) / UNIT_PATH).exists()
    # and the documented alternative works without systemd
    fallback = sh(env["env"], "install-autostart.sh", "--desktop")
    assert fallback.returncode == 0, fallback.stderr
    desktop = Path(env["tmp"]) / "config/autostart/aatm.desktop"
    assert f"Exec={ROOT}/scripts/ubuntu/autostart.sh" in desktop.read_text()


def test_unknown_option_is_an_error_not_a_silent_install(env):
    stub(env["bin"], "systemctl", "#!/bin/sh\nexit 0\n")
    result = sh(env["env"], "install-autostart.sh", "--enable-now")
    assert result.returncode == 2
    assert "unknown argument" in result.stderr


# ------------------------------------------------------------------------------- uninstall: remove it
def test_uninstall_removes_a_stray_enable_symlink(env):
    """A leftover wants/ symlink must not survive, or the unit comes back on the next boot."""
    calls = Path(env["tmp"]) / "calls.log"
    stub(env["bin"], "systemctl", f"#!/bin/sh\necho \"systemctl $*\" >> {calls}\nexit 0\n")
    unit_dir = Path(env["tmp"]) / "config/systemd/user"
    (unit_dir / "default.target.wants").mkdir(parents=True)
    unit = unit_dir / "aatm.service"
    unit.write_text("[Unit]\nDescription=x\n")
    link = unit_dir / "default.target.wants/aatm.service"
    link.symlink_to(unit)
    assert sh(env["env"], "uninstall-autostart.sh").returncode == 0
    assert not unit.exists() and not link.exists()
    logged = calls.read_text()
    assert "systemctl --user disable aatm.service" in logged
    assert "systemctl --user daemon-reload" in logged
    assert "running processes were not stopped" in sh(env["env"], "uninstall-autostart.sh").stdout


def test_uninstall_removes_only_our_desktop_entry(env):
    desktop = Path(env["tmp"]) / "config/autostart"
    desktop.mkdir(parents=True)
    other = desktop / "something-else.desktop"
    other.write_text("[Desktop Entry]\nName=other\n")
    (desktop / "aatm.desktop").write_text(f"[Desktop Entry]\nExec={ROOT}/scripts/ubuntu/autostart.sh\n")
    stub(env["bin"], "systemctl", "#!/bin/sh\nexit 1\n")
    assert sh(env["env"], "uninstall-autostart.sh").returncode == 0
    assert not (desktop / "aatm.desktop").exists() and other.exists()


def test_uninstall_is_a_no_op_when_nothing_was_installed(env):
    stub(env["bin"], "systemctl", "#!/bin/sh\nexit 1\n")
    result = sh(env["env"], "uninstall-autostart.sh")
    assert result.returncode == 0
    assert "nothing to remove" in result.stdout


# --------------------------------------------------------------------- the wrapper the unit executes
def write_fake_start(env: dict, fails: int = 0) -> Path:
    """A stand-in for start-all.sh that fails ``fails`` times before succeeding, and counts attempts."""
    marker = Path(env["tmp"]) / "attempts"
    fake = Path(env["tmp"]) / "fake-start-all.sh"
    fake.write_text(textwrap.dedent(f"""
        #!/usr/bin/env bash
        n=$(cat {marker} 2>/dev/null || echo 0)
        n=$((n + 1)); echo $n > {marker}
        echo "start-all attempt $n"
        [ "$n" -le {fails} ] && exit 1
        exit 0
    """), encoding="utf-8")
    fake.chmod(0o755)
    return fake


def autostart_env(env: dict, start_script: Path) -> dict:
    return {**env["env"], "AATM_START_ALL": str(start_script), "AATM_AUTOSTART_LOG": str(Path(env["tmp"]) / "log"),
            "AATM_AUTOSTART_RETRY_DELAY": "0", "AATM_AUTOSTART_NETWORK_TIMEOUT": "0"}


def test_autostart_retries_start_all_until_it_succeeds(env):
    """The point of the wrapper: a start that fails because the box is still settling gets retried."""
    fake = write_fake_start(env, fails=2)
    result = subprocess.run(["bash", str(ROOT / "scripts/ubuntu/autostart.sh")], env=autostart_env(env, fake),
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert fake.with_name("attempts").read_text().strip() == "3"
    assert "attempt 1/5" in result.stdout and "the stack started on attempt 3" in result.stdout


def test_autostart_gives_up_loudly_after_its_attempts(env):
    fake = write_fake_start(env, fails=99)
    env_vars = {**autostart_env(env, fake), "AATM_AUTOSTART_RETRIES": "2"}
    result = subprocess.run(["bash", str(ROOT / "scripts/ubuntu/autostart.sh")], env=env_vars,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert fake.with_name("attempts").read_text().strip() == "2"
    assert "leaving it for a person" in result.stdout
    assert "attempt 2 failed" not in result.stdout  # the last attempt does not promise another retry
    assert "still failing after 2 attempts" in (Path(env["tmp"]) / "log").read_text()


def test_autostart_continues_when_the_network_never_arrives(env):
    """No route is a warning, not a hang: it says so, then tries anyway."""
    fake = write_fake_start(env, fails=0)
    env_vars = {**autostart_env(env, fake), "AATM_AUTOSTART_NETWORK_TIMEOUT": "1",
                "AATM_AUTOSTART_PROBE": "exit 1"}
    result = subprocess.run(["bash", str(ROOT / "scripts/ubuntu/autostart.sh")], env=env_vars,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "no default route after 1s" in result.stdout


def test_autostart_refuses_to_install_a_missing_target(env, tmp_path):
    """start-all.sh missing (a partial checkout) is reported, not retried five times."""
    env_vars = {**env["env"], "AATM_START_ALL": str(tmp_path / "nope.sh"),
                "AATM_AUTOSTART_NETWORK_TIMEOUT": "0", "AATM_AUTOSTART_RETRIES": "3",
                "AATM_AUTOSTART_LOG": str(tmp_path / "log")}
    result = subprocess.run(["bash", str(ROOT / "scripts/ubuntu/autostart.sh")], env=env_vars,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "nothing to start" in result.stdout


def test_install_refuses_when_autostart_script_is_missing(tmp_path):
    """The unit points at autostart.sh, so installing without it would create a unit that cannot run."""
    broken = tmp_path / "repo"
    (broken / "scripts/ubuntu").mkdir(parents=True)
    (broken / "scripts/ubuntu/install-autostart.sh").write_text(
        (ROOT / "scripts/ubuntu/install-autostart.sh").read_text(), encoding="utf-8")
    (broken / "scripts/ubuntu/start-all.sh").write_text("#!/bin/sh\nexit 0\n")
    stub(tmp_path / "bin", "systemctl", "#!/bin/sh\nexit 0\n")
    env = {**os.environ, "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(broken / "scripts/ubuntu/install-autostart.sh")], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "autostart.sh is missing" in result.stderr
    assert not (tmp_path / "config/systemd/user/aatm.service").exists()
