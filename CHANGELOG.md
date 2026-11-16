# Changelog

All notable changes to AATM are documented in this file.

## [Unreleased] — 3.1.0 (branch `aatm-complete`)

### Security (A)
- **Provider credential exfiltration fix**: `credential_ref` accepts only provider API-key env names (`*_API_KEY` or `PROVIDER_CREDENTIAL_ENV_ALLOWLIST`); server secrets (reusing `SENSITIVE_ENV_EXACT`) are always denied; `ARENA_BRIDGE_TOKEN` is bound to the local bridge; `endpoint_url` validated (http/https, no embedded credentials); operators limited to official presets, custom endpoints need admin; every test-connection is audited without the key. Applied to the wizard, provider upsert/test and account creation, plus a runtime deny-list in the adapters.
- Arena provider accounts no longer default to `env:ARENA_SESSION_TOKEN` (Arena uses the human's own browser sign-in).

### Added — Phase 4: Review, Merge and Completion (B)
- **Reviewer employee** (reuses the `code_reviewer` template, role `reviewer`, created on first need, read-only). The manager's orchestration cycle turns every `pr_open` implementation task into a `review` task; the reviewer gets the branch diff (quarantined as untrusted data) and must finish with the new `review` action (`COMMENT` or `REQUEST_CHANGES` — AI reviewers can never approve).
- `REQUEST_CHANGES` → `follow_up` task for the original employee on the same branch/worktree (review text quarantined), commits pushed, PR re-reviewed; capped by `MAX_REVIEW_ROUNDS` (default 2), then escalated to a human. GitHub refuses REQUEST_CHANGES on the account's own PR, so AATM falls back to a COMMENT review carrying the verdict.
- **Approval-gated merge**: `ApprovalRequest(action_type="merge")` bound to repo + base branch + PR number + task; a human approval in the Approval Center runs `gh pr ready` + `gh pr merge --squash`; single use (replay refused), mismatched PR refused; task `approved → merged → done`, worktree cleaned.
- **Project completion**: when all implementation tasks are done, the manager posts a factual summary (what was built, merged PRs, cost from `provider_usage_logs`, elapsed time) to the project chat and marks the project `completed`.
- API: `GET /reviews/pull-requests`, `GET /reviews/tasks/{id}/diff`, `POST /reviews/tasks/{id}/request-merge`, `POST /reviews/tasks/{id}/merge`, `GET /projects/{id}/completion`. Migration `f5a6b7c8d9e0` (task_type, parent_task_id, review_round, project completed_at/completion_summary).

### Added — Phase 6: Profiles Manager + Arena Browser Connector (D)
- **Real Arena is OFF by default.** Every real-Arena path (provider calls, userscript generation, bridge registration, "open for sign-in") returns `not_enabled` unless `ARENA_CONNECTOR_ENABLED=true`, `ARENA_AUTOMATION_APPROVED=true` and a non-empty `ARENA_APPROVAL_REFERENCE` are set; the bridge checks the same three flags in its own environment before it accepts a non-loopback origin. The UI shows "Waiting for Arena approval". `ARENA_MOCK_URL` (loopback only) enables the connector against the local mock page served at `/mock-arena/` for testing.
- **Profiles Manager**: one persistent profile per employee under `BROWSER_PROFILES_DIR/<agent_id>` (visible mode, for manual sign-in), created with the employee and closed on scale-down. `GET /arena/profiles` feeds the Browser Wall (color, status, URL, snapshot, lock, connector health). The `browser_use`/`openbrowser` engines are relabelled as Playwright name aliases.
- **Arena browser connector**: a Tampermonkey userscript (generated per profile, `@grant none`, `@noframes`, `@match` only the mock URL plus approved Arena domains; reviewed and installed manually by a human) connects to the bridge WebSocket `/v1/connector` with a per-profile rotatable token (only its SHA-256 is stored; the stored copy of the script is redacted). New provider mode `arena_browser` (provider `arena-browser`, created on first use) returns an OpenAI-compatible completion with session-isolation headers. It is the default for workers with a profile once the connector is enabled. Connector health is shown in bridge `/v1/sessions`.
- **Human-in-the-loop safety**: one in-flight request per profile, a minimum delay (`ARENA_MIN_REQUEST_INTERVAL_SECONDS`), a daily cap (`ARENA_DAILY_CAP_PER_PROFILE`, which survives token rotation), and no account pooling or rotation. Any human-verification element, verification page, rate-limit or terms prompt stops that profile with `needs_human`: the task is blocked, a critical notification is raised, and there is no failover to paid providers. After the human resolves it in the browser, `POST /arena/profiles/{id}/resume` re-queues the task. The connector never solves or bypasses checks, never reads cookies or storage, and AATM never injects scripts (`run_userscript` stays 501).
- Owner-configurable real-Arena DOM selectors (`ARENA_DOM_SELECTORS`, JSON), sent to the userscript as data. `scripts/ubuntu/start.sh` now also starts the loopback bridge (`ARENA_BRIDGE_AUTOSTART=false` to skip). Migration `a6b7c8d9e0f1` adds the connector fields to `browser_profiles`.
- The pre-existing `arena_agent` stub is unchanged (still reports not implemented); the working connector is the new `arena_browser` provider type.

### Added — Phase 5: True Parallelism (C)
- **Default team = 1 manager + 4 parallel workers** (`DEFAULT_ACTIVE_WORKERS=4`, `AGENT_MAX_PARALLEL=4`), each with its own browser profile. `ARENA_BRIDGE_MAX_CONCURRENCY` (default 1) is now a **per-profile/account** in-flight limit, so four profiles run in parallel while each profile keeps one request in flight.
- **Autoscaling** (`app/orchestration/autoscale.py`): when >= `SCALE_UP_BACKLOG_THRESHOLD` (3) ready tasks waited >= `SCALE_UP_WAIT_SECONDS` (120) and no worker is idle, `AUTO_SCALE_MODE=approval` (default) opens one `scale_up` spend ApprovalRequest; workers are created only after a human approves it (single use). `automatic` creates them directly (audited); `off` disables scaling. Never exceeds `MAX_CONCURRENT_WORKERS` (25). Extra workers idle for `SCALE_DOWN_IDLE_MINUTES` (30) are disabled and reused later; default workers are never removed.
- **Message broker wake bus** (`app/queue/`): RabbitMQ via aio-pika (durable `aatm.tasks.<device_id>`, `aatm.manager`, `aatm.events`, dead-letter exchange `aatm.dlx` → `aatm.dead_letter`, ack after the claim committed) → Postgres `LISTEN/NOTIFY` fallback → in-process. The atomic `UPDATE … RETURNING` claim stays the source of truth, so duplicate or redelivered wakes never cause double execution; the agent loop still polls when the broker is down.
- **Redis coordination** (`app/coordination/`, optional): distributed locks (one orchestration cycle at a time across backend processes), shared login rate limiting and WebSocket event fan-out; in-process fallback when `REDIS_URL` is unset.
- **Per-provider concurrency limits** (`PROVIDER_CONCURRENCY_LIMITS="id=N,…"`) around both normal and streaming provider calls.
- **No blocking subprocess in async code**: GitHub client, git workflow manager, sandbox status, worker-daemon tool runner and the QA secret scan now use `asyncio.create_subprocess_exec` (`app/core/async_subprocess.run_exec`, with timeout kill). An AST guard test fails if `subprocess.run`/`time.sleep` appears inside an `async def`.
- **Postgres by default**: `docker-compose.yml` now runs postgres, rabbitmq (AMQP + management UI on localhost only), redis (password required), backend, bridge and a scalable `worker` (`docker compose up --scale worker=N`). `scripts/ubuntu/install.sh` starts them and writes Postgres/RabbitMQ/Redis URLs into `.env` (pass `--sqlite` to stay on SQLite). The dashboard warns when more than 3 employees run on SQLite.
- API: `GET /scaling/status`, `POST /scaling/workers` ("Add employee" → approval), `POST /scaling/scale-down`. UI: Team-size panel ("4 default + N extra", backlog, queue/coordination backend, SQLite warning, Add employee) in EN/AR/FA.
- `scripts/load_test.py`: in-process load test (scripted model, no spend; checks exactly-once execution, no "database is locked", API p95) on SQLite or a throwaway Postgres, plus a read-only `--mode http` latency probe.

### Performance
- SQLite engine uses WAL + `synchronous=NORMAL` + `busy_timeout=30000`; adapters reuse one verified TLS context. Backend suite runtime 9 min → ~3 min and the flaky retry-latency budget test passes.

### Docs
- Added `docs/AATM_BUILD_PLAN.md` (source of truth); marked `AATM_MASTER_SPECIFICATION.md`, `docs/IMPLEMENTATION_ROADMAP.md`, `docs/DEVELOPMENT_PLAN.md` as SUPERSEDED.

## [3.0.0] - 2026-11-16 — Phase 8: Packaging, Installers, Docs & Release Verification

### Changed
- **Release `v3.0.0` Version Parity**: Updated `APP_VERSION` in `backend/app/core/config.py` and `frontend/package.json` to `3.0.0`.
- **Single-Head Migration Gate**: Updated `scripts/ubuntu/verify.sh` and `scripts/windows/verify.ps1` to verify `alembic heads` resolves to a single head alongside `alembic upgrade head`.
- **Documentation & Packaging**: Updated `README.md` and `docs/DEPLOYMENT.md` for `v3.0.0` installer-generated admin credentials, security posture, live PNG browser snapshots, hardened sandbox, and cross-platform verification gates.
- **Phase 8 Test Suite**: Added `backend/tests/test_phase8_packaging_docs.py` verifying `3.0.0` version parity, `/api/health`, single Alembic head, `bash -n` syntax on all Ubuntu scripts, Windows PowerShell parity, Dockerfiles, CI workflow, and README credential hygiene.

## [2.6.0-phase7] - 2026-11-16 — Phase 7: Frontend UX, RTL/LTR & Real Live Status

### Added
- **Full 12-Tab Mobile Navigation**: Replaced truncated 6-tab mobile header nav with a full 12-tab horizontally scrollable navigation bar in `App.tsx`.
- **Real WebSocket Live Status & Auto-Reconnect**: Added `wsStatus` (`live` / `connecting` / `offline`) indicator badge in the workspace header driven by real `/ws/events` `auth.ok` frames with reconnect backoff.
- **Crisp Light-Mode Surface Palette**: Added `html.light` contrast rules in `frontend/src/index.css` so toggling Light ↔ Dark updates surfaces, borders, and typography across views.
- **Runtime Security Posture Card in QAView**: Wired `GET /api/v1/governance/security-posture` into `QAView` with localized EN/AR/FA labels.

## [2.6.0-phase6] - 2026-11-16 — Phase 6: Security & Governance Hardening

### Added
- **Security Headers & No-Store Auth Cache**: Added HTTP security headers middleware (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, `Content-Security-Policy`) and `Cache-Control: no-store` on `/api/v1/auth/*` responses.
- **Server-Side Logout Token Revocation**: Added `POST /api/v1/auth/logout` incrementing `token_version` + recording `auth.logout` audit events (allowed even during forced password change), wired into `AuthContext.logout()`.
- **Approval Replay Protection & Payload/Audit Redaction**: `decide_approval` rejects re-deciding already decided approvals (`409 Conflict`); `redact_sensitive_mapping` strips secret/token/password keys from approval payloads and audit detail responses.
- **Security Posture Endpoint**: Added `GET /api/v1/governance/security-posture` reporting sandbox, model, password-change, warning, device, and header enforcement status.
- **Phase 6 Test Suite**: Added `backend/tests/test_phase6_security_governance.py`.

## [2.6.0-phase5] - 2026-11-16 — Phase 5: Real Browser Engine & Live PNG Snapshots

### Added
- **Live Playwright PNG Snapshots & Streaming**: `BrowserManager` caches real Playwright PNG screenshots (`capture_snapshot` and live navigation actions) and exposes `POST /api/v1/browsers/{profile_id}/snapshot` and `GET /api/v1/browsers/{profile_id}/snapshot` (`image/png`), returning HTTP `501` when no live Playwright context exists.
- **Real Chromium RSS Memory Telemetry**: `BrowserManager._measure_profile_memory_mb` measures actual RSS MB of Chromium processes bound to each profile directory via `psutil` (`memory_status: "measured"` when running, `"not_measured"` otherwise).
- **Browser Wall UI**: `BrowserWallView` displays live PNG screenshots, includes a Snapshot capture trigger, and shows honest `role="alert"` error banners when Chromium is unavailable (`501`).
- **Phase 5 Test Suite**: Added `backend/tests/test_phase5_real_browser_engine.py`.

## [2.6.0-phase4] - 2026-11-16 — Phase 4: Worker Sandbox & Hardening

### Added
- **Docker Non-Root & Fail-Closed Enforcement**: `build_isolated_command` adds explicit `--user 10001:10001`, `--env HOME=/tmp`, and `--env PYTHONDONTWRITEBYTECODE=1` alongside `--pull=never`, `--network=none`, `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and resource limits; blocks `process` mode at runtime in `production` unless `AATM_ALLOW_PROCESS_SANDBOX=true`.
- **Non-Blocking Async Sandbox Subprocesses**: `AgentRuntimeExecutor.run_workspace_command` uses `asyncio.create_subprocess_exec` with `asyncio.wait_for` timeout kill and bounded stdout/stderr capture so concurrent worker tasks never block the event loop.
- **Sandbox Policy Endpoint**: Added `GET /api/v1/sandbox/status` exposing live sandbox availability (`READY` vs `SANDBOX_UNAVAILABLE`) and active `SandboxExecutionPolicy` resource bounds.
- **Phase 4 Test Suite**: Added `backend/tests/test_phase4_sandbox_hardening.py`.

## [2.6.0-phase3] - 2026-11-16 — Phase 3: Isolated Task Worktrees, Auto-Commit, Draft PRs & Webhook Lifecycle

### Added
- **Per-Task Git Worktrees (`aatm/<task-id-short>-<slug>`)**: `GitWorkflowManager.create_task_worktree` creates isolated per-task git worktrees under `data/worktrees/task-<short>/` and `AgentLoop` executes worker actions directly inside the task's worktree when `worktree_mode` is enabled on the project.
- **Worktree `.git` Sandbox Isolation**: `safe_workspace_path` and `validate_run_command` block `.git` file/directory access inside task worktrees while allowing `read`, `write`, `pytest`, and `ruff` on project files.
- **Auto-Commit & Draft PR Automation**: `GitWorkflowManager.commit_task_worktree` stages and commits worktree edits as `AATM(<agent_name>): <task title>` with an `AATM-Task: <task_id>` trailer, and `push_and_create_draft_pr` pushes to `origin` and opens a draft PR via `gh pr create --draft` using `asyncio.create_subprocess_exec`.
- **Extended Task Lifecycle & Alembic Migration (`e4b2c9d7a1f3`)**: Added `branch_name`, `worktree_path`, `commit_sha`, `pr_number`, `pr_url`, and `pr_status` on `tasks` and `task_id` / `pr_url` on `git_workflow_records`; supports `pending → in_progress → pr_open → in_review → changes_requested → approved → merged → done`.
- **GitHub Webhook Lifecycle & Post-Merge Cleanup**: `POST /api/v1/github/webhooks` updates linked `Task` and `GitWorkflowRecord` lifecycle statuses on `pull_request` / `pull_request_review` events and runs `cleanup_task_worktree` after PR merge or cancellation.
- **Migration-Only Startup**: Removed `Base.metadata.create_all` from `backend/app/main.py` lifespan and added `alembic upgrade head` to `scripts/ubuntu/start.sh` and `scripts/windows/start.ps1`.
- **Phase 3 Test Suite**: Added `backend/tests/test_phase3_worktrees_pr_lifecycle.py`.

## [2.6.0-phase2] - 2026-11-16 — Phase 2: Real Model Connection & Setup Wizard

### Added
- `backend/app/providers/model_pricing.json` & `compute_cost_details()`: Externalized model rate catalog where real API models record `pricing_known=True` and Arena/unlisted models record `0.0` with `pricing_known=False`.
- `ProviderUsageLog.pricing_known` (`backend/alembic/versions/d8f1a3b5c7e9_add_provider_usage_pricing_known.py`, revising `c2d4e6f8a1b3`), surfaced in `GET /api/v1/providers/usage` (`summary.has_unknown_pricing`, `summary.pricing_known`, per-log `pricing_known`) and rendered in the UI as `"Unknown cost"` / `"تكلفة غير معروفة"` / `"هزینه نامشخص"`.
- `get_model_configuration_status()` in `backend/app/providers/router.py`, surfaced in `GET /api/health` and `GET /api/v1/dashboard/overview`, plus `NO_MODEL_CONFIGURED` gating in `TaskScheduler.handle_manager_directive` and `AgentLoop._execute_message`.
- Provider Setup Wizard API (`GET /api/v1/providers/wizard/presets`, `POST /api/v1/providers/wizard/test-connection` running a live `max_tokens=1, dry_run=False` probe, and `POST /api/v1/providers/wizard/configure` enforcing `env:`-only credentials).
- Localized UI (`en`, `ar`, `fa`): red `NO_MODEL_CONFIGURED` Dashboard banner linking directly to the 3-option Provider Setup Wizard, per-employee provider/model assignment controls in `AgentsView`, and `Unknown cost` badges in `ProvidersView`.
- `backend/tests/test_phase2_real_model_wizard.py` & `bridge/tests/test_bridge.py`: Tests covering pricing catalog, `diagnostic_only` bridge labeling/rejection, wizard endpoints, per-employee model assignment, and an Arabic directive end-to-end execution creating `hello.py` + `test_hello.py` and passing `pytest`.

### Changed
- Labeled `bridge/server.py` `/health` and `/v1/chat/completions` payloads with `mode: "diagnostic_only"` / `diagnostic_only: true` when `ARENA_BRIDGE_UPSTREAM_URL` is unset, and blocked `ArenaLocalBridgeAdapter.generate()` when `allow_diagnostic_fallback=False`.
- Fixed `POST /api/v1/chat` (`backend/app/api/routes/orchestration.py`) to snapshot `user_message` fields before `handle_manager_directive` commits, preventing `MissingGreenlet` errors under live provider execution.
- Updated `PATCH /api/v1/agents/{id}` and `POST /api/v1/employees` (`backend/app/employees/catalog.py`) to validate `provider_id` and accept per-employee `model_name`.

## [2.5.1-phase1b] - 2026-11-16 — Phase 1B: Security Review Follow-Up Fixes

### Added
- `Settings.TRUSTED_PROXIES` (default empty) and CIDR/IP-aware `X-Forwarded-For` validation in `POST /api/v1/auth/login`, plus an independent per-email sliding-window rate limit so rotating `X-Forwarded-For` still triggers `429`.
- `User.token_version` (`backend/alembic/versions/c2d4e6f8a1b3_add_user_token_version_and_flag_default_passwords.py`), embedded in JWT claims, verified in `get_current_user`, `get_current_user_optional`, and `/ws/events`, and incremented on password change to revoke older tokens immediately.
- Startup and migration detection (`c2d4e6f8a1b3` + `seed_initial_data`) flagging existing accounts whose password matches known default/placeholder passwords with `must_change_password=True`.
- `get_sandbox_status()` in `backend/app/runtime/sandbox.py`, surfaced in `GET /api/health`, `GET /api/v1/dashboard/overview`, and the localized Dashboard UI (`SANDBOX_UNAVAILABLE` warning banner when Docker or `aatm-sandbox:latest` is missing).

### Changed
- Enforced `must_change_password` server-side in `get_current_user` (`403 {"code": "PASSWORD_CHANGE_REQUIRED"}` on all endpoints except `GET /api/v1/auth/me` and `POST /api/v1/auth/change-password`) and rejected unrotated accounts (`4403`) in `/ws/events`.
- Hardened `build_isolated_command` with `--pull=never` and default `docker` mode (no silent fallback to `process`), and updated `scripts/ubuntu/{install,start,start-worker}.sh` and `scripts/windows/{install,start,start-worker}.ps1` to build `aatm-sandbox:latest` from `docker/sandbox.Dockerfile` when Docker is available.
- Configured `docker-compose.yml` `worker` service with `AATM_SANDBOX_MODE=process` and `AATM_ALLOW_PROCESS_SANDBOX=true` (treating the non-root `read_only`, `cap_drop=ALL` container as the sandbox without mounting `docker.sock`).
- Restricted `/ws/events` authentication strictly to the first WebSocket message (`?token=` query strings are ignored).
- Cleaned up `POST /api/v1/auth/change-password` validation to reject any placeholder/default password and any password equal to the user's email.
- Expanded `backend/tests/test_phase1_security.py` with full all-method route enumeration, `lifespan` task tests, and regression tests for all 8 security fixes.

## [2.5.0-phase1] - 2026-11-15 — Phase 1: Security Hardening

### Added
- `backend/app/core/rate_limit.py`: Sliding-window `LoginRateLimiter` (5 failed attempts / 5 min per IP + email) with `429 Too Many Requests` + `Retry-After` and failed-login audit logs.
- `backend/app/models/user.py` & `backend/alembic/versions/9a4e3d2c1b0f_add_user_must_change_password.py`: `User.must_change_password` flag (set on newly seeded admin) and `POST /api/v1/auth/change-password` endpoint.
- `docker/sandbox.Dockerfile`: Non-root (`UID 10001`) sandbox image with `python`, `pytest`, and `ruff`.
- `docker/worker.Dockerfile`: Non-root (`UID 65534`) worker image installing `backend/requirements.txt`.
- `backend/tests/test_phase1_security.py`: Parametrized route enumeration (`app.routes`) verifying `401` on all non-public endpoints, RBAC `viewer` `403` enforcement on writes, `/ws/events` JWT auth + timeout rejection, `/git/status?repo_path=` 403 allowlist, production settings validator checks, login rate-limiting/auditing, forced password rotation, and docker sandbox isolation.

### Changed
- Router-level `Depends(get_current_user)` added across `governance`, `orchestration`, `projects`, `infra`, and `platform` routers, with explicit public allowlist (`POST /api/v1/auth/login`, `GET /api/health`, HMAC-verified `POST /api/v1/github/webhooks`, and device-token routes in `devices.py`), and `require_role("admin", "operator")` on write actions.
- `/ws/events` WebSocket requires a valid JWT (`?token=` or first-message `{token}` within 5s) before attaching to `event_bus`.
- `GET /api/v1/git/status?repo_path=` restricted to `REPO_ROOT` or registered `Project.repo_path` records (`403` otherwise).
- `Settings` defaults hardened: `HOST=127.0.0.1`, `CORS_ORIGINS=http://localhost:5173`, `AATM_SANDBOX_MODE=docker`, plus startup `validate_runtime_security()` blocking weak/default secrets or un-exempted `process` sandbox in `ENVIRONMENT=production`.
- `docker-compose.yml` binds Postgres to `127.0.0.1`, removes the `aatm_password` fallback, and builds `worker` from `docker/worker.Dockerfile`.
- `scripts/ubuntu/install.sh` and `scripts/windows/install.ps1` generate CSPRNG `SECRET_KEY`, `DEFAULT_ADMIN_PASSWORD`, `ARENA_BRIDGE_TOKEN`, and `POSTGRES_PASSWORD` on first install and print the initial admin password once.
- `frontend/src/App.tsx` & `AuthContext.tsx`: Removed pre-filled admin credentials, gated telemetry & `/ws/events` (`{token}` handshake) on authentication, and added a localized forced password-change gate when `must_change_password` is true.
