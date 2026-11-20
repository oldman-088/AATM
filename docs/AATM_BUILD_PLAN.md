# AATM — Full Build Instructions for the Manager Agent (one batch)

Repository: `oldman-088/AATM` — start from the latest `main`.
You are the lead engineer and integration manager. Execute ALL phases below, in order, without waiting for further instructions between phases. Read the actual code (not README or old docs) before each phase.

Target: make AATM work like the owner's infographic:
Dashboard (Arabic) → Orchestrator (manager agent) → Profiles Manager → 8 parallel AI workers by default (more on demand, up to 20+), each in its own Chrome profile with its own Arena session driven by a Tampermonkey connector → results collected → Pull Request on GitHub → review → approved merge → project completed.

Important: Arena agents are connected through the Arena web page in the browser (Chrome profile + Tampermonkey userscript), NOT through an API. API/local models (Claude API, Ollama, OpenAI-compatible) are only an additional provider and a test harness for the pipeline.

---

## 0. Working Rules (every phase)

1. One phase = one branch from the latest `main` = one PR. Name it `phase-N-<name>` if the platform allows; if the platform locks the branch name (e.g. `arena/<id>-aatm`), use the locked branch, but still keep one PR per phase and update it from the latest `main` before each new phase. Merge a PR only when `bash scripts/ubuntu/verify.sh` passes fully (alembic upgrade head, ruff backend + bridge, backend pytest, bridge pytest, secret scan, frontend tsc + vitest + build) AND `alembic heads` shows a single head. Then start the next phase from the updated `main`.
2. Never force-push or commit directly to `main`. Never rewrite history.
3. Keep every existing safety control: strict action protocol (`runtime/agent_protocol.py`), workspace containment (`runtime/sandbox.py`), approval gates (`verify_approval`), emergency stop, per-task budget, SSRF guard, CAPTCHA/stealth/cookie blocklists. Extend, never weaken.
4. No fake features: unimplemented things return explicit `not_implemented` / `not_configured`. Keep `tests/test_phase7_fake_features.py` passing. Never weaken a test assertion; if a combination breaks a test, fix the code.
5. Use `asyncio.create_subprocess_exec` for subprocesses in async code; never `subprocess.run` inside `async def`.
6. Do not rewrite the backend in Node.js. Keep Python/FastAPI.
7. All UI strings in `frontend/src/i18n/{ar,en,fa}.ts`; Arabic is primary and fully RTL.
8. Update `CHANGELOG.md` in every phase.
9. Branch cleanup after each merge: for every remote branch fully contained in `main`, push a backup tag `archive/<branch-name>`, then delete the branch. Never delete a branch with commits not in `main` or with an open PR.
10. After each phase, post a short report (PR link, merge commit, what now works + demo steps, full verify.sh counts, branches deleted + tags) and CONTINUE to the next phase. Stop only if blocked by something that requires the owner (credentials, Arena approval, a decision that cannot be undone).

---

## Phase 1b — Remaining Security Fixes

**Status: already done in PR #7 (branch `arena/ba386950-aatm`, commit `2e3ffab`).** First action: re-run verify.sh on PR #7, merge it into `main`, then continue with Phase 2. Re-implement the items below only if something is missing after the merge.

Phase 1 (PR #6) is merged; these defects remain in `main`:
1. `api/routes/auth.py::_client_ip` trusts `X-Forwarded-For` unconditionally (rate-limit bypass). Add `TRUSTED_PROXIES` (default empty); use the header only when `request.client.host` is a trusted proxy. Add a per-email limit independent of IP. Test: rotating `X-Forwarded-For` still gets 429.
2. `must_change_password` is enforced only in the frontend. Enforce it in `api/deps.py::get_current_user`: when true, return 403 `{"code":"PASSWORD_CHANGE_REQUIRED"}` everywhere except `GET /auth/me` and `POST /auth/change-password`; the WebSocket rejects too.
3. Existing installs: migration `9a4e3d2c1b0f` defaults the flag to false. Set `must_change_password=true` for any user whose password verifies against a known default/placeholder password.
4. Add `User.token_version` (migration), include it in the JWT, verify it in `get_current_user` and the WebSocket, increment on password change. Test: old token rejected after password change.
5. `aatm-sandbox:latest` is never built. Install/start scripts (Ubuntu + Windows) build `docker/sandbox.Dockerfile` when Docker exists; `build_isolated_command` passes `--pull=never`; if Docker/image is missing, report `SANDBOX_UNAVAILABLE` in `/api/health` and the Dashboard — never silent fallback to process mode.
6. The compose `worker` has no Docker CLI: set `AATM_SANDBOX_MODE=process` and `AATM_ALLOW_PROCESS_SANDBOX=true` for that hardened service only, document why, never mount the Docker socket.
7. WebSocket: accept the JWT only as the first message, not `?token=` (query strings get logged). Update frontend + tests.
8. `change-password`: remove the `== DEFAULT_ADMIN_PASSWORD and == "aatm-admin-2026"` check; reject placeholder/default passwords and a password equal to the email.

## Phase 2 — Real Model Connection (no more echo)

**Problem:** the default provider is the Arena Local Bridge in `local_reference` mode, which echoes the prompt. The agent loop fails 8 times on invalid JSON with no clear message. The manager (`orchestration/scheduler.py::_request_manager_plan`) also needs a real model.

**Tasks:**
1. Add a provider setup wizard (backend endpoint + Dashboard screen) with three options:
   - **Anthropic Claude API** (`ANTHROPIC_API_KEY`, existing `AnthropicClaudeAdapter`).
   - **OpenAI-compatible endpoint** (OpenRouter, LiteLLM, vLLM, LM Studio, **Ollama** at `http://127.0.0.1:11434/v1`) via existing `OpenAICompatibleAdapter`.
   - **Arena Local Bridge** with an upstream (Phase 6 adds the Arena connector).
   Each option has a "Test connection" button that sends a real 1-token request and shows latency + model name.
2. Provider credentials are stored only as `env:` references (keep `resolve_credential_ref`); never persisted in plaintext in the DB.
3. When no real model is configured: the bridge's `local_reference` mode must be labelled `diagnostic_only`; the `AgentLoop` and manager must **not** dispatch work to it and must set a visible system status `NO_MODEL_CONFIGURED` shown as a red banner in the Dashboard with a link to the wizard.
4. Allow assigning provider + model per employee (manager can use a stronger model than workers).
5. Replace the invented prices in `providers/adapters.py::MODEL_PRICING` for arena models with `0.0` + `pricing_known=false`; show "unknown cost" in UI instead of fake numbers. Keep real prices for known API models, loaded from a JSON file that's easy to update.

**Acceptance:** with Ollama running locally, a directive in Arabic in the Chat screen produces a manager plan with tasks assigned to workers, and at least one worker completes a task that writes a file and passes a test.

---

## Phase 3 — Git Worktree per Task + Automatic Pull Requests

**Problem:** agents work in an empty isolated folder `data/workspaces/<agent_id>` unrelated to the project repo. Branch/PR creation exists only as manual API calls (`git_workflows/manager.py`, `github_client/client.py`).

**Tasks:**
1. On task dispatch (`TaskScheduler.run_orchestration_cycle` / `dispatch_ready_tasks`): if the task's project has a repo, call `git_workflow_manager.create_agent_worktree` to create branch `aatm/<task-id-short>-<slug>` from the project's default branch, and set that worktree as the agent's workspace for this task. Fall back to the isolated folder only for projects without a repo.
2. Keep all containment rules: the worktree root is the sandbox root; `.git` stays blocked for read/write by the agent.
3. When the agent returns `done` and verification passes (existing logic in `agent_loop.py::_execute_action` for `DoneAction`):
   - `git add` only the verified deliverables + files the agent wrote (tracked via `WorkspaceArtifact`);
   - commit with message `AATM(<agent_name>): <task title>` and trailer `AATM-Task: <task_id>`;
   - push the branch (using the host's `gh` auth, never a token passed to the agent);
   - open a **draft** PR via `github_client.create_pull_request` with: task description, agent summary, list of deliverables, test results, cost, link back to the task in the Dashboard;
   - store the PR number/URL on the task and in `GitWorkflowRecord`.
   Pushing a non-protected feature branch and opening a draft PR is allowed without approval; it must be audited.
4. Task status flow becomes: `pending → in_progress → pr_open → in_review → changes_requested → approved → merged → done` (add an Alembic migration; remove the `Base.metadata.create_all` call in `main.py` lifespan and run `alembic upgrade head` in start scripts instead).
5. GitHub webhooks (`POST /github/webhooks`, already HMAC-verified) update task status on `pull_request` (opened/closed/merged) and `pull_request_review` events.
6. Clean up the worktree after merge or cancel.

**Acceptance:** a directive "أضف دالة جمع مع اختبار" on a test repo ends with a real draft PR on GitHub containing the code and a passing test, visible in the Dashboard task card with a PR link.

---

## Phase 4 — Review, Merge and Completion (workflow steps 6–8)

**Tasks:**
1. Add a **Reviewer** employee role (template in `employees/catalog.py`). When a PR opens, the manager creates a review task assigned to the reviewer. The reviewer reads the diff (read-only worktree), runs tests, and posts a PR review via `github_client.comment_on_pull_request` with `event=COMMENT` or `REQUEST_CHANGES`.
2. `REQUEST_CHANGES` → the original agent gets a follow-up task on the same branch with the review comments as input (treated as untrusted data).
3. Merge always requires a human approval in the Approval Center (`ApprovalRequest` with `action_type="merge"`, payload includes repo + base branch + PR number; enforced by `verify_approval`). After approval: merge via `gh pr merge --squash`, mark task `merged → done`.
4. Project completion: when all tasks of a project are `done`, the manager writes a final summary (what was built, PRs merged, cost, time) in the project's chat and marks the project `completed` (trophy state in the UI — workflow step 8).

**Acceptance:** full loop on a test repo: directive → plan → 2 parallel tasks → 2 PRs → reviewer comments → one change request handled → human approves → both merged → project marked completed.

---

## Phase 5 — True Parallelism for 20+ Employees

**Problems:** `ARENA_BRIDGE_MAX_CONCURRENCY=1` and `AGENT_MAX_PARALLEL=1` by default; DB polling; SQLite locking under concurrent loops; blocking subprocess calls in `runtime/executor.py`, `git_workflows/manager.py`, `github_client/client.py`.

**Tasks:**
1. Convert all subprocess calls in async paths to `asyncio.create_subprocess_exec` with timeouts.
2. Introduce a message broker abstraction `app/queue/` with two backends:
   - **RabbitMQ** (preferred, matches the owner's plan) using `aio-pika`: queues `aatm.tasks.<device_id>`, `aatm.manager`, `aatm.events`; durable, ack-after-commit, dead-letter queue for failed tasks.
   - **Postgres LISTEN/NOTIFY** fallback for small installs.
   Keep the existing atomic `UPDATE ... RETURNING` claim as the source of truth; the queue only wakes workers (no double execution).
3. **Redis** (shown in the infographic): use it for distributed locks (per-task, per-browser-profile), rate limiting, and WebSocket fan-out across multiple backend processes. Optional: if `REDIS_URL` is unset, fall back to in-process implementations.
4. Make PostgreSQL the default in `docker-compose.yml` and install scripts; SQLite stays for single-user dev only and the UI warns when more than 3 employees are enabled on SQLite.
5. **Default team = 1 manager + 8 workers running in parallel.** The manager runs in its own lane and does not count toward the worker limit. Set `DEFAULT_ACTIVE_WORKERS=8` and `AGENT_MAX_PARALLEL=8`; seed exactly these 9 employees (manager + 8 workers) (update `db/seed.py`), each with its own browser profile.
6. **Extra workers are added only when needed (on-demand scaling), up to `MAX_CONCURRENT_WORKERS=25`:**
   - Trigger: the ready-task backlog stays above `SCALE_UP_BACKLOG_THRESHOLD` (default 3) for `SCALE_UP_WAIT_SECONDS` (default 120), or a task needs a skill no active worker has.
   - The manager proposes a new employee (role template from `employees/catalog.py`, model/provider, reason). By default this creates an `ApprovalRequest` (`action_type="spend"`) that the human approves in the Approval Center; an admin setting `AUTO_SCALE_MODE=approval|automatic|off` (default `approval`) controls it. `automatic` still respects daily/monthly budgets and the per-provider concurrency limits.
   - New employees get their own profile, workspace and session exactly like the default 4.
   - Scale down: an extra employee idle for `SCALE_DOWN_IDLE_MINUTES` (default 30) is paused and its browser profile closed (kept on disk for reuse); the 8 default workers are never auto-removed.
   - Dashboard: "Add employee" button for manual addition, and a visible indicator `8 default + N extra`.
7. Per-provider concurrency limits are configured per provider/account, not globally.
8. Add `docker-compose` services: `rabbitmq` (management UI on localhost only), `redis`, `backend`, `bridge`, `worker` (scalable with `--scale worker=N`).

**Acceptance:**
- With the default team, 4 independent tasks run truly in parallel (overlapping execution timestamps) while the manager stays responsive.
- Load test `scripts/load_test.py` creates 20 tasks: the backlog triggers scale-up proposals; after approval, extra employees join, all 20 tasks complete with no duplicated execution and no `database is locked`; event-loop latency (heartbeat endpoint) stays under 200 ms; after the idle period extras scale back down to the 8 defaults.

---

## Phase 6 — Profiles Manager + Arena Browser Connector (build fully now; live use stays switched off)

This is the heart of the infographic. Build and test it completely now. Using it against the real Arena site stays switched off until the owner records Arena's approval (the owner's own decision).

### 6a. Profiles Manager
1. One persistent Chromium profile per employee under `BROWSER_PROFILES_DIR/<agent_id>` (reuse `BrowserManager.launch_profile` and its lock files). Link `BrowserProfile.agent_id` ↔ `Agent` one-to-one; auto-create on employee creation; close on replacement/scale-down.
2. Profiles launch in `visible` mode so the human signs in to Arena manually, once per profile. AATM never stores, types or reads passwords, cookies or tokens.
3. Dashboard "Browser Wall": a grid of all employee profiles (color per employee, status dot, current URL, last screenshot via `capture_snapshot`, lock status, connector status) — like the 5 profile cards in the infographic.
4. Remove or clearly relabel the `browser_use` / `openbrowser` adapters (they only delegate to Playwright).

### 6b. Arena connector (Tampermonkey ↔ local bridge)
1. Flags in `core/config.py`: `ARENA_CONNECTOR_ENABLED=false`, `ARENA_AUTOMATION_APPROVED=false`, `ARENA_APPROVAL_REFERENCE=""`. The connector talks to the real Arena domain only when both flags are true AND the reference is non-empty; otherwise Arena actions return `not_enabled` and the UI shows "Waiting for Arena approval". A `ARENA_MOCK_URL` setting allows full end-to-end testing against a local mock Arena page at any time.
2. If the owner provides an existing connector/userscript (e.g., files like agent-client.js, agent-dom.js, worker.js, panel.js), review it and reuse what complies with the constraints below instead of rewriting.
3. Bridge: add a WebSocket endpoint `ws://127.0.0.1:4100/v1/connector` in `bridge/server.py`. Each profile's userscript authenticates with a per-profile token (generated by AATM, rotatable). The bridge exposes the connector as a provider mode `arena_browser`, returned to `AgentLoop` as an OpenAI-compatible completion, keeping per-employee session isolation (`X-AATM-Session-Id`, `X-AATM-Account-Id`, `X-AATM-Agent-Id`). Implement `ArenaAgentAdapter` (currently `not_implemented`) on top of it.
4. Userscript (generated and versioned by AATM in the `userscripts` table, shown to the admin for review, installed manually by the human in each profile): receives one prompt at a time, types it into the Arena page, waits for the complete answer, sends the text back, reports status (`ready`, `busy`, `needs_human`, `offline`).
5. Constraints — enforce in code and tests:
   - one in-flight request per profile; configurable minimum delay between requests and a daily cap per profile (conservative defaults);
   - on any CAPTCHA, verification page, rate-limit message or terms prompt: stop immediately, mark the profile `needs_human`, notify the Dashboard — never solve or bypass it;
   - no access to cookies, `localStorage` tokens or `GM_cookie`; minimal `@grant`; `@match` limited to the approved Arena domain(s) and the mock URL;
   - each employee uses the account the human signed into in that profile; no account pooling or rotation;
   - `BrowserManager.run_userscript` stays disabled (501); AATM never injects scripts.
6. Provider selection: when the connector is enabled, `arena_browser` becomes the default provider for workers; the API/local providers remain available as alternatives.
7. Session health in bridge `/v1/sessions` and on each profile card (green/yellow/red dots).

**Acceptance:** flags off → every real-Arena path returns `not_enabled` (tests). Against the local mock Arena page: the 4 default worker profiles process 4 tasks in parallel through the userscript → bridge → AgentLoop → worktree → draft PR; a simulated CAPTCHA element stops that profile with `needs_human`; planted fake tokens never appear in logs.

## Phase 7 — Dashboard Matching the Infographic

1. **Home screen**: architecture strip (Dashboard → Orchestrator → Profiles → Results), live counters (active employees, tasks per status, open PRs, today's cost vs. budget).
2. **8-step workflow timeline** per project, exactly the infographic steps: 1) new project request, 2) manager analyzes and splits tasks, 3) tasks distributed by specialty, 4) execution in isolated sessions, 5) results collected, 6) PR created on GitHub, 7) review and merge, 8) project completed. Each step lights up from real task/PR state, never from mock data.
3. **Employee cards** (color per employee): name, role, model/provider, profile status, current task, last action, cost, buttons: pause, emergency stop, replace.
4. **Live agent log**: stream every iteration (action type, file, command, result excerpt — already persisted by `_persist_iteration`) via the authenticated WebSocket.
5. **PR view**: list of PRs with diff viewer, reviewer comments, approve/merge button (opens approval flow).
6. **Errors & recovery panel**: failed tasks, retries, replacements, `needs_human` sessions.
7. Add React Router and a data-fetching library (TanStack Query) instead of manual fetch state; store the JWT in memory + httpOnly refresh cookie instead of `localStorage`.

---

## Phase 8 — Memory, Docs and Code Health

1. Memory engine (`memory/engine.py` loads all rows and does token overlap in Python): move to Postgres full-text search + optional `pgvector` embeddings; keep scope permissions.
2. Split large files: `providers/adapters.py` (one file per adapter), `runtime/agent_loop.py` (claim / iteration / actions / verification), `browser/manager.py`, `api/routes/platform.py` (by domain).
3. Consolidate docs into four files: `README.md` (quick start), `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md`. Delete outdated status files (`STATUS.txt`, `PROGRESS_LOG.txt`, `NEXT_ACTIONS.txt`, `BLOCKERS.txt`, duplicate audit/report docs) after moving any still-true content.
4. Add an end-to-end demo script `scripts/demo_e2e.sh` that runs the Phase 4 acceptance scenario against a throwaway GitHub repo with a local Ollama model.

---

## Definition of Done

From a fresh Ubuntu machine: `install.sh` → `start.sh` → Dashboard in Arabic → create a project linked to a GitHub repo → write a directive in Arabic → the manager splits it into tasks → the 8 default workers execute **in parallel**, each in its own Chrome profile and worktree (via the Arena browser connector against the mock page, or a configured API/local model) → each produces a draft PR with passing tests → the reviewer comments → the owner approves in the Approval Center → PRs merge → the project shows **completed** with a summary and real cost. When the backlog grows, an extra worker is proposed, approved and joins. No mock data in the UI, no secret in logs, and real-Arena use stays off until `ARENA_AUTOMATION_APPROVED=true` with an approval reference.

## Final Report (after the last phase)
1. Table of phases: PR link, merge commit, status.
2. What works now, with step-by-step demo instructions for the owner.
3. What needs the owner: Arena approval, credentials, manual sign-in per profile, userscript installation steps.
4. Branches deleted + backup tags; final `main` commit; full verify.sh output with real counts.

---

## Final Step — Push Everything to GitHub and Update `main`

When all phases are done (or when you stop because the owner is required):
1. Make sure every commit of every phase is pushed to GitHub. Nothing may remain only in the local session.
2. Make sure every phase PR is merged into `main` (after verify.sh passes). If any PR is still open, update it from the latest `main`, resolve conflicts (keep both features and the stricter security behavior), re-run verify.sh, and merge it.
3. Check every remote `arena/*` branch: if it has commits not in `main` that are part of this work, open a PR, verify, and merge it so `main` contains the latest changes of all branches. If a branch is obsolete, explain why in the report instead of merging.
4. Update `main` to the final version: pull the latest `main`, run `alembic heads` (single head) and the full `bash scripts/ubuntu/verify.sh` on a fresh clone of `main`; all gates must pass.
5. Bump the version: set `APP_VERSION` in `backend/app/core/config.py` and `version` in `frontend/package.json` to the next version (e.g. `3.0.0`), add the release section to `CHANGELOG.md`, commit it through a PR, merge, then create and push the git tag `v3.0.0` on `main`.
6. Clean up: tag `archive/<branch-name>` and delete every remote branch fully contained in `main` (never delete a branch with unmerged commits or an open PR).
7. Send the final report with: final `main` commit hash, version tag, list of merged PRs, deleted branches + backup tags, and the full verify.sh output from the fresh clone.
