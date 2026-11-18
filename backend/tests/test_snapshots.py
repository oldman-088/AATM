"""Snapshots and rollback against a fake GitHub remote and a fake gh command."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.orchestration import review

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "ubuntu"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin, work, tools = tmp_path / "github.com" / "o" / "r.git", tmp_path / "work", tmp_path / "bin"
    origin.parent.mkdir(parents=True)
    tools.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "r8-aatm", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "checkout", "-q", "-B", "r8-aatm")
    _git(work, "config", "user.email", "t@example.test")
    _git(work, "config", "user.name", "t")
    (work / "app.txt").write_text("one\n")
    (work / "node_modules").mkdir()
    (work / "node_modules" / "skip.js").write_text("x")
    (work / ".gitignore").write_text("node_modules/\ndata/\n")
    (work / "data").mkdir()
    db = sqlite3.connect(work / "data" / "aatm.db")
    db.execute("create table t(v)")
    db.commit()
    db.close()
    _git(work, "add", "app.txt", ".gitignore")
    _git(work, "commit", "-qm", "First change (#1)")
    _git(work, "push", "-q", "origin", "r8-aatm")
    log = tmp_path / "gh.log"
    gh = tools / "gh"
    gh.write_text(f'''#!/usr/bin/env bash
echo "$*" >> "{log}"
if [[ "$1" == api ]]; then echo "{{\\"protected\\": ${{FAKE_PROTECTED:-false}}}}"; else echo https://github.test/pull/5; fi
''')
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "AATM_ROOT": str(work), "AATM_BACKUP_DIR": str(tmp_path / "backups"), "AATM_PYTHON": sys.executable,
           "PATH": f"{tools}:{os.environ['PATH']}", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
    return work, origin, env, tmp_path / "backups", log


def _run(env, *args: str, script: str = "aatm_snapshot.py"):
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=env, capture_output=True, text=True, timeout=60)


def _change(work: Path, text: str, number: int) -> None:
    (work / "app.txt").write_text(text)
    _git(work, "commit", "-qam", f"Change (#{number})")
    _git(work, "push", "-q", "origin", "r8-aatm")


def _tags(origin: Path) -> list[str]:
    return _git(origin, "tag", "--list", "snapshot/*").split()


def test_snapshot_tags_backs_up_and_records_the_rollback_command(repo):
    work, origin, env, backups, _log = repo
    result = _run(env, "snapshot")
    assert result.returncode == 0, result.stderr
    tag = _tags(origin)[0]
    name = tag.split("/")[1]
    with tarfile.open(backups / f"{name}.tgz") as tar:
        names = tar.getnames()
    assert "work/app.txt" in names and not any("node_modules" in n or "/.git/" in n or "/data" in n for n in names)
    assert (backups / f"{name}.db").exists() and stat.S_IMODE((backups / f"{name}.tgz").stat().st_mode) == 0o600
    notes = (work / "SNAPSHOTS.md").read_text()
    assert tag in notes and "/pull/1" in notes and f"rollback.sh {tag}" in notes
    assert "SNAPSHOTS.md" not in _git(work, "status", "--short")


def test_same_commit_makes_no_new_snapshot_and_new_work_gets_the_next_number(repo):
    work, origin, env, _backups, _log = repo
    _run(env, "snapshot")
    assert "No new changes" in _run(env, "snapshot").stdout and len(_tags(origin)) == 1
    _change(work, "two\n", 2)
    assert _run(env, "snapshot").returncode == 0
    tags = _tags(origin)
    assert len(tags) == 2 and tags[1].endswith("-02")
    assert "/pull/2" in (work / "SNAPSHOTS.md").read_text().splitlines()[-1]


def test_rollback_adds_a_commit_and_keeps_history_and_backs_up_first(repo):
    work, origin, env, backups, _log = repo
    _run(env, "snapshot")
    tag = _tags(origin)[0]
    _change(work, "bad\n", 2)
    before = _git(origin, "rev-parse", "r8-aatm")
    result = _run(env, "rollback", tag)
    assert result.returncode == 0, result.stderr
    after = _git(origin, "rev-parse", "r8-aatm")
    assert _git(origin, "rev-parse", "r8-aatm^") == before and after != before
    assert _git(origin, "rev-parse", "r8-aatm^{tree}") == _git(origin, "rev-parse", f"{tag}^{{tree}}")
    assert list(backups.glob("pre-rollback-*.tgz")) and _tags(origin) == [tag]


def test_protected_branch_gets_a_rollback_pr_and_is_not_pushed(repo):
    work, origin, env, _backups, log = repo
    _run(env, "snapshot")
    tag = _tags(origin)[0]
    _change(work, "bad\n", 2)
    before = _git(origin, "rev-parse", "r8-aatm")
    env["FAKE_PROTECTED"] = "true"
    result = _run(env, "rollback", tag)
    assert result.returncode == 0 and "Rollback PR" in result.stdout
    assert _git(origin, "rev-parse", "r8-aatm") == before and "pr create" in log.read_text()


def test_invalid_tag_changes_nothing(repo):
    _work, origin, env, _backups, _log = repo
    before = _git(origin, "rev-parse", "r8-aatm")
    assert _run(env, "rollback", "main").returncode != 0
    assert _git(origin, "rev-parse", "r8-aatm") == before


class _CountDb:
    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    async def scalar(self, _query) -> int:
        return self.remaining


@pytest.mark.parametrize(("remaining", "enabled", "base", "runs"), [
    (0, "1", "r8-aatm", 1), (2, "1", "r8-aatm", 0), (0, "", "r8-aatm", 0), (0, "1", "main", 0),
])
async def test_snapshot_runs_only_when_the_batch_is_finished(monkeypatch, remaining, enabled, base, runs):
    calls = []

    async def _run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(review, "run_exec", _run)
    monkeypatch.setenv("AATM_AUTO_SNAPSHOT", enabled)
    project = SimpleNamespace(id="p", config={"base_branch": base})
    await review.review_service._snapshot_finished_batch(_CountDb(remaining), project)
    assert len(calls) == runs and all(cmd[0].endswith("snapshot.sh") for cmd in calls)
