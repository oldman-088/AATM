# AATM autonomy issues (E1-E42)

Source of truth for the self-development work. Each item has a symptom, cause, location and fix.
Written from the 2026-11-21 run; see also the review rules at the end.


> Each item: **symptom** (what you saw), **cause** (from the code), **location**, **fix**, **test**.
> Priority: 🔴 stops work · 🟠 causes confusion or wasted effort · 🟡 improvement.
> Fixed items are moved to "Fixed" at the end with their PR number.

### 🔴 E1: Tasks are not dispatched after a cancel or a replacement
- **Symptom:** the manager replies and creates tasks, but nothing reaches the workers (2026-11-19 18:00).
- **Cause:** the scheduler gives tasks only to workers with `status == "idle"`. Cancelling a task does not free its worker (`worker-1` stayed `working` with `current_task_id` set), and "Replace employee" sets the replaced worker to `offline` and nothing returns it to `idle` when its window reconnects.
- **Location:** `backend/app/api/routes/orchestration.py:265` (cancel branch of `update_task`); `backend/app/orchestration/replacement.py:212` (`failing.status = "offline"`); `backend/app/orchestration/scheduler.py:278` (`idle_workers`).
- **Fix:** on cancel/fail/complete, free the assigned agent (`status="idle"`, `current_task_id=None`) when it has no other running task. When the bridge reports a connected `ready` window and the agent is `offline` with no open session, set it to `idle`.
- **Test:** cancelling a running task frees its worker; a reconnected window of an `offline` worker makes it `idle`; a queued task is then dispatched.

### 🔴 E2: "Replace employee" can hand tasks to the manager
- **Symptom:** Worker 3 was replaced by `manager-0`; 9 tasks (including W1-T3) moved to the manager and never ran (2026-11-19 15:46).
- **Cause:** replacement candidates are filtered only by `is_enabled`, emergency stop and status, not by role.
- **Location:** `backend/app/orchestration/replacement.py:143-151`.
- **Fix:** exclude `role == "manager"` (and reviewers for implementation tasks) from the candidates; when no worker is free, refuse with a clear message instead of picking the manager.
- **Test:** replacing a worker never picks `manager-0`.

### 🔴 E3: Sessions of finished tasks stay open and reserve the worker
- **Symptom:** W1-T2's session stayed `reviewing` after PR #17 was merged; W1-T3's stayed `needs_human` after the task was completed. That worker then only accepts follow-ups for the old task.
- **Cause:** `RESERVING_PHASES` includes `reviewing` and `needs_human`; nothing closes a session when its task reaches `done/completed/cancelled/merged`. The runner only skips `blocked/cancelled` tasks.
- **Location:** `backend/app/orchestration/arena_runner.py:74-75` and `:649`; `backend/app/browser/arena_sessions.py:139` (`close_session`).
- **Fix:** when a task reaches a final status (API update, `complete_task`, merge), close its open Arena session with `close_session(..., reason=...)`.
- **Test:** completing or cancelling a task closes its session and the worker takes the next task.

### 🔴 E4: Merges done directly on GitHub are not detected
- **Symptom:** the owner merged PR #17 on GitHub; AATM kept showing W1-T2 as `changes_requested` and queued a pointless round 1.
- **Cause:** the merged-PR check runs only for sessions in the monitored phases; `reviewing` is not monitored.
- **Location:** `backend/app/orchestration/arena_runner.py:497` and `:624-627` (the tick only selects `MONITORED_PHASES`).
- **Fix:** also poll the PR state (cheaply, e.g. every 5 minutes) for sessions in `reviewing/awaiting_approval`; when it is merged, mark the task `merged`, close the session and cancel queued correction rounds for it.
- **Test:** a PR merged outside AATM moves its task to merged within one cycle.

### 🔴 E5: No autostart after a reboot or power cut
- **Symptom:** after the power cut on 2026-11-19 nothing came back except the Redis and RabbitMQ containers.
- **Cause:** no systemd unit or autostart entry for `start-all.sh`.
- **Location:** `scripts/ubuntu/` (new `install-autostart.sh`).
- **Fix:** a `systemd --user` unit (or `~/.config/autostart/aatm.desktop`) that runs `start-all.sh` after login and waits for the network; install and remove it with one command.
- **Test:** after a reboot and login, 4/4 without any command.

### 🟠 E6: Workers in Arena start from the old `main`
- **Symptom:** Worker 1 counted 364 tests (the old base) while `r8-aatm` has 486, then re-did work that was already shipped (W1-T1, 2026-11-19 17:00).
- **Cause:** the task prompt does not require syncing the Arena workspace to the project base branch before starting.
- **Location:** the worker task prompt in `backend/app/orchestration/arena_runner.py` (prompt build) and `MANAGER_PROMPT.md`.
- **Fix:** the first steps of every prompt: `git fetch && git checkout <base_branch> && git pull`, print the head commit, and stop if it does not match the head AATM sends.
- **Test:** the prompt contains the base branch and its expected head.

### 🟠 E7: A message can go out to a worker right as it is being cancelled
- **Symptom:** a PR request went to Worker 1 one second before W1-T1 was cancelled (17:41:55).
- **Cause:** `_follow_up` does not re-read the task status just before sending; there is no bridge command that presses Stop in Arena.
- **Location:** `backend/app/orchestration/arena_runner.py:916` (`_follow_up`); `backend/app/browser/arena_connector.py` (COMMANDS).
- **Fix:** re-read the task and session from the database right before every submit and drop the message if the task is finished; add a `stop` command (presses Arena's Stop button, verified), used when a running task is cancelled.
- **Test:** a cancel arriving before the send blocks the message.

### 🟠 E8: The manager accepts requests that break the project's rules
- **Symptom:** the manager planned a phone-number → social-media OSINT tool with 7 tasks (2026-11-19 17:57), against AGENTS.md rule 5 (no unauthorized scraping or anti-bot evasion).
- **Cause:** there is no policy check on the goal before tasks are created.
- **Location:** `backend/app/orchestration/manager_arena.py` (plan handling), `MANAGER_PROMPT.md`.
- **Fix:** an explicit policy section in the manager prompt plus a validator that rejects the plan (with a clear reason to the owner) before any task is created.
- **Test:** a scraping or surveillance goal creates no tasks and returns a reason.

### 🟠 E9: The manager turns one short line into many tasks
- **Symptom:** "create task verify automated QA coverage" → 3 REL-FIX tasks; a calculator goal → 10 tasks, with no confirmation.
- **Fix:** above 5 tasks, or for an ambiguous goal, show the plan to the owner with "Approve" / "Edit" before creating tasks; reject a plan whose first task depends on an unavailable worker.

### 🟠 E10: start-all says 4/4 while a page is still loading
- **Symptom:** Worker 1's page (a long chat) read `offline` for 30–60 s after start-all reported 4/4 ready.
- **Location:** `scripts/ubuntu/aatm_stack.py:207` (`readiness`, which only checks `connected`).
- **Fix:** count a window as ready only when its page state is `ready/idle/generating`; otherwise show "loading" and wait up to 60 s.

### 🟠 E11: The admin password in `.env` goes stale after a reset
- **Symptom:** login with `DEFAULT_ADMIN_PASSWORD` from `.env` fails with 401 because the owner reset the password on 2026-11-18.
- **Location:** `scripts/ubuntu/reset-admin-password.sh`.
- **Fix:** the script says clearly that `.env` was not updated; add `scripts/ubuntu/admin-login.sh` (hidden password, token file 0600 with an expiry) for administrative calls.

### 🟠 E12: There is no way to clear chats or to cancel a manager job
- **Symptom:** chats pile up and cannot be deleted; an old manager job stays `needs_human` with no cancel button; worker sessions cannot be closed from the dashboard.
- **Location:** `backend/app/api/routes/orchestration.py:371` (`GET /chat` with no paging, no DELETE); `backend/app/api/routes/arena.py` (no cancel for manager jobs, no session close).
- **Fix:** `DELETE /chat` (archive with a backup, never the audit log), paging on `GET /chat`, `POST /arena/manager/jobs/{id}/cancel`, `POST /arena/sessions/{id}/close`, with buttons in the dashboard.

### 🟠 E13: A fresh install still creates fake data
- **Symptom:** fake browser profiles (fastapi.tiangolo.com, github.com, localhost), the account `aatm-org@arena.ai`, `conv-…` sessions, a demo approval and notification, and a greeting message that returns after every chat clear.
- **Location:** `backend/app/db/seed.py:29` (`seed_initial_data`), `:134`, `:346`, `:473`.
- **Fix:** with `DEMO_MODE=false`, seed only what is essential (the admin user, the agents' definitions, the default policies), and mark demo rows so a cleanup can delete only them.

### 🟡 E14: Commands from the dashboard have no project picker
- **Symptom:** every new goal lands in "AATM self-dev", which means the AATM repo itself.
- **Fix:** a required project picker in Manager Chat and the Command Bridge; refuse to plan code for an unrelated product inside the AATM repo.

### 🟡 E15: Verification is slow
- **Symptom:** `verify.sh` takes about 25 minutes (backend pytest alone about 21 minutes).
- **Fix:** `pytest -n auto` (xdist) and run the changed test files first; also lint `scripts/` (existing ruff findings in `scripts/load_test.py`, `scripts/ubuntu/telegram_alerts.py`, `scripts/ubuntu/watchdog.py`).

### 🟡 E16: Installing the userscript needs manual steps
- **Fix:** the backend serves a one-time install link per profile (expires after use or after 10 minutes), and the dashboard warns when a window runs a script older than `SCRIPT_VERSION`.

### 🟡 E17: No routine consistency check (prevention)
- **Fix:** an `aatm doctor` command, also run by the watchdog every 10 minutes and after every `start-all`. It looks for: a worker `working` with no running task, a worker `offline` whose window is connected, an open session for a finished task, tasks assigned to the manager, a task queued for more than 15 minutes, and an older userscript. It reports first, and fixes only the safe cases.

### 🟡 E18: No automatic backup before bulk changes
- **Fix:** take a database backup before a bulk cancel, a chat clear or a replacement, with the backup path in the audit entry.

### 🟡 E19: Settings that are not done yet
- Telegram alerts are built but not configured (the owner creates the bot).
- The Arena approval allows 4 profiles while 5 are set up (worker-4 is disabled).

### 🔴 E20: The manager chains tasks, so one worker runs and seven wait (found 2026-11-21)
- **Symptom:** the `osint-app` plan has 8 tasks for 8 workers, but 7 depend on task 1 (research and docs) and 5 also on tasks 2 and 2-b. One worker ran, seven sat idle for about 50 minutes.
- **Cause:** the planner prompt only describes the `depends_on` format; it never asks for independent tasks. `plan_warnings` only prints a warning, and with auto-approve nobody stops the chain. The scheduler itself is fine: two independent game tasks were dispatched in the same second.
- **Location:** `backend/app/orchestration/arena_prompts.py` (`build_manager_plan_prompt`), `backend/app/orchestration/manager_arena.py` (`plan_warnings`).
- **Fix:** prompt rules (independent by default; never make research or documentation a prerequisite; define shared contracts inside the plan; give each task file ownership). Reject, and ask for a re-plan, when more than half of the tasks depend on one task.
- **Test:** a chained plan is rejected; an independent plan is accepted.

### 🟠 E21: Every stage waits for the manager's review and the owner's merge
- **Symptom:** task 1's PR was ready at 12:12 and nothing else started until the merge at 13:04.
- **Cause:** `dependencies_satisfied` needs `completed/done/merged`, and workers branch from `main`, so a dependent task cannot see unmerged code.
- **Location:** `backend/app/orchestration/scheduler.py` (`dependencies_satisfied`).
- **Fix (owner chooses):** (a) an optional auto-merge when the manager approves (the owner's setting, off by default); (b) stacked branches: a dependent task starts from its dependency's branch once that PR is approved.

### 🟠 E22: The manager reviews one PR at a time
- **Symptom:** with several workers finishing together, reviews queue behind one Arena window.
- **Fix:** review in rounds with small diffs; allow a second review window; or let the owner skip review for docs-only PRs.

### 🔴 E23: The manager's review of a large PR hangs and fails
- **Symptom:** PR #1 of `osint-app` (20 files, +4133 lines): the review prompt was 26,348 characters; the manager generated for 20 minutes with no output and the job stopped at `needs_human` ("stalled: no Arena activity for 20 minutes").
- **Location:** `backend/app/orchestration/manager_arena.py` (review prompt, stall limit).
- **Fix:** send a per-file summary and the risky hunks instead of the whole diff, review large PRs in parts, and let a docs-only PR skip the line-by-line review.

### 🔴 E24: Self-healing requeues a review that is still running
- **Symptom:** at 12:28 the manager's review task was requeued ("Stalled task timeout detected", retry 1 of 3) while the manager was still generating. After a long downtime every `in_progress` task would be requeued on the first tick after start.
- **Cause:** a task with no `updated_at` change for `STALLED_TASK_MINUTES` (15) is treated as stalled, even when its Arena or manager session is alive.
- **Location:** `backend/app/orchestration/scheduler.py` (`detect_and_recover_stalled_tasks`).
- **Fix:** skip tasks whose session is `generating` and whose window sent a heartbeat; refresh `updated_at` while a manager job runs; use a per-kind timeout and a grace period after startup.

### 🟠 E25: Arena's Terms of Service stops each account on first use
- **Symptom:** worker-1, worker-2 and worker-5 stopped at `needs_human` ("page shows: terms of service") when their first task started; the rest will do the same.
- **Note:** stopping there is by design; AATM never accepts terms for a human.
- **Fix:** an `aatm doctor` check after setup lists the accounts that have not accepted yet, so the owner clears them once before any task.

### 🟠 E26: A refusal or an empty-tasks reply looks like an error
- **Symptom:** "تعذّر على المدير إرسال رد صالح" hid the manager's real answer (it refused a request, with the reason in `summary_ar`).
- **Cause:** PR #54 only handles `ambiguous: true`; any other reply with `tasks: []` is still sent to `needs_human`.
- **Fix:** show `summary_ar` whenever `tasks` is empty, whatever the reason.

### 🟠 E27: New session race (select_repo sent to the page that is unloading)
- **Status:** fixed in PR #55 (waits for the new page to connect); not deployed live yet.

### 🟡 E28: Missing dashboard controls
- No buttons to approve, edit or reject a manager draft plan (API only: `POST /arena/manager/jobs/{id}/draft/approve`).
- No button to disable or complete a project (the API accepts `active` and `disabled` only).
- The "create project" form sends only a name, so the repo path defaults to AATM itself (see E14).

### 🟡 E29: A frontend and a backend built from the same plan disagree
- **Symptom:** `test-program`: the frontend sends `{playerName, seconds, "normal"}`, the backend expects `{player_name, duration_seconds, "medium"}`; every score save failed with 422.
- **Fix:** when a plan has both a frontend and a backend task, the planner must add one shared contract file (for example `docs/API.md`) that both tasks cite, and a final integration check task.

### 🔴 E30: AATM still needs a person to babysit it (owner's goal: run alone)
- **Today a human (or Claude) has to:** start everything after a reboot (E5), watch for `needs_human` and resume, spot stuck sessions and races, restart after a config change, and merge between stages.
- **Goal:** the owner writes an idea to the manager; AATM plans, runs, recovers and reports by itself. Only real human checks (terms, CAPTCHA, login) and, unless the owner opts out, merges stay manual.
- **Plan:** fix E20 to E27, add autostart (E5), `aatm doctor` with safe auto-fixes (E17), alerts to Telegram (E19), and the optional auto-merge (E21a).

### 🔴 E31: Parallel PRs that touch the same files conflict
- **Symptom:** `osint-app` PR #3 (51 files) conflicted with merged PR #1 and PR #2 in `.github/workflows/docs.yml` and `tests/test_docs.py`; stage three waited about 25 minutes.
- **Fix:** conflict-aware scheduling (do not run, or merge in order, tasks that share files); when a PR becomes `CONFLICTING`, AATM itself sends its worker "merge origin/main, resolve, run tests, push to the same branch" (done by hand on 2026-11-21 and worked in one minute); a merge queue that rebases before each merge.

### 🟠 E32: Resume restarts a task whose PR is already merged
- **Symptom:** resuming worker-2 on `test-program` re-sent the whole frontend task although PR #1 had been merged; two minutes of duplicate work until it was stopped by hand.
- **Location:** `backend/app/api/routes/arena.py` (`resume_after_human`).
- **Fix:** before requeueing, look for an open or merged PR for the task (by the `[AATM-<task_id>]` title); close the task instead of repeating it.

### 🟠 E33: The manager answers a review in plain text instead of JSON
- **Symptom:** two reviews of `osint-app` PR #2 stopped at `needs_human` with "The reply contains no JSON object".
- **Fix:** on the retry send an empty JSON template, then try to extract the verdict from the prose, and ask a human only after that.

### 🟡 E34: A window can have the wrong repository selected
- **Symptom:** worker-2 stopped with `repo_mismatch` (selected `main`, task needs `oldman-088/osint-app`).
- **Fix:** before dispatching, run `select_repo` on the worker's window and fix it; list windows that still cannot select the repository.

### 🟡 E35: Merges done outside AATM are noticed up to 5 minutes late
- **Location:** `ARENA_PR_STATE_POLL_SECONDS = 300`.
- **Fix:** a GitHub webhook, or poll every 30-60 seconds only while a PR is waiting.

### 🟡 E36: "Text typed but not sent" on the first submit after a page loads
- **Symptom:** worker-5 and worker-8 each hit `send_failed` at their first submit (the Terms page appeared at the same moment).
- **Fix:** distinguish this case from a real failure and retry once after the page is stable.

### 🔴 E37: A busy manager burns a review's 3 attempts in one minute
- **Symptom:** the reviews of `osint-app` PR #4 and PR #5 failed with "stopped after 3 attempts to avoid an endless loop" within about a minute (14:19-14:20). The manager window was still generating an earlier reply, so each submit failed at once with `busy: the agent is still generating`.
- **Cause:** `busy` is a transient state but each failed submit counts as an attempt, and the retries come seconds apart (`MAX_JOB_ATTEMPTS = 3`, `manager_arena.py`).
- **Fix:** do not count `busy` as an attempt; wait for the window to become idle (with a timeout), and back off between real retries. Show the owner a "review waiting for the manager" state instead of a failure.
- **Test:** a submit that meets `busy` waits and succeeds; a real failure still stops after 3 attempts.

### 🔴 E38: A long but healthy generation is flagged "stalled" after 20 minutes
- **Symptom:** at 14:37 AATM put worker-2 and worker-4 of `osint-app` into `needs_human` and blocked their tasks ("stalled: no Arena activity or new commits for 20 minutes"), while their Arena pages were still `generating` and their last replies showed real work (an API client being written, a worker package being built). worker-8 was one minute from the same fate.
- **Cause:** the rule looks at commits and page text changes only; a single long generation produces neither for 20 minutes. Same family as E24.
- **Danger:** pressing Resume on such a worker re-sends the whole task and throws away its work (see E32).
- **Fix:** treat a page that is `generating` with a live heartbeat as active, extend the limit for large tasks, and make Resume refuse (or ask) when the worker is still generating.

### 🔴 E39: A quiet worker is flagged and released instead of being told "continue" (owner's request)
- **Owner's point (2026-11-21):** when a worker stops moving, AATM should write "continue" in its chat and send it, as the old Arena Auto Continue userscript did, before giving up on the worker.
- **What exists today:** while a session is in a monitored phase (`working`, `correcting`, `reporting`) the runner clicks "Keep working" and sends "continue" when the page shows `keep_working_visible` or `tool_call_limit` (`arena_runner.py`, `_handle_tool_call_limit`, `TOOL_LIMIT_CONTINUE`). The original userscript timers were removed on purpose: all actions are decided by the backend.
- **The gap:** after the 20-minute "stalled" rule marks a session `needs_human` (and 10 minutes later releases the worker), monitoring stops, so nothing sends "continue" any more. worker-2 and worker-4 of `osint-app` were released that way while their pages kept working.
- **Fix:** on "no activity", first look at the page. `generating` with the Stop button = healthy, wait. `idle`, `ready`, `keep_working_visible` or `tool_call_limit` while the task is unfinished = send "continue" (at most 2 times, 5 minutes apart), and only then `needs_human`. Never release a worker whose page is still generating.
- **Test:** a worker whose page sits idle gets "continue" and resumes; a generating worker is never flagged.
- **Confirmed on 2026-11-21 at 15:32:** worker-4 (task 8, `osint-app`) reached `tool_call_limit` and waited for "continue". The runner sent nothing because the session had been closed at 14:47 by the stall rule (E38). The owner typed "continue" by hand and the page returned to `generating` at 15:34. This is exactly the missing behaviour; without the owner the task would have stayed paused for ever.
- **My earlier judgement was wrong:** at 15:02 I called worker-4 "probably hung" (same 304-character reply for 20 minutes). It was working in long steps; its reply changed at 15:16 and 15:20 and it pushed its branch at 15:16. A page that is `generating` with a Stop button must not be judged stuck from the reply text alone.
- **Exception seen on 2026-11-21:** after its PR was merged, worker-2's page showed `tool_call_limit` with Arena's own "Continue" button ("start a clean branch"). Its report said "No remaining implementation work". Send "continue" only while the task is unfinished (its PR is not merged or closed); never after delivery, or the worker starts new, unrequested work.

### 🔴 E40: OWNER DECISION (2026-11-21 15:07) - merge automatically, never ask
- **Decision:** the owner does not want to be asked about merging. When the manager decides a PR is ready, AATM merges it by itself. The whole process must be automatic.
- **Scope:** pull requests that AATM workers open in the projects AATM manages. It does not cover pull requests to AATM's own repository.
- **Conditions to build in:** (1) the manager's verdict is `approve`; (2) the PR is mergeable and its CI checks are green *after being rebased on the latest `main`*; (3) merges go through a queue, one at a time, each rebased before merging; (4) after each merge, `main`'s CI must stay green, otherwise stop the queue and fix `main` first.
- **Consequence:** the manager's review becomes the critical path. E23, E24, E33 and E37 (reviews that hang, are requeued, answer without JSON, or burn their attempts) must be fixed before auto-merge is useful; today none of PR #7's reviews produced a verdict.
- **Fallback to decide with the owner:** what to do when the manager cannot produce a verdict (suggested: retry with a smaller review prompt, then CI-green plus worker report, never merge on a failing CI).

### 🔴 E41: Two PRs merged cleanly but `main` went red (semantic conflict)
- **Symptom:** after `osint-app` PR #4 (auth) and PR #5 (AI engine) were merged, `main`'s CI failed: `tests/test_migrations.py` -> "Multiple head revisions are present". Each PR added an Alembic revision with the same parent. Git saw no conflict.
- **Also:** the docs check on `main` failed after earlier merges (13:08, 13:56); PR #7 (phones) has two failing checks.
- **Fix:** (a) a merge queue that rebases each PR on the latest `main` and re-runs CI before merging; (b) a post-merge check on `main` that stops further merges and opens a repair task when it turns red; (c) planner rule: tasks that add database migrations are ordered, or one worker owns Alembic; (d) a repair task template "merge Alembic heads".
- **Status:** `main` of `osint-app` is red now; a "merge heads" migration is needed.

### 🔴 E42: `osint-app` `POST /api/v1/phone/analyze` needs no login (found by the final integration run)
- **Symptom:** the endpoint returns 200 without a token, while `/api/v1/cases` returns 401 and the AI analysis endpoint requires an analyst role. Seen on the merged `main` (5306c8c) running against PostgreSQL.
- **Cause:** `backend/app/api/v1/phone/router.py` has no authentication dependency on the route. All tests passed because none checks the route without a token.
- **Risk:** anyone who can reach the API can run lookups (and, with `search_enabled`, trigger outbound web searches from the server).
- **Fix:** require `require_analyst_or_admin` like the other routes, rate-limit per user, and add a test "no token -> 401".
- **Planner lesson:** each module router must be created from one template that already contains the auth dependency.

### ⚙️ Owner choices made on 2026-11-21 (in `.env`, branch `feature/manager-auto-approve`, no PR yet)
- `ARENA_MANAGER_AUTO_APPROVE_PLANS=true`: a plan creates its tasks directly. Policy refusals, the ambiguity check and merge approval are unchanged.
- `ARENA_MANAGER_MAX_PLAN_TASKS=0`: no limit on the number of tasks in a plan.

---

### ✅ Fixed (2026-11-19)
- **#33:** a changed-file chip such as `scripts/ubuntu/stop.sh` was read as the Stop button, so task chats stayed "generating" forever. Fixed and confirmed on the real pages.
- **#34:** start-all/stop-all output in Arabic, with the dashboard link and a section in SETUP_AR.
- **#29 / #30:** `verify.sh --install` now installs the frontend packages too; a worker window returns to its task chat after a restart.
- Checked and not a problem: Arena's "Keep working" panel on the manager window is handled (`manager_arena.py:660`).

### ✅ Fixed / in review (2026-11-21)
- **PR #52:** each planning request is isolated from other projects and history.
- **PR #53:** stalled sessions release their worker, plan/review jobs stop after 3 attempts, window cap 9, seeded workers start in Agent mode (workers 5-8 were `offline` forever), lint clean.
- **PR #54:** manager's clarifying question is shown, the dashboard follows the active project, the manager refreshes its context when the team changes.
- **PR #55:** `new_session` waits for the new page (E27).

---

## Rules for the self-development work (read before starting any task)

1. **Goal:** AATM must run, recover and report without a person watching it. Every manual rescue that was needed in the
   2026-11-21 run is a bug in AATM. Fix the cause, do not add a script that a person has to run.
2. **Never automate real human checks:** terms of service, CAPTCHA and login pages must still stop the worker and alert the owner.
3. **One task, one concern, one pull request.** Touch only the files your task owns (the task text lists them). If you need a
   change in another task's file, keep it to a few lines and say so in your report.
4. **Every fix needs a regression test that fails before the change and passes after it.** Include the failing output of the old
   behaviour in the report when you can. Use the mock Arena page in `backend/tests` and add failure scenarios (busy page,
   tool-call limit, terms page, invalid JSON, conflicting pull request) instead of testing only the happy path.
5. **Do not run the full `scripts/ubuntu/verify.sh`** (about an hour). Run `ruff check backend scripts bridge` and the test files you
   changed or that import the code you changed. Say which ones in the report.
6. **Never touch `data/aatm.db`, `.env` or the running services.** Tests use their own temporary database.
7. **Base branch:** `integration/autonomy-base`. Open one pull request against it with the title `[AATM-<task_id>] ...`.
8. **Report honestly:** list only files you changed and tests you really ran; say what you did not run.
