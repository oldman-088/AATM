# AATM — Verification Report (Worker 4: Orchestration, QA & Ops)

**Report Date (UTC):** 2026-11-15
**Branch:** `arena/e5fef882-aatm` (integrated W1 + W2 + W3 + W4)
**Scope:** Divisions 01–12 — end-to-end verification, Round 2 integration,
Multi-Device worker daemon, isolated per-agent Git worktrees, SSE bridge streaming, GitHub CI/templates, E2E test suite and deployment hardening.

---

## 1. Verification Summary

| Gate | Command | Result |
|---|---|---|
| Database migrations | `alembic upgrade head` | **PASSED** — head `6c3ab8f4d2e1` |
| Backend lint | `ruff check backend` | **PASSED** — 0 errors |
| Bridge lint | `ruff check --config backend/pyproject.toml bridge` | **PASSED** — 0 errors |
| Backend tests | `pytest -q` | **PASSED** — 80/80 (0 warnings) |
| ↳ E2E verification suite | `pytest tests/test_e2e_verification.py` | **PASSED** — 32/32 |
| Bridge tests | `pytest -v bridge/tests/test_bridge.py` | **PASSED** — 8/8 |
| Static secret scan | `python -m app.qa.engine` | **PASSED** — 0 findings (132 files) |
| Frontend typecheck | `npm run lint` (`tsc --noEmit`) | **PASSED** — 0 errors |
| Frontend unit tests | `npm run test` (vitest) | **PASSED** — 38/38 (2 files) |
| Frontend production build | `npm run build` (vite) | **PASSED** (`1,584` modules) |
| One-command pipeline | `scripts/ubuntu/verify.sh` | **PASSED** — 12/12 gates, exit 0 |
| Live multi-device round trip | real uvicorn + daemon OS process | **PASSED** |

---

## 2. Defects Found and Remediated

Five defects were found during verification. All five are fixed and covered by
regression tests in `backend/tests/test_e2e_verification.py`.

### D1 — Emergency Stop orphaned in-flight work (HIGH, Division 07/11)

*Symptom.* A global Emergency Stop set running tasks to `blocked`. Clearing the stop
only resumed *agents*; the tasks stayed `blocked` **permanently**. The scheduler only
dispatches `queued` tasks and the stalled-task sweep only inspects `in_progress` tasks,
so no code path could ever recover them. An employee-scoped stop was worse: it did not
touch the employee's tasks at all, leaving a task `in_progress` with `lock_owner` set to
a halted agent — unreachable by the scheduler and recoverable only after the 15-minute
stall timeout.

*Fix.* `ReplacementAndEmergencyService` now marks halted tasks with an
`EMERGENCY_STOP_MARKER`, an employee-scoped stop also halts that employee's active
tasks (releasing their locks), and `resume_emergency_stopped_tasks()` requeues halted
work. It is called by `clear_emergency_stop()` and by the QA self-healing sweep.

*Verification.* `test_e2e_emergency_stop_global_scope_and_recovery`,
`test_e2e_emergency_stop_employee_scope_halts_own_task`.

### D2 — Secret scanner had severe blind spots (HIGH, Division 11)

*Symptom.* `run_security_scan()` only read `*.py`, `*.ts` and `*.tsx` and matched three
token formats. It could not detect a secret in `.env`, a shell script, PowerShell,
YAML/JSON/TOML — exactly the files where credentials most often leak — and it missed
generic credential forms (PEM private keys, `sk-proj-`, Slack `xoxb-`, AWS secret keys,
`password = ...`). This directly contradicted the hard rule *never commit secrets,
`.env`, or private browser profiles*. It also scanned generated `data/` and `backups/`
directories, producing noise.

*Fix.* The scanner now:
- covers 24 file types (`.py .ts .tsx .js .jsx .sh .ps1 .bat .env .yaml .yml .json
  .toml .ini .cfg .conf .properties .txt .sql .html .xml` and friends);
- evaluates 22 detection rules (17 high-confidence token formats + 5 generic rules);
- excludes generated/local directories (`data`, `backups`, `logs`, `node_modules`,
  `.venv`, `dist`, `coverage`, caches);
- checks the **git index** for forbidden artifacts — a committed `.env`, private key
  file, browser profile, local database or credential dump is reported;
- never echoes the matched secret value (findings carry file, line and rule id only);
- separates *reported* findings from *suppressed* ones, so documented development
  placeholders stay visible without failing the build.

*Verification.* `test_e2e_secret_scan_detects_secrets_across_file_types`,
`test_e2e_secret_scan_flags_forbidden_committed_artifacts`.

### D3 — Approval Center gate was bypassable (HIGH, Division 07/10)

*Symptom.* The Manager Chat governance gate matched three exact substrings
(`"merge main"`, `"deploy prod"`, `"increase budget"`). These directives bypassed it
entirely: *"merge master into main"*, *"merge to production"*, *"push to main"*,
*"force-push to master"*, *"delete all data"*, *"rotate credentials"*. This violated
the protected-branch approval gate and the Manager's operating principle that merging
into protected branches, deleting critical data and changing credentials all require
explicit Approval Center authorization.

*Fix.* The gate now uses seven tolerant regex rules over protected branch names
(`main`, `master`, `production`, `prod`, `release`) and the risk categories
`merge`, `deploy`, `spend`, `delete` and `credential`. One request is created per
distinct risk category. Benign directives (*"merge the feature branch"*, *"build a
dashboard for all users"*) are still not gated.

*Verification.* `test_e2e_high_risk_directives_are_gated` (9 variants),
`test_e2e_benign_directives_are_not_gated` (3 variants).

### D4 — Lint failure hidden by an inaccurate status report (MEDIUM, Division 12)

*Symptom.* `STATUS.txt` claimed *"Backend ruff check: PASSED (0 errors)"* while
`ruff check` reported 4 errors (import ordering and trailing whitespace in the Alembic
migration files). The status report had drifted from reality.

*Fix.* The lint errors are fixed and `STATUS.txt` is regenerated from verified command
output rather than asserted.

### D5 — Timezone comparison crash in recovery paths (MEDIUM, Division 08/12)

*Symptom.* SQLite stores `DateTime(timezone=True)` columns as *naive* UTC strings.
Comparing them in Python with an *aware* `datetime` raises
`TypeError: can't compare offset-naive and offset-aware datetimes`. The device offline
sweep hit this immediately; the existing stalled-task recovery avoided the crash only
because it compared in SQL, where it silently relied on ISO string ordering — correct
by accident and not portable to PostgreSQL.

*Fix.* `app/runtime/temporal.py` normalises both sides to naive UTC, and the stalled-task
recovery now filters in Python instead of relying on SQL string comparison.

---

## 3. Manager Assignments Executed

### A1 — Multi-Device Worker Daemon (Divisions 10 & 12)

The feature matrix previously recorded Multi-Device Execution as *Partially Complete*:
only the central `ExecutionDevice` registry existed, with no way to actually run work on
a remote host.

**Delivered:**
- `backend/app/runtime/worker_daemon.py` — a standalone daemon that registers with the
  central server, heartbeats, claims tasks, executes them inside an isolated local
  workspace and reports results. Configurable via CLI flags or `AATM_*` environment
  variables.
- `backend/app/runtime/device_auth.py` — device-scoped tokens derived as
  `HMAC-SHA256(SECRET_KEY, device_id)`. The token cannot be forged without the server
  secret and is bound to exactly one device id. Nothing is persisted, so a database
  leak does not expose device tokens.
- `backend/app/api/routes/devices.py` — the device work API: `register`, `heartbeat`,
  `claim`, `report`, `status`, plus an offline-device sweep.
- `backend/app/runtime/sandbox.py` — the workspace-containment rules, now shared
  between the server runtime and the remote daemon so the two cannot drift apart.
- `scripts/ubuntu/start-worker.sh` and `scripts/windows/start-worker.ps1` — daemon
  launchers.
- The local scheduler now skips tasks pinned to a remote device, so a remote task can
  never be stolen by a local worker.

**Verified live** (real uvicorn process + daemon in a separate OS process): device
registered → task claimed → executed in the remote isolated workspace → deliverable
written to disk → server marked the task `completed` and attributed it to the device.
A dead node's task is reclaimed by the self-healing sweep within the offline window.

### A2 — End-to-End Test Suite (Division 11)

`backend/tests/test_e2e_verification.py` — 23 tests covering all six verification areas
plus the governance gate and the multi-device round trip. Every defect above has a
regression test. The suite runs as part of `pytest` and of `scripts/ubuntu/verify.sh`.

### A3 — Deployment Hardening (Division 12)

- `scripts/ubuntu/verify.sh` and `scripts/windows/verify.ps1` — a single command that
  runs every release gate (prerequisites, migrations, lint, tests, secret scan,
  frontend typecheck/test/build) and exits non-zero on failure.
- `python -m app.qa.engine` — a reusable secret-scanner CLI, usable as a CI or
  pre-commit gate.
- `start.sh` / `start.ps1` — the backend is now health-checked before success is
  reported, so a failed start is detected immediately instead of leaving a dead process.
- `stop.sh` / `stop.ps1` — PIDs are validated against the expected process command line
  before any signal is sent, so a recycled PID belonging to an unrelated process is
  never killed. Worker daemons are stopped too.
- `install.sh` / `install.ps1` — prerequisite checks, idempotent venv creation,
  `.env` creation with `0600` permissions, and an automatic verification run.
- `update.sh` / `update.ps1` — re-verifies after updating.
- `backup.sh` / `backup.ps1` — the archive is verified after creation, permissions are
  restricted, and a failure is reported instead of silently producing a partial backup.
- `restore.sh` / `restore.ps1` — archives are validated, and member paths that are
  absolute or contain `..` are rejected, so a tampered archive cannot write outside the
  repository root.
- `docker-compose.yml` — the PostgreSQL credentials are now environment-overridable.
- `.gitignore` — additionally excludes `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.pid` and
  `browser_profiles/`.

---

## 4. Residual Risks & Recommendations

1. **Device tokens cannot be revoked individually.** They are derived from
   `SECRET_KEY`, so rotating that secret revokes every device token at once. A
   per-device revocation list would be the next hardening step.
2. **A default `SECRET_KEY` makes device tokens forgeable.** The register endpoint
   returns `token_hardened: false` and a warning while the development secret is in
   use. Operators must set `SECRET_KEY` in production.
3. **The workspace sandbox is a containment boundary, not an OS-level jail.** Tool
   execution is restricted to an allowlist and a working directory, but a `python`
   invocation can still read files outside the workspace. A production deployment
   should additionally run workers under a restricted user or container.
4. **Windows 11 `.ps1` scripts could not be executed natively** in this Linux sandbox;
   they were verified by inspection and matched against the Ubuntu scripts, which were
   executed end to end.
5. **The governance gate is keyword/pattern based.** It is deliberately biased towards
   gating (over-gating is safer than under-gating), but a sufficiently oblique
   directive may still bypass it. The structured `ApprovalRequest` API remains the
   authoritative path for high-risk operations.
