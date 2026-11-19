#!/usr/bin/env bash
# AATM: start everything with one command (containers, services, Arena windows) and report readiness.
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
PY="${AATM_PYTHON:-$ROOT_DIR/.venv/bin/python}"
START_SCRIPT="${AATM_START_SCRIPT:-$ROOT_DIR/scripts/ubuntu/start.sh}"

ensure_container() {
  local name="$1" state
  state="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)"
  if [[ "$state" != "true" ]]; then
    echo "[AATM] تشغيل الحاوية $name..."
    docker start "$name" >/dev/null || echo "[AATM] تحذير: تعذّر تشغيل $name." >&2
  fi
}

if command -v docker >/dev/null 2>&1; then
  ensure_container aatm-redis
  ensure_container aatm-rabbitmq
else
  echo "[AATM] تحذير: docker غير موجود؛ لم يُشغَّل Redis ولا RabbitMQ." >&2
fi

"$START_SCRIPT" || { echo "[AATM] خطأ: لم تبدأ الخدمات." >&2; "$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" wait-ready; exit 1; }
"$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" open-windows || true
exec "$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" wait-ready
