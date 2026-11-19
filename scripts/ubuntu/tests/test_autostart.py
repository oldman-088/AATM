import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_user_unit_install_and_uninstall(tmp_path):
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
    assert f'{ROOT}/scripts/ubuntu/start-all.sh' in text
    assert "WantedBy=default.target" in text
    subprocess.run(["bash", "scripts/ubuntu/uninstall-autostart.sh"], cwd=ROOT, env=env, check=True)
    assert not unit.exists()
