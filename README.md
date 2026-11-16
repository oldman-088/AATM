# AATM — AI Agent Team Manager (v3.1.0)

A manager AI turns your directive (Arabic or English) into tasks. Four AI workers execute them **in
parallel**, each in its own git worktree and browser profile, and open draft PRs. A reviewer comments,
**you** approve merges and spending, and the dashboard shows every step live, Arabic RTL first.

| Doc | What it covers |
|---|---|
| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) | Components, the 8-step workflow, team ownership |
| [`docs/SECURITY.md`](./docs/SECURITY.md) | Controls, approval gates, Arena connector safety, risk register |
| [`docs/OPERATIONS.md`](./docs/OPERATIONS.md) | Install, run, providers, dashboard usage, remote nodes, known limitations |
| [`CHANGELOG.md`](./CHANGELOG.md) | Release notes |
| [`AGENTS.md`](./AGENTS.md) · [`AATM_MASTER_SPECIFICATION.md`](./AATM_MASTER_SPECIFICATION.md) | Rules for coding agents working on this repo |

## Quick start (Ubuntu)
```bash
./scripts/ubuntu/install.sh      # Postgres + RabbitMQ + Redis via Docker (use --sqlite to skip), venv, npm, Chromium
./scripts/ubuntu/start.sh        # backend :8000, local Arena bridge 127.0.0.1:4100, UI :5173
./scripts/ubuntu/restart.sh      # after changing .env / pulling (health-checked restart)
./scripts/ubuntu/reset-admin-password.sh   # lost the admin password? rotate it, printed once
./scripts/ubuntu/verify.sh       # all release gates (lint, migrations, backend/bridge/frontend tests, secret scan, build)
cd frontend && npm run test:e2e  # Playwright: 1440×900 + 390×844 bridge layout + docs/design screenshots
./scripts/demo_e2e.sh --check    # end-to-end demo prerequisites (see docs/OPERATIONS.md)
```
Windows 11: `.\scripts\windows\install.ps1`, then `.\scripts\windows\start.ps1`.

**First sign-in:** `admin@aatm.local` (or `DEFAULT_ADMIN_EMAIL`). The installer generates a random
`DEFAULT_ADMIN_PASSWORD` into `.env`, and you must change it on first sign-in.

## What needs you
- **A model:** an API key referenced as `env:VAR` (Claude, OpenAI-compatible) or a local Ollama model.
- **GitHub:** `gh auth login` on the host, and a project linked to your repository.
- **Approvals:** merges, extra workers and other high-risk actions wait for you in the Approval Center.
- **Arena (optional):** real Arena use stays **off** until you record Arena's approval
  (`ARENA_CONNECTOR_ENABLED`, `ARENA_AUTOMATION_APPROVED`, `ARENA_APPROVAL_REFERENCE`). You then sign in
  yourself in each employee's profile and install the reviewed userscript. AATM never solves
  verification checks, reads cookies, or rotates accounts.

## Upgrading from 3.0.0
Run `git pull`, then `scripts/ubuntu/install.sh` (it applies migrations), then `scripts/ubuntu/start.sh`. New settings are listed in
`.env.example`. The docs were consolidated: `docs/DEPLOYMENT.md` is now `docs/OPERATIONS.md`, and
`docs/SECURITY_POLICY.md` is now `docs/SECURITY.md`.
