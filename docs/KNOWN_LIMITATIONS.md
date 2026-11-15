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
   - `ExecutionDevice` registration, heartbeats, and node capability tracking are implemented on the central server; running remote worker daemons on separate physical hosts requires deploying the backend/worker agent to those hosts and pointing them to the central server URL.
6. **Live External Anthropic Spend**:
   - Live Anthropic Claude completions require `ANTHROPIC_API_KEY` set in the host environment; without it, `AnthropicClaudeAdapter` safely runs in zero-spend diagnostic/dry-run mode.
