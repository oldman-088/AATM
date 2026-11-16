---
name: AATM Engineering Task
about: Structured engineering task aligned with AATM_MASTER_SPECIFICATION.md Section 24
title: "[D00] <Short Task Title>"
labels: ["aatm-task"]
assignees: []
---

## Task Objective
<!-- Clear description of the problem to solve or capability to implement. -->

## Ownership & Routing
- **Human Lead:** `Project Director | Backend Lead | Frontend Lead | AI Systems Lead | DevOps & GitHub Lead | QA & Security Lead`
- **Assigned Agent:** `manager-0 | worker-1 | worker-2 | worker-3 | worker-4 | worker-5`
- **Division Code:** `D01–D12`
- **Priority:** `critical | high | medium | low`
- **Execution Mode:** `automatic | manual | collaborative`

## Owned Files & Dependencies
- **Target Files / Directories:**
  - `path/to/file`
- **Prerequisite Tasks / Issues:**
  - None

## Acceptance Criteria & Expected Deliverables
- [ ] Deliverable 1:
- [ ] Deliverable 2:
- [ ] Unit / integration / E2E tests added and passing.
- [ ] Documentation updated (`AATM_MASTER_SPECIFICATION.md` Section 0.5 alignment).

## Security & Governance Gate Check
- [ ] Does this task touch protected branches (`main`, `master`, `production`), credentials, budgets, or destructive operations? If yes, link the required `ApprovalRequest`.
- [ ] Are external inputs sanitized via `sanitize_untrusted_content()` and CLI outputs scrubbed via `sanitize_cli_output()`?

## Section 24 Execution Checklist

### Before Starting a Task
- [ ] Read `AATM_MASTER_SPECIFICATION.md` and the repository `AGENTS.md`.
- [ ] Check GitHub Issues and existing project plans for duplicate or related work.
- [ ] Confirm task owner, scope, acceptance criteria, dependencies, and permissions.
- [ ] Inspect branch and working-tree status; preserve uncommitted user changes.
- [ ] Create or use an isolated working branch/worktree.

### Before Opening a Pull Request
- [ ] Run relevant unit, integration, build, lint, type, and security checks available in the repository.
- [ ] Review the diff for unrelated changes, secrets, generated artifacts, and accidental deletions.
- [ ] Update documentation and tests where needed.
- [ ] Include evidence of verification and clearly list any checks not run.
- [ ] Link the related issue and request the required human reviews.

### Before Marking a Task Complete
- [ ] Confirm the requested deliverable exists.
- [ ] Verify it in the actual environment or test suite where feasible.
- [ ] Record commit/PR references and test evidence.
- [ ] Document remaining risks, limitations, or follow-up work.
- [ ] Obtain required approvals.
