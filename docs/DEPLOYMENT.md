# AATM — Deployment Instructions

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

## 8. Operations Scripts

| Script | Purpose |
|---|---|
| `scripts/ubuntu/install.sh` | Dependencies, Playwright Chromium, migrations, `.env` bootstrap, verification. |
| `scripts/ubuntu/start.sh` | Starts the API + UI and waits for the health endpoint. |
| `scripts/ubuntu/stop.sh` | PID-validated shutdown of the API, UI and worker daemons. |
| `scripts/ubuntu/update.sh` | Update dependencies, migrate, re-verify. |
| `scripts/ubuntu/backup.sh` | Verified, permission-restricted archive of `data/` and `.env`. |
| `scripts/ubuntu/restore.sh` | Validated restore (rejects absolute/traversal member paths). |
| `scripts/ubuntu/verify.sh` | All release gates in one command. |
| `scripts/ubuntu/start-worker.sh` | Launches the remote worker daemon on this host. |
