# AATM — Open-Source Project Reuse & Integration Report

**Date (UTC):** 2026-11-15  
**Scope:** Evaluation of external open-source projects for AATM reuse, pattern adaptation, or exclusion.

---

## Evaluation Matrix

| Project | Repository | License | Lang / Stack | Maintenance (Observed) | Decision | Rationale & Security Notes |
|---|---|---|---|---|---|---|
| **Paperclip** | `paperclipai/paperclip` | MIT | TypeScript / Node | Active (Nov 2026) | **Adapt Patterns** | Strong AI company org-chart, role hierarchy & multi-agent coordination concepts; adapted into AATM's Dynamic Employee Org & Team Builder without adopting a separate Node backend. |
| **Agent HQ** | `mkanasani/agent-hq` | Unspecified | TypeScript | Low activity (11 stars) | **Exclude (Reference Only)** | No clear SPDX license and small ecosystem; conceptual task-hub ideas already exceeded by AATM's FastAPI + React Command Center. |
| **Orchestra** | `understudylabs/orchestra-agent-tools` | MIT | TypeScript | Small utility repo | **Adapt Patterns** | Useful parallel tool-dispatch contracts; AATM's Python `TaskScheduler` natively handles DAG dependencies, locks, and retries. |
| **OpenHands** | `OpenHands/OpenHands` | MIT | Python / TypeScript | Very active (93k+ stars) | **Adapt Architecture** | Industry reference for sandboxed workspace execution, event-stream runtime & code-editing agents; AATM adapts its isolated workspace runtime pattern (`AgentRuntime`) without heavy Docker-in-Docker runtime coupling. |
| **CrewAI** | `crewAIInc/crewAI` | MIT | Python | Very active (60k+ stars) | **Adapt Patterns** | Role/goal/backstory agent modeling and sequential/hierarchical crew delegation adapted into AATM's 16+ Role Templates and Collaborative mode; avoids duplicate orchestration state stores. |
| **LangGraph** | `langchain-ai/langgraph` | MIT | Python | Very active (44k+ stars) | **Adapt / Optional Adapter** | Excellent stateful graph & checkpointing model; AATM preserves lightweight SQLAlchemy-backed DAG checkpoints natively, keeping LangGraph optional so no heavy LangChain dependency tree is forced. |
| **Arena Agent Auto (AAA)** | `Clyroas/Arena-Agent-Auto-AAA-` | Unspecified (Chrome Ext JS) | JavaScript | Small extension (1 star) | **Reference Only (Do Not Bundle)** | Demonstrates DOM/panel automation patterns for Arena web views, but lacks explicit OSS license and relies on extension DOM scraping. AATM supports authorized Arena adapters + explicit Tampermonkey userscripts with policy/audit guardrails instead. |
| **Browser Use** | `browser-use/browser-use` | MIT | Python | Very active (121k+ stars) | **Modular Adapter Interface** | Leading Python Playwright-based AI browser control library; AATM exposes `BrowserUseEngineAdapter` inside `backend/app/browser/engines.py` so it can plug directly into AATM profiles when enabled. |
| **OpenBrowser** | `ntegrals/openbrowser` | MIT | TypeScript | Active (9.5k+ stars) | **Modular Adapter Interface** | Lightweight browser automation abstraction; AATM supports `OpenBrowserEngineAdapter` in the modular browser adapter registry. |
| **Automa** | `AutomaApp/automa` | Custom / NOASSERTION | Vue / Chrome Ext | Moderate (21k+ stars) | **Modular Adapter / Workflow Import** | Visual block-based browser workflow JSON format; AATM implements an `AutomaWorkflowAdapter` that validates and executes authorized step workflows via Playwright without embedding a browser extension build. |
| **Trigger.dev** | `triggerdotdev/trigger.dev` | Apache-2.0 | TypeScript | Very active (16k+ stars) | **Adapt Patterns** | Reliable long-running job, idempotency key, retry & checkpoint semantics adapted directly into AATM's `TaskScheduler` + background runner without requiring an external Node job cluster. |
| **Langfuse** | `langfuse/langfuse` | MIT / EE mixed | TypeScript | Very active (37k+ stars) | **Adapt Observability Schema** | Trace/span/token/cost observability model adapted into AATM's `ProviderUsageLog`, `ExecutionTrace`, and Analytics dashboard; optional external Langfuse webhook export can be toggled cleanly. |
| **Octokit** | `octokit/octokit.js` | MIT | TypeScript / REST | Active (7.8k+ stars) | **Adapt / REST SDK Integration** | Canonical GitHub REST API model; AATM implements a clean Python `GitHubClient` (matching Octokit endpoints via `httpx` + `gh` CLI) for repos, branches, files, PRs, reviews, and issues. |

## Security & Dependency Hygiene
- Zero GPL/AGPL dependencies introduced.
- Avoided pulling bloated overlapping agent frameworks (`crewai` + `langgraph` + `openhands` simultaneously) into core runtime, preserving fast startup, low memory footprint (<4 GB sandbox safe), and single authoritative PostgreSQL/SQLite state.
- Untrusted extension/userscript code is isolated behind explicit admin registration, role permissions, and audit logging.
