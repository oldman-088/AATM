# AATM — Target Architecture Document

## 1. Architectural Principles
1. **Single Authoritative State**: PostgreSQL (production) / SQLite (local/CI) via SQLAlchemy 2.0 + Alembic.
2. **Separation of Concerns**:
   - **AI Reasoning Layer** (`providers/`): Provider accounts, sessions, Arena adapters, routing, usage/cost tracking.
   - **Browser Automation Layer** (`browser/`): Interchangeable engine adapters (`playwright`, `browser_use`, `openbrowser`, `automa`) + Tampermonkey `Userscript` manager + isolated persistent Chrome profiles.
   - **Orchestration & Messaging Layer** (`orchestration/`): 3-mode scheduler (`manual`, `automatic`, `collaborative`), `AATM Agent Messaging System`, Smart Employee Replacement, Emergency Stop.
   - **Executable Runtime & SDLC Layer** (`runtime/`): Isolated per-employee workspaces (`data/workspaces/<agent_id>/`), real file/code/test execution, 22-step SDLC Idea-to-Delivery pipeline.
   - **Governance & Security Layer** (`core/security.py`, `qa/engine.py`): Autonomy policies (`supervised | semi_autonomous | highly_autonomous`), prompt-injection sanitization, RBAC, Approval Center, immutable audit logs.
   - **Multi-Device Control Plane** (`devices/`): Central server node registry, device heartbeats, disconnected-node recovery.

## 2. Scalable Employee & Account Allocation Model
- Supports 20+ dynamic AI employees created from 16+ role templates or custom roles.
- **Allocation Modes**: `one_per_employee`, `shared`, `hybrid` linking `Agent` -> `ProviderAccount` -> `ProviderSession` -> `BrowserProfile` -> `Workspace`.

## 3. Four-Layer Permissioned Memory
- **Employee Memory** (private to employee unless shared), **Team Memory**, **Project Memory**, **Organizational Memory** + Decision records, with access checks and failure-learning retrieval.
