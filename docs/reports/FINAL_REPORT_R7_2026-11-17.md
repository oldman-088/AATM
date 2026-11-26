# AATM — R7: Manager on Arena Agent mode — Final report

Branch: `manager-arena` (from `main` @ 1b52d47). Local commits only — nothing pushed, no PR opened, no sudo used.
No existing test or protection was removed or weakened; every existing setting is kept.

## Status per step

| Step | Status | Commit |
|---|---|---|
| R7.1 Manager profile and connection | Complete | `20af605` |
| R7.2 `arena_agent` assignable to the manager | Complete | `ce3e19c` (+ test follow-up `fb6c4e3`) |
| R7.3 `ManagerArenaRuntime` | Complete | `e0fc3f0` |
| R7.4 Mock page and tests | Complete | `e92f885` |
| R7.5 Docs | Complete | `5fbf3e0` |
| `verify.sh` | Partially complete — 10 of 12 gates pass; the 2 failing gates are caused by the local `.env` / `.env.bak` files (see below) | — |

## What was done

### R7.1 — manager-0 as a first-class Arena profile
- Seeded profile `profile-manager` (name `manager`, Arena mode `agent`, profile dir `.../manager`), linked to `manager-0`; applied idempotently to existing databases.
- The manager uses the same userscript generator and connector WebSocket as the workers, with its own token, and counts as one Arena profile (dashboard counts include it).
- Browsers tab → Arena Agent dashboard: a manager card above the worker cards, with Generate script, Selector test, New session, Read last reply and Keep working; it shows connected / not connected (with an Arabic hint), current session, current activity and last activity, plus the Selector test state.

### R7.2 — Arena Agent (browser) for the manager
- Wizard: 4th option "Arena Agent (browser)" (no API key; endpoint `http://127.0.0.1:4100`, credential `env:ARENA_BRIDGE_TOKEN`). "تعيين للمدير (manager-0)" stores the choice; Test connection for this type only reads readiness and sends nothing to Arena.
- "Arena Agent Runtime" provider card is healthy only when the manager profile is connected and its Selector test passed; otherwise it gives the reason in Arabic (connector disabled, bridge unreachable, no script yet, offline, needs human, not in agent mode, Selector test not run / failed).
- New setting `AATM_MANAGER_PROVIDER` (`arena_agent` | `anthropic_claude` | `openai_compatible` | `demo`; empty = previous behaviour / wizard choice). The `.env` value overrides the wizard choice. `demo` gives a clearly labelled demo reply and creates nothing.

### R7.3 — ManagerArenaRuntime
- One submit path: every Arena submit (workers, manager, operator's manual submit) goes through `arena_submit_queue` — one prompt per profile at a time, recorded per source. The runtime also runs only one manager job at a time.
- Jobs persisted in the new `manager_jobs` table (migration `e0f1a2b3c4d5`, tested upgrade → downgrade → upgrade on a fresh database):
  1. Owner chat → reply in the Command Console (the "المدير يكتب الرد عبر Arena…" placeholder is replaced in place) and streamed to the manager robot screen (`agent.code` frames).
  2. Planning → one JSON block `{"summary_ar","tasks":[…]}`, validated (known assignees only, max 10 tasks, no empty fields, `depends_on` = indexes of earlier tasks) → tasks created and dispatched to the workers' Arena sessions through the existing scheduler / R3 flow; the acceptance criteria reach the worker prompt.
  3. Review → Arena-built PRs are reviewed by `manager-0` (task, acceptance criteria, worker report, real diff truncated with a marker). `request_changes` → comments go back to the same worker session (R3 correction loop); `approve` → merge approval waits for the owner. The manager never merges.
  4. Status ("وين وصلتوا؟") → a real summary of tasks, PRs and blockers is attached.
- Parsing: first fenced ```json block, else the first balanced `{…}`; on failure one retry "Reply again with only the JSON block, no other text."; a second failure marks the job "needs human" with the raw reply. Nothing is invented.
- Prompts in `backend/app/orchestration/arena_prompts.py`.
- Sessions: one long-lived manager session per project; a new one on "New session", project change or `ARENA_MANAGER_MAX_EXCHANGES` (default 40), each starting with a context prompt. Keep working obeys `ARENA_AGENT_MAX_KEEP_WORKING`. 20 minutes without change → needs human.
- Manager not connected → "المدير غير متصل بـ Arena — افتح ملف Chrome الخاص بالمدير", the job waits and runs once the profile connects. No demo or model fallback with `arena_agent`.
- API: `POST /api/v1/arena/manager/new-session`, `GET /api/v1/arena/manager/jobs`; chat `intent` (`chat` | `goal` | `status`) and `/chat`, `/goal`, `/status` prefixes.

### R7.4 — mock page and tests
- `bridge/mock_arena/agent.html` answers as the manager: context ack, Arabic chat, status echo, valid plan, approve / request_changes reviews, invalid replies (`mgr_invalid=N`), and counts submits sent while generating.
- `backend/tests/test_r7_manager_arena.py` — 23 tests, all passing, including two Chromium end-to-end tests (mock page, real bridge and userscripts, git + fake `gh`, manager and worker in separate browser contexts):
  - manager connects, Selector test passes, provider card healthy;
  - owner chat → reply in the console and on the robot screen;
  - goal → plan → task dispatched to the worker's Arena session;
  - worker PR → request_changes → correction in the same worker session → approve → merge approval pending, `gh pr merge` never called;
  - invalid JSON → exactly one retry → needs human, no tasks created;
  - not connected → Arabic message, job waits, no demo, no model call;
  - manager and worker use the same submit path; at most one manager prompt in flight; 0 submits while generating; no model API used.
  - plus unit tests (classification, JSON extraction, plan/review validation, prompts, diff truncation) and scripted-page runtime tests (retry fixing an unknown assignee, session rotation, New session context, Keep working limit, stall, status from real data).
- Frontend: manager card tests (3) and wizard option tests (2) in vitest.

### R7.5 — docs
- `docs/OPERATIONS.md`: Arabic section «تشغيل المدير عبر Arena» (Chrome command, Arena sign-in, script from the manager card, Selector test, `AATM_MANAGER_PROVIDER=arena_agent`, first goal, message types, review/approval rules, sessions, failures, troubleshooting). A doc test checks it and that every setting it names exists.
- `.env.example`: `AATM_MANAGER_PROVIDER=` and `ARENA_MANAGER_MAX_EXCHANGES=40`.

## Test results

### `bash scripts/ubuntu/verify.sh` (final run, after `fb6c4e3`) — exit 1, 10 passed / 2 failed gates
| Gate | Result |
|---|---|
| Prerequisites (python3, git, node) | PASS |
| `alembic upgrade head` (single head) | PASS |
| `ruff check backend` | PASS |
| `ruff check --config backend/pyproject.toml bridge` | PASS |
| `pytest` backend | **FAIL** — 318 passed, 4 failed, 4 skipped |
| `pytest -v bridge/tests/test_bridge.py` | PASS — 11 passed |
| Static secret scan | **FAIL** — 6 findings, all in the local `.env` / `.env.bak` |
| `tsc --noEmit` | PASS |
| `vitest run` | PASS — 97 tests in 6 files |
| `vite build` | PASS |

The 4 backend failures in that run:
- `test_aatm_backend.py::test_memory_git_protected_branch_approval_and_qa`, `test_e2e_verification.py::test_e2e_secret_scan_detects_secrets_across_file_types` and `test_e2e_verification.py::test_e2e_github_ci_templates_and_security_policy_scan_clean` — the secret scanner reports the local `.env` / `.env.bak` files (see "Needs your attention"). They fail in every run on this machine and pass on a clean checkout of `main` that has no such files.
- `test_phase2_real_model_wizard.py::test_phase2_arabic_directive_end_to_end_creates_hello_and_runs_pytest` — intermittent, see below.

### Intermittent failures (not reproduced, cause not confirmed)
Each failed once and then passed every time I re-ran it:
| Test | Failed | Passed |
|---|---|---|
| `bridge/tests/test_bridge.py::test_adapter_generate_stream_and_router_route_and_stream_e2e` | 1st `verify.sh` run | 2nd `verify.sh` run, 4 runs alone, once on `main` |
| `test_phase2_real_model_wizard.py::test_phase2_arabic_directive_end_to_end_creates_hello_and_runs_pytest` (no worker message after one cycle) | 2nd `verify.sh` run | 1st `verify.sh` run, 3 runs of its file, 3 runs of all earlier test files |
| `test_aatm_backend.py::test_providers_and_browser_profiles_lifecycle` | 1 of 3 runs of the earlier test files | both `verify.sh` runs, 2 other runs, alone |

On a clean `main` worktree, the same earlier test files ran twice without these failures. That sample is small, so I cannot say whether R7 makes them more likely. The R7 manager runtime only starts background work when the manager mode is `arena_agent`, which only the R7 tests set, and those run after these files.

### First `verify.sh` run (before `fb6c4e3`)
Same result, plus `test_phase2_real_model_wizard.py::test_wizard_endpoints_and_per_employee_model_assignment`: it expected exactly 3 wizard presets and R7.2 adds a fourth. Fixed in `fb6c4e3`; that file now passes 5 of 5.

Earlier targeted runs (from `backend/`):
- R1–R7 + Arena connector + manager understanding + review/merge suites: 51 passed (before R7.4 tests were added).
- `test_r7_manager_arena.py`: 23 passed (including both Chromium end-to-end tests).
- `test_phase2_real_model_wizard.py`: 5 passed after the follow-up fix.
- `.venv/bin/ruff check --config backend/pyproject.toml bridge`: passed.
- `.venv/bin/pytest -v bridge/tests/test_bridge.py`: 11 passed in 4 separate runs and in the final `verify.sh` run; failed once in the first `verify.sh` run.

## Decisions
- Profile id `profile-manager`, profile name `manager`.
- Empty `AATM_MANAGER_PROVIDER` keeps the old behaviour; the wizard's "assign to manager" is stored as a system setting; `.env` wins over it.
- The Arena Agent provider keeps its priority, so adding it never makes it every employee's first provider.
- Jobs run only when the manager is connected; the Selector test result affects the health card only (it is read-only and the owner may run it at any time).
- Owner messages are classified heuristically (status question / other question / goal), with prefix and `intent` overrides.
- Manager jobs are persisted (`manager_jobs`) so they survive restarts and can wait for a connection.
- With `arena_agent`, the manager reviews only PRs built through Arena sessions; other PRs keep the existing reviewer flow.
- The operator's manual submit also goes through the shared queue, so nothing can submit to a busy profile.
- `depends_on` uses indexes of earlier tasks in the same plan.
- The existing wizard test that expected exactly 3 presets was updated to 4, and now also checks that the three original presets are unchanged (follow-up commit `fb6c4e3`).

## Skipped / not done, and why
- Nothing from R7 was skipped.
- Not tested against the real Arena site: everything was verified on the local mock page. Selectors on the real page must be confirmed with the Selector test in the manager's Chrome profile.

## Needs your attention
- **Secret scan on local files.** The secret-scan gate and 3 backend tests fail on this machine. The findings are `basic_auth_url` (a URL with a username:password) on lines 15, 16 and 83 of the local `.env` and `.env.bak` at the repo root. Both files are ignored by git and were never committed. They were changed at 06:22 and 06:25 today; I did not write to them (I only read three setting names). I did not change them. To clear the gate: remove `.env.bak`, or move it out of the repo, and replace the credentials inside those URLs with `env:` references / separate variables.
- **Note:** `bridge/tests/test_bridge.py` drops and recreates the tables of the database configured in the root `.env` (existing behaviour, not changed). Earlier in this work I also ran `alembic upgrade head` once against that database by mistake; the same upgrade is what `verify.sh` runs.
- Three tests failed intermittently (see "Intermittent failures"). I could not reproduce them, so they are reported as is, not fixed.

## `.env` lines to run the manager on Arena
```
ARENA_CONNECTOR_ENABLED=true
AATM_MANAGER_PROVIDER=arena_agent
ARENA_MANAGER_MAX_EXCHANGES=40
```
(Plus the existing Arena settings from R1–R6: `ARENA_ALLOWED_ORIGINS`, `ARENA_BRIDGE_TOKEN`, `ARENA_BRIDGE_URL`.)

## `git log --oneline main..HEAD`
```
fb6c4e3 R7.2 follow-up: wizard presets test expects the Arena Agent (browser) option
5fbf3e0 R7.5: operations guide — تشغيل المدير عبر Arena
e92f885 R7.4: mock Arena answers as the manager; manager-on-Arena tests
e0fc3f0 R7.3: ManagerArenaRuntime — manager-0 works through its own Arena session
ce3e19c R7.2: Arena Agent (browser) is assignable to the manager
20af605 R7.1: manager-0 as a first-class Arena profile with its own dashboard card
```
