## Summary & Purpose
<!-- Describe the purpose, scope, and user/system impact of this Pull Request. -->

- **Related Issue / Task ID:** `#`
- **Primary Division(s):** `D01–D12`
- **Owner / Agent Slot:** `manager-0 | worker-1 | worker-2 | worker-3 | worker-4 | worker-5`

## Scope of Changes
<!-- List modified/added files and summarize architectural or contract changes. -->

- 

## Implementation Notes
<!-- Document key design decisions, database migrations, or cross-division touchpoints. -->

- 

## Pre-PR Execution Checklist (Master Specification Section 24)

### Before Starting the Task
- [ ] Read `AATM_MASTER_SPECIFICATION.md` and `AGENTS.md`.
- [ ] Checked GitHub Issues and existing project plans for duplicate or related work.
- [ ] Confirmed task owner, scope, acceptance criteria, dependencies, and permissions.
- [ ] Inspected branch and working-tree status; preserved uncommitted user changes.
- [ ] Created or used an isolated working branch/worktree.

### Before Opening This Pull Request
- [ ] Ran relevant unit, integration, build, lint, type, and security checks available in the repository (`bash scripts/ubuntu/verify.sh`).
- [ ] Reviewed the diff for unrelated changes, secrets, generated artifacts, and accidental deletions.
- [ ] Updated documentation and tests where needed.
- [ ] Included evidence of verification below and clearly listed any checks not run.
- [ ] Linked the related issue and requested the required human reviews (Domain Lead + QA/Security Lead).

### Before Marking the Task Complete
- [ ] Confirmed the requested deliverable exists.
- [ ] Verified it in the actual environment or test suite where feasible.
- [ ] Recorded commit/PR references and test evidence.
- [ ] Documented remaining risks, limitations, or follow-up work.
- [ ] Obtained required approvals.

## Verification Evidence
<!-- Paste actual command results. Never claim a check passed without executing it. -->

```text
bash scripts/ubuntu/verify.sh
alembic upgrade head:
ruff check backend & bridge:
pytest backend & bridge:
static secret scan (python -m app.qa.engine):
frontend tsc / vitest / vite build:
```

## Risks, Limitations & Follow-Up Work
<!-- Note any known limitations, rollback considerations, or next-sprint items. -->

- 
