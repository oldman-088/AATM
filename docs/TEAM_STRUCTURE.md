# AATM — Team Structure & Ownership Matrix

Aligned with **Section 0.2** of [`AATM_MASTER_SPECIFICATION.md`](../AATM_MASTER_SPECIFICATION.md) and the active multi-agent workflow:

| Role / Slot | Human Lead | Primary Focus | Exclusive Owned Paths |
|---|---|---|---|
| **AI Manager / Coordinator** (`manager-0`) | Project Director | Specification alignment, task decomposition, dependency/conflict resolution, integration, final verification & approval escalation | `AATM_MASTER_SPECIFICATION.md`, `AGENTS.md`, `docs/{EXISTING_PROJECT_AUDIT,TEAM_STRUCTURE,DEVELOPMENT_PLAN}.md`, `STATUS.txt`, `PROGRESS_LOG.txt`, `NEXT_ACTIONS.txt` |
| **Worker 1** (`Agent 2` — Backend Lead) | Backend Lead | Backend APIs, contracts, database models, Alembic migrations, per-device token revocation, provider usage headroom | `contracts/`, `backend/app/models/`, `backend/alembic/`, `backend/app/schemas/`, `backend/app/runtime/device_auth.py`, `backend/app/api/routes/{devices,infra,platform}.py`, `backend/tests/test_contracts_v2.py` |
| **Worker 2** (`Agent 3` — Frontend Lead) | Frontend Lead | React/TS Command Center UI, GitHub PR/Issue approval controls, budget headroom & revocation UI, EN/AR/FA RTL parity, Vitest | `frontend/src/` (`i18n/`, `views/`, `App.tsx`, `App.test.tsx`, `views/__tests__/`, `test/`) |
| **Worker 3** (`Agent 4` — AI Systems Lead) | AI Systems Lead | `bridge/` Arena Local Bridge daemon, AI provider adapters/router, Playwright/Tampermonkey engine adapters, dynamic employees, replacement & worker daemon | `bridge/`, `backend/app/providers/`, `backend/app/browser/`, `backend/app/employees/`, `backend/app/orchestration/replacement.py`, `backend/app/runtime/worker_daemon.py`, `backend/tests/test_worker3_providers_browser.py` |
| **Worker 4** *(with **Worker 5** as Standby Backup)* — DevOps, GitHub, QA & Security Lead | DevOps/GitHub & QA/Security Leads | GitHub CI/CD workflows, PR/Issue templates, GitHub write client, `qa/engine.py`, `docs/SECURITY_POLICY.md`, E2E regression tests, `scripts/` verification | `.github/`, `backend/app/github_client/`, `backend/app/git_workflows/`, `backend/app/qa/`, `scripts/`, `docs/{SECURITY_POLICY,SECURITY_ASSESSMENT,VERIFICATION_REPORT,FEATURE_IMPLEMENTATION_MATRIX,KNOWN_LIMITATIONS}.md`, `backend/tests/test_e2e_verification.py` |

## Continuity & Standby Protocol (W4 / W5)
- **Worker 5** acts as the designated **Standby Backup** for **Worker 4**. If Worker 4 becomes unavailable, Worker 5 inherits Worker 4's latest pushed branch/commit (never starting from scratch) and completes the remaining deliverables without altering ownership boundaries.
