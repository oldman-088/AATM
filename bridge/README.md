# AATM Arena Local Bridge — Compliant Reference Daemon

The `bridge/` package provides a compliant, loopback-bound reference implementation of the **Arena Local Bridge** daemon consumed by `backend/app/providers/adapters.py::ArenaLocalBridgeAdapter` and `backend/app/providers/router.py::ProviderRouter`.

## Security & Compliance Guarantees
- **Loopback by default:** Binds to `127.0.0.1:4100` (`ARENA_BRIDGE_HOST`, `ARENA_BRIDGE_PORT`).
- **Environment-indirect Bearer auth:** Reads `ARENA_BRIDGE_TOKEN` exclusively from the host environment and verifies it with constant-time `hmac.compare_digest`. Set `ARENA_BRIDGE_REQUIRE_AUTH=true` in production to fail closed if no token is configured.
- **Per-employee session & account isolation:** Every AI employee's `ProviderSession.id` and `account_id` are forwarded by `ProviderRouter` + `ArenaLocalBridgeAdapter` via headers (`X-AATM-Session-Id`, `X-AATM-Account-Id`, `X-AATM-Agent-Id`, `X-AATM-Conversation-Id`) and JSON metadata. Attempts to read, write, or delete a `session_id` with a mismatched `account_id` are rejected with HTTP `403`.
- **Zero unauthorized automation:** NEVER implements CAPTCHA bypass, anti-bot evasion, browser session scraping, or undocumented consumer API calls. Optional upstream delegation (`ARENA_BRIDGE_UPSTREAM_URL`) only forwards to an explicitly configured official or local OpenAI-compatible HTTP(S) endpoint.

---

## HTTP API Contract

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness & diagnostic telemetry (`status`, `version`, `mode`, `auth_required`, `streaming_supported`, `models`, `active_sessions`, `uptime_seconds`). |
| `POST` | `/v1/chat/completions` | OpenAI-compatible completion endpoint returning `choices`, `usage`, and isolated `session` summary; supports optional SSE streaming (`"stream": true`, `text/event-stream`) with strict `account_id` isolation. |
| `GET` | `/v1/sessions` | Lists active bridge sessions with health status & aggregate count/token metrics (strictly scoped to `X-AATM-Account-Id` when supplied). |
| `GET` | `/v1/sessions/{session_id}` | Inspects a specific employee session summary + bounded conversation `history`. |
| `DELETE` | `/v1/sessions/{session_id}` | Clears/resets an isolated employee session context (used by `ArenaLocalBridgeAdapter.delete_session()` during employee replacement handoffs). |

---

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `ARENA_BRIDGE_HOST` | `127.0.0.1` | Bind address (keep on loopback unless behind an authenticated reverse proxy). |
| `ARENA_BRIDGE_PORT` | `4100` | Listening TCP port. |
| `ARENA_BRIDGE_TOKEN` | _(unset)_ | Shared secret matched against `Authorization: Bearer <token>` (referenced by AATM as `env:ARENA_BRIDGE_TOKEN`). |
| `ARENA_BRIDGE_REQUIRE_AUTH` | `false` | Set `true` in production to refuse requests when `ARENA_BRIDGE_TOKEN` is unset. |
| `ARENA_BRIDGE_MAX_HISTORY` | `40` | Maximum messages retained per isolated employee session. |
| `AATM_BRIDGE_STATE_FILE` | _(unset)_ | Optional JSON file path for atomic (`0600`) session-store persistence across daemon restarts (never stores secrets/tokens). |
| `AATM_BRIDGE_SESSION_TTL_SECONDS` | _(unset)_ | Optional idle-session TTL in seconds; sessions inactive longer than this threshold are evicted automatically. |
| `ARENA_BRIDGE_UPSTREAM_URL` | _(unset)_ | Optional official/local OpenAI-compatible base URL (e.g. local Ollama/vLLM/LiteLLM). |
| `ARENA_BRIDGE_UPSTREAM_API_KEY` | _(unset)_ | Optional Bearer key for `ARENA_BRIDGE_UPSTREAM_URL` (env-only, never persisted). |

---

## Running Locally

### Ubuntu / Linux
```bash
export ARENA_BRIDGE_TOKEN="$(openssl rand -hex 24)"
export ARENA_BRIDGE_REQUIRE_AUTH=true
.venv/bin/python -m bridge.server
# or via uvicorn directly:
.venv/bin/uvicorn bridge.server:app --host 127.0.0.1 --port 4100
```

### Windows 11 (PowerShell)
```powershell
$env:ARENA_BRIDGE_TOKEN = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:ARENA_BRIDGE_REQUIRE_AUTH = "true"
.\.venv\Scripts\python.exe -m bridge.server
```

### Verification Commands
```bash
# 1. Bridge health check
curl -fsS -H "Authorization: Bearer $ARENA_BRIDGE_TOKEN" http://127.0.0.1:4100/health

# 2. Sample per-employee completion
curl -fsS -X POST http://127.0.0.1:4100/v1/chat/completions \
  -H "Authorization: Bearer $ARENA_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-AATM-Session-Id: sess-worker-3" \
  -H "X-AATM-Account-Id: acct-arena-primary" \
  -d '{"model":"arena-bridge-default","messages":[{"role":"user","content":"Verify session isolation"}]}'

# 3. Sample SSE streaming completion (`stream: true`)
curl -fsSN -X POST http://127.0.0.1:4100/v1/chat/completions \
  -H "Authorization: Bearer $ARENA_BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-AATM-Session-Id: sess-worker-3" \
  -H "X-AATM-Account-Id: acct-arena-primary" \
  -d '{"model":"arena-bridge-default","stream":true,"messages":[{"role":"user","content":"Verify SSE streaming"}]}'

# 4. Inspect session metrics & isolated session context
curl -fsS -H "Authorization: Bearer $ARENA_BRIDGE_TOKEN" \
  -H "X-AATM-Account-Id: acct-arena-primary" \
  http://127.0.0.1:4100/v1/sessions

curl -fsS -H "Authorization: Bearer $ARENA_BRIDGE_TOKEN" \
  -H "X-AATM-Account-Id: acct-arena-primary" \
  http://127.0.0.1:4100/v1/sessions/sess-worker-3

# 5. Teardown session (employee replacement handoff)
curl -fsS -X DELETE -H "Authorization: Bearer $ARENA_BRIDGE_TOKEN" \
  -H "X-AATM-Account-Id: acct-arena-primary" \
  http://127.0.0.1:4100/v1/sessions/sess-worker-3
```

---

## Running Bridge Tests
```bash
.venv/bin/pytest -v bridge/tests/test_bridge.py
```
