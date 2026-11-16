# AATM User & Operations Guide

## 1. Sign In & Governance
Click **Demo Director Login** (or **Sign In** with `admin@aatm.local` / `aatm-admin-2026`) in the top bar to acquire a JWT session token for executing mutations, launching browser profiles, running orchestration cycles, and approving protected actions.

## 2. Managing the 4 Workers & 12 Divisions
- **Executive Dashboard**: View KPIs, 12-Division completion matrix, CPU/RAM/Disk metrics, Team Intelligence recommendations, and live WebSocket events.
- **Manager Chat**: Issue executive directives (e.g., `"create task build metrics"`, `"orchestrate"`, `"merge main"`). High-risk directives automatically route to the **Approval Center**.
- **Tasks & DAG**: Create tasks with priority and dependencies; click **Run Orchestration Cycle** to dispatch dependency-ready tasks to idle workers with idempotency keys and locks.

## 3. Browser Wall & AI Providers
- **Browser Wall**: Launch any of the 4 persistent profiles in `visible` or `background` mode, navigate URLs, test lock contention, or trigger **Crash & Self-Heal** recovery.
- **AI Providers**: Configure Arena Local Bridge, Arena Agent, and Anthropic Claude via environment variable references (`env:ANTHROPIC_API_KEY`). Use **Safe Dry-Run Test** to verify routing without spending funds.

## 4. Multilingual & RTL
Switch between **EN** (English LTR), **AR** (العربية RTL), and **FA** (فارسی RTL) in the top header or Settings view at any time.
