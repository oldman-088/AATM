#!/usr/bin/env bash
# AATM: stop gracefully. Closes only AATM's Chrome windows; containers keep running unless --containers.
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
PY="${AATM_PYTHON:-$ROOT_DIR/.venv/bin/python}"
STOP_SCRIPT="${AATM_STOP_SCRIPT:-$ROOT_DIR/scripts/ubuntu/stop.sh}"
CONTAINERS=0
for arg in "$@"; do
  case "$arg" in
    --containers) CONTAINERS=1 ;;
    -h|--help) sed -n '2p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
status=0
"$PY" "$ROOT_DIR/scripts/ubuntu/aatm_stack.py" close-windows || status=1
"$STOP_SCRIPT" || status=1
if [[ $CONTAINERS -eq 1 ]]; then
  docker stop aatm-redis aatm-rabbitmq >/dev/null || status=1
fi
exit "$status"
