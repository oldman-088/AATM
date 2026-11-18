"""Arena never merges; AATM publishes finished work and owns the PR into the project's base branch."""

from __future__ import annotations

import json
import stat
from datetime import datetime
from types import SimpleNamespace

import pytest
from bridge.connector import AGENT_COMMANDS

from app.git_workflows.manager import git_workflow_manager
from app.orchestration import arena_runner
from app.orchestration.arena_prompts import reply_has_final_report, report_is_complete
from app.orchestration.arena_runner import ArenaAgentRunner
from tests.test_arena_page_states import _command, _report, needs_chromium, open_page  # noqa: F401

CHAT = "/agent/01a1ef92-abcd1234"
BRANCH = "arena/abcd1234-aatm"
TASK = "bf794110-4c88-439b-a2bb-9e20e7b19a0f"


@pytest.mark.parametrize("text", ["## AATM REPORT\nDone", "Finished.\n\nAATM REPORT\n\nTask: x", "**AATM REPORT**\nDone"])
def test_final_report_is_found_with_or_without_rendered_markdown(text):
    assert reply_has_final_report(text)


@pytest.mark.parametrize("text", ["Start your reply with AATM REPORT later", "AATM REPORT format is required below", "working"])
def test_mentioning_the_report_is_not_a_report(text):
    assert not reply_has_final_report(text)


def test_rendered_report_counts_as_complete():
    sections = "Implementation summary\nModified files\nCompleted requirements\nTests performed\n"
    assert report_is_complete(f"AATM REPORT\n{sections}") and not report_is_complete(f"Notes\n{sections}")


def test_the_bridge_has_no_merge_command():
    assert "create_pr" in AGENT_COMMANDS and not any("merge" in name for name in AGENT_COMMANDS)


@needs_chromium
async def test_finished_page_reports_pr_ready_and_merge_is_never_clicked(open_page):  # noqa: F811
    page = await open_page("merge_pr=1&no_send=1&feedback=1", CHAT)
    assert await _report(page) == ("pr_ready", None)
    assert (await _command(page, "merge"))["code"] == "unknown_command"
    await _command(page, "submit", {"text": "continue"})
    closed = await _command(page, "close_feedback")
    assert closed["data"]["closed"] is True
    assert await page.evaluate("window.__mergeClicks") == 0
    assert await page.get_by_text("Was this task successful?").count() == 0


@needs_chromium
async def test_create_pr_is_pressed_only_for_this_chats_branch_with_changes(open_page):  # noqa: F811
    page = await open_page("create_pr=1&diff=1&merge_pr=1", CHAT)
    wrong = await _command(page, "create_pr", {"branch": "arena/00000000-aatm", "repo": "o/r"})
    assert wrong["code"] == "branch_mismatch" and await page.evaluate("window.__createClicks") == 0
    done = await _command(page, "create_pr", {"branch": BRANCH, "repo": "o/r"})
    assert done.get("data", {}).get("clicked") is True, done
    assert await page.evaluate("[window.__createClicks, window.__mergeClicks]") == [1, 0]


@needs_chromium
async def test_create_pr_needs_visible_changes(open_page):  # noqa: F811
    page = await open_page("create_pr=1", CHAT)
    assert (await _command(page, "create_pr", {"branch": BRANCH}))["code"] == "no_changes"
    assert await page.evaluate("window.__createClicks") == 0


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    log = tmp_path / "gh.log"
    script = tmp_path / "gh"
    script.write_text(f'''#!/usr/bin/env bash
echo "$*" >> "{log}"
case "$1 $2" in
  "api repos/"*) echo "{{\\"ahead_by\\": $FAKE_AHEAD}}" ;;
  "pr list") cat "{tmp_path}/prs.json" ;;
  "pr create") echo https://github.test/o/r/pull/9 ;;
  "api -X") if [[ "$*" == *base=* && -n "${{FAKE_EDIT_FAILS:-}}" ]]; then echo refused >&2; exit 1; fi ;;
  "pr edit") echo "GraphQL: Projects (classic) is being deprecated" >&2; exit 1 ;;
esac
''')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("FAKE_AHEAD", "3")

    def setup(prs: list[dict]):
        (tmp_path / "prs.json").write_text(json.dumps(prs))
        return tmp_path, log

    return setup


async def _ensure(cwd):
    return await git_workflow_manager.ensure_task_pull_request(
        repo_slug="o/r", task_id=TASK, title="W1-T3 start-all", branch=BRANCH, base="r8-aatm", cwd=cwd)


async def test_pr_into_main_is_moved_to_the_base_branch_and_titled(fake_gh):
    cwd, log = fake_gh([{"number": 7, "title": "Arena work", "url": "u", "baseRefName": "main"}])
    result = await _ensure(cwd)
    calls = log.read_text()
    assert result["ok"] and result["actions"] == ["retargeted #7", "renamed"]
    assert "api -X PATCH repos/o/r/pulls/7 -f base=r8-aatm" in calls and f"-f title=[AATM-{TASK}] W1-T3 start-all" in calls
    assert "merge" not in calls


async def test_pr_that_cannot_be_moved_is_closed_and_replaced(fake_gh, monkeypatch):
    monkeypatch.setenv("FAKE_EDIT_FAILS", "1")
    cwd, log = fake_gh([{"number": 7, "title": "Arena work", "url": "u", "baseRefName": "main"}])
    result = await _ensure(cwd)
    calls = log.read_text()
    assert result["actions"] == ["closed #7", "created"] and result["url"].endswith("/pull/9")
    assert "pr close 7" in calls and "--base r8-aatm --head arena/abcd1234-aatm" in calls


async def test_branch_without_commits_gets_no_pr(fake_gh, monkeypatch):
    monkeypatch.setenv("FAKE_AHEAD", "0")
    cwd, log = fake_gh([])
    assert (await _ensure(cwd))["code"] == "no_commits"
    assert "pr create" not in log.read_text()


class _Db:
    async def commit(self) -> None:
        return None

    async def get(self, model, _key):
        return _TASK if model is arena_runner.Task else None


_TASK = SimpleNamespace(id=TASK, title="t", status="in_progress", updated_at=None)


class _Adapter:
    def __init__(self, page: str) -> None:
        self.page, self.closed, self.forbidden = page, 0, []

    async def state(self, _profile):
        return {"agent_state": self.page}

    async def close_feedback(self, _profile):
        self.closed += 1
        return {"closed": True}

    async def keep_working(self, _profile):
        self.forbidden.append("keep_working")
        return {}

    async def submit(self, _profile, text, **_kwargs):
        self.forbidden.append(text)
        return {}


async def test_finished_task_with_merge_button_goes_to_review_without_continuing(monkeypatch):
    runner = ArenaAgentRunner.__new__(ArenaAgentRunner)
    runner.adapter, runner.stall_minutes, runner._quiet_misses = _Adapter("pr_ready"), 20, {}
    reviewed = []

    async def _project(*_args):
        return None

    async def _poll(*_args):
        return {"ok": True, "pr": {"number": 5}}

    async def _collect(*_args):
        reviewed.append(True)
        return {"phase": "reviewing"}

    runner._project, runner._poll_github, runner._collect_report = _project, _poll, _collect
    runner._github_due = lambda *_args, **_kwargs: True
    session = SimpleNamespace(id="s", task_id=TASK, active_task_id=TASK, profile_id="p", page_state="pr_ready", pr_number=5,
                              submitted_at=None, last_state_change_at=None, events=[], recovery_count=0, phase="working",
                              needs_human_reason=None, keep_working_count=0, updated_at=None)
    monkeypatch.setattr(arena_runner, "apply_bridge_state", lambda *_args: False)
    monkeypatch.setattr(arena_runner, "append_event", lambda s, kind, *_a, **_k: s.events.append({"kind": kind}))
    assert await runner._tick_session(_Db(), session) == {"phase": "reviewing"}
    assert reviewed == [True] and runner.adapter.closed == 1 and runner.adapter.forbidden == []


async def test_long_generation_counts_as_progress_only_while_the_transcript_changes(monkeypatch):
    runner = ArenaAgentRunner.__new__(ArenaAgentRunner)
    pages = [(5, 10), (6, 10), (6, 10)]

    class Adapter:
        async def selector_test(self, _profile):
            count, chars = pages.pop(0)
            return {"latest_reply": {"count": count, "chars": chars}}

    def record(session, kind, detail=None, **_kwargs):
        session.events.append({"kind": kind, "detail": detail, "ts": "2026-11-18T12:00:00+00:00"})

    monkeypatch.setattr(arena_runner, "append_event", record)
    runner.adapter = Adapter()
    session = SimpleNamespace(id="s", profile_id="p", events=[])
    reference = datetime(2026, 11, 18, 11, 0)
    assert await runner._page_progressed(session, reference) is False  # no earlier mark: a real stall still stops
    await runner._note_page_mark(session, reference)
    assert await runner._page_progressed(session, reference) is True
    session.events[-1]["detail"] = "6:10"
    assert await runner._page_progressed(session, reference) is False
