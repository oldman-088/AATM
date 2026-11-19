You are a senior engineer working on the AATM repository (GitHub: oldman-088/AATM).
Base branch: r8-aatm (currently bfc9cbb, same content as release v3.6.0).

## Before you start
1. Read AGENTS.md and AATM_MASTER_SPECIFICATION.md. Their rules override this prompt.
2. git fetch && git checkout r8-aatm && git pull. Print the head commit and confirm it is bfc9cbb or newer.
3. Run `bash scripts/ubuntu/verify.sh` once to record the baseline (expected: all green, about 486 backend tests).
4. Read ~/aatm-reports/known_issues.md (if available). It has the evidence for every item below.

## Hard rules
- One focused PR per item (or per small group marked below), titled `[CLAUDE-FIX] <topic>`, from its own branch into r8-aatm.
- Every fix starts with a regression test that FAILS before the fix and PASSES after it.
- Never touch main, never create tags or releases, never merge anything. The owner merges.
- Never commit secrets, .env, databases, browser profiles or tokens. Use env:VAR_NAME indirection.
- Do not delete or rewrite working functionality. Make the smallest change that fixes the cause.
- Keep EN/AR/FA parity for any new UI text (CSS logical properties, i18n keys in all three languages).
- Before each PR: `bash scripts/ubuntu/verify.sh`, `.venv/bin/ruff check --config backend/pyproject.toml bridge`,
  `.venv/bin/pytest -v bridge/tests/test_bridge.py` — all green.
- Report each item as Complete / Partially complete / Not started / Blocked, with test names and PR links.
  Never claim something works unless you ran it.

## Work order (highest impact first)

### PR 1 — E1: workers are never freed, so tasks stop being dispatched  🔴
Cause: the scheduler dispatches only to agents with status == "idle"
(backend/app/orchestration/scheduler.py:278 `idle_workers`). Cancelling/failing/completing a task does not
free its agent (backend/app/api/routes/orchestration.py:265, cancel branch of update_task), and
"Replace employee" sets the replaced agent to "offline" (backend/app/orchestration/replacement.py:212)
and nothing ever returns it to "idle".
Fix:
- One helper `release_agent_if_free(session, agent_id)` that sets status="idle", current_task_id=None
  when the agent has no other task in assigned/in_progress and no reserving Arena session.
- Call it on every transition to cancelled/failed/completed/done/merged (API update, complete_task, merge path).
- When the bridge reports a connected window in page state ready/idle for an agent that is "offline"
  and has no open session, set it to "idle" (and audit-log it).
Tests: cancelling a running task frees its worker; a reconnected window turns an offline worker idle;
a queued task with satisfied dependencies is then dispatched in the next scheduler tick.

### PR 2 — E2: "Replace employee" can pick the manager  🔴
Location: backend/app/orchestration/replacement.py:143-151 (candidates filtered only by is_enabled,
emergency stop and status).
Fix: exclude role == "manager" (and reviewer-only roles for implementation tasks). If no worker is
available, refuse with a clear message (EN/AR/FA) instead of falling back to the manager.
Also add a one-time safe migration/command that moves tasks currently assigned to manager-0 back to
"queued" with assigned_agent_id = NULL (report the count; do not run it automatically on startup).
Tests: replacement never selects manager-0; with no free worker the API returns a 409 with a reason.

### PR 3 — E3: sessions of finished tasks stay open and reserve the worker  🔴
Cause: RESERVING_PHASES includes reviewing and needs_human (backend/app/orchestration/arena_runner.py:74-75);
nothing closes a session when its task reaches a final status; the runner only skips blocked/cancelled (:649).
Fix: whenever a task reaches done/completed/cancelled/failed/merged, call
backend/app/browser/arena_sessions.py:139 `close_session(..., reason=<status>)` for its open session,
then call the PR 1 helper. Do it in one shared function used by every final-status path.
Tests: completing, cancelling or merging a task closes its session and the worker takes the next task.

### PR 4 — E4: merges done directly on GitHub are not detected  🔴
Cause: the merged-PR check runs only for MONITORED_PHASES (arena_runner.py:497, :624-627);
"reviewing" and "awaiting_approval" are not polled.
Fix: poll PR state for sessions in reviewing/awaiting_approval at a low rate (default every 5 minutes,
configurable). When merged: mark the task merged, close the session (PR 3), cancel queued correction
rounds for that task. When closed without merge: mark the task blocked with a reason.
Tests: a PR merged outside AATM moves its task to merged within one cycle (mock GitHub client).

### PR 5 — E7: messages can be sent to a worker while its task is being cancelled  🟠
Location: arena_runner.py:916 `_follow_up`; backend/app/browser/arena_connector.py COMMANDS.
Fix:
- Re-read the task and session from the database immediately before every submit/keep_working/nudge;
  drop the message (and audit-log "dropped: task <status>") if the task is no longer active.
- Add a bridge command `stop` that clicks Arena's Stop button (reuse the hardened isStopLabel detection
  from #33; verify the page leaves "generating"). Call it when a running task is cancelled.
- Update bridge/mock_arena/index.html and the userscript; bump SCRIPT_VERSION.
Tests: a cancel landing before the send blocks the message; the stop command works on the mock page.

### PR 6 — E9 + E8: the manager over-plans, chains everything, and accepts out-of-policy goals  🟠
Location: backend/app/orchestration/manager_arena.py (plan handling), MANAGER_PROMPT.md.
Fix:
- Manager prompt: plan independent tasks that can start in parallel; add a dependency only when a task
  truly needs another task's output; prefer few, larger tasks; one-line requests → at most 1-3 tasks.
- Plan validator before any task is created:
  - reject cycles and dependencies on unknown tasks;
  - warn when more than 50% of tasks depend on a single task (parallelism score shown to the owner);
  - when the plan has more than 5 tasks or the goal is ambiguous, store it as a draft and show
    Approve / Edit / Reject in Manager Chat instead of creating tasks;
  - policy check: goals involving unauthorized scraping, surveillance or profiling of private
    individuals, CAPTCHA bypass or anti-bot evasion (AGENTS.md rule 5) create no tasks and return a
    clear reason to the owner.
Tests: a 10-task plan becomes a draft; a fully chained plan gets a warning; a policy-violating goal creates
no tasks; an approved draft creates the tasks.

### PR 7 — E12 + E18: no way to clear chats, cancel manager jobs or close sessions; no backup first  🟠
Location: backend/app/api/routes/orchestration.py:371 (GET /chat, no paging, no DELETE);
backend/app/api/routes/arena.py (no cancel for manager jobs, no session close).
Fix:
- Shared helper `backup_database(reason) -> path` (SQLite online backup API, file mode 0600, under
  data/backups/, keep the newest 20). Call it before bulk cancel, chat clear and replacement; put the path
  in the audit entry.
- DELETE /api/v1/chat (archive, never touch the audit log), paging on GET /chat,
  POST /api/v1/arena/manager/jobs/{id}/cancel, POST /api/v1/arena/sessions/{id}/close
  (closing a session frees the worker via PR 1). Admin only, audited.
- Dashboard buttons with confirmation dialogs (EN/AR/FA).
Tests: each endpoint (auth, audit entry, backup created, worker freed); frontend Vitest for the buttons.

### PR 8 — E6: Arena workers start from an old main  🟠
Location: worker task prompt build in arena_runner.py; MANAGER_PROMPT.md.
Fix: every worker prompt starts with: fetch, checkout the project's base branch, pull, print the head
commit, and STOP and report if it differs from the expected head that AATM sends (include base branch
and expected head SHA in the prompt). PRs must target the project's base branch.
Tests: the built prompt contains the base branch and the expected head SHA.

### PR 9 — E5 + E10: no autostart; start-all reports ready while pages still load  🔴/🟠
Location: scripts/ubuntu/ (new install-autostart.sh / uninstall), scripts/ubuntu/aatm_stack.py:207 `readiness`.
Fix:
- systemd --user unit that runs start-all.sh after login once the network is up; install/remove with one
  command; document it in docs/SETUP_AR.md.
- readiness counts a window as ready only when its page state is ready/idle/generating/pr_ready/
  keep_working_visible; otherwise show "loading" and wait up to 60 s before reporting.
Tests: unit file content test; readiness test with a connected-but-loading window.

### PR 10 — E17: `aatm doctor` consistency check (prevention)  🟡
Fix: scripts/ubuntu/doctor.py (+ `aatm doctor` alias) that reports: worker working with no running task;
worker offline while its window is connected; open session for a finished task; tasks assigned to the
manager; task with satisfied dependencies queued > 15 min; window running an older SCRIPT_VERSION;
manager jobs stuck in needs_human. Report-only by default; `--fix` repairs only the safe cases using the
helpers from PR 1/3 (with a backup from PR 7). Run it from the watchdog every 10 minutes and at the end of
start-all.
Tests: each check against a seeded test database; --fix repairs only the safe cases.

### PR 11 — E11 + E13: stale admin password; fake demo data on a fresh install  🟠
- scripts/ubuntu/reset-admin-password.sh: say clearly that .env was NOT updated. Add
  scripts/ubuntu/admin-login.sh (hidden password via getpass, token file mode 0600 with expiry; never
  stores the password).
- backend/app/db/seed.py (:29 seed_initial_data, :134, :346, :473): with DEMO_MODE=false seed only the admin
  user, agent definitions and default policies. Mark demo rows (is_demo) and add a cleanup that deletes only
  demo rows. The greeting message must not return after a chat clear.
Tests: fresh seed with DEMO_MODE=false creates no demo profiles/accounts/sessions/approvals/notifications.

### PR 12 — E14 + E16: project picker; easier userscript install  🟡
- Required project picker in Manager Chat and the Command Bridge (EN/AR/FA); the manager refuses to plan
  code for an unrelated product inside the AATM self-dev project.
- One-time userscript install link per profile (expires after use or 10 minutes); dashboard warning when a
  window's script_version is older than SCRIPT_VERSION.
Tests: API + Vitest.

### PR 13 — E15: faster verification  🟡
- pytest-xdist (`-n auto`) in verify.sh with a flag to disable it; a `--changed` mode that runs the test
  files related to changed paths first.
- Extend ruff to scripts/ and fix the existing findings in scripts/load_test.py,
  scripts/ubuntu/telegram_alerts.py and scripts/ubuntu/watchdog.py.
Target: full verify under 10 minutes on this machine. Report before/after timings.

## Out of scope (owner actions — do not do these)
- Creating the Telegram bot or putting its token anywhere but .env (E19).
- Enabling worker-4 or changing the Arena profile limit (E19).
- Acting on pending approvals, merging PRs, touching main, tagging or releasing.

## Final report
A table: item, PR link, tests added, verify.sh result, status (Complete / Partially complete /
Not started / Blocked), plus anything you found that is not in this list (as new items E20+).
