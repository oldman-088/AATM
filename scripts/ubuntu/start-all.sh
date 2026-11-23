#!/usr/bin/env bash
# AATM: start everything with one command (containers, services, Arena windows) and report readiness.
#
# After a successful start it runs `aatm doctor` once (E17): report only, never --fix, and never
# fatal — a finding is something the owner should see, not a reason to call the start a failure.
# AATM_DOCTOR_ON_START=0 skips that pass; AATM_DOCTOR_ON_START_LOG=0 also skips logs/doctor.log.
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

run_doctor() { # report-only consistency check; never changes the exit status of the start
  [[ "${AATM_DOCTOR_ON_START:-1}" == "0" ]] && return 0
  local doctor_py="$PY"
  [[ -x "$doctor_py" ]] || doctor_py="$(command -v python3 || true)"
  [[ -n "$doctor_py" ]] || { echo "[AATM] تحذير: لا Python لتشغيل aatm doctor." >&2; return 0; }
  local -a log_opt=()
  [[ "${AATM_DOCTOR_ON_START_LOG:-1}" == "0" ]] || log_opt=(--log)
  echo "[AATM] فحص ما بعد التشغيل: aatm doctor (تقرير فقط، بلا إصلاح)..."
  "$doctor_py" "$ROOT_DIR/scripts/ubuntu/aatm_doctor.py" "${log_opt[@]}" || true
}

if command -v docker >/dev/null 2>&1; then
  ensure_container aatm-redis
  ensure_container aatm-rabbitmq
else
  echo "[AATM] تحذير: docker غير موجود؛ لم يُشغَّل Redis ولا RabbitMQ." >&2
fi

"$START_SCRIPT" || { echo "[AATM] خطأ: لم تبدأ الخدمات." >&2; "$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" wait-ready; run_doctor; exit 1; }
"$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" open-windows || true
status=0
"$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" wait-ready || status=$?
run_doctor
exit "$status"
