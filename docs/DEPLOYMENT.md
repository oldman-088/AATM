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
# Edit .env and set DATABASE_URL=postgresql+psycopg://aatm:aatm_password@localhost:5432/aatm
docker compose up -d
cd backend && ../.venv/bin/alembic upgrade head
```

### SQLite (Zero-Dependency Local)
Default `.env.example` uses `sqlite+aiosqlite:///./data/aatm.db`.

## 3. Ubuntu Installation & Run
```bash
chmod +x scripts/ubuntu/*.sh
./scripts/ubuntu/install.sh
./scripts/ubuntu/start.sh
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
