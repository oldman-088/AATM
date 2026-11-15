# AATM Shared Architecture & API Contracts

All four workers adhere to these canonical contracts:

1. **Error Envelope** (every 4xx/5xx API response):
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
3. **API Prefix**: `/api/v1` (plus `/api/health` unversioned probe).
4. **Canonical Status Enums**:
   - Agent Status: `idle | working | paused | error | offline`
   - Task Status: `backlog | queued | assigned | in_progress | review | blocked | completed | failed | cancelled`
   - Task Priority: `critical | high | medium | low`
   - Approval Types: `spend | deploy | merge | delete | credential | security | irreversible`
   - Memory Scopes: `global | project | agent | task | decision`
   - Browser Modes: `visible | background`
   - Languages: `en | ar | fa`
