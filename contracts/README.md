# AATM Shared Architecture & API Contracts

The canonical machine-readable contract is [`schemas.json`](schemas.json), currently **v2.0.0**. Workers and integrations should keep their request validation, responses, and generated API documentation aligned with it.

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

## Credential references and approved GitHub writes

- Credential fields accept environment-variable references matching `env:[A-Z_][A-Z0-9_]*`; do not submit literal tokens, API keys, or passwords. Validation errors must not echo submitted credentials.
- `GitHubPRCreateRequest` and `GitHubIssueCreateRequest` include an `approval_id`. Before performing a write, an integration must verify that the referenced governance approval exists and is approved.
