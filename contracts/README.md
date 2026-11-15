# AATM Shared Architecture & API Contracts

The canonical machine-readable contract is [`schemas.json`](schemas.json), currently **v2.1.0**. Workers and integrations should keep their request validation, responses, and generated API documentation aligned with it.

## Shared envelopes and routing

1. **Error Envelope** (4xx/5xx API responses):
   ```json
   {
     "error": {
       "code": "STRING_ENUM",
       "message": "Human-readable description",
       "details": {},
       "request_id": "uuid"
     }
   }
   ```
2. **WebSocket Event Envelope** (`/ws/events`):
   ```json
   {
     "event_id": "uuid",
     "type": "task.updated | agent.heartbeat | browser.snapshot | approval.requested | notification.created | chat.message | qa.completed",
     "timestamp": "ISO-8601 UTC",
     "payload": {}
   }
   ```
3. **API Prefix**: `/api/v1` (plus `/api/health` as the unversioned health probe). The active HTTP routes are published by the backend at `/openapi.json`.

## Canonical enums

- **Agent Status**: `idle | working | paused | error | offline`
- **Task Status**: `backlog | queued | assigned | in_progress | review | blocked | completed | failed | cancelled`
- **Task Priority**: `critical | high | medium | low`
- **Approval Types**: `spend | deploy | merge | delete | credential | security | irreversible`
- **Memory Scopes**: `employee | team | organization | global | project | agent | task | decision`
- **Browser Modes**: `visible | background`
- **Provider Types**: `arena_local_bridge | arena_agent | anthropic_claude`
- **Execution Modes**: `manual | automatic | collaborative`
- **Autonomy Modes**: `supervised | semi_autonomous | highly_autonomous`
- **Allocation Modes**: `one_per_employee | shared | hybrid`
- **Browser Engines**: `playwright | browser_use | openbrowser | automa`
- **Device Statuses**: `online | degraded | offline`
- **Message Types**: `task_assignment | handoff | status_update | direct | group`
- **Languages**: `en | ar | fa`

## Device token revocation and provider usage

- `POST /api/v1/devices/{device_id}/revoke` requires an authenticated user with role `admin` or `operator`. Its response is exactly `{ "id": string, "token_version": integer, "device_token": string }`; the token is version-bound and must be handled as a secret.
- `GET /api/v1/providers/usage` includes `budget_summary` with the six global spend/limit/headroom numbers listed in `schemas.json`, alongside token totals and recent logs.
- `POST /api/v1/providers/{provider_id}/test` accepts optional `agent_id` and `session_id` fields; both may be omitted or null.

## Credential references and approved GitHub writes

- Credential fields accept environment-variable references matching `env:[A-Z_][A-Z0-9_]*`; do not submit literal tokens, API keys, or passwords. Validation errors must not echo submitted credentials.
- GitHub branch, pull-request, and issue write requests include an `approval_id`. Branch creation uses an approved `branch` request; PRs and issues must reference an approved governance request. The backend also enforces the repository allowlist and protected-branch policy before a write.
- `POST /api/v1/github/branches` accepts `approval_id`, `repo`, `branch_name`, and `from_branch`. PR and issue request fields and defaults are defined in the canonical operations schemas.
- `GET /api/v1/traces` is a read-only timeline of persisted audit, provider-usage, workspace-artifact, and agent-message records. It omits free-form instructions, error text, artifact summaries, and audit details to avoid exposing credentials.
