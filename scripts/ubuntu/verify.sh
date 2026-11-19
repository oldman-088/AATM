#!/usr/bin/env bash
# AATM — full verification suite (Ubuntu).
#
# Runs every gate that must pass before a release is considered deliverable:
#   1. Prerequisites
#   2. Database migrations (alembic upgrade head)
#   3. Backend & Bridge lint (ruff)
#   4. Backend & Bridge tests (pytest, including E2E verification & Arena Local Bridge suites)
#   5. Static secret scan  (no secrets / .env / browser profiles committed)
#   6. Frontend typecheck, unit tests and production build (fail when node_modules is absent; --install installs it)
#
# Usage:  scripts/ubuntu/verify.sh [--install] [--skip-frontend] [--no-parallel] [--changed]
#   --install        install python/npm dependencies before verifying
#   --skip-frontend  skip the frontend gates
#   --no-parallel    run pytest serially
#   --changed        run related backend test files first, then the full suite
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

INSTALL=0
SKIP_FRONTEND=0
PARALLEL=1
CHANGED=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --no-parallel) PARALLEL=0 ;;
    --changed) CHANGED=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
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
if [[ $INSTALL -eq 1 ]]; then
  "$PY" -m pip install -r scripts/ubuntu/requirements-verify.txt >/dev/null
fi
PYTEST_ARGS=()
if [[ $PARALLEL -eq 1 ]]; then
  if ! "$PY" -c 'import xdist' 2>/dev/null; then
    echo "pytest-xdist missing; run verify.sh --install or use --no-parallel" >&2
    exit 1
  fi
  # Avoid spawning hundreds of workers on large hosts; users can override this.
  export PYTEST_XDIST_AUTO_NUM_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-2}"
  PYTEST_ARGS=(-n auto --dist loadfile)
fi

step "2/6 Database migrations"
if (cd backend && "$ROOT_DIR/.venv/bin/alembic" upgrade head && test "$("$ROOT_DIR/.venv/bin/alembic" heads | wc -l)" -eq 1); then
  ok "alembic upgrade head (single head)"
else
  fail "alembic upgrade head"
fi

step "3/6 Backend & Bridge lint"
run_gate "ruff check backend" "$ROOT_DIR/.venv/bin/ruff" check backend
run_gate "ruff check scripts" "$ROOT_DIR/.venv/bin/ruff" check --config backend/pyproject.toml scripts
run_gate "ruff check bridge" "$ROOT_DIR/.venv/bin/ruff" check --config backend/pyproject.toml bridge

step "4/6 Backend & Bridge tests"
if [[ $CHANGED -eq 1 ]]; then
  # Include committed branch changes, staged, unstaged and untracked files.
  mapfile -t changed_paths < <( { git diff --name-only "$(git merge-base HEAD origin/main)" HEAD; git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u )
  declare -A related=()
  for path in "${changed_paths[@]}"; do
    if [[ $path == backend/tests/test_*.py && -f $path ]]; then
      related["${path#backend/}"]=1
    elif [[ $path == backend/app/*.py ]]; then
      stem="$(basename "$path" .py)"
      for test_file in backend/tests/test_*"$stem"*.py; do
        [[ -f $test_file ]] && related["${test_file#backend/}"]=1
      done
    fi
  done
  if [[ ${#related[@]} -gt 0 ]]; then
    if (cd backend && "$PY" -m pytest -q "${PYTEST_ARGS[@]}" "${!related[@]}"); then
      ok "changed-related backend tests"
    else
      fail "changed-related backend tests"
    fi
  else
    echo "  No related backend test files found; running the full suite."
  fi
fi
if (cd backend && "$PY" -m pytest -q "${PYTEST_ARGS[@]}"); then
  ok "pytest (backend + E2E verification suite)"
else
  fail "pytest (backend)"
fi
if "$PY" -m pytest -v bridge/tests/test_bridge.py; then
  ok "pytest (bridge suite)"
else
  fail "pytest (bridge suite)"
fi

run_gate "pytest (Ubuntu script regressions)" "$PY" -m pytest -q scripts/ubuntu/tests

step "5/6 Static secret scan"
if (cd backend && "$PY" -m app.qa.engine --repo-root "$ROOT_DIR"); then
  ok "secret scan"
else
  fail "secret scan"
fi

if [[ $SKIP_FRONTEND -eq 0 ]]; then
  step "6/6 Frontend gates"
  if [[ ! -d frontend/node_modules && $INSTALL -eq 1 ]]; then
    # A clean copy has no node_modules; --install must cover the frontend too.
    if (cd frontend && if [[ -f package-lock.json ]]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi) >/dev/null; then
      ok "frontend dependencies installed"
    else
      fail "frontend dependency install"
    fi
  fi
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
