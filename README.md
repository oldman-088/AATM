# AATM — AI Agent Team Manager (v3.0.0)

> **Master Specification:** [`AATM_MASTER_SPECIFICATION.md`](./AATM_MASTER_SPECIFICATION.md) | **Deployment Guide:** [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md) | **Agent Guide:** [`AGENTS.md`](./AGENTS.md)

Full-stack autonomous engineering orchestration platform managing a **Chief AI Engineering Manager** and **Specialized Worker Agents** (scalable to 20+ dynamic AI employees) across **12 Development Divisions**.

## Key Capabilities (v3.0.0)
- **Real AI Providers, Model Wizard & Pricing Catalog**: Connect Anthropic Claude, OpenAI/OpenRouter, Ollama, or Arena Local Bridge (explicitly labeled `diagnostic_only` when unconfigured) with credential reference indirection (`env:*`), `GET /api/v1/providers/catalog`, model setup wizard, per-agent/task assignment, and honest `pricing_known` cost accounting.
- **Git Worktree Isolation & PR Lifecycle**: Dedicated per-task worktrees (`aatm/task-<id>-<slug>`), automatic commit & draft PR creation, GitHub webhook lifecycle transitions (`needs_review` / `completed`), and post-merge worktree cleanup.
- **Hardened Execution Sandbox**: Non-root Docker sandbox (`--user 10001:10001`, `--network=none`, `--read-only`, `--cap-drop=ALL`) and fail-closed `process` guard in production (`GET /api/v1/sandbox/status` + `SANDBOX_UNAVAILABLE` warning banner).
- **Persistent Real Browser Engine**: Playwright persistent Chromium contexts with SSRF public-IP/DNS route blocking, single-owner `.aatm.lock` + DB locking, live PNG snapshots (`POST/GET /api/v1/browsers/{id}/snapshot`), Chromium RSS memory telemetry, DOM secret redaction, and honest HTTP `501` when Chromium is absent.
- **Security & Governance Hardening**: PBKDF2-SHA256 auth with sliding-window login rate limiting, forced first-login password rotation, server-side JWT revocation (`POST /api/v1/auth/logout`), HTTP security headers (`CSP`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `no-store`), approval replay guard, audit/payload redaction, and `GET /api/v1/governance/security-posture`.
- **Multilingual RTL/LTR & Live UI**: Native **English (`en`)**, **Arabic (`ar`)**, and **Persian (`fa`)** with automatic `dir="rtl"`/`dir="ltr"` mirroring, dark/light theme support, 12-tab desktop + mobile navigation, and real WebSocket `/ws/events` status indicator.
- **Cross-Platform Installers & Verification**: Operational scripts for **Ubuntu** (`scripts/ubuntu/*.sh`) and **Windows 11** (`scripts/windows/*.ps1`), plus `docker-compose.yml` for PostgreSQL & hardened worker containers.

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

### Verification Gate
```bash
./scripts/ubuntu/verify.sh
```
```powershell
.\scripts\windows\verify.ps1
```

### Initial Admin Login (Local Development)
- **Email**: `admin@aatm.local` (or `DEFAULT_ADMIN_EMAIL` in `.env`)
- **Password**: Generated cryptographically by `install.sh` / `install.ps1` and written to `.env` (`DEFAULT_ADMIN_PASSWORD`); first sign-in enforces immediate password rotation.
- **UI**: `http://localhost:5173` (or `http://localhost:8000` when serving built `frontend/dist`)
- **Backend API**: `http://localhost:8000/api/health`
