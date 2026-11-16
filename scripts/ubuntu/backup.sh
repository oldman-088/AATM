#!/usr/bin/env bash
# AATM — back up the runtime data and configuration (Ubuntu).
#
# The archive contains the database, the employee workspaces and (when present) the .env
# configuration file. It is written to backups/ which is git-ignored, so credentials are
# never committed. The archive is verified after creation.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$ROOT_DIR/backups"
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/aatm-backup-${STAMP}.tar.gz"

ITEMS=()
[[ -d data ]] && ITEMS+=("data")
[[ -f .env ]] && ITEMS+=(".env")

if [[ ${#ITEMS[@]} -eq 0 ]]; then
  echo "[AATM Backup] ERROR: nothing to back up (no data/ directory and no .env)." >&2
  exit 1
fi

tar -czf "$ARCHIVE" --no-same-owner -C "$ROOT_DIR" "${ITEMS[@]}"

# Verify the archive is readable and contains what we expect.
if ! tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
  echo "[AATM Backup] ERROR: the created archive failed verification." >&2
  rm -f "$ARCHIVE"
  exit 1
fi

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo "[AATM Backup] Created $ARCHIVE ($SIZE, items: ${ITEMS[*]})"
echo "[AATM Backup] Restrict access to this file - it may contain credentials from .env."
chmod 600 "$ARCHIVE"
