# Changelog

All notable changes to AATM are documented in this file.

## [2.5.0-phase1] - 2026-11-15 — Phase 1: Security Hardening

### Added
- `backend/app/core/rate_limit.py`: Sliding-window `LoginRateLimiter` (5 failed attempts / 5 min per IP + email) with `429 Too Many Requests` + `Retry-After` and failed-login audit logs.
- `backend/app/models/user.py` & `backend/alembic/versions/9a4e3d2c1b0f_add_user_must_change_password.py`: `User.must_change_password` flag (set on newly seeded admin) and `POST /api/v1/auth/change-password` endpoint.
- `docker/sandbox.Dockerfile`: Non-root (`UID 10001`) sandbox image with `python`, `pytest`, and `ruff`.
- `docker/worker.Dockerfile`: Non-root (`UID 65534`) worker image installing `backend/requirements.txt`.
- `backend/tests/test_phase1_security.py`: Parametrized route enumeration (`app.routes`) verifying `401` on all non-public endpoints, RBAC `viewer` `403` enforcement on writes, `/ws/events` JWT auth + timeout rejection, `/git/status?repo_path=` 403 allowlist, production settings validator checks, login rate-limiting/auditing, forced password rotation, and docker sandbox isolation.

### Changed
- Router-level `Depends(get_current_user)` added across `governance`, `orchestration`, `projects`, `infra`, and `platform` routers, with explicit public allowlist (`POST /api/v1/auth/login`, `GET /api/health`, HMAC-verified `POST /api/v1/github/webhooks`, and device-token routes in `devices.py`), and `require_role("admin", "operator")` on write actions.
- `/ws/events` WebSocket requires a valid JWT (`?token=` or first-message `{token}` within 5s) before attaching to `event_bus`.
- `GET /api/v1/git/status?repo_path=` restricted to `REPO_ROOT` or registered `Project.repo_path` records (`403` otherwise).
- `Settings` defaults hardened: `HOST=127.0.0.1`, `CORS_ORIGINS=http://localhost:5173`, `AATM_SANDBOX_MODE=docker`, plus startup `validate_runtime_security()` blocking weak/default secrets or un-exempted `process` sandbox in `ENVIRONMENT=production`.
- `docker-compose.yml` binds Postgres to `127.0.0.1`, removes the `aatm_password` fallback, and builds `worker` from `docker/worker.Dockerfile`.
- `scripts/ubuntu/install.sh` and `scripts/windows/install.ps1` generate CSPRNG `SECRET_KEY`, `DEFAULT_ADMIN_PASSWORD`, `ARENA_BRIDGE_TOKEN`, and `POSTGRES_PASSWORD` on first install and print the initial admin password once.
- `frontend/src/App.tsx` & `AuthContext.tsx`: Removed pre-filled admin credentials, gated telemetry & `/ws/events` (`{token}` handshake) on authentication, and added a localized forced password-change gate when `must_change_password` is true.
