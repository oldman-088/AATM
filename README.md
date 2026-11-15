# AATM — AI Agent Team Manager

Full-stack autonomous engineering orchestration platform managing a **Chief AI Engineering Manager** and **4 Specialized Worker Agents** across **12 Development Divisions**.

## Key Capabilities
- **Four-Worker Architecture**: Backend Engineer (`worker-1`), Frontend Engineer (`worker-2`), AI Provider & Browser Engineer (`worker-3`), Orchestration & QA Engineer (`worker-4`).
- **FastAPI + SQLAlchemy 2.0 + Alembic**: PostgreSQL & SQLite dual-engine support, JWT RBAC auth, WebSocket `/ws/events` live stream, structured audit timeline.
- **React + TypeScript + Vite + Tailwind CSS**: Executive Dashboard, Manager Chat, Agents, DAG Task Scheduler, Browser Wall, AI Providers, 5-Tier Memory Explorer, GitHub/Worktrees, QA & Self-Healing, Approval Center.
- **Multilingual & Complete RTL**: Native **English (`en`)**, **Arabic (`ar`)**, and **Persian (`fa`)** with automatic `dir="rtl"`/`dir="ltr"` switching, CSS logical properties, and localized `Intl` formatting.
- **AI Provider Integration**: Arena Local Bridge, Arena Agent Runtime, and Anthropic Claude API with credential reference indirection (`env:ANTHROPIC_API_KEY`), dry-run testing, health checks, and daily budget caps.
- **Persistent Browser Wall**: Playwright isolated persistent Chromium profiles with visible/background modes, file+DB locking, live viewport snapshots, and crash recovery.
- **Protected Branch & Approval Center**: Blocks merges to protected branches (`main`, `master`, `production`) and high-risk actions until explicitly authorized in the Approval Center.
- **Cross-Platform**: Native operational scripts for **Ubuntu** (`scripts/ubuntu/*.sh`) and **Windows 11** (`scripts/windows/*.ps1`), plus `docker-compose.yml` for PostgreSQL.

## Quick Start

### Ubuntu / Linux
```bash
chmod +x scripts/ubuntu/*.sh
./scripts/ubuntu/install.sh
./scripts/ubuntu/start.sh
```

### Windows 11 (PowerShell)
```powershell
.\scripts\windows\install.ps1
.\scripts\windows\start.ps1
```

### Default Credentials (Local Development)
- **Email**: `admin@aatm.local`
- **Password**: `aatm-admin-2026`
- **UI**: `http://localhost:5173`
- **Backend API**: `http://localhost:8000/api/health`
