# AATM — Deployment Instructions

## 1. Prerequisites
- Python 3.11+
- Node.js 20+ & npm
- Git & GitHub CLI (`gh`)
- Optional (Production DB): Docker + Docker Compose for PostgreSQL 16

## 2. Database Setup (PostgreSQL Production or SQLite Local)
### PostgreSQL via Docker Compose
```bash
cp .env.example .env
# Edit .env and set DATABASE_URL=postgresql+psycopg://aatm:<password>@localhost:5432/aatm
docker compose up -d
cd backend && ../.venv/bin/alembic upgrade head
```
The Compose file reads `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` and
`POSTGRES_PORT` from the environment, so the credentials are not hard-coded.

### SQLite (Zero-Dependency Local)
Default `.env.example` uses `sqlite+aiosqlite:///./data/aatm.db`.

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
- Set a strong, unique `SECRET_KEY` in `.env`. Device tokens are derived from it; while
  the development default is in place the register endpoint reports
  `"token_hardened": false`.
- Run the daemon under a restricted user or inside a container: the workspace sandbox
  restricts paths and tools, but it is not an OS-level jail.

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
