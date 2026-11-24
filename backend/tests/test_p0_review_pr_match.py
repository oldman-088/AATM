"""P0 / E48: a manager verdict that names another pull request is never posted."""

from __future__ import annotations

from types import SimpleNamespace

from app.orchestration.arena_prompts import REVIEW_FORMAT, validate_manager_review
from app.orchestration.manager_arena import _json_retry_prompt, _review_pr_mismatch


def _verdict(**extra):
    return {"verdict": "request_changes", "summary_ar": "ملخص", "comments": ["fix it"], **extra}


def test_review_format_and_retry_template_ask_for_the_pr_number():
    assert '"pr_number"' in REVIEW_FORMAT
    assert '"pr_number"' in _json_retry_prompt("review")


def test_stated_pr_number_is_kept_and_normalised():
    assert validate_manager_review(_verdict(pr_number=15))[0]["pr_number"] == 15
    assert validate_manager_review(_verdict(pr_number="#15"))[0]["pr_number"] == 15


def test_missing_or_zero_pr_number_is_not_invented():
    assert "pr_number" not in validate_manager_review(_verdict())[0]
    assert "pr_number" not in validate_manager_review(_verdict(pr_number=0))[0]
    assert "pr_number" not in validate_manager_review(_verdict(pr_number=True))[0]


def test_verdict_for_another_pr_is_rejected():
    job = SimpleNamespace(context={"pr_number": 15})
    reason = _review_pr_mismatch(job, {"pr_number": 10})
    assert reason is not None and "#10" in reason and "#15" in reason


def test_matching_or_unstated_pr_number_is_accepted():
    job = SimpleNamespace(context={"pr_number": 15})
    assert _review_pr_mismatch(job, {"pr_number": 15}) is None
    assert _review_pr_mismatch(job, {}) is None
    assert _review_pr_mismatch(SimpleNamespace(context={}), {"pr_number": 10}) is None
