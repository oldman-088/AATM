# AATM — Security Assessment

**Assessment Date (UTC):** 2026-11-15  
**Last Re-verified (UTC):** 2026-11-15T08:15:00Z  
**Scope:** Authentication, Authorization, Workspace Isolation, Browser Policy, AI Credentials, Prompt-Injection Defense, Multi-Device Authentication, and Governance.

---

## 1. Security Controls Implemented & Verified
| Control Area | Implementation | Verification |
|---|---|---|
| **Password & Token Security** | PBKDF2-SHA256 (`120,000` iterations, per-user random salt) + HS256 JWT (`backend/app/core/security.py`). | `pytest`: unauthenticated mutations return `401`. |
| **Credential Protection** | Only environment variable references (`env:ANTHROPIC_API_KEY`, `env:ARENA_SESSION_TOKEN`) stored in DB; API responses return masked statuses (`mask_credential_ref`). | Static secret scanner + API response inspection. |
| **Workspace Isolation** | `app/runtime/sandbox.py` enforces strict `Path.resolve()` containment inside `data/workspaces/<agent_id>/` and an allowlisted tool binary set; `GitWorkflowManager` additionally enforces isolated per-agent git worktrees under `data/worktrees/<agent_id>/` and `qa_engine.run_self_healing(db)` automatically prunes orphaned worktrees for `offline`/`replaced` or nonexistent agents (`pruned_worktrees`). The same sandbox module is used by the server runtime *and* the remote worker daemon, so the two cannot drift. | `pytest`: `../../etc/passwd` traversal returns `403`; absolute paths, deep traversal and symlink escapes also return `403` (`test_e2e_workspace_isolation`, `test_e2e_agent_git_worktree_lifecycle_isolation_and_hygiene`, `test_e2e_self_healing_prunes_orphaned_worktrees_and_preserves_active`). |
| **Prompt-Injection Defense** | `detect_prompt_injection()` & `sanitize_untrusted_content()` isolate untrusted external content inside `[UNTRUSTED_DATA]` fences before passing to reasoning agents. | `pytest`: injection pattern flagged & quarantined. |
| **Tampermonkey Userscript Policy** | `validate_userscript_policy()` blocks CAPTCHA/anti-bot evasion and cookie/token harvesting patterns at registration time. | `pytest`: prohibited userscript returns `400`. |
| **Protected Branch & High-Risk Governance** | `GitWorkflowManager` blocks merges to `main`, `master`, `production` until an approved `ApprovalRequest` exists in the Approval Center, and `POST /api/v1/github/branches`, `/prs`, and `/issues` enforce exact `action_type` and payload matching. The Manager Chat gate additionally routes protected-branch merges, production deploys, spend increases, data deletion and credential changes to the Approval Center using tolerant regex rules. | `pytest`: protected merge blocked until approved; `test_e2e_high_risk_directives_are_gated` (9 bypass variants previously slipped through); `test_e2e_github_branches_endpoint_enforces_branch_approval_and_payload_match`. |
| **Emergency Stop** | `ReplacementAndEmergencyService.emergency_stop()` supports `global`, `employee`, `task`, and `browser` immediate halts with critical audit log entry. Halted work is explicitly marked and requeued when the stop is cleared or by the self-healing sweep, so an emergency stop never silently strands in-flight tasks. | `pytest`: global emergency stop & clear verified; `test_e2e_emergency_stop_global_scope_and_recovery`, `test_e2e_emergency_stop_employee_scope_halts_own_task`. |
| **Static Code Scanning** | `qa_engine.run_security_scan()` scans 24 file types (`.py .ts .tsx .js .sh .ps1 .env .yaml .json .toml .sql ...`) with 22 rules, checks the **git index** for committed `.env` files, private keys, browser profiles, local databases and credential dumps, and never echoes a matched secret value. Documented development placeholders are reported separately as *suppressed* rather than silently ignored. | `pytest`: `security_scan.status == "passed"`; `test_e2e_secret_scan_detects_secrets_across_file_types`; `test_e2e_secret_scan_flags_forbidden_committed_artifacts`. |
| **Multi-Device Authentication** | Remote worker daemons authenticate with a version-bound device token derived as `HMAC-SHA256(SECRET_KEY, f"{device_id}:v{token_version}")`. `POST /devices/{id}/revoke` increments `token_version` so individual device tokens can be revoked immediately without rotating `SECRET_KEY`, and `WorkerDaemon` halts cleanly on `HTTP 401`. | `pytest`: anonymous, forged, and revoked device tokens are rejected with `401`; `test_e2e_multi_device_worker_daemon_round_trip`, `test_e2e_device_token_revocation_halts_worker_daemon_on_401`. |
| **Multi-Device Work Reclamation** | The QA self-healing sweep marks execution nodes that stopped reporting as offline and requeues the tasks they owned, so a dead host cannot strand work. | `test_e2e_offline_device_tasks_are_reclaimed`. |
| **Immutable Audit Trail** | Every dispatch, completion, retry, replacement, emergency stop, approval decision, device registration, token revocation and device work report is written to `AuditLog`. | `pytest`: audit entries observed after each flow. |

---

## 2. Risk Register & Accepted Limitations

| Risk | Severity | Mitigation / Status |
|---|---|---|
| Per-device token revocation | Resolved | `ExecutionDevice.token_version` + `POST /devices/{id}/revoke` (`require_role({"admin", "operator"})`) are implemented and verified end to end; `WorkerDaemon` halts cleanly on `HTTP 401`. |
| The default development `SECRET_KEY` makes device tokens forgeable | High (in production) | The device register endpoint returns `token_hardened: false` plus an explicit warning while the default secret is in use. Operators **must** set a strong `SECRET_KEY` in production (`docs/DEPLOYMENT.md`). |
| Workspace tool execution is not an OS-level jail in `process` mode | Low | Execution is limited to an allowlist of developer tools and a fixed working directory, and file access is path-contained. `AATM_SANDBOX_MODE=docker` and the hardened `worker` service in `docker-compose.yml` (`read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, `pids_limit: 64`, `mem_limit: 512m`) provide container-level isolation for production deployments. |
| The Manager Chat governance gate is pattern based | Low | Deliberately biased towards gating (over-gating is safer than under-gating). The structured `ApprovalRequest` API remains the authoritative path for high-risk operations. |
| Windows 11 `.ps1` scripts could not be executed natively in the verification sandbox | Low | Verified by inspection and matched against the Ubuntu scripts, which were executed end to end. |
| Static secret scanning is regex based | Low | 22 rules covering the common credential formats, plus a git-index check for forbidden artifact types. High-confidence token formats are never suppressed; only generic literal assignments can be suppressed, and only for documented placeholders. |

---

## 3. Secret Handling Rules Enforced in the Repository

- `.env` is git-ignored; only `.env.example` (a template) is tracked.
- Private key files (`*.pem`, `*.key`, `*.p12`, `*.pfx`), browser profile directories, local
  databases (`*.db`, `*.sqlite*`) and `*.pid` files are git-ignored **and** reported by
  the secret scanner if they ever appear in the git index.
- Backups are written to `backups/` (git-ignored) with `0600` permissions, and the
  backup script warns that the archive may contain credentials from `.env`.
- Scanner findings record only the file, line and rule id — never the secret value — so
  scan output is safe to persist in the audit trail.
