# AATM — Prioritized Implementation Roadmap

| Phase | Scope | Key Deliverables | Verification Gate |
|---|---|---|---|
| **Phase 1–2** | Audit, Open-Source Eval & Target Architecture | `AUDIT_REPORT.md`, `OPEN_SOURCE_INTEGRATION_REPORT.md`, `TARGET_ARCHITECTURE.md` | Docs complete |
| **Phase 3–4** | Dynamic Employees (20+), Arena Accounts/Sessions, Multi-Device | DB models + Alembic migration `0002`, role templates, account allocation (`one_per_employee`/`shared`/`hybrid`), device registry | Alembic upgrade + API tests |
| **Phase 5** | Modular Browser Engines & Tampermonkey Userscripts | `Playwright`, `BrowserUse`, `OpenBrowser`, `Automa` adapters + `Userscript` CRUD/audit/role-binding | Browser & userscript pytest |
| **Phase 6–8** | 3-Mode Orchestration, Smart Replacement, Agent Messaging & Executable Runtime | `AgentMessage` bus, automatic task delivery & activation, isolated workspace file/test runner, SDLC Idea-to-Delivery workflow | Messaging, replacement & workspace execution pytest |
| **Phase 9–10** | Full GitHub Integration & 4-Layer Permissioned Memory | GitHub REST + `gh` client (repos, branches, files, PRs, comments, issues), Employee/Team/Project/Org memory | GitHub & memory pytest |
| **Phase 11–12** | Command Center, Autonomy Policies, Prompt-Injection Guard & Emergency Stop | Global/scoped Emergency Stop, autonomy policy enforcement, prompt-injection sanitizer, all 16 UI sections + EN/AR/FA RTL | Security + E2E pytest + Vitest + Vite build |
| **Phase 13** | Final Verification & Delivery | Full test suite, Feature Implementation Matrix, Security Assessment, Deployment & User Guide, Known Limitations | All tests green & live preview verified |
