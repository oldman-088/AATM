#!/usr/bin/env bash
# AATM end-to-end demo (Phase 4 acceptance scenario) against a throwaway GitHub repository and a
# local Ollama model:
#   Arabic directive -> manager splits tasks -> workers run in parallel in isolated worktrees ->
#   draft PRs -> AI review -> human-approved merge -> project completed with summary and cost.
#
# Usage:
#   scripts/demo_e2e.sh --check                         # prerequisites only, no side effects
#   scripts/demo_e2e.sh --repo OWNER/NAME [options]     # use an existing THROWAWAY repository
#   scripts/demo_e2e.sh --create-repo [options]         # create a new PRIVATE throwaway repo with gh
#
# Options:
#   --model NAME        Ollama model (default: $DEMO_MODEL or qwen2.5-coder:7b)
#   --auto-approve      approve merge requests from this script (you act as the human approver);
#                       without it the script waits for you to approve in the Approval Center
#   --timeout MIN       overall timeout in minutes (default 60)
#   --port N            backend port for the isolated demo instance (default 8765)
#   --yes               do not ask for confirmation before writing to the repository
#   --keep              keep the temporary work directory (database, clone, logs)
#
# Safety: the demo runs an isolated backend (temporary SQLite DB, data dirs, random admin password
# and SECRET_KEY) and never touches your normal AATM data. It pushes branches, opens PRs and merges
# them ONLY in the repository you name or create. It never deletes the repository: delete it
# yourself with `gh repo delete OWNER/NAME` when you are done. Execution uses
# AATM_SANDBOX_MODE=process for the demo (the docker sandbox image is not required).
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MODE=""
REPO=""
MODEL="${DEMO_MODEL:-qwen2.5-coder:7b}"
AUTO_APPROVE=0
TIMEOUT_MIN=60
PORT=8765
ASSUME_YES=0
KEEP=0
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

usage() { sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "[demo] ERROR: $*" >&2; exit 1; }
log() { echo "[demo] $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE="check" ;;
    --repo) MODE="${MODE:-run}"; REPO="${2:-}"; shift ;;
    --create-repo) MODE="${MODE:-create}" ;;
    --model) MODEL="${2:-}"; shift ;;
    --auto-approve) AUTO_APPROVE=1 ;;
    --timeout) TIMEOUT_MIN="${2:-}"; shift ;;
    --port) PORT="${2:-}"; shift ;;
    --yes) ASSUME_YES=1 ;;
    --keep) KEEP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done
[[ -n "$MODE" ]] || { usage >&2; die "choose --check, --repo OWNER/NAME or --create-repo"; }
[[ "$TIMEOUT_MIN" =~ ^[0-9]+$ && "$PORT" =~ ^[0-9]+$ ]] || die "--timeout and --port must be numbers"
if [[ "$MODE" == "run" ]]; then
  [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "--repo must be OWNER/NAME"
fi

# ----------------------------------------------------------------------------- prerequisites
check_prereqs() {
  local failed=0
  local item
  for item in git curl jq gh; do
    if command -v "$item" >/dev/null 2>&1; then log "ok: $item"; else log "MISSING: $item (sudo apt install $item)"; failed=1; fi
  done
  if [[ -x "$ROOT_DIR/.venv/bin/uvicorn" ]]; then log "ok: .venv"; else log "MISSING: .venv (run scripts/ubuntu/install.sh)"; failed=1; fi
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then log "ok: gh authenticated"; else log "MISSING: gh login (run: gh auth login)"; failed=1; fi
  if command -v ollama >/dev/null 2>&1; then log "ok: ollama binary"; else log "MISSING: ollama (see https://ollama.com/download)"; failed=1; fi
  local tags
  if tags="$(curl -fsS -m 3 "$OLLAMA_URL/api/tags" 2>/dev/null)"; then
    log "ok: Ollama server at $OLLAMA_URL"
    if jq -e --arg m "$MODEL" '[.models[]?.name] | index($m) != null' <<<"$tags" >/dev/null; then
      log "ok: model $MODEL is pulled"
    else
      log "MISSING: model $MODEL (run: ollama pull $MODEL)"; failed=1
    fi
  else
    log "MISSING: Ollama server not reachable at $OLLAMA_URL (run: ollama serve)"; failed=1
  fi
  return $failed
}

if [[ "$MODE" == "check" ]]; then
  if check_prereqs; then log "All prerequisites present."; exit 0; fi
  log "Prerequisites missing (see above)."; exit 2
fi
check_prereqs || die "prerequisites missing; run with --check for details"

# ----------------------------------------------------------------------------- repository
if [[ "$MODE" == "create" ]]; then
  REPO="$(gh api user --jq .login)/aatm-demo-$(date +%Y%m%d-%H%M%S)"
fi
if [[ $ASSUME_YES -ne 1 ]]; then
  if [[ "$MODE" == "create" ]]; then
    echo "This will CREATE the private GitHub repository $REPO, push branches, open PRs and merge them."
  else
    echo "This will push branches, open draft PRs and (after approval) merge them into $REPO."
  fi
  read -r -p "Continue? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || die "aborted by user"
fi
if [[ "$MODE" == "create" ]]; then
  gh repo create "$REPO" --private --add-readme --description "AATM end-to-end demo (throwaway)" >/dev/null
  log "created private repository $REPO"
fi
gh repo view "$REPO" >/dev/null 2>&1 || die "repository $REPO not found or not accessible"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/aatm-demo.XXXXXX")"
BACKEND_PID=""
cleanup() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then kill "$BACKEND_PID" 2>/dev/null || true; wait "$BACKEND_PID" 2>/dev/null || true; fi
  if [[ $KEEP -eq 1 ]]; then log "kept work directory: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT

gh repo clone "$REPO" "$WORK/repo" -- --quiet
DEFAULT_BRANCH="$(git -C "$WORK/repo" symbolic-ref --short HEAD 2>/dev/null || echo main)"

# ----------------------------------------------------------------------------- isolated backend
ADMIN_EMAIL="demo-admin@aatm.local"
ADMIN_PASSWORD="$(openssl rand -hex 16)"
NEW_PASSWORD="$(openssl rand -hex 16)"
export DATABASE_URL="sqlite+aiosqlite:///$WORK/aatm-demo.db"
export SYNC_DATABASE_URL="sqlite:///$WORK/aatm-demo.db"
export DATA_DIR="$WORK/data" WORKSPACES_DIR="$WORK/data/workspaces" BROWSER_PROFILES_DIR="$WORK/data/profiles"
export DEFAULT_ADMIN_EMAIL="$ADMIN_EMAIL" DEFAULT_ADMIN_PASSWORD="$ADMIN_PASSWORD"
export SECRET_KEY="$(openssl rand -hex 32)"
export AATM_SANDBOX_MODE=process AGENT_LOOP_ENABLED=true
export OLLAMA_API_KEY="ollama"   # Ollama ignores the key; AATM requires an env: reference
export QUEUE_BACKEND=memory REDIS_URL="" RABBITMQ_URL=""
(cd "$ROOT_DIR/backend" && "$ROOT_DIR/.venv/bin/alembic" upgrade head >"$WORK/alembic.log" 2>&1) || die "migrations failed (see $WORK/alembic.log)"
"$ROOT_DIR/.venv/bin/uvicorn" app.main:app --app-dir "$ROOT_DIR/backend" --host 127.0.0.1 --port "$PORT" >"$WORK/backend.log" 2>&1 &
BACKEND_PID=$!
BASE="http://127.0.0.1:$PORT/api/v1"
for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null || die "backend did not start (see $WORK/backend.log)"
log "isolated backend on 127.0.0.1:$PORT (logs: $WORK/backend.log)"

TOKEN=""
api() {  # api METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  local args=(-fsS -X "$method" "$BASE$path" -H "Content-Type: application/json")
  [[ -n "$TOKEN" ]] && args+=(-H "Authorization: Bearer $TOKEN")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}"
}
login() { TOKEN="$(api POST /auth/login "$(jq -nc --arg e "$ADMIN_EMAIL" --arg p "$1" '{email:$e,password:$p}')" | jq -r .access_token)"; }
login "$ADMIN_PASSWORD"
api POST /auth/change-password "$(jq -nc --arg c "$ADMIN_PASSWORD" --arg n "$NEW_PASSWORD" '{current_password:$c,new_password:$n}')" >/dev/null
login "$NEW_PASSWORD"
(umask 077; printf '%s\n' "$NEW_PASSWORD" >"$WORK/admin-password.txt")

# ----------------------------------------------------------------------------- provider, team, project
api POST /providers "$(jq -nc --arg m "$MODEL" --arg u "$OLLAMA_URL/v1" \
  '{id:"ollama-demo",name:"Ollama (demo)",provider_type:"openai_compatible",endpoint_url:$u,credential_ref:"env:OLLAMA_API_KEY",default_model:$m,available_models:[$m],priority:1,daily_limit_usd:1}')" >/dev/null
for agent in $(api GET /agents | jq -r '.[] | select(.role=="manager" or .role=="worker" or .role=="reviewer") | .id'); do
  api PATCH "/agents/$agent" "$(jq -nc --arg m "$MODEL" '{provider_id:"ollama-demo",model_name:$m}')" >/dev/null
done
PROJECT_ID="$(api POST /projects "$(jq -nc --arg p "$WORK/repo" --arg u "https://github.com/$REPO" --arg b "$DEFAULT_BRANCH" \
  '{name:"AATM demo",repo_path:$p,repo_url:$u,default_branch:$b,worktree_mode:true,auto_pr:true}')" | jq -r .id)"
log "project $PROJECT_ID linked to $REPO ($DEFAULT_BRANCH)"

DIRECTIVE='أنشئ وحدة حاسبة صغيرة بلغة بايثون: (1) ملف calculator.py فيه الدوال add و subtract مع اختبارات pytest في test_calculator.py، (2) ملف strings_util.py فيه الدالة reverse_words مع اختبارات pytest في test_strings_util.py. كل مهمة في طلب دمج مستقل.'
api POST /chat "$(jq -nc --arg p "$PROJECT_ID" --arg c "$DIRECTIVE" '{project_id:$p,content:$c}')" >/dev/null
log "Arabic directive sent to the manager; waiting for tasks, PRs and reviews (timeout ${TIMEOUT_MIN}m)..."

# ----------------------------------------------------------------------------- follow the workflow
DEADLINE=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
LAST=""
while :; do
  [[ $(date +%s) -lt $DEADLINE ]] || die "timed out (see $WORK/backend.log; rerun with --keep to inspect)"
  TIMELINE="$(api GET "/projects/$PROJECT_ID/timeline")"
  STATE="$(jq -r '[.steps[] | "\(.id)=\(.state)"] | join(" ")' <<<"$TIMELINE")"
  [[ "$STATE" != "$LAST" ]] && { log "timeline: $STATE"; LAST="$STATE"; }
  if [[ "$(jq -r '.steps[-1].state' <<<"$TIMELINE")" == "done" ]]; then break; fi

  PRS="$(api GET "/reviews/pull-requests?project_id=$PROJECT_ID")"
  for task_id in $(jq -r '.[] | select((.status|IN("merged","done"))|not) | select([.reviews[]? | select(.status=="completed")] | length > 0) | .task_id' <<<"$PRS"); do
    pending="$(jq -r --arg t "$task_id" '.[] | select(.task_id==$t) | [.merge_approvals[]? | select(.status=="pending") | .id][0] // empty' <<<"$PRS")"
    approved="$(jq -r --arg t "$task_id" '.[] | select(.task_id==$t) | [.merge_approvals[]? | select(.status=="approved") | .id][0] // empty' <<<"$PRS")"
    if [[ -z "$pending" && -z "$approved" ]]; then
      pending="$(api POST "/reviews/tasks/$task_id/request-merge" | jq -r '.approval_id')"
      log "merge approval requested for task $task_id ($pending)"
    fi
    if [[ -n "$pending" && $AUTO_APPROVE -eq 1 ]]; then
      api POST "/approvals/$pending/decide" '{"decision":"approved","review_note":"approved by demo_e2e.sh --auto-approve"}' >/dev/null
      log "approved merge $pending (--auto-approve)"
    elif [[ -n "$pending" ]]; then
      log "waiting for you to approve merge $pending in the Approval Center (http://127.0.0.1:$PORT, user $ADMIN_EMAIL; password in $WORK/admin-password.txt)"
    fi
  done
  sleep 10
done

COMPLETION="$(api GET "/projects/$PROJECT_ID/completion")"
log "PROJECT COMPLETED"
jq . <<<"$COMPLETION"
log "Merged PRs are in https://github.com/$REPO/pulls?q=is%3Apr+is%3Amerged"
log "When finished, delete the throwaway repository yourself: gh repo delete $REPO"
