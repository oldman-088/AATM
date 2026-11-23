#!/usr/bin/env bash
# AATM: one command that runs the full Arena agent cycle on the built-in scripted Arena page.
# plan -> implement -> PR -> review -> corrections -> approval -> new session. It does not touch
# the live Arena profiles or the running backend; it uses an isolated test database.
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR/backend" || exit 1
TEST="tests/test_r3_arena_agent_workflow.py::test_full_arena_agent_workflow_with_review_corrections_approval_and_new_session"
"$ROOT_DIR/.venv/bin/python" -m pytest -q -p no:cacheprovider "$TEST"
status=$?
if [[ $status -eq 0 ]]; then echo "[AATM E2E] PASS: full Arena agent cycle completed."; else echo "[AATM E2E] FAIL (exit $status)"; fi
exit $status
