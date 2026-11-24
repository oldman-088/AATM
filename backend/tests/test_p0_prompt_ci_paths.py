"""P0 / E49 and E50: prompts ask for green CI on every job and for exact file paths."""

from __future__ import annotations

from types import SimpleNamespace

from app.orchestration.arena_prompts import build_manager_plan_prompt, build_manager_review_prompt, build_task_prompt
from app.orchestration.manager_arena import plan_warnings


def _task(**extra):
    base = dict(id="t-1", title="Reports", description="Build PDF reports", project_id=None, assigned_agent_id="worker-1",
                checkpoints=[{"acceptance_criteria": ["reports render"]}], pr_number=19, pr_url=None, branch_name=None,
                metadata_json={}, input_payload={}, priority=1)
    base.update(extra)
    return SimpleNamespace(**base)


def test_worker_prompt_asks_to_read_ci_workflows_and_guard_heavy_imports():
    from app.models import Task

    task = Task(id="t-1", title="Reports", description="Build PDF reports", project_id="p-1", priority=1)
    prompt = build_task_prompt(task, None, expected_head_sha="a" * 40)
    assert ".github/workflows/" in prompt and "pytest.importorskip" in prompt


def test_review_prompt_asks_the_manager_to_check_every_ci_job():
    prompt = build_manager_review_prompt(_task(), acceptance=["reports render"], report="done", diff="", pr_url=None,
                                         round_number=1, project=None, team=[])
    assert "check every CI job" in prompt and "A failing job is request_changes" in prompt


def test_plan_prompt_asks_for_exact_paths_not_globs():
    prompt = build_manager_plan_prompt("Build the reports", None, [], [])
    assert "never glob patterns" in prompt and "test_*.py" in prompt


def _plan_task(description: str) -> dict:
    return {"title": "Reports", "assignee": "worker-1", "description": description,
            "acceptance_criteria": ["c"], "depends_on": []}


def test_a_lost_glob_is_warned():
    warnings = plan_warnings([_plan_task("Add tests in tests/test_reports_.py for the PDF export")])
    assert len(warnings) == 1 and "tests/test_reports_.py" in warnings[0]


def test_real_file_names_are_not_warned():
    assert plan_warnings([_plan_task("Add tests/test_reports_pdf.py and app/__init__.py and tests/test_*.py")]) == []
