#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="$ROOT_DIR/backups"
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/aatm-backup-${STAMP}.tar.gz"

tar -czf "$ARCHIVE" -C "$ROOT_DIR" data .env 2>/dev/null || tar -czf "$ARCHIVE" -C "$ROOT_DIR" data
echo "[AATM Backup] Created $ARCHIVE"
