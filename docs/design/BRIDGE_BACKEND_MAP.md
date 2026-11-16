# Command Bridge — backend map (PHASE 0 inspection, v3.2.0)

Scope: map every visual element of the **"جسر القيادة" / Command Bridge** (spec section 2) to a
real data source in the existing AATM backend, or record the gap and how it is closed in item **B1**.

Sources inspected (code, not docs):

| Area | File |
| --- | --- |
| App bootstrap, routers, `/ws/events` auth | `backend/app/main.py` |
| Event bus (in-process + Redis fan-out) | `backend/app/core/events.py` |
| Agent loop iterations, persisted history | `backend/app/runtime/agent_loop.py`, `backend/app/runtime/loop/context.py::_persist_iteration` |
| Structured actions (read/write/run/review/done) | `backend/app/runtime/agent_protocol.py`, `backend/app/runtime/loop/actions.py` |
| Sandbox + sensitive-path rules | `backend/app/runtime/sandbox.py` (`is_sensitive_workspace_path`) |
| Secret redaction | `backend/app/runtime/agent_protocol.py` (`redact_secrets`), `app/core/…` |
| Workspace execution | `backend/app/runtime/executor.py` |
| Dashboard payloads | `backend/app/api/routes/dashboard.py` |
| Agents / tasks / chat | `backend/app/api/routes/orchestration.py` |
| Approvals / audit / notifications | `backend/app/api/routes/governance.py` |
| Emergency stop | `backend/app/api/routes/platform_ops.py` |
| Arena connector & Profiles Manager | `backend/app/api/routes/arena.py`, `app/browser/arena_connector.py` |
| PR review + merge | `backend/app/api/routes/reviews.py`, `app/orchestration/review.py` |
| Providers / model status | `backend/app/api/routes/infra.py`, `app/providers/router.py::get_model_configuration_status` |
| Scaling | `backend/app/api/routes/scaling.py` |
| Workspace files (real writes) | `backend/app/api/routes/platform_workspaces.py` |

## 1. Endpoints the Bridge consumes (all authenticated, all existing)

| Endpoint | Shape used by the Bridge |
| --- | --- |
| `GET /api/health` | `{status, version, sandbox:{status,mode,image,reason}, model_status:{status, configured, configured_providers, diagnostic_only_providers, reason}}` — drives the empty state, the "engine" HUD chip and the per-robot المحرك row |
| `GET /api/v1/agents` | `Agent[]` (`id, name, role, role_template, status, provider_id, model_name, is_enabled, is_emergency_stopped, current_task_id, git_branch, tasks_completed, tasks_failed, last_heartbeat, …`) — seats, robot states, station controls |
| `GET /api/v1/dashboard/employees` | colored employee cards: `current_task{id,title,status,progress}`, `last_action{summary,failed}`, `profile_status`, `connector_status`, `cost_usd`, `provider`, `model` — name tag state line, side-panel rows |
| `GET /api/v1/tasks` | `Task[]` (`status, assigned_agent_id, progress_percent, pr_number, pr_url, pr_status, branch_name, worktree_path, checkpoints, result_summary`) — station footer status, PR link, stage lights |
| `GET /api/v1/dashboard/home` | counters: `active_employees, total_employees, tasks_by_status, open_prs, cost_today_usd, daily_budget_usd, needs_human` — HUD crew chip, cost row |
| `GET /api/v1/projects` + `GET /api/v1/projects/{id}/timeline` | project name (HUD "المهمة") and the **8 real steps** `request, analyze_split, distribute, execute, collect, pull_request, review_merge, completed` with `state ∈ done/active/pending` — the 8 holo-table lights |
| `GET /api/v1/reviews/pull-requests` | PRs with reviews and `merge_approvals` — station "PR #n مفتوح", console status line |
| `GET /api/v1/approvals?status=pending` + `POST /api/v1/approvals/{id}/decide` | real human approval flow (`{decision: "approved"\|"rejected", review_note}`) — "الموافقات (N)" |
| `GET /api/v1/arena/connector/status` | `{enabled, real_enabled, status_label, bridge}` — the "Arena: محاكاة — بانتظار الموافقة" chip and the per-robot جلسة Arena row |
| `GET /api/v1/scaling/status` | `default_workers, extra_workers, ready_backlog, mode, pending_approval_id` — second-arc seats / team size |
| `GET /api/v1/chat` + `POST /api/v1/chat` | `ChatMessage[]` (`sender_type: user\|manager`, `content`) — the commander console; POST runs `task_scheduler.handle_manager_directive` (real directive → manager plan) |
| `POST /api/v1/emergency-stop` / `…/clear` | `{scope: global\|employee\|task\|browser, target_id, reason}` — console "إيقاف طارئ" and station red state |
| `PATCH /api/v1/agents/{id}` | `{is_enabled}` — station إيقاف مؤقت / استئناف |
| `POST /api/v1/employees/{id}/replace` | station استبدال |
| `GET /api/v1/agents/{id}/code-stream?limit=` | **new in B1** — recent `agent.code` frames (per-agent live code buffer) |

## 2. WebSocket `/ws/events`

* Authenticated: the client must send `{"token": "<jwt>"}` as the **first message** (query-string tokens were
  removed in 3.1.0). The server replies `{"type": "auth.ok"}`.
* Payload envelope: `{event_id, type, timestamp, payload}`.
* Existing event types (emitted today):

| Type | Payload | Bridge use |
| --- | --- | --- |
| `agent.iteration` | `agent_id, task_id, iteration, action, result(≤500), failed` | station footer state (fallback when no `agent.code` frame yet) |
| `agent.heartbeat` | `agent_id, status, task_id` | robot animation state |
| `task.updated` / `task.created` | `task_id, status, progress_percent, …` | stage lights, station status line |
| `chat.message` | `id, sender, content` | console messages (manager reply) |
| `approval.requested` / `approval.decided`* / `approval.merge_result` / `approval.scale_result` | approval id + result | approvals chip and status line |
| `arena.needs_human` / `arena.resumed` | `profile_id, reason` | red robot state, "يحتاجك" |
| `scaling.proposed` / `scaling.workers_added` / `scaling.workers_retired` | seats change | extra seat arc |
| `security.emergency_stop` | stopped scope | global red state |
| `project.completed` | `project_id, merged_prs` | stage 8 light |
| `browser.snapshot` | `profile_id, url/status/action` | side panel Tampermonkey/جلسة Arena row |
| `github.webhook` | safe payload | PR status refresh |
| `orchestration.cycle` | `dispatched` | status line |
| `qa.completed`, `agent_message.delivered`, `self_healing` | misc | not used by the Bridge |

\* approval decisions are also visible through `task.updated`, `approval.merge_result`, `approval.scale_result`;
the Bridge refreshes `/approvals` on any `approval.*` event.

**Gap (closed by B1):** there is no code-level stream. `agent.iteration` only carries a redacted 500-char
result excerpt, so a station screen could not show the code an employee writes.

## 3. Element → data source → gap → closure

| Bridge element (spec §) | Real data source | Gap | Closure |
| --- | --- | --- | --- |
| §2.2 window / planet / floor grid | pure CSS (no data) | none | — |
| §2.3 "AATM // COMMAND BRIDGE" | static brand | none | — |
| §2.3 "المهمة" | `projects[active].name` (`/projects`) | none | — |
| §2.3 crew chip `● الطاقم N/N متصل` | `dashboard/home.counters.active_employees / total_employees` | "متصل" must mean a live connector/heartbeat, not just "exists" | count employees whose `last_heartbeat` is inside the heartbeat window, else label as `N/N` employees |
| §2.3 engine chip | `/api/health → model_status`, `/arena/connector/status` | none | if `NO_MODEL_CONFIGURED` → "غير متوفر" chip; Arena gate not approved → amber "محاكاة — بانتظار الموافقة" |
| §2.3 timer `T+HH:MM:SS` | first `/agents` heartbeat or project `created_at` | no mission start timestamp | use `project.created_at` (real) and label `T+` |
| §2.3 WS status | socket state | none | local state |
| §2.4 station screen: header file + branch | **new** `agent.code` frames (`path`, `language`) + `Task.branch_name` | B1 | B1 emits frames on every write/edit/test/review action |
| §2.4 screen: streamed code | **new** `agent.code` (`content` capped 400 lines / 32 KB, `diff` hunks, `output_lines`) | B1 | B1 |
| §2.4 screen footer (`writing…`, `pytest running…`, `pushed · draft PR #14`, `paused: needs_human`) | `agent.code.action`, `Task.pr_number`, `agent.status`, `connector_status` | partial | derived from real rows; unknown → "غير متوفر" |
| §2.4 robot state animation | `agent.status`, `is_emergency_stopped`, `connector_status=needs_human`, last frame action | none | mapping table in `frontend/src/views/Bridge.tsx` |
| §2.4 name tag state line | `status` + `current_task.title` + `pr_number` | none | real text only |
| §2.4 extra seats | `/agents` beyond 4 + `/scaling/status` | none | second arc, scale 0.8 |
| §2.5 8 stage lights | `/projects/{id}/timeline.steps` | no project → no state | all `pending`, tooltip = stage name, `غير متوفر` for detail |
| §2.5 manager robot + tag | `/agents` where `role=manager` | none | falls back to the first agent with `role=manager`, else hidden with "غير متوفر" |
| §2.6 console chat | `GET/POST /chat` + `chat.message` events | none | real directive via `handle_manager_directive` |
| §2.6 stage cells in console | same timeline | none | — |
| §2.6 status line (e.g. `PR #14 بانتظار موافقتك · روبوت-4 يحتاجك`) | `reviews/pull-requests → merge_approvals[pending]`, `needs_human` profiles | none | built from real rows only |
| §2.6 "الموافقات (N)" | `GET /approvals?status=pending` + `POST /approvals/{id}/decide` | none | inline approval list in the console (real decide call) |
| §2.6 "إيقاف طارئ" | `POST /emergency-stop` (scope employee/global) | none | keeps the existing safety control |
| §2.7 focus view code | same `agent.code` buffer (`/agents/{id}/code-stream?limit=40` + events) | B1 | B1 |
| §2.7 side panel rows | `dashboard/employees` + `/agents` + `arena/connector/status` + `/providers` + `/reviews/pull-requests` | "Tampermonkey" state is only modelled as `connector_status` | show `connector_status` (ready/busy/needs_human/offline); if a profile is missing → "غير متوفر" |
| §2.7 progress bar / step | `Task.progress_percent`, `Task.checkpoints[-1].iteration` + `AGENT_MAX_ITERATIONS` | step total is not exposed per task | show `iteration / max_iterations` when both known, else hidden |
| §2.7 side-panel controls | إيقاف مؤقت = `PATCH /agents/{id}`; استبدال = `POST /employees/{id}/replace`; إيقاف طارئ = `POST /emergency-stop`; استئناف = `POST /arena/profiles/{id}/resume` (real) | "استئناف" exists only as the connector resume | control is rendered **only** when the backend supports it |
| §2.8 empty state | `/api/health.model_status`, `/projects`, `/arena/connector/status` | none | 3-step onboarding, each step checked from real state |
| §2.9 mobile | same data, list layout | none | — |

## 4. Safety rules the stream must keep (B1)

1. **Redaction first.** Every frame body passes `redact_secrets()` and is re-checked with
   `contains_credentials()`; a frame that still looks like it carries a secret is dropped, not shown.
2. **Sensitive paths never stream.** `is_sensitive_workspace_path()` blocks `.env`, credential stores,
   `.git/`, `*.pem`, `*.key`, `*token*` etc. before the frame is buffered or emitted.
3. **No private reasoning.** Only concrete artefacts leave the process: file content the agent wrote, diffs,
   the allowlisted command it ran (pytest/ruff) with its output, review comment bodies and explicit
   `done` summaries. Model prompts/responses are never streamed.
4. **Bounded.** ≤400 lines and ≤32 KB per frame; ≤120 frames buffered per agent; per-agent event throttle
   (default 10/s) with batching so 20 agents cannot flood the socket.
5. **Read-only endpoint.** `GET /api/v1/agents/{id}/code-stream` returns the buffer for an existing agent, is
   authenticated (`get_current_user`), and never accepts content from the client.

## 5. Deliberate non-goals

* No demo/replay data in live mode (B3 adds a separately labelled demo source, off by default).
* No new database tables: the live-code buffer is in-memory per backend process (it is a live view, not a log);
  the durable record of iterations stays `workspace_artifacts` + `agent_messages.response_payload`.
* No WebGL/Three.js; the scene is CSS 3D transforms + inline SVG.
