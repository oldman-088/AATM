# AATM — AI Agent Team Manager Architecture (Division 01)

## 1. System Overview
AATM is a full-stack autonomous AI engineering management platform orchestrating a Chief AI Engineering Manager and four specialized Worker Agents across 12 structured development divisions.

## 2. Core Components

### 2.1 Backend (`backend/app/`)
- **Framework**: FastAPI + Pydantic v2 + SQLAlchemy 2.0 + Alembic.
- **Database**: Dual-engine compatibility — PostgreSQL (`postgresql+psycopg://`) for production and SQLite (`sqlite+aiosqlite://`) for zero-dependency local development & CI.
- **Security**: JWT Bearer tokens, API keys, PBKDF2-SHA256 password hashing, role-based access control (`admin`, `manager`, `operator`, `viewer`), credential reference indirection (`env:VAR_NAME`), protected-branch & high-risk Approval Center gates.
- **Real-time**: WebSocket event bus (`/ws/events`) broadcasting task transitions, worker heartbeats, browser wall snapshots, approvals, and notifications.

### 2.2 Frontend (`frontend/src/`)
- **Stack**: React 18, TypeScript, Vite, Tailwind CSS, shadcn/ui-pattern components, Lucide icons.
- **Multilingual & RTL**: Native `en` (LTR), `ar` (RTL), `fa` (RTL) localization engine with automatic `dir` & `lang` attributes, CSS logical properties, localized `Intl` dates/numbers, and persistent user preferences.
- **Live Preview Proxy**: Vite dev server proxies `/api` and `/ws` to backend (`127.0.0.1:8000`) and binds `0.0.0.0:5173` with `allowedHosts: true` so proxied browser previews work seamlessly; production mode also serves built `frontend/dist` directly from FastAPI.

### 2.3 AI Provider Layer (`backend/app/providers/`)
- **Adapters**: `ArenaLocalBridgeAdapter`, `ArenaAgentAdapter`, `AnthropicClaudeAdapter`.
- **Router**: Priority, capability, health-score, and cost-cap aware routing with circuit breaker and exponential backoff.
- **Usage & Cost Control**: Every call logs tokens, latency, estimated USD cost, and enforces budget caps before execution.

### 2.4 Browser Profile Infrastructure (`backend/app/browser/`)
- **Engine**: Playwright persistent Chromium contexts (`launch_persistent_context`) with isolated user-data directories under `data/browser_profiles/`.
- **Concurrency & Safety**: DB + file lock per profile, visible/background mode toggle, session health heartbeat, Browser Wall live viewport/screenshot feed, automatic crash recovery.
- **Graceful Fallback**: When Chromium binary is unavailable in minimal/sandbox environments, runs deterministic fallback browser session renderer while reporting exact `chromium_installed: false` capability status.

### 2.5 Orchestration, Memory, Git & QA (`backend/app/orchestration/`, `memory/`, `git/`, `qa/`)
- **4 Specialized Workers**:
  1. `worker-backend`: FastAPI, DB, Migrations, APIs, Auth, Audit.
  2. `worker-frontend`: React, TS, Tailwind, Dashboard, RTL i18n (en/ar/fa).
  3. `worker-ai-browser`: Arena Bridge, Claude API, Provider Router, Playwright Profiles, Browser Wall.
  4. `worker-orchestration-qa`: Orchestrator, DAG Scheduler, GitHub/Worktrees, QA Engine, Security, Self-Healing.
- **5-Tier Memory**: `global`, `project`, `agent`, `task`, `decision` with TF-IDF/keyword relevance ranking.
- **GitHub & Worktrees**: Branch/worktree management via `git` & `gh` CLI with protected branch merge blocking until Approval Center sign-off.
- **QA & Self-Healing**: Automated test runner, static security scanner, system resource monitor (`psutil`), stalled-task detection, lock cleanup, and worker recovery.
