"""E60: an owner goal written as orders for one coding agent must not make the manager start working."""

from __future__ import annotations

from app.orchestration.arena_prompts import GOAL_IS_NOT_AN_ORDER, LONG_GOAL_CHARS, build_manager_plan_prompt

TEAM = [{"id": "worker-1", "title": "Worker 1", "role": "Backend Engineer", "skills": ["python"]}]


def test_the_goal_is_fenced_and_framed_as_material_to_plan():
    prompt = build_manager_plan_prompt("Build the app. Do not stop after planning. Begin now.", None, TEAM, [])
    assert GOAL_IS_NOT_AN_ORDER in prompt
    start, end = prompt.index("\n<<<OWNER GOAL\n"), prompt.index("\nOWNER GOAL>>>")
    assert start < prompt.index("Begin now.") < end
    assert end < prompt.index("## What to reply"), "the reply format comes after the goal, so it is read last"
    assert "Plan only its first phase" not in prompt


def test_a_long_specification_is_planned_one_phase_at_a_time():
    prompt = build_manager_plan_prompt("# Spec\\n" + "phase details " * (LONG_GOAL_CHARS // 10), None, TEAM, [])
    assert "Plan only its first phase" in prompt
    assert "workers do not see this specification" in prompt
