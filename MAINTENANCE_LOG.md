# AATM maintenance log (local, not committed)

## 2026-11-18 reliable-prs
- Problem: W1-T3 PR into r8-aatm failed with "No commits between".
- Real cause: `arena/ff0f70cb-aatm` on GitHub is identical to `main` (eba261e). Worker 3's changes exist only in the Arena workspace (+91 diff), although its report said they were pushed.
- Fix: task prompt and no-PR follow-up require commit, rebase on the base branch, push, and a verified remote head.
- PR: https://github.com/oldman-088/AATM/pull/12
- Still open: the W1-T3 PR itself needs Worker 3's work to reach GitHub; Worker 3 is held by the sprint freeze.

## 2026-11-18 reconnect
- Problem: after restarts Arena windows could stay disconnected until reloaded.
- Real cause: bridge registrations were memory-only, and installed userscripts stop retrying on close codes 4401/4410.
- Fix: registrations and pending human checks are stored in ~/.aatm/bridge (hashes only, 0600); new userscripts never stop retrying.
- PR: https://github.com/oldman-088/AATM/pull/13

## 2026-11-18 start-all
- Problem: starting AATM needed several manual steps; stop.sh did not stop the frontend.
- Real cause: no combined command; frontend PID file pointed at a wrapper shell.
- Fix: start-all.sh, stop-all.sh, scripts/ubuntu/aatm_stack.py; vite started directly. W1-T3's branch had no commits to reuse.
- PR: https://github.com/oldman-088/AATM/pull/14 and https://github.com/oldman-088/AATM/pull/15 (window detection)

## 2026-11-18 window-race
- Problem: after a clean start-all, the manager and Worker 3 windows never connected (2/4).
- Real cause: the first page load ran before Tampermonkey was ready, so the userscript did not run on it.
- Fix: open windows empty, load Arena 10s later, reload once at 45s if still unconnected.
- PR: https://github.com/oldman-088/AATM/pull/16

## 2026-11-18 W1-T2 continue (owner request)
- No PR and no final report, so the task was mid-way. Keep working click only closed the panel; one "continue" message resumed it. keep_working_count is 2 of 5. Task stays held in AATM.

## Where I stopped
- Done: items 1, 2 (server side), 3. Next: items 5, 6, 12 together (userscript), then 4, 7, 8, 11, 9, 10, then the report.
- Owed: reinstall the userscript in all 4 windows after the userscript changes.

## 2026-11-18 human-checks
- Problem: false needs_human from page words; tool-call limit pause not handled.
- Real cause: stop phrases matched any text outside the transcript; no page state for the pause.
- Fix: notice-based phrase matching; tool_call_limit state with backend-controlled "continue". Needs userscript 2.2.0.
- PR: https://github.com/oldman-088/AATM/pull/18

## 2026-11-18 suggestion-chips
- Problem: Arena's suggestion chips could be mistaken for the composer or reply.
- Real cause: composer = lowest text box; replies = any matching text.
- Fix: the panel is detected and excluded everywhere, never clicked. Needs userscript 2.2.0.
- PR: https://github.com/oldman-088/AATM/pull/19

## 2026-11-18 arena-pr-flow
- Problem: work could stay only in Arena; a PR could target the wrong base; nothing forbade pressing Merge on the page; CI did not run for r8-aatm.
- Real cause: AATM relied on the agent to push and open the PR; no page guard; CI branch list omitted r8-aatm.
- Fix: safeClick refuses merge, pr_ready state, create_pr command, ensure_task_pull_request, CI on r8-aatm. Needs the final userscript.
- PR: https://github.com/oldman-088/AATM/pull/20

## 2026-11-18 report-detection
- Problem: when W1-T2 was released for review, AATM pressed Keep working although the task had finished (count 3 of 5).
- Real cause: Arena renders "## AATM REPORT" as plain text without "##", and `gh pr edit` failed, so the PR title was not updated before the tick.
- Fix: heading detection accepts the rendered form; PR edits use the REST API.
- PR: https://github.com/oldman-088/AATM/pull/21

## 2026-11-18 snapshots
- Added snapshot.sh, rollback.sh and an automatic snapshot after the last task of a batch is merged.
- PR: https://github.com/oldman-088/AATM/pull/22

## 2026-11-18 false-stalls
- Problem: agents still generating were marked needs_human after 20 minutes.
- Real cause: the check only saw state changes.
- Fix: store a transcript mark halfway and compare it before stopping.
- PR: https://github.com/oldman-088/AATM/pull/23

## Where I stopped (2026-11-18, r8-aatm 1deef4f)
- Next: item 4, 7, 8, 11, the W1-T3 PR, final userscript version and one reinstall, then 9, 10 and the Phase A stop.
- PR 17 stays unmerged until CI is green, the manager reviewed it and the owner approves.

## 2026-11-18 resume-after-restart
- Problem: manager jobs stayed running after a restart; downtime counted as a stall; a restart could repeat a prompt.
- Real cause: no startup recovery and no record that a send had started.
- Fix: startup recovery, submit_started page marks, resume without resending.
- PR: https://github.com/oldman-088/AATM/pull/24

## 2026-11-18 quiet-watchdog
- Problem: alerts for normal 401s, single traceback lines and restarts; Redis fan-out stopped after an idle timeout.
- Real cause: broad log matching; pub/sub used a client with a 5s read timeout.
- Fix: LogScanner, grace periods, dedicated subscriber with retry.
- PR: https://github.com/oldman-088/AATM/pull/25

## 2026-11-18 finished-generating
- W1-T2 had a PR and report but the page kept showing Stop; the stall check now sends such a task to review.
- PR: https://github.com/oldman-088/AATM/pull/26

## 2026-11-18 telegram-alerts
- Added the optional send-only Telegram service (item 11). Off until the owner fills .env.
- PR: https://github.com/oldman-088/AATM/pull/27

## 2026-11-18 demo-mode and replaced-tab
- .env had DEMO_MODE=false then DEMO_MODE=true; removed the second line (not committed). Demo endpoint should now return 404.
- Replaced tabs wait 5 minutes: PR https://github.com/oldman-088/AATM/pull/28

## 2026-11-19 verify-install (item 9)
- Problem: on a clean copy `verify.sh --install` failed at the frontend gates (node_modules missing); the full run passed only after a manual npm ci (470 backend + 11 bridge tests, secret scan, tsc, 97 vitest, build).
- Real cause: --install set up Python only.
- Fix: --install runs npm ci (npm install without a lockfile) when node_modules is missing.
- PR: https://github.com/oldman-088/AATM/pull/29 (opened; merge was blocked by the session's permission mode, waits for the owner)

## 2026-11-19 task-chat
- Problem: W1-T1 and W1-T3, released by the owner this morning, stopped as "stalled" after 20 minutes; every PR follow-up was refused (send_failed, then repo_mismatch: window on aatm-arena-test).
- Real cause: after a restart the worker windows are not on the task's chat, and AATM never stored which chat a task lives in; failed follow-ups were ignored, so they repeated every minute until the stall timer.
- Fix: record chat_path after the prompt is sent, bring a quiet window back to it (never a busy one), and send refused or repeatedly failing follow-ups to needs_human with the real reason.
- PR: https://github.com/oldman-088/AATM/pull/30 (opened; merge waits for the owner)

## Where I stopped (2026-11-19)
- PR 17 was merged on GitHub by the owner; AATM still shows W1-T2 changes_requested (session phase reviewing is not watched for outside merges). Task 7f5be693 (round 1 for W1-T2) is queued and would prompt Worker 2 for merged work: owner decision.
- Pending approvals (owner only): "Merge or push into a protected branch", "GitHub pull request".
- Next: merge 29 and 30 after CI, userscript 2.2.0 on "جاهز", W1-T3 PR, 3 cold starts, Phase A.

## 2026-11-19 release prep (owner asked to merge verified work into r8-aatm and open the final PR)
- Merged #29 (107d045), #30 (31e4d20), #31 release 3.6.0 (06cff7c) after CI passed. Backend restarted; 4/4 windows reconnected at once.
- verify.sh --install on a clean clone of 31e4d20: 14/14 gates; 457 passed, 31 skipped (no Chromium under temporary HOME); the 8 Chromium test files then passed 136/136 with the installed browser.
- Backup: ~/aatm-backups/pre-v3.6.0-20261119.tgz and pre-v3.6.0-20261119-aatm.db (0600).
- Final PR r8-aatm -> main: https://github.com/oldman-088/AATM/pull/32 "v3.6.0 — stable", CI passed, NOT merged.

## Where I stopped (2026-11-19, r8-aatm 06cff7c)
- Waiting for the owner: "جاهز" (userscript 2.2.0 + install in 4 windows), then W1-T3 PR and 3 cold starts, then "رقّي" to merge #32 and tag; "انشر" for the GitHub Release.

## 2026-11-19 power outage recovery + stop-detection
- After the outage only Redis/RabbitMQ came back (no autostart). DB integrity_check ok; start-all.sh brought AATM up, 4/4 connected on userscript 2.1.0.
- Problem: Worker 2 and Worker 3 stayed "generating" on their task chats with nothing running.
- Real cause: hasStopButton() matched "stop" anywhere in a button's title; the changed-file chip "scripts/ubuntu/stop.sh" (W1-T2; W1-T3 has stop-all.sh) counted as Stop. A busy window refuses every submit.
- Fix: whole-word match, paths/file names never count; tests on the mock page. verify.sh 12/12.
- PR: https://github.com/oldman-088/AATM/pull/33 merged into r8-aatm as 01b9e12; ~/AATM pulled, backend restarted, 4/4 connected
- Userscript 2.2.0 not generated yet: admin login from .env was blocked by the permission system; waits for the owner's login or dashboard generation.

## 2026-11-19 userscript 2.2.0 installed
- Generated 2.2.0 (with #33 stop fix) for manager, worker-1..3 at 15:15; old 2.1.0 files moved to ~/aatm-scripts/old-2.1.0. Owner installed in all four windows.
- 15:48: 4/4 connected on 2.2.0, all ready, no false Stop; local install server stopped, admin session file deleted.
- Worker 4 left disabled (Arena approval allows 4 profiles).
- Next: W1-T3, then 3 cold starts.

## 2026-11-19 W1-T3 closed + release v3.6.0
- W1-T3 closed (owner choice) against #14/#15 + #34 (Arabic output, dashboard link, SETUP_AR section); duplicate task e2caed7f cancelled. Worker 3's branch arena/ff0f70cb-aatm had no commits.
- 3 cold starts (stop-all + start-all): 4/4 on 2.2.0 each time in ~21s; Worker 2/3 back on their stop.sh/stop-all.sh chats read ready (fix #33 confirmed live); nothing sent to Arena.
- Owner said "رقّي" + "انشر": archive tag archive/pre-v3.6.0 = eba261e; PR #32 merged into main as 8741529; tag v3.6.0; GitHub Release https://github.com/oldman-088/AATM/releases/tag/v3.6.0
- Still open (owner): 25 queued calculator tasks in "AATM self-dev" (8 sit on manager-0 after the 15:46 replacement); Worker 1 needs_human with an unpushed +408 W1-T1 diff; Worker 4 disabled.

## 2026-11-19 owner cleanup (after v3.6.0)
- Owner cancelled W1-T1 (Worker 1 was redoing DEMO_MODE work) and then every unfinished task: 26 cancelled, 0 left. W1-T2 marked done (PR #17 merged).
- Note: fresh installs still seed fake rows in backend/app/db/seed.py (browser profiles, aatm-org@arena.ai, conv- sessions, a greeting chat message): not done in v3.6.0.
- Chats cleared on owner request (AATM stopped first): chat_messages 43, agent_messages 34, manager_jobs 23, arena_sessions 11. Audit log kept. Backup: ~/aatm-backups/pre-chat-clear-20261119-aatm.db (0600). The seed greeting message comes back on start when the chat is empty.
- Arena chats themselves: deleted by the owner in each window (no open sessions remain, so this is safe).

## Improvement backlog (recorded 2026-11-19, owner asked to log every error and idea)
Reliability
1. No autostart: after a reboot/power cut nothing starts. Add a systemd --user unit (or ~/.config/autostart entry) that runs start-all.sh after login.
2. "Replace employee" can pick manager-0 as the replacement (worker-3 -> manager-0 moved 9 tasks); manager tasks never dispatch. Exclude role=manager from replacement candidates.
3. Sessions of finished tasks stay open and keep reserving the worker (W1-T2 "reviewing" after PR #17 merged; W1-T3 "needs_human" after completion). Close the session when its task reaches done/completed/cancelled.
4. Merges done on GitHub are not detected while a session is "reviewing" (PR #17). Poll PR state in that phase too.
5. Agent status stays "working" with current_task_id after its task is cancelled (worker-1). Reset the agent on cancel.
6. Race: a PR nudge went out 1 s before a cancel. Re-read task status right before every submit; add a bridge "stop" command so a cancel can press Arena's Stop.
7. Workers' Arena workspaces start from the old main (Worker 1 saw 364 tests, r8-aatm has 486). Task prompt should require syncing to the project base branch before work starts.
8. start-all reports 4/4 while a slow page still reads "offline" (Worker 1, long chat). Wait for page state ready, or report "loading".
Owner experience
9. Admin password in .env goes stale after reset-admin-password.sh; it should update .env or say clearly that it did not.
10. No way to clear chats (no delete endpoint/button); the chat list loads every message with no paging.
11. No API/button to cancel a manager job or to close a worker session.
12. Manager tasks assigned to manager-0 sit queued forever with no warning.
13. Command Bridge has no project picker (sends to the oldest project); new goals all land in "AATM self-dev", i.e. the AATM repo itself.
14. Manager turns short/vague chat lines into many tasks at once (e.g. "create task verify automated QA coverage" -> 3 REL-FIX tasks). Ask the owner to confirm plans above N tasks.
Safety / policy
15. Manager accepted and planned a phone-number -> social-media OSINT tool (7 tasks, 2026-11-19 17:57). The manager prompt and plan validator should refuse surveillance/scraping/anti-bot work per AGENTS.md rule 5 before creating tasks.
16. Arena approval allows 4 profiles; 5 are configured (worker-4 kept disabled).
Data
17. seed.py still creates fake rows on fresh installs (browser profiles with fastapi/github/localhost URLs, aatm-org@arena.ai, conv- sessions, greeting chat message, demo approval/notification). The greeting returns on every start when the chat is empty.
Speed / quality
18. verify.sh takes ~25 min (backend pytest ~21 min); run pytest with xdist to cut it.
19. verify.sh does not lint scripts/ (existing ruff findings in scripts/load_test.py, scripts/ubuntu/telegram_alerts.py, scripts/ubuntu/watchdog.py).
20. Userscript install needs a manual local http.server; the backend could serve a one-time install link per profile.
21. Telegram alerts are built but not configured (owner must create the bot).
22. (2026-11-19) Manager replied but nothing was dispatched: worker-1 stayed "working" after W1-T1 was cancelled, worker-3 stayed "offline" after its session was removed, and the plan chained most tasks behind a worker-3 task. Fixed by hand with POST /agents/{id}/restart. Real fix: reset the agent when its task is cancelled, and set a worker idle when its window reconnects with no open session.
Prevention (added 2026-11-19 on owner request: "develop the program without errors")
23. "aatm doctor" command (and a watchdog check every few minutes) that finds and reports state that should never exist: a worker "working" with no running task, a worker "offline" whose window is connected, an open session whose task is finished/cancelled, tasks assigned to manager-0, queued tasks whose dependencies sit on an unavailable worker, tasks stuck queued > 15 min. Report first; fix only the safe ones (reset agent to idle, close finished sessions).
24. Self-healing should run the same consistency sweep after every start-all, so a power cut or restart never leaves stale statuses (today's dispatch stop).
25. Plan validation before the manager creates tasks: refuse out-of-policy goals (rule 5), refuse plans whose first task targets an unavailable worker, warn above 5 tasks, and show the plan to the owner for one-click confirm.
26. One regression test per bug fixed today: false Stop on file chips (#33, done), cancel frees the agent, reconnect frees an offline worker, replacement never picks the manager, finished task closes its session, outside merge detected in "reviewing".
27. A supported admin login helper in the repo (scripts/ubuntu/admin-login.sh, getpass, token file 0600 with expiry) instead of a script in Claude's scratchpad; reset-admin-password.sh should tell the owner that .env is not updated.
28. Userscript version check: the dashboard and start-all should warn when a window runs an older script than SCRIPT_VERSION.
29. Automatic DB backup before any bulk change (cancel-all, chat clear, replacement), with the backup path in the audit entry.
30. Manager window: handle Arena's Keep working panel on the manager the same way as on workers, so the manager is never left waiting.
31. Faster feedback: run the changed test files first in verify.sh, then the full suite with xdist; keep CI at under 5 minutes.
32. (2026-11-25) Owner sent a prompt to the manager and nothing happened. Causes: (a) the manager window had a human check from 21:32, so every manager job waited in "waiting_connection"; (b) at 21:37 the whole stack (backend, bridge, frontend, watchdog, 9 Chrome windows) died at once with no shutdown log, because it was started from a Claude Code session and was killed when that session closed. nohup does not survive that. Restarted with `systemd-run --user --scope`. Real fix: start-all.sh should start itself in its own systemd user scope (or install-autostart.sh as a user service), so no terminal or agent session owns the stack.
33. (2026-11-25, owner idea) There is no clear "project settings" page. The Projects screen can create a project and change status/auto-merge, but cannot edit repo URL, base branch, worktree mode or the default project, although PATCH /projects/{id} supports them. test-program has repo_path "." (AATM's own folder) instead of ~/aatm-repos/test-program. Wanted: one settings page per project (repo, branch, mode, team, default) → Save → "Start work", which checks the repo, points every Arena window at it and reports readiness.
34. (2026-11-25, E57, fixed locally on feat/self-heal-manager) After a human check the manager stayed frozen even when the page was clean again: the bridge keeps "needs_human" until someone presses resume, and the manager closed the job for good as "needs_human", so the owner's request was lost. Fix: (a) the manager now pauses the job (waiting_connection, interrupted, attempt refunded, one chat note + one alert) and resumes it once the window is ready, without sending it twice; (b) every 2 minutes the watchdog runs the page's read-only selector test on a "needs_human" window: no check on the page → resume; check still there → plain reload up to 3 times, 15 minutes apart, then wait for the owner. AATM never solves or bypasses a check.
35. (2026-11-25) Stale manager jobs from a cleared chat (DELETE /chat) kept waiting and ran later ("merge main branch release" ran before the owner's new prompt). Fix wanted: clearing the chat cancels the manager jobs whose chat message it archives.
36. (2026-11-25, E58, fixed locally) The owner's 28k-character prompt was lost mid-answer: the manager page reconnects its WebSocket every 5 minutes and is briefly reported "ready" while Arena is still writing. The manager took that as the end, read an empty reply (0 chars), sent the JSON retry into the running answer, then read its own retry text as the "reply" and gave up as needs_human. Fix: a quiet page only ends the wait when a reply is on the page that is not our own prompt; otherwise it keeps polling (up to 90 polls). Still wanted: the bridge should not report "ready" on reconnect before the page says its real state, and the reply reader should never return a user bubble.
37. (2026-11-25, E59) After "new chat" the manager window was hidden (minimised/covered, visibility=hidden), Arena drew no composer, the page reported offline "composer not found" and the job timed out. Opening the manager URL again from its profile (aatm_stack._launch) brought it to the front and the job went on. Wanted: the watchdog does that itself when a connected window stays offline with "composer not found" or "page hidden" for more than 2 minutes (same cooldown/limit as E56).
38. (2026-11-25, E60, fixed locally) The owner's spec was written for one coding agent ("inspect the environment, create the files, do not stop after planning, begin now"). On Arena Agent mode the manager obeyed it and started working instead of returning the JSON plan. Fix: the plan prompt fences the goal (<<<OWNER GOAL … OWNER GOAL>>>) and says those orders are for the workers, not the manager; a goal over 6,000 characters is planned one phase at a time, and each task repeats the parts of the spec it needs.
39. (2026-11-25, E61, fixed locally) After a long agent run the live page kept only a fragment of the manager's answer ("Let", 3 chars) while the real 13,630-character answer was there after a reload. Fix: a JSON reply shorter than 40 characters with no brace makes the manager load the same chat (/agent/<id>) again and read once more. Still wanted in userscript 2.4: read the whole last assistant turn, not the last [class*=prose] element.
40. (2026-11-25) The new manager chat answered an older request ("merge main branch release") instead of the owner's new goal: requests from a cleared chat leaked into the next session (see 35). Wanted: clearing the chat also closes the manager session, so a new goal never shares a chat with old ones.
41. (2026-11-25) Userscript updates need the owner to reinstall in 9 Tampermonkey windows. Wanted for the next version: a safe update path (version check + one-click reinstall page per window, token never in a public URL).
42. (2026-11-25, E62) The manager's "new chat" click hid the tab and dropped the connection (tab_hidden → disconnected); the watchdog reloaded the window by itself after the 3-minute grace (E56 worked unattended). Wanted: new_session on the manager navigates the same tab to /agent by URL (open_chat) instead of clicking a link that can open or focus another tab, and the watchdog's grace for the manager is shorter (60 s) while a job waits on it.
43. (2026-11-25, E63, fixed locally) The manager's 12-task plan for NEXUS OSINT was 21,972 characters; every reply was cut at 20,000 before parsing, so valid JSON became invalid and the job went to needs_human. Fix: parse up to 200,000 characters; only the stored copy is cut at 20,000.
44. (2026-11-25, E64) The NEXUS OSINT plan (12 tasks) is held behind approval 7fcc8b0d "Delete or destroy critical data": the gate matched feature wording in the spec ("Delete investigations", "delete all stored investigation data"), not a request to delete anything. Wanted: the gate classifies what the request asks AATM to do to the repo/data, not words describing app features; and the approval card shows the matched phrase so the owner sees why. A stale approval 27141432 ("merge main branch release", from a cleared chat) is also pending; see 35/40.
45. (2026-11-26, E65, fixed locally) After the reboot at 07:29 nothing started: ~/.config/systemd/user/aatm.service had `WorkingDirectory="/home/jaimes/AATM"`. systemd does not strip quotes in WorkingDirectory=, reads the value as "not absolute" and refuses to load the whole unit (bad-setting), so autostart never ran. The installed unit was also an old copy (ExecStart start-all.sh, not autostart.sh). Fix: install-autostart.sh writes WorkingDirectory unquoted (only % escaped); reinstalled; `systemctl --user start aatm.service` brought 9/9 up. Test: test_autostart.py asserts no quoted WorkingDirectory.
46. (2026-11-26, E66, fixed locally) Manager job 23471417 created task 4265b845 titled "..." for worker-1: the text read back from the manager page was AATM's own plan prompt, and its reply-format example (every field "...") passed validation. Fix: validator refuses placeholder strings ("...", "…", "<...>"); `_parse` refuses a reply that starts with the [AATM-MANAGER:<KIND>] marker, so the job retries/re-reads. Open question: why the page returned the prompt (reader picked the user bubble after the 06:2x reloads?). Test: test_e66_e68_self_heal.py.
47. (2026-11-26, E67, fixed locally) start.sh opened backend/bridge/frontend logs with ">", so the restart after the reboot erased the only record of why the stack stopped at 06:23. Fix: keep the previous run as <name>.log.prev.
48. (2026-11-26, E68, fixed locally) Owner asked "why don't the workers all work in parallel?". worker-1 hit a human check at 05:10 on its first task (backend core 49a40f0c); 6 of 8 queued tasks depend on it. The watchdog saw the page clean at 08:22 and reset the bridge, but the backend kept worker-1 needs_human and the task blocked until a human pressed Resume, so the whole plan waited 3+ hours. Fix: scheduler tick runs resume_cleared_profiles(): a needs_human profile the bridge reports connected + ready is resumed exactly like the Resume button (same shared function, audited as actor self-heal). Never touches the check itself.
49. (2026-11-26, idea) The 12-task NEXUS plan hangs 6 tasks on one backend-core task (exactly 50%, so CHAIN_SHARE 0.5 let it through). Consider a stricter share, or warn when the gating task's worker is the only path forward.
50. (2026-11-26) Task c4e0aae6 (worker-4, research/docs only) blocked: "Arena Agent stopped without a pull request ... after 2 follow-ups". A docs task should still open a PR; investigate whether the agent wrote the doc and simply didn't open the PR, and auto-requeue once.
51. (2026-11-26, owner decisions) Owner asked for all 8 workers at once. MAX_CONCURRENT_WORKERS was already 25; the real limits were (a) plan dependencies: 6 tasks waited on one backend-core task, (b) a worker whose PR is open is reserved for that PR's follow-ups, so one stuck PR idles a worker, (c) MAX_REVIEW_ROUNDS=2 sent PR #3 to the owner, (d) PR #4 (docs only) had no CI, so auto-merge waited forever. Applied: PR #4 merged on the owner's word; MAX_REVIEW_ROUNDS=5 in .env and PR #3 round 3 queued; dependencies of 8 queued tasks dropped (originals kept in each task's checkpoints, audit task.dependencies_dropped); worker-4's blocked docs task requeued; "..." task cancelled. Result 08:55: 7/8 working; worker-8 waits only on the T16 file-overlap guard (correct).
52. (idea, so it does not recur) Planner: give every worker a task it can start at once (one wave), keep dependencies only for real outputs; merge queue: an approved PR that changes only *.md files and a repo with no workflows yet may merge without CI (owner chose this for PR #4); a worker reserved by an approved PR waiting on merge should be free to start its next task in a new Arena session.
53. (2026-11-26 09:00) worker-6's task 9eac8ff5 stopped without a PR after 2 follow-ups (same as worker-4 earlier). Now automatic: arena_runner._retry_without_pr re-queues it once in a new session (audit arena.no_pr_retry); a second stop goes to the owner.
54. (2026-11-26 09:00) worker-3 stayed "working" on task c77fe508 that a backend restart had put back in the queue (doctor: worker_working_without_task). Now automatic: heal_orphaned_state frees a worker whose task is queued and unlocked.
55. (2026-11-26 09:00) Manager review of PR #6 failed: "text was typed but the page did not send it" x3; the page showed "Reconnect" and no send button, and the review task stayed open. A fresh tab fixed the page (Reconnect not pressed: it may start a GitHub sign-in, which is the owner's). Now automatic: after 3 failed attempts the manager closes its session, opens a new chat and tries once more (audit manager.fresh_session_retry).
56. (2026-11-26) All fixes 45-55 committed locally as 9fd63f2 on feat/self-heal-manager (121 affected tests pass; full verify.sh pending under the live load) and live after a backend restart.
57. (2026-11-26 09:10) Worker 4's window went offline after "text was typed but the page did not send it"; the watchdog's reload (a new tab in the running Chrome) did not reconnect it for 8+ minutes. A full window restart did. Now automatic: from the second try the watchdog restarts the window's Chrome (aatm_stack.restart_window: close, open empty, then Arena) instead of another reload. Watchdog restarted with the new code.
58. (idea) doctor's queued_task_stuck fires for tasks that wait for a known, correct reason (file-overlap guard T16, worker reserved by its open PR). Report the reason instead of "needs a human decision".
59. (2026-11-26 09:30, owner decision) 6 PRs waited for the manager (one review at a time, 10-20 min each) while 7 workers sat idle: a worker stayed bound to its PR's Arena session until merge. Built on feat/park-review-sessions, cherry-picked to feat/self-heal-manager as ade6809, live 09:55: a 'reviewing' session with a recorded chat URL is parked when the worker's next task starts; a correction brings the window back to the PR's chat first. Result live: reservations went from 7 workers to 2 (worker-5 working; worker-3's session has no recorded chat). The 3 tasks left in the queue overlap the files of open PRs #9 and #11 (T16), so they correctly wait for those merges.
60. (2026-11-26) worker-3's session (task c77fe508, PR #5) never recorded its chat URL, so it cannot be parked. Find why _record_chat_path missed it (selector_test path vs chat_path_matches_branch). (The branch holds only the last 8 characters of the chat id, so the URL cannot be rebuilt from it.)
61. (idea) The manager is now the bottleneck: one review at a time, and each review re-runs the project's tests. Options: let an idle worker do a first review in parallel, or review several small PRs in one prompt.
62. (2026-11-26 10:10, owner decision) Peer review: idle workers review PRs in parallel with the manager (backend/app/orchestration/peer_review.py, commit 30cac7f on feat/self-heal-manager; built on feat/peer-review). 12 peer/park tests pass; manager/scheduler/session suites 102 passed. Goes live at the next backend restart, timed for when the manager is between reviews (a restart mid-review made it run PR #5's review twice).
63. (2026-11-26) Full verify.sh (on 9fd63f2..ade6809, under the live load of 9 Chrome windows): 13 gates passed, 1 failed: backend pytest 796 passed / 3 failed. test_stop_command_clicks_mock_agent_button (15 s node timeout) passes alone; test_twenty_tasks_scale_up... is timing-dependent and fails on the pre-change commit e21486e too (2/5 passes there, 1/5 now); test_device_heartbeat_latency_under_200ms is a latency limit. Bisecting the autoscale failure found a real bug from ade6809: open_session_for's "phase != 'parked'" dropped NULL-phase sessions; fixed in 0b593c9 with a test. verify.sh must be re-run green on a quiet machine before any push/PR.
64. (2026-11-26 10:19) Peer review live: the backend was restarted right after the manager finished a review (no review lost). In the first scheduler tick worker-2, worker-4 and worker-5 took 3 queued review jobs (PEER_REVIEW_MAX=3), each after its own waiting PR session was parked; the manager reviews in parallel. 9 PRs were waiting.
65. (2026-11-26 10:21) False alarms after peer review went live: the doctor reported worker-4/worker-5 as "working without a task" (they work on a review task assigned to manager-0) and 3 queued review tasks as stuck. Fixed in doctor.py (a worker with an open peer_review session is not flagged; review tasks are left to the manager job checks) with 2 tests; 24 doctor tests pass. No owner action was needed.
66. (2026-11-26 10:24) Peer review bugs seen live: (a) worker-4's failed review of PR #6 queued the manager's short fallback review AND my code requeued the original job -> PR #6 would be reviewed twice; closed the duplicate job 792d9a75 by hand (manual action -> now code: the original stays failed when a fallback exists). (b) worker-4's window failed 3 peer reviews in a row ("text was typed but the page did not send it"; one reply was the template echoed back) -> a worker whose peer review fails now rests 30 min (PEER_COOLDOWN). Commit 6th on feat/self-heal-manager; 14 tests pass. Open: why worker-4's window cannot send long prompts (its own tasks worked).
67. (2026-11-26 10:30) Peer review live result: 1 of 9 peer reviews succeeded (worker-2, PR #8 request_changes). Failures in worker windows: reply read as 0 chars after "quiet_without_reply" (agent mode answers differently), the JSON retry read back our own template (224 chars = the prompt echo), "send_failed: text was typed but the page did not send it" (worker-4), and sessions closed 9 s after creation. Each failure held a job for 2-4 min before the manager got it. Added an automatic brake: 3 failures in a row pause the pool 1 h with one owner notification (commit on feat/self-heal-manager, 8 tests pass). Open (needs investigation, not guessed): make the reply reader work for review chats in agent-mode worker windows; until then peer review mostly falls back to the manager.
68. (2026-11-26 10:33-10:36) A wave of "send_failed: text was typed but the page did not send it" hit several windows at once (profile-manager, profile-backend-docs, profile-worker-5), so it comes from the Arena page, not from peer review or agent mode alone. Recovered with no manual action: the watchdog reloaded the manager window (10:35, try 1/3), the 8 review jobs went waiting_connection -> queued/running, and self-heal re-dispatched worker-1's PR #8 follow-up after it was blocked. Open: find what the page shows when send is refused (limit, banner, disabled button) so the reason is logged, not only the symptom.
69. (2026-11-26 10:41) worker-5 stuck "working" since the 10:30 backend restart: the restart killed its peer review of PR #10 (the job was later given to worker-2), but worker-5 kept the review as its current task with the peer chat open, so it could not take the change request on its own PR #11. Manual fix: closed the orphan peer session and set worker-5 idle. Turned into code: PeerReviewPool.heal_orphans runs every tick (closes peer chats not running in this process, frees their workers), 1 test; 9 peer tests pass. Live at the next backend restart.
70. (2026-11-26 10:50, owner approved) Safe restart (commit 13107fa): scripts/ubuntu/safe_restart.py raises data/restart.pending (manager and peers take no new job), waits until no manager job runs (timeout 25 min -> no restart, flag removed; flag older than 45 min ignored), then restarts only the backend; the backend clears the flag on start. Replaces the manual "kill backend.pid + start.sh" that cut off PR #9's review and stranded worker-5 (items 64, 69). Tests: 3 script + 2 backend; manager suites 63 passed, script suite 78 passed. Rule from now on: every backend restart goes through safe_restart.py.
71. (2026-11-26 10:57) Safe restart used for real: waited for 4 running reviews, restarted at 10:57 when none ran, flag removed by the new backend. 18 manager tests then failed because they read the live data/restart.pending -> tests now use a private flag (conftest autouse).
72. (2026-11-26 10:48) PR #6 review blocked/needs_human: the reader took our own JSON retry (drawn with its code fence, 2 chars longer) for the reply, and its empty template failed as "invalid verdict". Fixed: _is_own_prompt also matches the prompt's opening; such a read reloads the chat and reads again (5cf5df2, live since 10:57). A review blocked only on an unreadable reply now gets one automatic new job after 5 min.
73. (2026-11-26 10:48) PR #12 follow-up (worker-4, profile-qa-e2e) failed: 3 quick retries all "send_failed", then status failed and nobody retried. Now self-heal queues such follow-ups again 5 min later, at most twice. Both rules: 109 tests pass. Open: why profile-qa-e2e refuses to send (it also showed "terms of service" at 10:24, which is the owner's to accept).
74. (2026-11-26 11:00-11:10) 7 of 9 Arena windows reported visibility "hidden" (screen not locked; likely minimised or on another workspace). Pages are not drawn, so 4 review follow-ups failed with page_hidden (PR #6, #10, #12, #13) and returning to a PR chat timed out. page_hidden added to self-heal's transient errors. Open: keep windows drawing when minimised (the backgrounding flags are set but are not enough on Wayland) or alert the owner at once when a window is hidden.
75. (2026-11-26 11:12, owner decision) Orderly stop for development: data/restart.pending raised (no new review), queued task 575e4df8 (worker-1) held as blocked and listed in data/paused_tasks.json (restore to queued after restart), waiting for worker-1 (PR #8 follow-up) and worker-5 (PR #11 follow-up), then backup.sh and stop-all.sh. Failed follow-ups for PR #6/#10/#12/#13 stay failed; self-heal requeues them after the restart (page_hidden/send_failed are transient now).
76. (2026-11-26 11:20) Loop found during the orderly stop: worker-1's PR #8 follow-up failed to send 3 times -> escalated needs_human -> E68 resume_cleared_profiles saw a "ready" window and resumed it seconds later, 65 times in a row (each cycle hit the window again). Fixed (commit above): escalations after repeated failures ("follow-up failed N times in a row", no-PR) are not auto-resumed, and a window is auto-resumed at most 3 times per 30 min. 2 tests; 21 pass. The orderly stop no longer waits for this task (it cannot finish); its state stays in the DB.
77. (2026-11-26 11:35) Orderly stop done: services stopped (watchdog, backend, frontend, bridge), Arena windows closed, DB copy backups/aatm-db-at-stop-20261126.db, full backup backups/aatm-backup-20261126T102236Z.tar.gz (1.6 GB; tar warned that Chrome profile files changed while being read, the DB copy is consistent). Resume: ~/aatm-reports/RESUME_2026-11-26.md.
78. (2026-11-26 dev, quiet machine) Hidden windows (item 74) -> E78, commit ad0ed5d: the bridge now remembers that a page is hidden (tab_hidden event, or a command refused with page_hidden) and shows page_hidden/page_hidden_seconds in the profile summary; the watchdog restarts a window that stays hidden 90 s and runs no command (at most 3 times, 5 min apart; Chrome opens it again on screen), then alerts the owner once. A human-check window is never touched. 4 watchdog + 2 bridge tests. No userscript change (no reinstall needed).
79. (2026-11-26 dev) send_failed cause (items 66, 68, 73) -> E79, commit 5dbb2bf: on "text was typed but the page did not send it" the adapter reads the page (selector_test, read-only). A running agent/stop button means the message did go out -> treated as sent (no duplicate prompt); a quiet, visible page without a human check gets one more try after 3 s; otherwise the error carries what the page showed (state, visibility, composer, send button, human check, offline) so the cause is in the task's error, not only the symptom. 5 tests.
80. (2026-11-26 dev) Peer review analysis from the DB: of the peer jobs, worker-2 (PR #8, #12), worker-8 (PR #12), worker-4 (PR #5 fallback) gave real verdicts; failures were send_failed (now E79), one 20-min stall, and worker-7 answering PR #13 with {"pr_number": 0, "verdict": "غير متاح"} twice. E80, commit 3f82aca: a peer whose JSON says it cannot review hands the PR to the manager at once (no JSON retry on that window); a verdict in other words keeps its retry; the manager keeps its retry. 3 tests; 12 peer tests pass.
81. (2026-11-26 dev) Full verify.sh on the quiet machine: first run 13/14 (only test_stop_command... timed out loading jsdom at 15 s under the parallel suite; autoscale and heartbeat latency passed) -> probe timeout raised (commit 0f9851d). Second run on the final code: VERIFICATION PASSED 14/14, backend 832 passed / 4 skipped; bridge ruff clean, bridge tests 12 passed, scripts tests 82 passed. Owner then asked to approve, push to GitHub and make it main: PR from feat/self-heal-manager to oldman-088/AATM main.
82. (2026-11-26) PR #108 merged into main (36ddcfb) after green CI on head 0f9851d; CI on main after the merge: green. AATM itself is still stopped (start from main when the owner wants).
83. (2026-11-26 14:56) v3.7.0 started from main with start-all.sh: 9/9 windows connected. Held task 575e4df8 restored to queued (data/paused_tasks.json -> paused_tasks.restored-20261126.json). The doctor reported review job 2bc8e558 (PR #6) as needs_human although the newer job f9329064 had given the verdict -> cancelled by hand, and now automatic: a needs_human review job is closed once a newer job for the same review is done (_close_superseded_review_jobs, 1 test; manager suite 45 passed).
