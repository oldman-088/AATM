# AATM — Master Development Prompt

## Vision & Objective
Build **AATM (AI Agent Team Manager)**, an autonomous software engineering organization platform led by a **Chief AI Engineering Manager** orchestrating **eight Worker Agents** (four specialized, four general) across **12 Development Divisions**.

## Core Architectural Pillars
1. **System Foundation & Contracts (Division 01)**:
   - Shared API, event, database, agent, provider, and browser contracts (`contracts/schemas.json`, `docs/ARCHITECTURE.md`).
2. **Backend & Database (Division 02)**:
   - FastAPI, SQLAlchemy 2.0, Alembic migrations, PostgreSQL + SQLite compatibility, JWT/RBAC auth, REST APIs, WebSocket `/ws/events`, logging, and immutable audit trail.
3. **Frontend & UX (Division 03)**:
   - React 18, TypeScript, Vite, Tailwind CSS, shadcn/ui component patterns, Executive Dashboard, Manager Chat, Agents, Tasks & DAG, Browser Wall, AI Providers, Projects & Memory, GitHub Workflows, QA & Self-Healing, Approval Center & Settings.
4. **Multilingual & RTL System (Division 04)**:
   - Full support for **English (`en`, LTR)**, **Arabic (`ar`, RTL)**, and **Persian (`fa`, RTL)** with dynamic `dir`/`lang`, CSS logical properties, localized `Intl` dates/numbers, and persistent preferences.
5. **AI Provider Integration (Division 05)**:
   - Adapters for **Arena Local Bridge**, **Arena Agent Runtime**, and **Anthropic Claude API** with credential reference indirection (`env:*`), health checks, failover routing, dry-run testing, and daily/monthly spend limits.
6. **Browser Profile Infrastructure (Division 06)**:
   - Playwright persistent Chromium contexts with isolated profile directories, DB + file locks, visible/background modes, Browser Wall snapshots, and crash recovery.
7. **Manager & Worker Agent System (Division 07)**:
   - Chief Manager + 8 Worker Agents (4 specialized + 4 general) with role permissions, heartbeats, pause/resume/restart controls, and Manager Chat directive orchestration.
8. **Task Orchestration & Scheduling (Division 08)**:
   - Dependency DAG validation, cycle detection, priority queues, idempotency keys, task locking, checkpoints, retries, and stalled-task recovery.
9. **Project Management & 5-Tier Memory (Division 09)**:
   - Multi-project builder and persistent memory across `global`, `project`, `agent`, `task`, and `decision` scopes.
10. **GitHub & Development Workflows (Division 10)**:
    - Repository/worktree tracking and protected-branch merge policies gated by Approval Center authorization.
11. **Quality, Intelligence, Security & Recovery (Division 11)**:
    - Static security scanning, `psutil` resource telemetry, Team Intelligence recommendations, and self-healing sweeps.
12. **Integration, Cross-Platform & Delivery (Division 12)**:
    - Ubuntu (`.sh`) and Windows 11 (`.ps1`) install/start/stop/update/backup/restore scripts, Docker Compose, automated tests, and documentation.
