#!/usr/bin/env bash
# AATM: tag r8-aatm, write a local backup and add a line to SNAPSHOTS.md.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "${AATM_PYTHON:-$ROOT_DIR/.venv/bin/python}" "$ROOT_DIR/scripts/ubuntu/aatm_snapshot.py" snapshot
