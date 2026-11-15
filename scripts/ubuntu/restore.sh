#!/usr/bin/env bash
# AATM — restore a backup archive (Ubuntu).
#
# The archive is validated and its members are checked before anything is written, so a
# tampered archive cannot write outside the repository root.
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <path-to-backup.tar.gz>" >&2
  exit 1
fi
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARCHIVE="$1"

if [[ ! -f "$ARCHIVE" ]]; then
  echo "[AATM Restore] ERROR: archive not found: $ARCHIVE" >&2
  exit 1
fi
if ! tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
  echo "[AATM Restore] ERROR: archive is corrupt or not a gzip tarball." >&2
  exit 1
fi

# Reject absolute paths and parent-directory traversal inside the archive.
UNSAFE="$(tar -tzf "$ARCHIVE" | grep -E '(^/|(^|/)\.\.(/|$))' || true)"
if [[ -n "$UNSAFE" ]]; then
  echo "[AATM Restore] ERROR: archive contains unsafe member paths:" >&2
  printf '%s\n' "$UNSAFE" >&2
  exit 1
fi

echo "[AATM Restore] Restoring backup from $ARCHIVE..."
tar -xzf "$ARCHIVE" --no-same-owner -C "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  chmod 600 "$ROOT_DIR/.env"
fi
echo "[AATM Restore] Restored backup from $ARCHIVE"
echo "[AATM Restore] Run scripts/ubuntu/verify.sh to confirm the restored state is healthy."
