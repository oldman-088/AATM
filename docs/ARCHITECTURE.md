# AATM — Architecture

AATM (AI Agent Team Manager) runs a team of AI employees: a **manager** that turns a directive
(Arabic or English) into tasks, **workers** that execute them in parallel in isolated git
worktrees and open draft PRs, and a **reviewer**. A human approves merges and spending.

```
Dashboard (React, AR/FA/EN) ──/api, /ws──► FastAPI backend ──► Orchestrator (manager + scheduler)
                                                │                       │
                                                │             AgentLoop workers (4 by default, autoscaled)
                                                │                       │
                                   Postgres / SQLite      Provider router ─► Claude / OpenAI-compatible / Ollama
                                   RabbitMQ (wake bus)                    └► arena_browser ─► local bridge ─► userscript
                                   Redis (locks, fan-out)                                         in each employee's profile
                                                │                       │
                                                └──────── git worktree per task ─► draft PR ─► review ─► human-approved merge
```

## 1. The 8-step workflow
1. **Request:** a user directive arrives in the manager chat.
2. **Analyze & split:** the manager creates implementation tasks.
3. **Distribute:** the scheduler assigns tasks by specialty (division, permissions) with an atomic claim.
4. **Execute:** each worker runs in its own worktree (`aatm/task-<id>-<slug>`), sandboxed tool execution.
5. **Collect:** deliverables are verified (files exist, workspace tests pass), then committed.
6. **Pull request:** a draft PR is opened on GitHub (`gh pr create --draft`).
7. **Review & merge:** the reviewer comments or requests changes (AI reviewers can never approve). The merge runs only after a human approves the `merge` ApprovalRequest.
8. **Completed:** when all implementation tasks are done, the project gets a factual summary (PRs, cost, elapsed time).

The dashboard timeline (`GET /projects/{id}/timeline`) computes each step's state from these rows.

## 2. Backend (`backend/app/`)
| Package | Responsibility |
|---|---|
| `api/routes/` | REST routes. `platform.py` aggregates the domain routers `platform_{employees,accounts,browsers,workspaces,ops,github}.py`; also `arena.py` (Phase 6), `dashboard.py` (Phase 7), `reviews.py`, `scaling.py`, … |
| `orchestration/` | `scheduler.py` (dispatch, atomic claims), `review.py` (review rounds, merge approvals, completion), `autoscale.py` (approval-gated extra workers), `replacement.py` (employee replacement, emergency stop) |
| `runtime/` | `agent_loop.py` (poll → claim → execute loop) built from the mixins in `runtime/loop/` (`claim`, `context`, `actions`, `approvals`); `agent_protocol.py` (structured actions); `executor.py`/`sandbox.py` (contained execution); `worker_daemon.py` (remote nodes) |
| `providers/` | `router.py` (priority/health/budget routing, failover; no failover on `needs_human`); `adapters/` (one module per provider type: `arena_bridge`, `arena_browser`, `arena_agent` stub, `anthropic`, `openai_compatible`, `unsupported`); `credential_policy.py`; `concurrency.py` (per-provider limits) |
| `browser/` | `manager.py` (persistent Playwright profiles, locks, snapshots); `safety.py` (selector/URL validation, SSRF guard, redaction); `capabilities.py`; `wall.py`; `engines.py` (userscript policy); `arena_connector.py` (Phase 6 gate, tokens, userscript generator) |
| `queue/` | Wake bus: RabbitMQ (aio-pika) → Postgres `LISTEN/NOTIFY` → in-process. The DB claim stays authoritative; duplicate wakes are harmless. |
| `coordination/` | Redis locks, shared rate limits and WebSocket fan-out, with in-process fallback |
| `memory/` | Permissioned memory. Postgres full-text search (GIN index, `simple` config, Arabic-safe); SQLite keyword fallback |
| `git_workflows/`, `github_client/` | Worktrees, commits, PRs, approval-bound GitHub writes (async subprocess) |
| `qa/` | Secret scanner, self-healing sweep, team intelligence |

The data model uses SQLAlchemy 2 async models with Alembic migrations, and verify.sh enforces a single head.
Events go out on `/ws/events` (authenticated). The live agent log streams `agent.iteration` events.

## 3. Bridge (`bridge/`)
A loopback FastAPI daemon (`127.0.0.1:4100`) with three parts:
- the OpenAI-compatible `/v1/chat/completions`, with per-employee session isolation;
- the **connector hub** (`bridge/connector.py`): the `/v1/connector` WebSocket used by each profile's userscript, plus admin routes under `/v1/connector/profiles`;
- the **mock Arena page** at `/mock-arena/`, used for end-to-end tests.

## 4. Frontend (`frontend/src/`)
React 18, TypeScript, Vite and Tailwind, with i18n in `i18n/{en,ar,fa}.ts`. Arabic is the default and the layout uses CSS logical properties for RTL.
- **Views:** `DashboardViews.tsx` (mission control, timeline, employee cards, live log, PR view, recovery), `ArenaViews.tsx` (connector panel), and the Core/Infra/Governance/Enterprise views.
- **API calls** use relative `/api` and `/ws` URLs only.

## 5. Team ownership (multi-agent development)
| Role | Owned paths |
|---|---|
| Coordinator (`manager-0`) | `AATM_MASTER_SPECIFICATION.md`, `AGENTS.md`, `docs/`, `CHANGELOG.md` |
| Worker 1 — Backend | `contracts/`, `backend/app/models/`, `backend/alembic/`, `backend/app/schemas/`, `backend/app/api/routes/` |
| Worker 2 — Frontend | `frontend/src/` (UI, i18n EN/AR/FA, RTL, Vitest) |
| Worker 3 — AI Systems | `bridge/`, `backend/app/providers/`, `backend/app/browser/`, `backend/app/employees/`, `backend/app/orchestration/`, `backend/app/runtime/` |
| Worker 4 (Worker 5 standby) — DevOps, GitHub, QA & Security | `.github/`, `backend/app/github_client/`, `backend/app/git_workflows/`, `backend/app/qa/`, `scripts/`, `docs/SECURITY.md`, `backend/tests/test_e2e_verification.py` |

If Worker 4 becomes unavailable, Worker 5 continues from Worker 4's latest pushed commit, never from scratch.

See [`SECURITY.md`](./SECURITY.md) for controls and [`OPERATIONS.md`](./OPERATIONS.md) for running AATM.
