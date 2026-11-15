#!/usr/bin/env bash
# AATM — full verification suite (Ubuntu).
#
# Runs every gate that must pass before a release is considered deliverable:
#   1. Prerequisites
#   2. Database migrations (alembic upgrade head)
#   3. Backend lint        (ruff)
#   4. Backend tests       (pytest, including the E2E verification suite)
#   5. Static secret scan  (no secrets / .env / browser profiles committed)
#   6. Frontend typecheck, unit tests and production build (skipped when node_modules is absent)
#
# Usage:  scripts/ubuntu/verify.sh [--install] [--skip-frontend]
#   --install        install python/npm dependencies before verifying
#   --skip-frontend  skip the frontend gates
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

INSTALL=0
SKIP_FRONTEND=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

FAILURES=0
PASSED=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASSED=$((PASSED + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

run_gate() { # run_gate <label> <command...>
  local label="$1"; shift
  if "$@"; then ok "$label"; else fail "$label"; fi
}

step "1/6 Prerequisites"
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
ok "python3 present ($(python3 --version 2>&1))"
if command -v git >/dev/null 2>&1; then ok "git present"; else fail "git missing"; fi
if command -v node >/dev/null 2>&1; then ok "node present ($(node --version))"; else fail "node missing"; fi

if [[ ! -d .venv ]]; then
  if [[ $INSTALL -eq 1 ]]; then
    step "Installing Python dependencies"
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip >/dev/null
    .venv/bin/pip install -r backend/requirements.txt >/dev/null
    ok "python dependencies installed"
  else
    echo "  .venv missing - run scripts/ubuntu/install.sh first (or pass --install)" >&2
    exit 1
  fi
fi
PY="$ROOT_DIR/.venv/bin/python"

step "2/6 Database migrations"
if (cd backend && "$ROOT_DIR/.venv/bin/alembic" upgrade head); then
  ok "alembic upgrade head"
else
  fail "alembic upgrade head"
fi

step "3/6 Backend lint"
run_gate "ruff check" "$ROOT_DIR/.venv/bin/ruff" check backend

step "4/6 Backend tests"
if (cd backend && "$PY" -m pytest -q); then
  ok "pytest (unit + E2E verification suite)"
else
  fail "pytest"
fi

step "5/6 Static secret scan"
if (cd backend && "$PY" -m app.qa.engine --repo-root "$ROOT_DIR"); then
  ok "secret scan"
else
  fail "secret scan"
fi

if [[ $SKIP_FRONTEND -eq 0 ]]; then
  step "6/6 Frontend gates"
  if [[ ! -d frontend/node_modules ]]; then
    fail "frontend/node_modules missing - run npm install in frontend/ (or use --skip-frontend)"
  else
    run_gate "tsc --noEmit" bash -c "cd frontend && npm run --silent lint"
    run_gate "vitest run" bash -c "cd frontend && npm run --silent test"
    run_gate "vite build" bash -c "cd frontend && npm run --silent build"
  fi
else
  step "6/6 Frontend gates (skipped)"
fi

printf '\n\033[1m==> Verification summary\033[0m\n'
printf '  passed: %d\n  failed: %d\n' "$PASSED" "$FAILURES"
if [[ $FAILURES -gt 0 ]]; then
  printf '  \033[31mVERIFICATION FAILED\033[0m\n'
  exit 1
fi
printf '  \033[32mVERIFICATION PASSED\033[0m\n'
