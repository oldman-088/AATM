# AATM — Security

> **Specification Reference:** [`AATM_MASTER_SPECIFICATION.md`](../AATM_MASTER_SPECIFICATION.md) (Sections 0.3, 0.4, 0.5, 16, 21, and 24) and [`docs/SECURITY.md (Part II)`](./SECURITY.md (Part II)).

This document defines the mandatory security architecture, permission boundaries, secret-management rules, prompt-injection threat model, governance approval gates, and emergency-stop procedures for the **AATM (AI Agent Team Manager)** platform.

---

## 1. Access Control, Permissions & Workspace Isolation

### 1.1 Human & Agent Ownership Model
- AATM operates with six human leads paired with six primary persistent agents (`manager-0` coordinator plus specialized domain engineers, with `worker-5` serving as standby backup for `worker-4`) and supports 20+ dynamically created AI employees.
- Every AI employee is provisioned with an explicit role template, division assignments (`D01`–`D12`), autonomy mode (`supervised`, `semi_autonomous`, `highly_autonomous`), and a least-privilege permission list (`backend/app/employees/catalog.py`).
- All mutating HTTP API endpoints require bearer JWT authentication (`PBKDF2-SHA256` with 120,000 iterations for password hashing; `HS256` signed access tokens in `backend/app/core/security.py`) or a verified device-scoped HMAC token for remote worker nodes.

### 1.2 Isolated Employee Workspaces & Tool Allowlist
- Each AI employee executes file and command operations strictly inside its isolated workspace directory (`data/workspaces/<agent_id>/` on the server or the configured per-device workspace root on remote nodes).
- `resolve_contained_path()` (`backend/app/runtime/sandbox.py`) canonicalizes paths via `Path.resolve()` and rejects relative path traversal (`../`), absolute paths outside the workspace, and symlink escapes with `HTTP 403`.
- Command execution is restricted by `assert_allowed_binary()` to an explicit allowlist of non-interactive developer tools (`python`, `python3`, `pytest`, `ruff`, `git`, `ls`, `cat`, `echo`, `npm`, `node`, `tsc`, `vitest`, `alembic`) without a shell interpreter (`shell=False`).

---

## 2. Secret & Credential Protection Rules

### 2.1 Environment-Variable Indirection (`env:VAR_NAME`)
- Raw API keys, OAuth tokens, session cookies, private keys, and passwords **must never** be stored in source code, database rows, prompts, GitHub issues, pull requests, or audit logs.
- Provider and account records accept only environment-variable references matching `^env:[A-Z_][A-Z0-9_]*$` (`validate_credential_ref()` in `backend/app/schemas/common.py`). Inline credentials are rejected with `HTTP 400` without echoing the submitted secret value.
- **Credential exfiltration guard** (`backend/app/providers/credential_policy.py`): a `credential_ref` may only name a provider API-key variable (`^[A-Z][A-Z0-9_]*_API_KEY$`) or an entry of `PROVIDER_CREDENTIAL_ENV_ALLOWLIST`. Server secrets (everything in `SENSITIVE_ENV_EXACT` from `runtime/sandbox.py` that is not a provider key, GitHub tokens, webhook secrets, `ARENA_SESSION_TOKEN`, and any `AATM_*` variable) are always rejected, even if allow-listed. `ARENA_BRIDGE_TOKEN` is bound to the `arena_local_bridge` provider and may only be sent to a loopback host or the configured `ARENA_BRIDGE_URL`.
- `endpoint_url` must be `http`/`https` with a host, no embedded credentials and no fragment. Operators may only use the official preset endpoint + credential pairs; custom endpoints (or custom pairings) require the `admin` role (`403 CUSTOM_ENDPOINT_REQUIRES_ADMIN`).
- Every wizard test-connection (allowed or denied), provider upsert/test and account creation writes an audit entry that names the `env:` reference and endpoint origin, never the key value. Adapters re-apply the server-secret deny-list at runtime, so legacy rows that bypassed the API still cannot resolve server secrets.
- API responses mask credential state through `mask_credential_ref()` (`backend/app/core/security.py`).

### 2.2 CLI & Subprocess Output Sanitization
- All Git and GitHub CLI (`gh`) invocations pass stdout, stderr, and exception messages through `sanitize_cli_output()` (`backend/app/github_client/client.py`).
- Embedded URL userinfo, GitHub PAT/OAuth/App tokens, provider API keys, JWTs, Bearer headers, key-value token assignments, and sensitive environment variable values (`GH_TOKEN`, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ARENA_SESSION_TOKEN`, `ARENA_BRIDGE_TOKEN`) are redacted to `[REDACTED]` before returning or logging.

### 2.3 Static Secret Scanner & Forbidden Artifact Gate
- `qa_engine.run_security_scan()` (`python -m app.qa.engine`) scans 25 file extensions across the repository (including `.py`, `.ts`, `.tsx`, `.js`, `.sh`, `.ps1`, `.env`, `.yaml`, `.yml`, `.json`, `.toml`, `.sql`, `.md`, and `.txt`) against 21 high-confidence and generic credential detection rules.
- The scanner also inspects the Git index (`git ls-files`) against `FORBIDDEN_ARTIFACT_RULES` to block accidental commits of `.env` files, private keys (`*.pem`, `*.key`, `*.p12`, `*.pfx`), browser profile directories (`browser_profiles/`), local SQLite databases (`*.db`, `*.sqlite*`), and credential dumps.
- Scanner findings report only `{file, line, rule, severity}` and never echo matched secret values.

---

## 3. Prompt-Injection Threat Model & Browser Automation Policy

### 3.1 Untrusted External Input Quarantine
- All external content—including web pages, DOM snapshots, GitHub issues, pull requests, external repository files, and third-party messages—is treated as **untrusted data**.
- Before external text is inspected by an agent, `sanitize_untrusted_content()` (`backend/app/core/security.py`):
  1. Scans for instruction-override and exfiltration patterns via `detect_prompt_injection()` (e.g., `ignore all previous instructions`, `system: you are now`, `bypass security/auth/captcha/guardrails`, `exfiltrate secrets/tokens/keys/passwords`).
  2. Neutralizes embedded system tags (`<system>`, ```` ```system ````).
  3. Wraps the payload inside explicit `[UNTRUSTED_DATA source=...]` / `[/UNTRUSTED_DATA]` fences so downstream models treat it strictly as data rather than executable instructions.

### 3.2 Browser Profile Locking & Tampermonkey Userscript Policy
- Browser profiles (`backend/app/browser/manager.py`) enforce exclusive per-agent ownership locks (`lock_owner`) so concurrent workers cannot hijack or collide on an active session (`HTTP 409` on lock conflict).
- Navigation URLs are validated to allow only `http://`, `https://`, and `about:blank` schemes, rejecting `javascript:`, `file:`, and `data:` URIs.
- Tampermonkey userscripts (`validate_userscript_policy()` in `backend/app/browser/engines.py`) are audited at registration time and rejected (`HTTP 400`) if they contain prohibited patterns (`captcha`, `recaptcha`, `hcaptcha`, `cloudflare bypass`, `turnstile bypass`, `document.cookie`, `localStorage.getItem('token')`, `eval(`) or wildcard `@match *://*/*` rules without explicit role scoping.

---

## 4. Governance & Approval Center Gates

### 4.1 High-Risk Operations Requiring Human Approval
Per Section 0.4 of `AATM_MASTER_SPECIFICATION.md`, the following operations require an explicit `ApprovalRequest` with `status == "approved"` in the Approval Center (`/api/v1/approvals`):
- Merging or writing directly to protected branches (`main`, `master`, `production`, `prod`).
- Production deployments or production infrastructure mutations.
- Destructive database migrations or bulk data deletion.
- Deleting repositories, protected branches, or releases.
- Rotating credentials, modifying security policies, or changing access controls.
- Increasing daily/monthly AI provider budgets (`DAILY_BUDGET_USD`, `MONTHLY_BUDGET_USD`) or paid resource limits.

### 4.2 Approval-Gated GitHub Write Helpers & Manager Chat Gate
- `GitHubClient` (`backend/app/github_client/client.py`) and `GitWorkflowManager` (`backend/app/git_workflows/manager.py`) enforce:
  1. Strict `owner/name` repository slug validation and allowlist checking (`validate_repo()`, `DEFAULT_ALLOWED_REPOS`, `AATM_ALLOWED_GITHUB_REPOS`).
  2. Ref-name validation (`validate_branch_name()`) blocking option injection (`-f`) and path traversal (`..`).
  3. Protected-branch refusal on `main`, `master`, `production`, and `prod` unless an approved `ApprovalRequest` is supplied and verified by `verify_approval()`.
- The Manager Chat directive router (`HIGH_RISK_PATTERNS` in `backend/app/orchestration/scheduler.py`) automatically intercepts high-risk natural-language directives and creates a pending `ApprovalRequest` instead of executing them autonomously.

---

## 5. Multi-Device Authentication & Token Revocation

- Remote execution hosts register via `POST /api/v1/devices` and authenticate all subsequent `/register`, `/heartbeat`, `/claim`, `/report`, and `/status` requests using the `X-AATM-Device-Token` header.
- Device tokens are derived via `HMAC-SHA256(SECRET_KEY, f"aatm-device-token:v1:{device_id}:tv{token_version}")` (`backend/app/runtime/device_auth.py`) and verified in constant time via `hmac.compare_digest()`.
- Operators can immediately revoke a compromised or decommissioned device token via `POST /api/v1/devices/{device_id}/revoke`, which increments `ExecutionDevice.token_version` in the database and invalidates all previously issued tokens for that device.
- When `WorkerDaemon` (`backend/app/runtime/worker_daemon.py`) receives `HTTP 401` on `register`, `heartbeat`, `claim`, or `report`, it logs an explicit `[aatm-worker] TOKEN REVOKED (HTTP 401)` notice, sets `token_revoked = True`, and halts its polling loop cleanly rather than spinning on rejected requests.

---

## 6. Emergency Stop & Self-Healing Recovery Procedures

### 6.1 Scoped & Global Emergency Stop
- Operators can trigger an immediate Emergency Stop via `POST /api/v1/emergency-stop` (`ReplacementAndEmergencyService.emergency_stop()` in `backend/app/orchestration/replacement.py`) across four scopes:
  - `global`: Pauses all agents (`is_emergency_stopped = True`, `status = "paused"`), blocks all in-progress/assigned tasks while releasing task locks, and stops all running browser profiles.
  - `employee`: Pauses the target employee and blocks/unlocks its active tasks so work is never stranded on a halted agent.
  - `task`: Cancels the target task immediately and releases its lock.
  - `browser`: Stops the target browser profile and closes its active session.
- Every Emergency Stop emits a `critical` `AuditLog` entry (`security.emergency_stop`) and system notification.

### 6.2 Safe Clearance & Replacement Session Cleanup
- Clearing an Emergency Stop via `POST /api/v1/emergency-stop/clear` (or running `POST /api/v1/qa/self-heal`) resumes halted agents and requeues tasks marked with `EMERGENCY_STOP_MARKER` so work resumes deterministically.
- When an unavailable employee is replaced via `POST /api/v1/employees/{agent_id}/replace`, `ReplacementAndEmergencyService._cleanup_bridge_session()` marks the outgoing employee's `ProviderSession` as `"closed"` and performs a best-effort `DELETE /v1/sessions/{old_session_id}` against `ArenaLocalBridgeAdapter` (never failing the handoff if the local bridge is offline).

---

## 7. Continuous Security Verification

Before opening or merging any Pull Request, run the unified verification suite:

```bash
# Ubuntu / Linux / CI
bash scripts/ubuntu/verify.sh
```

```powershell
# Windows 11 PowerShell
.\scripts\windows\verify.ps1
```

## 8. Arena Browser Connector Safety (Phase 6)
- Real Arena use is **off by default**: it needs `ARENA_CONNECTOR_ENABLED=true`, `ARENA_AUTOMATION_APPROVED=true` and a non-empty `ARENA_APPROVAL_REFERENCE`. Otherwise every real-Arena path returns `not_enabled`. The bridge checks the same three flags in its own environment before accepting a non-loopback origin. `ARENA_MOCK_URL` accepts loopback URLs only.
- The human signs in to Arena manually in each employee's own profile. There is no account pooling or rotation, and AATM never reads cookies, storage or tokens.
- Demo mode (`DEMO_MODE`) is a read-only scripted feed: no writes, no model calls, refused in production,
  every frame flagged `demo: true`, badged in the UI and never mixed with live agent output.
- The live code stream (`agent.code` / `GET /api/v1/agents/{id}/code-stream`) only carries artefacts an agent
  actually produced: file content and diffs, the logical pytest/ruff command with its output, and review
  comment bodies. Every frame is redacted (`redact_secrets`) and re-checked before it is buffered, frames
  that still look like they carry a credential are dropped, sensitive paths (`.env`, keys, tokens, `.git/`)
  never enter the stream, model prompts/private reasoning are never streamed, and frames are capped at
  400 lines / 32 KB with a 120-frame per-agent ring buffer.
- The userscript is generated per profile with `@grant none`, `@noframes`, and `@match` limited to the mock URL plus approved Arena domains. It is reviewed and installed by a human in Tampermonkey. AATM never injects scripts (`run_userscript` stays HTTP 501). The per-profile connector token is shown once; only its SHA-256 is stored, and the stored copy of the script is redacted.
- One in-flight request per profile, a minimum delay between requests and a daily cap, which survives token rotation.
- Any human-verification element, verification page, rate-limit message or terms prompt stops that profile with `needs_human`. The task is blocked, a critical notification is raised, and there is no failover to paid providers. AATM never solves or bypasses such checks. A human resolves it in the browser and clicks **Resume**.

# Part II — Security Assessment & Risk Register

### 1. Security Controls Implemented & Verified
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

### 2. Risk Register & Accepted Limitations

| Risk | Severity | Mitigation / Status |
|---|---|---|
| Per-device token revocation | Resolved | `ExecutionDevice.token_version` + `POST /devices/{id}/revoke` (`require_role({"admin", "operator"})`) are implemented and verified end to end; `WorkerDaemon` halts cleanly on `HTTP 401`. |
| The default development `SECRET_KEY` makes device tokens forgeable | High (in production) | The device register endpoint returns `token_hardened: false` plus an explicit warning while the default secret is in use. Operators **must** set a strong `SECRET_KEY` in production (`docs/OPERATIONS.md`). |
| Workspace tool execution is not an OS-level jail in `process` mode | Low | Execution is limited to an allowlist of developer tools and a fixed working directory, and file access is path-contained. `AATM_SANDBOX_MODE=docker` and the hardened `worker` service in `docker-compose.yml` (`read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, `pids_limit: 64`, `mem_limit: 512m`) provide container-level isolation for production deployments. |
| The Manager Chat governance gate is pattern based | Low | Deliberately biased towards gating (over-gating is safer than under-gating). The structured `ApprovalRequest` API remains the authoritative path for high-risk operations. |
| Windows 11 `.ps1` scripts could not be executed natively in the verification sandbox | Low | Verified by inspection and matched against the Ubuntu scripts, which were executed end to end. |
| Static secret scanning is regex based | Low | 22 rules covering the common credential formats, plus a git-index check for forbidden artifact types. High-confidence token formats are never suppressed; only generic literal assignments can be suppressed, and only for documented placeholders. |

---

### 3. Secret Handling Rules Enforced in the Repository

- `.env` is git-ignored; only `.env.example` (a template) is tracked.
- Private key files (`*.pem`, `*.key`, `*.p12`, `*.pfx`), browser profile directories, local
  databases (`*.db`, `*.sqlite*`) and `*.pid` files are git-ignored **and** reported by
  the secret scanner if they ever appear in the git index.
- Backups are written to `backups/` (git-ignored) with `0600` permissions, and the
  backup script warns that the archive may contain credentials from `.env`.
- Scanner findings record only the file, line and rule id — never the secret value — so
  scan output is safe to persist in the audit trail.
