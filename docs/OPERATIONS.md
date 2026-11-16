# AATM — Operations

Install, run, configure providers, use the dashboard, verify, and operate remote nodes.

## 1. Prerequisites
- Python 3.11+
- Node.js 20+ & npm
- Git & GitHub CLI (`gh`)
- Optional (Production DB): Docker + Docker Compose for PostgreSQL 16

## 2. Database, Queue & Coordination (Phase 5: PostgreSQL + RabbitMQ + Redis by default)
### Default: `scripts/ubuntu/install.sh`
When Docker is available the installer generates `POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD`
and `REDIS_PASSWORD` into `.env` (mode 600), runs `docker compose up -d --wait postgres rabbitmq redis`
(all ports published on `127.0.0.1` only) and writes `DATABASE_URL`, `SYNC_DATABASE_URL`,
`RABBITMQ_URL` and `REDIS_URL` into `.env` before running migrations.
`scripts/ubuntu/install.sh --sqlite` keeps the single-file SQLite database.

### Full stack in Compose
```bash
# .env must define POSTGRES_PASSWORD, RABBITMQ_PASSWORD, REDIS_PASSWORD and SECRET_KEY (32+ chars)
docker compose up -d                        # postgres, rabbitmq, redis, backend, bridge, worker
docker compose up -d --scale worker=3       # more remote worker daemons
```
The RabbitMQ management UI is at `http://127.0.0.1:15672` (user `aatm`, password from `.env`).
The `worker`, `backend` and `bridge` containers are non-root, `read_only`, `cap_drop: [ALL]`,
`no-new-privileges:true`; the worker also has `pids_limit: 64`, `mem_limit: 512m`, user `65534:65534`.

### Fallbacks (all optional services)
| Missing | Behaviour |
|---|---|
| `RABBITMQ_URL` | Postgres `LISTEN/NOTIFY` wake bus when on Postgres, otherwise in-process. Workers always also poll the database, and the atomic DB claim is the source of truth. |
| `REDIS_URL` | In-process locks, login rate limits and event fan-out — correct for a single backend process. |
| PostgreSQL | SQLite (WAL). The dashboard warns when more than 3 employees run on SQLite. |

### Parallelism & autoscaling settings
`DEFAULT_ACTIVE_WORKERS=4`, `AGENT_MAX_PARALLEL=4`, `ARENA_BRIDGE_MAX_CONCURRENCY=1` (per profile),
`AUTO_SCALE_MODE=approval|automatic|off`, `SCALE_UP_BACKLOG_THRESHOLD=3`, `SCALE_UP_WAIT_SECONDS=120`,
`MAX_CONCURRENT_WORKERS=25`, `SCALE_DOWN_IDLE_MINUTES=30`, `PROVIDER_CONCURRENCY_LIMITS=id=N,...`.
Load test: `.venv/bin/python scripts/load_test.py --tasks 20` (scripted model, no provider spend).

### Arena browser connector (Phase 6)
Real Arena stays **off** until the owner records Arena's approval. Until then every real-Arena path returns `not_enabled`.
1. **Test any time with the mock page:** set `ARENA_MOCK_URL=http://127.0.0.1:4100/mock-arena/` and `ARENA_BRIDGE_TOKEN` in `.env`, then run `scripts/ubuntu/start.sh` (it also starts the loopback bridge). In **Browsers → Arena browser connector**, generate an employee's userscript, review it, install it in Tampermonkey in that employee's own Chrome profile, and open the mock URL.
2. **Real Arena (owner's decision, after approval):** set `ARENA_CONNECTOR_ENABLED=true`, `ARENA_AUTOMATION_APPROVED=true` and `ARENA_APPROVAL_REFERENCE=<your approval reference>` for **both** the backend and the bridge. Set `ARENA_DOMAINS` and, if needed, `ARENA_DOM_SELECTORS` (JSON with `input`, `send`, `assistant_message`, `user_message`, `busy`) to match the real chat page. Regenerate the userscripts (the new `@match` includes the approved domain), use **Open for sign-in** and sign in to Arena yourself in each profile.
3. If a profile shows **Needs human**, resolve it yourself in that browser window, then click **Resume**. AATM never solves verification checks, never reads cookies or tokens and never rotates accounts.
4. Limits: `ARENA_MIN_REQUEST_INTERVAL_SECONDS` (default 20), `ARENA_DAILY_CAP_PER_PROFILE` (default 100), `ARENA_REPLY_TIMEOUT_SECONDS` (default 600).

## 3. Ubuntu Installation & Run
```bash
chmod +x scripts/ubuntu/*.sh
./scripts/ubuntu/install.sh     # installs deps, migrates, and verifies
./scripts/ubuntu/start.sh       # starts the API + UI (health-checked)
```

## 4. Windows 11 Installation & Run (PowerShell)
```powershell
.\scripts\windows\install.ps1
.\scripts\windows\start.ps1
```

## 5. Production Single-Server Serving
After running `npm run build` in `frontend/`, FastAPI automatically serves `frontend/dist` alongside `/api/v1/*` and `/ws/events` on port `8000`:
```bash
.venv/bin/uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

## 6. Verification (one command, all release gates)
```bash
./scripts/ubuntu/verify.sh              # everything
./scripts/ubuntu/verify.sh --skip-frontend
./scripts/ubuntu/verify.sh --install     # install deps first
```
```powershell
.\scripts\windows\verify.ps1
```
The script runs prerequisite checks, `alembic upgrade head`, `ruff check`, `pytest`
(unit + E2E), the static secret scan, and the frontend typecheck / unit tests /
production build. It exits non-zero if any gate fails.

The secret scanner can also be used standalone as a CI or pre-commit gate:
```bash
cd backend && ../.venv/bin/python -m app.qa.engine --repo-root .. --json
```

## 7. Remote Execution Nodes (Multi-Device Worker Daemon)

### 7.1 Register the node on the central server
```bash
curl -X POST http://<server>:8000/api/v1/devices \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"id":"node-ubuntu-01","name":"Ubuntu Build Node","device_type":"remote_server","host_address":"10.0.0.21"}'
```
The response contains a `device_token`. It is shown once — store it securely.

### 7.2 Configure and launch the daemon on the remote host
```bash
export AATM_SERVER_URL=http://<server>:8000
export AATM_DEVICE_ID=node-ubuntu-01
export AATM_DEVICE_TOKEN=<device_token>
./scripts/ubuntu/start-worker.sh
```
```powershell
$env:AATM_SERVER_URL="http://<server>:8000"
$env:AATM_DEVICE_ID="node-ubuntu-01"
$env:AATM_DEVICE_TOKEN="<device_token>"
.\scripts\windows\start-worker.ps1
```
Optional variables: `AATM_WORKER_WORKSPACE`, `AATM_WORKER_POLL_SECONDS`,
`AATM_WORKER_MAX_CONCURRENT`.

The daemon registers itself, heartbeats, claims eligible tasks, executes them inside
an isolated local workspace (`AATM_WORKER_WORKSPACE/<device-id>/`) and reports the
result back. `scripts/*/stop.sh` stops the daemon along with the backend and frontend.

### 7.3 Security requirements for production
- Set a strong, unique `SECRET_KEY` in `.env` (auto-generated by `install.sh` / `install.ps1` along with `DEFAULT_ADMIN_PASSWORD`, `ARENA_BRIDGE_TOKEN`, and `POSTGRES_PASSWORD`). Device tokens are derived from `SECRET_KEY`; while the development default is in place the register endpoint reports `"token_hardened": false`.
- Use `POST /api/v1/auth/logout` (automatically triggered by UI Sign Out) to revoke active JWTs via `token_version` rotation, and inspect `GET /api/v1/governance/security-posture` for live sandbox, header, and warning telemetry.
- Run the daemon under a restricted user or inside a container: the workspace sandbox restricts paths and tools, but it is not an OS-level jail. In `ENVIRONMENT=production`, `AATM_SANDBOX_MODE=process` is rejected (`SANDBOX_UNAVAILABLE`) unless Docker + `aatm-sandbox:latest` is available.

## 7b. Demo mode (showing the bridge without a model)

Set `DEMO_MODE=true` in `.env` and restart (`scripts/ubuntu/restart.sh`). The bridge then renders a
scripted feed from `GET /api/v1/demo/code-stream` so the workstations, stage lights and console can be
demonstrated with no model key, repository or crew.

* every frame carries `demo: true`, `/api/health` reports `demo_mode: true`, and the HUD shows an amber
  "وضع العرض / Demo mode" badge — demo output can never be mistaken for a real run;
* it is read-only: no database writes, no model calls, no agent workspace access;
* it is refused when `ENVIRONMENT=production`, and it is off by default;
* while it is on, the bridge ignores the live `agent.code` stream, so real and demo frames never mix.

## 8. Operations Scripts

| Script | Purpose |
|---|---|
| `scripts/ubuntu/install.sh` | Dependencies, Playwright Chromium, migrations, `.env` bootstrap, verification. |
| `scripts/ubuntu/start.sh` | Starts the API + UI and waits for the health endpoint. A healthy backend is reused; a stale PID is replaced instead of printing "already running". |
| `scripts/ubuntu/restart.sh` | `stop.sh` + `start.sh` with the same health gate — restart after changing `.env` or `git pull`. |
| `scripts/ubuntu/reset-admin-password.sh` | Rotates the admin password (PBKDF2 hash, `must_change_password=true`, `token_version += 1`, audit entry) and prints it once. No password reset by hand-editing the database. |
| `scripts/ubuntu/stop.sh` | PID-validated shutdown of the API, UI and worker daemons. |
| `scripts/ubuntu/update.sh` | Update dependencies, migrate, re-verify. |
| `scripts/ubuntu/backup.sh` | Verified, permission-restricted archive of `data/` and `.env`. |
| `scripts/ubuntu/restore.sh` | Validated restore (rejects absolute/traversal member paths). |
| `scripts/ubuntu/verify.sh` | All release gates in one command. |
| `scripts/ubuntu/start-worker.sh` | Launches the remote worker daemon on this host. |

## 8a. End-to-end demo (`scripts/demo_e2e.sh`)
This runs the full workflow: Arabic directive → tasks → parallel workers in worktrees → draft PRs → AI review → human-approved merge → completed project. It uses a **throwaway** GitHub repository and a local Ollama model.
```bash
ollama serve &                                # in another terminal
ollama pull qwen2.5-coder:7b                  # or pass --model NAME
scripts/demo_e2e.sh --check                   # prerequisites only, no side effects
scripts/demo_e2e.sh --create-repo             # creates a PRIVATE aatm-demo-<timestamp> repository
scripts/demo_e2e.sh --repo you/aatm-demo-x    # or use an existing throwaway repository
```
- The demo runs an isolated backend: temporary SQLite database, temporary data directories, random admin password and `SECRET_KEY`, and `AATM_SANDBOX_MODE=process`. It never touches your normal AATM data.
- By default it waits for **you** to approve each merge in the Approval Center. `--auto-approve` makes the script approve on your behalf.
- It never deletes the repository. Delete it yourself with `gh repo delete OWNER/NAME`.

## 9. Using AATM (Dashboard)
- **Sign in** with the admin account written to `.env` by the installer (`DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD`). The first sign-in forces a password change.
- **Language:** the UI opens in Arabic (RTL) by default. Switch between العربية, فارسی and English in the header; your choice is remembered. Set `VITE_DEFAULT_LANGUAGE` at build time to change the first-visit default.
- **Dashboard (mission control):** the architecture strip, live counters, the 8-step workflow timeline of the active project, the live agent log, the errors & recovery panel, and the colored employee cards with Pause / Emergency stop / Replace.
- **Manager chat:** write a directive (Arabic or English). The manager splits it into tasks. High-risk directives go to the Approval Center.
- **Browsers:** the Arena connector panel (approval status, per-employee userscript generation and rotation, Resume after `needs_human`) and the Browser Wall.
- **Git:** pull requests with diffs and reviewer results, **Request merge approval**, and **Merge** after a human approves in **Approvals**.
- **Agents:** team scaling (approval-gated extra workers), employee creation and provider assignment.

## 10. Provider Configuration

This guide connects providers using environment configuration and the existing AATM provider API. No source-code changes are required. It describes the current adapters, not a claim that an external account or model was exercised in this checkout.

#### How AATM stores provider settings

AATM accepts these provider types: `arena_local_bridge`, `anthropic_claude`, and `openai_compatible` (plus the legacy `arena_agent` stub). OpenRouter and Ollama use the generic `openai_compatible` adapter; there are no separate OpenRouter or Ollama adapter types.

Provider credentials are **environment-variable references**, never inline values. For example, store `env:OPENROUTER_API_KEY` in the provider record and make `OPENROUTER_API_KEY` available to the backend process. The API rejects inline credential values. Keep `.env` out of Git, restrict its permissions (`chmod 600 .env`), and use a secret manager for deployments.

The seeded provider rows are `arena-local-bridge` and `anthropic-claude`. The `arena-agent` runtime is not implemented. Provider records can be created or updated through authenticated `POST /api/v1/providers`; the current Providers view has health-check and test controls but no create/edit form.

For local API examples, set `BASE` to the AATM backend URL and obtain an access token using an account configured in AATM. Do not use example/default credentials in a deployed system:

```bash
BASE=http://127.0.0.1:8000
TOKEN="$(curl -fsS "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"<AATM account email>","password":"<AATM account password>"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
```

Replace the two angle-bracket values before running the command. The token is kept in the shell variable and is not written into a provider record.

#### Arena Local Bridge

The bundled `bridge/` service is a loopback reference daemon. Its default address is **`127.0.0.1:4100`**; both `backend/app/core/config.py` and `bridge/server.py` use port `4100`. It authenticates with a Bearer token when a token is configured. It does not provide an interactive Arena account sign-in flow or a hosted model. If an OpenAI-compatible upstream is configured, completions are forwarded there; without one, the daemon returns a deterministic, zero-spend local reference response, not a real model completion.

1. Add these settings to the private `.env` used by both the backend and the bridge process:

   ```dotenv
   ARENA_BRIDGE_URL=http://127.0.0.1:4100
   ARENA_BRIDGE_HOST=127.0.0.1
   ARENA_BRIDGE_PORT=4100
   ARENA_BRIDGE_TOKEN=<generate a random token>
   ARENA_BRIDGE_REQUIRE_AUTH=true
   ```

   Generate a token locally (for example, `openssl rand -hex 24`) and put the result in `.env`; do not commit it. The same non-empty `ARENA_BRIDGE_TOKEN` must be visible to the bridge and AATM backend. `ARENA_BRIDGE_REQUIRE_AUTH=true` makes the bridge fail closed if the token is missing. The reference bridge binds to loopback by default; do not expose it directly to an untrusted network.

2. Configure an OpenAI-compatible upstream if you want actual model responses. The bridge posts to `<upstream-root>/v1/chat/completions`, so set the upstream root **without** a trailing `/v1`. Examples are `http://127.0.0.1:11434` for a local Ollama server or `https://openrouter.ai/api` for OpenRouter. If the upstream needs a key, set `ARENA_BRIDGE_UPSTREAM_API_KEY` in the same private environment. When no upstream is configured, `/health` still responds and completions use the local reference behavior described above; this does not test or provide model inference.

3. If starting the bridge from the repository, load the private environment and run the reference server:

   ```bash
   set -a
   . ./.env
   set +a
   .venv/bin/python -m bridge.server
   ```

   The AATM backend must also receive the same settings. Restart the backend after changing its environment. `scripts/ubuntu/start.sh` starts the backend and frontend; it does not start the bridge daemon.

4. Ping the authenticated daemon on port 4100:

   ```bash
   curl -fsS \
     -H "Authorization: Bearer ${ARENA_BRIDGE_TOKEN}" \
     http://127.0.0.1:4100/health
   ```

   HTTP 200 with `status: "ok"` confirms the bridge process and token path responded. The bridge's current `/health` endpoint does not probe upstream model readiness. A real completion request with an upstream model ID is the generation check and can incur provider charges; no `/ready` route is implemented by the bundled daemon.

5. The seeded AATM bridge provider uses id `arena-local-bridge`, endpoint `http://127.0.0.1:4100`, and credential reference `env:ARENA_BRIDGE_TOKEN`. Its seeded default model is `agent`, but the bridge forwards the selected model name unchanged. When proxying to Ollama or OpenRouter, update this provider's `default_model` and `available_models` to the exact model ID accepted by the upstream. The bridge health response advertises `arena-bridge-default`, `arena-reasoning-pro`, and `arena-fast`; these names are not proof that the configured upstream provides them. The seeded `agent` name is also only a forwarding label, not a built-in model.

Example authenticated upsert for an upstream model (replace `YOUR_UPSTREAM_MODEL` with a real model ID):

```bash
curl -fsS -X POST "$BASE/api/v1/providers" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"arena-local-bridge",
    "name":"Arena Local Bridge",
    "provider_type":"arena_local_bridge",
    "endpoint_url":"http://127.0.0.1:4100",
    "credential_ref":"env:ARENA_BRIDGE_TOKEN",
    "default_model":"YOUR_UPSTREAM_MODEL",
    "available_models":["YOUR_UPSTREAM_MODEL"],
    "priority":1,
    "is_enabled":true,
    "daily_limit_usd":20.0
  }'
```

#### Anthropic Claude API

The seeded row `anthropic-claude` already uses provider type `anthropic_claude`, endpoint `https://api.anthropic.com/v1`, and credential reference `env:ANTHROPIC_API_KEY` by default.

1. Put the API key in the backend's private environment as `ANTHROPIC_API_KEY` (or configure `ANTHROPIC_API_KEY_REF` to reference another environment-variable name), not in the database or a request body.
2. Choose a model ID currently enabled for your Anthropic account. Set the seeded provider row's `default_model` and `available_models` to that ID if it differs from the seed. The adapter sends Anthropic's `x-api-key` and `anthropic-version` headers.
3. Restart the backend so it receives the environment value. An authenticated `POST /api/v1/providers/health-check` checks the API's `/models` endpoint; generating a completion is a separate, potentially billable request.

Example row update (replace the model ID with one supported by your account):

```bash
curl -fsS -X POST "$BASE/api/v1/providers" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"anthropic-claude",
    "name":"Anthropic Claude API",
    "provider_type":"anthropic_claude",
    "endpoint_url":"https://api.anthropic.com/v1",
    "credential_ref":"env:ANTHROPIC_API_KEY",
    "default_model":"YOUR_ANTHROPIC_MODEL_ID",
    "available_models":["YOUR_ANTHROPIC_MODEL_ID"],
    "priority":3,
    "is_enabled":true,
    "daily_limit_usd":20.0
  }'
```

#### OpenRouter

OpenRouter uses `openai_compatible`; AATM sends a Bearer credential and calls `<endpoint_url>/models` for health and `<endpoint_url>/chat/completions` for generation.

1. Put the OpenRouter key in the backend's private environment as `OPENROUTER_API_KEY` and restart the backend.
2. Create a provider row with `provider_type: "openai_compatible"`, `endpoint_url: "https://openrouter.ai/api/v1"`, `credential_ref: "env:OPENROUTER_API_KEY"`, and the exact model ID enabled for your OpenRouter account.

#### Ollama

The same `openai_compatible` adapter can use a local Ollama server that exposes its OpenAI-compatible `/v1` routes.

1. Start Ollama and make sure the requested model is present (`ollama list`; pull a model first if needed).
2. Use `endpoint_url: "http://127.0.0.1:11434/v1"`, `provider_type: "openai_compatible"`, and the exact Ollama model name as `default_model` and in `available_models`.
3. AATM requires every provider credential reference to resolve to a non-empty environment variable. Set `credential_ref: "env:OLLAMA_API_KEY"` and define `OLLAMA_API_KEY` in the backend environment. The adapter sends it as `Authorization: Bearer ...`; use a real secret if your endpoint requires one. A local Ollama endpoint may ignore this header, but keep the endpoint bound to loopback and do not assume it is safe to expose without authentication.

Use this body shape to create either an OpenRouter or Ollama row. Replace every `YOUR_...` value with an actual ID/name and set a unique provider `id`:

```bash
curl -fsS -X POST "$BASE/api/v1/providers" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"YOUR_PROVIDER_ID",
    "name":"YOUR_PROVIDER_NAME",
    "provider_type":"openai_compatible",
    "endpoint_url":"YOUR_OPENAI_COMPATIBLE_BASE_URL",
    "credential_ref":"env:YOUR_CREDENTIAL_ENV_NAME",
    "default_model":"YOUR_MODEL_ID",
    "available_models":["YOUR_MODEL_ID"],
    "priority":4,
    "is_enabled":true,
    "daily_limit_usd":20.0
  }'
```

For OpenRouter, use the endpoint and credential reference above. For Ollama, use the loopback `/v1` endpoint and `env:OLLAMA_API_KEY` reference. Provider keys are never passed in this JSON.

#### Checking a provider

- `POST /api/v1/providers/health-check` performs an adapter-specific probe. For OpenAI-compatible APIs and Anthropic, it checks `/models`. The bridge adapter tries `/ready` and falls back to `/health` on HTTP 404; the bundled bridge currently implements only `/health`, so this confirms daemon/auth reachability, not upstream model readiness. A passing health check is not a generation test.
- `POST /api/v1/providers/{provider_id}/test` defaults to `dry_run: true`. The current Providers view uses this dry-run mode, which intentionally sends no request and cannot prove connectivity.
- To send a real test completion, explicitly set `dry_run` to `false` in that authenticated request and choose a prompt/model. It may consume quota and incur charges; check the returned `provider_id` because the router may fail over to another enabled provider.

```bash
curl -fsS -X POST "$BASE/api/v1/providers/PROVIDER_ID/test" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Reply with a short connectivity check.","model":"YOUR_MODEL_ID","dry_run":false}'
```

#### Important limits

A valid provider row and a green bridge `/health` do not prove that model generation is configured. The bundled bridge has no `/ready` endpoint: when no upstream is configured, it returns a deterministic local reference response rather than calling a model. The default `arena_agent` row reports that its runtime is not implemented. Real manager planning and employee execution require an enabled provider that can return a valid response for AATM's prompts. No provider key or model availability is bundled with this repository.

## 11. Known Limitations
- Real Arena use has not been exercised: it stays off until the owner records Arena's approval, and the real chat page's DOM selectors must then be set with `ARENA_DOM_SELECTORS`. The connector is tested end to end against the local mock page.
- Memory search uses Postgres full-text search (`simple` configuration, no stemming). Embedding/vector search (pgvector) is not implemented. SQLite deployments use the bounded keyword fallback.
- Windows 11 `.ps1` scripts were written to match the Ubuntu scripts but are not executed by the Linux verification gate.
- The secret scanner is regex based: an unrecognised credential format will not be detected.
- The Manager Chat governance gate is pattern based. The structured `ApprovalRequest` API remains the authoritative path for high-risk operations.
- Live Anthropic/OpenAI-compatible calls need your own API key (`env:` reference) or a local Ollama model. None is bundled.
