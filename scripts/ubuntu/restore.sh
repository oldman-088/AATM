#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <path-to-backup.tar.gz>"
  exit 1
fi
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCHIVE="$1"
tar -xzf "$ARCHIVE" -C "$ROOT_DIR"
echo "[AATM Restore] Restored backup from $ARCHIVE"
