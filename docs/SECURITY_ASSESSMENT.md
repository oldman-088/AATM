# AATM — Security Assessment

**Assessment Date (UTC):** 2026-11-15  
**Scope:** Authentication, Authorization, Workspace Isolation, Browser Policy, AI Credentials, Prompt-Injection Defense, and Governance.

---

## 1. Security Controls Implemented & Verified
| Control Area | Implementation | Verification |
|---|---|---|
| **Password & Token Security** | PBKDF2-SHA256 (`120,000` iterations, per-user random salt) + HS256 JWT (`backend/app/core/security.py`). | `pytest`: unauthenticated mutations return `401`. |
| **Credential Protection** | Only environment variable references (`env:ANTHROPIC_API_KEY`, `env:ARENA_SESSION_TOKEN`) stored in DB; API responses return masked statuses (`mask_credential_ref`). | Static secret scanner + API response inspection. |
| **Workspace Isolation** | `AgentRuntimeExecutor.resolve_safe_path()` enforces strict `Path.resolve()` containment inside `data/workspaces/<agent_id>/` and an allowlisted tool binary set. | `pytest`: `../../etc/passwd` traversal returns `403`. |
| **Prompt-Injection Defense** | `detect_prompt_injection()` & `sanitize_untrusted_content()` isolate untrusted external content inside `[UNTRUSTED_DATA]` fences before passing to reasoning agents. | `pytest`: injection pattern flagged & quarantined. |
| **Tampermonkey Userscript Policy** | `validate_userscript_policy()` blocks CAPTCHA/anti-bot evasion and cookie/token harvesting patterns at registration time. | `pytest`: prohibited userscript returns `400`. |
| **Protected Branch & High-Risk Governance** | `GitWorkflowManager` blocks merges to `main`, `master`, `production` until an approved `ApprovalRequest` exists in the Approval Center. | `pytest`: protected merge blocked until approved. |
| **Emergency Stop** | `ReplacementAndEmergencyService.emergency_stop()` supports `global`, `employee`, `task`, and `browser` immediate halts with critical audit log entry. | `pytest`: global emergency stop & clear verified. |
| **Static Code Scanning** | `qa_engine.run_security_scan()` scans `.py`, `.ts`, `.tsx` files for hardcoded secret patterns. | `pytest`: `security_scan.status == "passed"`. |
