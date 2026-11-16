# Changelog

All notable changes to AATM are documented in this file.

## [2.5.1-phase1b] - 2026-11-16 — Phase 1B: Security Review Follow-Up Fixes

### Added
- `Settings.TRUSTED_PROXIES` (default empty) and CIDR/IP-aware `X-Forwarded-For` validation in `POST /api/v1/auth/login`, plus an independent per-email sliding-window rate limit so rotating `X-Forwarded-For` still triggers `429`.
- `User.token_version` (`backend/alembic/versions/c2d4e6f8a1b3_add_user_token_version_and_flag_default_passwords.py`), embedded in JWT claims, verified in `get_current_user`, `get_current_user_optional`, and `/ws/events`, and incremented on password change to revoke older tokens immediately.
- Startup and migration detection (`c2d4e6f8a1b3` + `seed_initial_data`) flagging existing accounts whose password matches known default/placeholder passwords with `must_change_password=True`.
- `get_sandbox_status()` in `backend/app/runtime/sandbox.py`, surfaced in `GET /api/health`, `GET /api/v1/dashboard/overview`, and the localized Dashboard UI (`SANDBOX_UNAVAILABLE` warning banner when Docker or `aatm-sandbox:latest` is missing).

### Changed
- Enforced `must_change_password` server-side in `get_current_user` (`403 {"code": "PASSWORD_CHANGE_REQUIRED"}` on all endpoints except `GET /api/v1/auth/me` and `POST /api/v1/auth/change-password`) and rejected unrotated accounts (`4403`) in `/ws/events`.
- Hardened `build_isolated_command` with `--pull=never` and default `docker` mode (no silent fallback to `process`), and updated `scripts/ubuntu/{install,start,start-worker}.sh` and `scripts/windows/{install,start,start-worker}.ps1` to build `aatm-sandbox:latest` from `docker/sandbox.Dockerfile` when Docker is available.
- Configured `docker-compose.yml` `worker` service with `AATM_SANDBOX_MODE=process` and `AATM_ALLOW_PROCESS_SANDBOX=true` (treating the non-root `read_only`, `cap_drop=ALL` container as the sandbox without mounting `docker.sock`).
- Restricted `/ws/events` authentication strictly to the first WebSocket message (`?token=` query strings are ignored).
- Cleaned up `POST /api/v1/auth/change-password` validation to reject any placeholder/default password and any password equal to the user's email.
- Expanded `backend/tests/test_phase1_security.py` with full all-method route enumeration, `lifespan` task tests, and regression tests for all 8 security fixes.

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
