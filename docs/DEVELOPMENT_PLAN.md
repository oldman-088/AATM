> **SUPERSEDED — see [docs/AATM_BUILD_PLAN.md](AATM_BUILD_PLAN.md)** (phase numbering below is obsolete).

# AATM — Prioritized Development Plan (Post-Specification Alignment)

**Baseline:** `main` (`6ee1f92`, 80/80 verified tests)  
**Authoritative Spec:** [`AATM_MASTER_SPECIFICATION.md`](../AATM_MASTER_SPECIFICATION.md)

---

## Increment 1 — Specification & Governance Foundation (AI Manager / Coordinator)
- **Scope:** Commit `AATM_MASTER_SPECIFICATION.md`, `AGENTS.md`, `docs/EXISTING_PROJECT_AUDIT.md`, `docs/TEAM_STRUCTURE.md`, and `docs/DEVELOPMENT_PLAN.md`; link `README.md`.
- **Acceptance Criteria:** Docs match repository reality with zero fabricated status claims; static secret scan passes.

## Increment 2 — Per-Device Token Revocation & Usage Headroom API (Worker 1)
- **Scope:** Add `ExecutionDevice.token_version` + Alembic migration, `POST /api/v1/devices/{device_id}/revoke`, `budget_summary` on `GET /api/v1/providers/usage`, and `agent_id`/`session_id` fields on `ProviderTestRequest`.
- **Acceptance Criteria:** Old device tokens return `401` after revocation; new tokens succeed; `alembic upgrade head`, `ruff`, and `pytest` pass.

## Increment 3 — Approval-Gated GitHub UI, Budget Headroom & Revocation UI (Worker 2)
- **Scope:** Add GitHub PR/Issue creation controls wired to `/api/v1/github/{prs,issues}` and `/api/v1/approvals`, render daily/monthly USD budget headroom, add "Revoke Token" action on Devices, and expand Vitest specs.
- **Acceptance Criteria:** 100% EN/AR/FA translation parity, 0 physical CSS utilities, 0 `localhost`/`127.0.0.1` URLs in `frontend/src/`, `npm run lint && npm test && npm run build` pass.

## Increment 4 — Bridge Session Lifecycle, Safe Browser Actions & Replacement Cleanup (Worker 3)
- **Scope:** Add `ArenaLocalBridgeAdapter.delete_session`, SSE streaming & metrics on `bridge/server.py`, safe Playwright/fallback action helpers (`click_element`, `fill_input`, `capture_snapshot`), best-effort bridge session cleanup in `replacement.py`, and `401` halt handling in `worker_daemon.py`.
- **Acceptance Criteria:** Bridge & provider/browser tests pass (`pytest` + `ruff`), zero CAPTCHA/anti-bot evasion.

## Increment 5 — GitHub CI/CD, Templates, Security Policy & Unified E2E Gate (Worker 4 / Standby Worker 5)
- **Scope:** Add `.github/workflows/ci.yml`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE/task.md`, `docs/SECURITY_POLICY.md`, include `bridge/` checks inside `scripts/ubuntu/verify.sh` and `scripts/windows/verify.ps1`, and add E2E regression tests for 401 daemon revocation & replacement bridge cleanup.
- **Acceptance Criteria:** `scripts/ubuntu/verify.sh` validates backend + bridge + secret scan + frontend in a single exit-0 run.
