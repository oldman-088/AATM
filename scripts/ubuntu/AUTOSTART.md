# AATM — boot, autostart and the doctor (E5, E17, E25, E34)

Everything in this file is owned by `scripts/ubuntu/`. Nothing here needs sudo, nothing here stops a
running service, and the whole boot path works with the system Python 3 (no virtualenv required).

## Start by itself after a reboot or a power cut (E5)

```bash
scripts/ubuntu/install-autostart.sh        # one command: write the unit, enable it, enable linger
scripts/ubuntu/uninstall-autostart.sh      # one command: disable it and remove the files
```

`aatm autostart-install` / `aatm autostart-remove` do the same through the `aatm` command.

The unit is a `systemd --user` service, so it belongs to the owner's session, not to the machine:

| what it does | how |
|---|---|
| starts without anyone logging in | `loginctl enable-linger` — the installer turns it on when it is off, and says so. Without it the unit only starts at login, which is useless after a power cut at 03:00. Pass `--no-linger` to decline. |
| waits for the network | `Wants=`/`After=network-online.target` records the intent, but **the user manager has no such target** — the real wait is in `autostart.sh`, which holds until `/proc/net/route` shows a default route (up to `AATM_AUTOSTART_NETWORK_TIMEOUT`, 180 s) and then starts anyway with a warning. |
| survives a start that fails because the box is still settling | the unit is `Type=oneshot` and runs `scripts/ubuntu/autostart.sh`, which retries `start-all.sh` 5 times with a 30 s backoff. `StartLimitIntervalSec=0` stops systemd from rate-limiting those attempts away, and `TimeoutStartSec=infinity` stops it from killing a slow but healthy boot. |
| leaves evidence when nobody was watching | `logs/autostart.log` (attempts) and `logs/doctor.log` (the check after the start). |
| does not touch a running stack | the unit owns the *start*, not the services `start-all.sh` left behind, so removing it never stops anything. Stop the stack with `scripts/ubuntu/stop-all.sh`. |

No systemd `--user` manager (a container, WSL, a minimal install)? The installer exits with code 3
and says so; `install-autostart.sh --desktop` writes `~/.config/autostart/aatm.desktop` instead,
which runs the same `autostart.sh` at graphical login.

Check it without waiting for a power cut:

```bash
systemctl --user is-enabled aatm.service                 # enabled
systemctl --user cat aatm.service                        # the generated unit
loginctl show-user "$USER" --property=Linger             # Linger=yes
systemctl --user start aatm.service && tail -f logs/autostart.log
```

## The doctor: the same check, everywhere (E17)

`scripts/ubuntu/aatm_doctor.py` is the entry point. It runs the state checks in
`scripts/ubuntu/doctor.py` on one read-only connection and adds the two window checks. No check is
implemented twice: `doctor.py` owns the state checks and the repair SQL, `aatm_doctor.py` owns the
window checks (`window_on_wrong_repo`, the human-check alarm), the report shape and the wiring.

```bash
scripts/ubuntu/aatm doctor                      # report: exit 0 clean, 1 findings, 2 unreadable database
scripts/ubuntu/aatm doctor --fix                # back up, repair only the safe cases, report the rest
scripts/ubuntu/aatm doctor --json               # machine-readable (this is what the watchdog reads)
scripts/ubuntu/aatm doctor --log                # also append one line to logs/doctor.log
scripts/ubuntu/aatm doctor --no-bridge          # skip what needs a live bridge
scripts/ubuntu/aatm doctor --bridge-file w.json # read the windows from a file instead
scripts/ubuntu/aatm doctor-state                # doctor.py alone, without the window checks
```

What it looks for, and what happens to it:

| check | on | fixed by `--fix`? |
|---|---|---|
| `worker_working_without_task` | E17 | yes, to `idle`, when no session is open for that worker |
| `window_connected_worker_offline` | E17 | yes, to `idle`, when no task is running for it |
| `session_open_for_finished_task` | E17 | yes, closed |
| `window_on_wrong_repo` | E34 | **never** — see below |
| `account_terms_not_accepted` | E25 | **never** — see below |
| `account_human_check_pending` | E25 | never |
| `task_assigned_to_manager`, `queued_task_stuck`, `window_script_outdated`, `manager_job_stuck_needs_human` | E17 | never (a person decides) |

The three "never" rows are the safety policy, not a TODO:

* **A window on the wrong repository (E34)** is only fixable by touching the repository control of a
  live Arena chat. One wrong click sends the task's prompt into the wrong repository, so the doctor
  reports the window, the repository it is showing, the one the task needs, and the code
  (`repo_mismatch`, `repo_unverified`, `repo_not_selected`, …). The repair belongs to the dispatcher.
* **Terms of Service, CAPTCHA and login (E25)** are human checks by design. AATM never accepts terms
  for a person and never solves a check. The report lists the accounts stopped at the terms page
  (`pending_human.terms_accounts`, and the `checks_run` list proves the check ran) so the owner clears
  each one once, before any task is dispatched.

Every other rule: without `--fix` nothing is written (the database is opened `mode=ro`, the bridge is
only read); with `--fix` a backup is taken first and the fix is **refused** (`BackupRefused`, exit 2)
when that copy cannot be written. The copy is made by the app's own helper when the venv has it, and
by a stdlib SQLite online backup to `data/backups/` (mode 0600, newest 20 kept) when it does not — a
broken virtualenv must not leave a drift unfixed and un-explained.

## Who runs it, and when

* **after every start** — the last step of `start-all.sh` is one read-only pass with `--log`, so the
  boot that nobody saw still leaves a line in `logs/doctor.log`. It is never `--fix` there, and it
  never changes the exit status of the start. `AATM_DOCTOR_ON_START=0` skips it.
* **every 10 minutes** — `watchdog.py` runs `aatm_doctor.py --alerts --once` and turns each finding
  into the desktop notification and `logs/watchdog.log` line it already uses. The watchdog passes
  `--alerts` only: it is an observer, so a finding it cannot fix is an alert, not an action.
  `AATM_DOCTOR_INTERVAL` (seconds) changes the cadence; `watchdog.sh --no-doctor` drops the pass.

## Tests

`python3 -m pytest -q scripts/ubuntu/tests` — `test_aatm_doctor.py` (the four checks in one report,
the safe-fix policy, the backup-before-write rule, no duplicated checks), `test_autostart.py` (the
generated unit, enable, linger, the no-systemd message, the retry wrapper), `test_start_all_doctor.py`
(ordered after readiness, never `--fix`), `test_watchdog_doctor.py` (10-minute cadence, recovery
messages). They run against a temp SQLite file with their own backup and log paths: they never open
`data/aatm.db`, never read `.env`, and never contact a running service.
