#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

for name in backend frontend; do
  PID_FILE="$ROOT_DIR/logs/${name}.pid"
  if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID" || true
      echo "[AATM] Stopped $name (PID $PID)"
    fi
    rm -f "$PID_FILE"
  fi
done
