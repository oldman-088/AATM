# AATM — Completion Prompt (for Claude Code in the terminal)

You are completing the AATM project in this repository (local clone of oldman-088/AATM, current branch: aatm-complete, based on main 2062e84 / v3.0.0).

Source of truth: docs/AATM_BUILD_PLAN.md. Read it fully first. Ignore the phase numbering in docs/IMPLEMENTATION_ROADMAP.md, AATM_MASTER_SPECIFICATION.md and docs/DEVELOPMENT_PLAN.md; mark each of them at the top with "SUPERSEDED — see docs/AATM_BUILD_PLAN.md". Whenever your context is compacted or you are unsure what is next, re-read docs/AATM_BUILD_PLAN.md and this file (docs/AATM_COMPLETE_PROMPT.md).

## Verified current state of main (already done — do not redo)
Phase 1 + 1b security, Phase 2 model wizard + NO_MODEL_CONFIGURED gating, Phase 3 per-task git worktrees + auto draft PRs.

## Verified NOT IMPLEMENTED (your job)
- credential_ref accepts ANY env var and the wizard can send it to ANY endpoint_url (secret exfiltration).
- No reviewer agent, no approval-gated `gh pr merge`, no Project "completed" rollup.
- AGENT_MAX_PARALLEL and ARENA_BRIDGE_MAX_CONCURRENCY default to 1; no RabbitMQ, no Redis, no auto-scaling; subprocess.run still used in async paths (github_client/client.py, git_workflows/manager.py, qa/engine.py, worker_daemon.py, sandbox.py).
- ArenaAgentAdapter returns "not implemented"; no bridge /v1/connector WebSocket; no Tampermonkey connector; no ARENA_CONNECTOR_ENABLED / ARENA_AUTOMATION_APPROVED / ARENA_APPROVAL_REFERENCE / ARENA_MOCK_URL flags; profiles not linked to employees.
- No 8-step workflow timeline in the Dashboard.
- Phase 8 items (memory FTS/pgvector, split large files, consolidate docs, scripts/demo_e2e.sh) not done.

## Implement in this order (follow the matching sections of docs/AATM_BUILD_PLAN.md exactly)

**A. Credential security fix.** credential_ref only for provider-key env names (`^[A-Z][A-Z0-9_]*_API_KEY$` or an explicit PROVIDER_CREDENTIAL_ENV_ALLOWLIST), always deny server secrets (reuse SENSITIVE_ENV_EXACT from runtime/sandbox.py); validate endpoint_url (http/https, no embedded credentials); operators may only test official preset hosts, custom endpoints need admin; audit every test-connection without the key. Apply to the wizard and every provider/account create/update.

**B. "Phase 4 — Review, Merge and Completion".**

**C. "Phase 5 — True Parallelism".** Default 1 manager + 4 parallel workers; on-demand extra workers with approval + scale-down; RabbitMQ (aio-pika) with Postgres LISTEN/NOTIFY fallback; Redis (optional, in-process fallback); Postgres default in compose; convert ALL subprocess.run in async paths to asyncio.create_subprocess_exec; scripts/load_test.py.

**D. "Phase 6 — Profiles Manager + Arena Browser Connector".** Arena is connected through the Arena web page in each employee's Chrome profile via a Tampermonkey userscript and the local bridge — NOT via an API. Build it fully and test it end-to-end against a local mock Arena page (ARENA_MOCK_URL). Real-Arena use stays OFF by default (both flags false + approval reference required). Never solve/bypass CAPTCHA (stop with needs_human), never read cookies/tokens, no account rotation, one in-flight request per profile with a delay and a daily cap, run_userscript stays 501.

**E. "Phase 7 — Dashboard Matching the Infographic".** 8-step timeline from real state, colored employee cards, Browser Wall with connector status, live agent log, PR view with approve/merge, errors & recovery panel. Arabic RTL first.

**F. "Phase 8" items 1–4.**

## Working rules
- Read the real code before changing it. Keep every existing safety control. No fake features: anything missing returns explicit not_implemented / not_configured.
- One commit per item (prefix "A:", "B:", ...). Never commit to main directly; never force-push.
- Before each commit run `bash scripts/ubuntu/verify.sh` and `cd backend && alembic heads` (single head). All gates must pass; fix code, never weaken tests.
- Every acceptance criterion in docs/AATM_BUILD_PLAN.md for B–F must have a test. Never say an item is complete without the test.
- If a dependency (Docker, RabbitMQ, Redis, Postgres, Playwright Chromium, gh) is missing on this machine, tell me the exact install command and wait, or use the documented fallback and say so.
- After each item, print a short report: what was built, files changed, test names per acceptance criterion, verify.sh counts, and anything still NOT IMPLEMENTED. Then continue to the next item.
- Ask me before: git push, opening a PR, installing system packages with sudo, or deleting files outside the repo.

## At the end
Run verify.sh on a fresh clone of this branch, bump the version to 3.1.0 (backend APP_VERSION, frontend/package.json, CHANGELOG.md), then ask me to approve `git push -u origin aatm-complete` and opening the PR, and give me a final table: acceptance criterion → test name → commit.
