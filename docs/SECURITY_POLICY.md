# AATM — Security Policy

> **Specification Reference:** [`AATM_MASTER_SPECIFICATION.md`](../AATM_MASTER_SPECIFICATION.md) (Sections 0.3, 0.4, 0.5, 16, 21, and 24) and [`docs/SECURITY_ASSESSMENT.md`](./SECURITY_ASSESSMENT.md).

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
