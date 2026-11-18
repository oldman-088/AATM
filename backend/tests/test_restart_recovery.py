"""After a restart, unfinished Arena work continues and no prompt is sent twice."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.orchestration import arena_runner as runner_module
from app.orchestration import manager_arena as manager_module
from app.orchestration.arena_runner import ArenaAgentRunner
from app.orchestration.manager_arena import ManagerArenaRuntime


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class _Db:
    def __init__(self, *results, task=None):
        self.results, self.task, self.commits = list(results), task, 0

    async def execute(self, _query):
        return _Result(self.results.pop(0))

    async def get(self, _model, _key):
        return self.task

    async def commit(self):
        self.commits += 1


class _Adapter:
    def __init__(self, state="idle", mark="1:10"):
        self.page, self.mark, self.sent = {"state": state, "mark": mark}, mark, []

    async def transcript_mark(self, _profile):
        return self.page

    async def submit(self, _profile, text, **_kwargs):
        self.sent.append(text)

    async def state(self, _profile):
        return {"agent_state": "idle", "agent_state_since": "later", "connected": True}

    async def read_last_reply(self, _profile):
        return {"text": "the finished reply"}


@pytest.fixture(autouse=True)
def plain_events(monkeypatch):
    def record(session, kind, detail=None, **extra):
        session.events.append({"kind": kind, "detail": detail, **extra})

    monkeypatch.setattr(runner_module, "append_event", record)
    monkeypatch.setattr(manager_module, "append_event", record)


def _session(phase="working", events=None):
    return SimpleNamespace(id="s", task_id="t", active_task_id="t", profile_id="p", phase=phase, events=events or [],
                           last_state_change_at=datetime(2020, 1, 1), kind="worker", page_state="idle",
                           exchange_count=0, submitted_at=None)


def _runner(adapter):
    runner = ArenaAgentRunner.__new__(ArenaAgentRunner)
    runner.adapter = adapter
    return runner


async def test_downtime_is_not_a_stall_and_unsent_work_is_requeued():
    monitored, sending = _session(), _session("submitting")
    task = SimpleNamespace(id="t", status="in_progress", lock_owner="x", updated_at=datetime(2020, 1, 1))
    message = SimpleNamespace(execution_status="running")
    result = await _runner(_Adapter()).recover_after_restart(_Db([monitored, sending], [message], task=task))
    assert result == {"monitored": 1, "requeued": 1}
    assert monitored.last_state_change_at.year > 2020 and task.updated_at.year > 2020
    assert task.status == "queued" and message.execution_status == "cancelled"
    assert sending.submitted_at is None


@pytest.mark.parametrize(("state", "mark", "landed"), [
    ("generating", "1:10", True), ("idle", "2:40", True), ("idle", "1:10", False),
])
async def test_worker_prompt_is_recognized_on_the_page(state, mark, landed):
    session = _session("submitting", [{"kind": "submit_started", "detail": "1:10"}])
    assert await _runner(_Adapter(state, mark))._submit_landed(session) is landed


async def test_running_manager_job_returns_to_the_queue_marked_interrupted():
    runtime = ManagerArenaRuntime(adapter=_Adapter())
    statuses = []

    async def _status(_db, status):
        statuses.append(status)

    runtime._set_manager_status = _status
    job = SimpleNamespace(id="j", status="running", error="x", context={"a": 1})
    assert await runtime.recover_after_restart(_Db([job])) == 1
    assert job.status == "queued" and job.context == {"a": 1, "interrupted": True} and statuses == ["idle"]


@pytest.mark.parametrize(("events", "page", "sent"), [
    ([], ("idle", "1:10"), False),
    ([{"kind": "submitted", "detail": "plan: 9 chars", "job_id": "j"}], ("idle", "1:10"), True),
    ([{"kind": "submitted", "detail": "context: 9 chars", "job_id": "j"}], ("idle", "1:10"), False),
    ([{"kind": "submit_started", "detail": "1:10", "exchange": "plan", "job_id": "j"}], ("idle", "1:10"), False),
    ([{"kind": "submit_started", "detail": "1:10", "exchange": "plan", "job_id": "j"}], ("idle", "2:50"), True),
    ([{"kind": "submit_started", "detail": "1:10", "exchange": "plan", "job_id": "j"}], ("generating", "1:10"), True),
    ([{"kind": "submitted", "detail": "plan: 9 chars", "job_id": "other"}], ("idle", "1:10"), False),
])
async def test_manager_prompt_is_only_treated_as_sent_with_evidence(events, page, sent):
    runtime = ManagerArenaRuntime(adapter=_Adapter(*page))
    assert await runtime._interrupted_prompt_sent(_session(events=events), SimpleNamespace(id="j")) is sent


async def test_resumed_manager_job_waits_for_the_reply_without_sending(monkeypatch):
    adapter = _Adapter()
    runtime = ManagerArenaRuntime(adapter=adapter)

    async def _stream(*_args):
        return None

    runtime._stream = _stream
    job = SimpleNamespace(id="j", kind="chat")
    reply = await runtime._exchange(_Db(), _session("manager_busy"), job, "the prompt", resume=True)
    assert reply == "the finished reply" and adapter.sent == []
