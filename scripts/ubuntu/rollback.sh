#!/usr/bin/env bash
# AATM: restore r8-aatm to a snapshot tag with a new commit. Usage: rollback.sh snapshot/r8-YYYYMMDD-NN
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ $# -eq 1 ]] || { echo "Usage: $0 snapshot/r8-YYYYMMDD-NN" >&2; exit 2; }
exec "${AATM_PYTHON:-$ROOT_DIR/.venv/bin/python}" "$ROOT_DIR/scripts/ubuntu/aatm_snapshot.py" rollback "$1"
