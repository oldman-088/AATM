# AATM — Known Limitations Report

1. **Arena Web Session Automation / Undocumented APIs**:
   - Arena does not expose a public official REST API for headless consumer chat sessions. AATM implements `ArenaLocalBridgeAdapter` (for an explicitly running local bridge daemon) and `ArenaAgentAdapter` plus `ProviderAccount` / `ProviderSession` tracking without relying on undocumented APIs or unauthorized scraping.
2. **Chromium Binary in Sandbox**:
   - Chromium is not pre-installed in this sandbox container and external Playwright browser CDN hosts are outside the sandbox outbound allowlist. `BrowserManager` reports `chromium_installed: false` and operates in `headless_fallback` mode locally. On Ubuntu or Windows 11 target hosts, `install.sh` / `install.ps1` runs `playwright install chromium`.
3. **Browser Use / OpenBrowser / Automa Optional External Packages**:
   - Modular engine adapters are implemented in `backend/app/browser/engines.py` and delegate cleanly to Playwright. Installing `browser-use` or running external Automa extension workflows requires those optional runtime components on the target host.
4. **Windows 11 PowerShell Verification**:
   - `scripts/windows/*.ps1` were authored for Windows 11 PowerShell and inspected for correctness, but could not be executed natively inside this Linux sandbox container.
5. **Multi-Device Remote Worker Execution**:
   - The central server keeps the authoritative `ExecutionDevice` registry, and a working remote worker daemon (`backend/app/runtime/worker_daemon.py`) is implemented and verified end to end: it registers, heartbeats, claims tasks, executes them in an isolated local workspace and reports results. Per-device token revocation (`ExecutionDevice.token_version` + `POST /api/v1/devices/{id}/revoke`) is implemented and verified, and the daemon halts cleanly on `HTTP 401`. Isolated per-agent git worktrees (`data/worktrees/<agent_id>/`) are created/removed via `GitWorkflowManager` and orphaned worktrees for `offline`/`replaced` or nonexistent agents are automatically pruned by `qa_engine.run_self_healing(db)`. Launchers exist for Ubuntu (`scripts/ubuntu/start-worker.sh`), Windows 11 (`scripts/windows/start-worker.ps1`), and a hardened container definition in `docker-compose.yml` (`worker` service with `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges:true`, `pids_limit: 64`, and `AATM_SANDBOX_MODE`).
6. **Live External Anthropic Spend**:
   - Live Anthropic Claude completions require `ANTHROPIC_API_KEY` set in the host environment; without it, `AnthropicClaudeAdapter` safely runs in zero-spend diagnostic/dry-run mode.
7. **Secret Scanner Rule Coverage**:
   - `qa_engine.run_security_scan()` evaluates 22 rules across 24 file types and additionally inspects the git index for committed `.env` files, private keys, browser profiles, local databases and credential dumps. It is regex based: an unrecognised credential format, or a secret split across an unusual encoding, will not be detected. Findings record the file, line and rule id only, never the matched value.
8. **Manager Chat Governance Gate**:
   - High-risk directives are detected with tolerant regex rules and routed to the Approval Center. The gate is deliberately biased towards gating, but it is pattern based; the structured `ApprovalRequest` API remains the authoritative path for high-risk operations.
