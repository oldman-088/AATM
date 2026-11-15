# AATM — Existing Project Audit

**Audit Date (UTC):** 2026-11-15  
**Base Commit Audited:** `6ee1f92` (`main`, post-PR #2 merge)  
**Authoritative Spec:** [`AATM_MASTER_SPECIFICATION.md`](../AATM_MASTER_SPECIFICATION.md)

---

## 1. Current Verified Baseline (Post-v2.0.0 & Sprint v2.1)
| Layer | Status | Verified Components | Evidence |
|---|---|---|---|
| **Backend API (`backend/`)** | Implemented & Tested | FastAPI + SQLAlchemy 2.0 Async + Alembic (`ee92483cefa3`), JWT/RBAC, 64 OpenAPI paths, WebSocket `/ws/events`, AuditLog, Approval Center. | `55/55` backend `pytest` passing, `ruff` 0 errors |
| **Frontend (`frontend/`)** | Implemented & Tested | React 18 + TypeScript + Vite + Tailwind CSS, 12-tab / 16-section Command Center, full EN (LTR) / AR (RTL) / FA (RTL) parity with logical CSS and relative `/api`/`/ws` URLs. | `22/22` Vitest passing, `tsc --noEmit` & `vite build` clean |
| **AI Providers & Reference Bridge (`bridge/`, `providers/`)** | Implemented (External Arena web chat restricted by official API availability) | `ArenaLocalBridgeAdapter`, `ArenaAgentAdapter`, `AnthropicClaudeAdapter`, `ProviderRouter` (daily/monthly spend caps, failover), `ProviderAccount` / `ProviderSession`, compliant reference `bridge/server.py` daemon. | `11/11` `test_worker3_providers_browser.py` + `3/3` `bridge/tests/test_bridge.py` |
| **Browser Automation (`browser/`)** | Implemented & Tested (`headless_fallback` in sandbox without Chromium) | Playwright persistent profiles, atomic `.aatm.lock` + PID liveness + async + DB locks, `BROWSER_ENGINE_REGISTRY` (`playwright`, `browser_use`, `openbrowser`, `automa`), policy-audited role-scoped Tampermonkey userscripts. | `test_worker3_providers_browser.py` |
| **Employees, Orchestration, Messaging & SDLC** | Implemented & Tested | 16 role templates + 20+ dynamic employee scale, isolated per-employee workspaces, `AgentMessage` bus with real execution, 3-mode DAG `TaskScheduler`, Idea-to-Delivery `/api/v1/sdlc/execute`, Smart Employee Replacement, 4-layer permissioned memory. | `test_enterprise_platform.py`, `test_e2e_verification.py` |
| **Multi-Device Worker Daemon** | Implemented & Tested | `ExecutionDevice` registry, `worker_daemon.py`, HMAC `device_auth.py`, `/devices/{id}/register|heartbeat|claim|report|status`, offline node task reclamation, Ubuntu/Windows launchers. | `test_e2e_multi_device_worker_daemon_round_trip` |
| **GitHub, QA & Ops (`github_client/`, `qa/`, `scripts/`)** | Implemented & Tested (live external repo writes require host auth + approval) | `gh`-backed repo/PR/issue read + approval-gated branch/PR/issue write helpers, 21-rule / 24-filetype static secret scanner, prompt-injection sanitizer, Global/Scoped Emergency Stop, `verify.sh`/`verify.ps1`. | `test_e2e_verification.py`, `test_contracts_v2.py` |

## 2. Gaps Identified Against `AATM_MASTER_SPECIFICATION.md`
1. **Master Spec & Agent Governance Files (Section 0.5):**
   - `AATM_MASTER_SPECIFICATION.md`, `AGENTS.md`, `docs/TEAM_STRUCTURE.md`, `docs/DEVELOPMENT_PLAN.md`, and `docs/SECURITY_POLICY.md` were missing or used earlier filenames (`AUDIT_REPORT.md`, `IMPLEMENTATION_ROADMAP.md`, `SECURITY_ASSESSMENT.md`).
2. **GitHub CI/CD & Templates (Section 0.3 & 24):**
   - Missing `.github/workflows/ci.yml`, `.github/PULL_REQUEST_TEMPLATE.md`, and `.github/ISSUE_TEMPLATE/`.
3. **Per-Device Token Revocation (`docs/SECURITY_ASSESSMENT.md` Medium Risk):**
   - Device HMAC tokens currently derive from `SECRET_KEY` + `device_id` without a per-device `token_version` / `/devices/{id}/revoke` endpoint.
4. **Cross-Division UI/API Polish:**
   - Provider budget headroom (`provider_router.get_spend_summary`) is not yet surfaced on `GET /api/v1/providers/usage` or in the Providers UI.
   - Approval-gated `POST /api/v1/github/prs` & `POST /api/v1/github/issues` endpoints exist in backend but lack dedicated UI creation controls in the GitHub view.
   - `replace_employee` in `replacement.py` does not yet invoke best-effort bridge session reset (`DELETE /v1/sessions/{id}`) on `arena_local_bridge`, and `verify.sh` does not yet include `bridge/tests/test_bridge.py`.
   - Worker daemon (`worker_daemon.py`) should explicitly halt polling on `HTTP 401` token revocation.

## 3. Architectural Decision
Preserve the working FastAPI + SQLAlchemy 2.0 + React/TS/Tailwind + `bridge/` + `worker_daemon.py` architecture and close the remaining gaps incrementally through non-overlapping worker PRs.
