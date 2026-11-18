#!/usr/bin/env bash
# AATM — make sure the Redis and RabbitMQ containers are up (Postgres is not used: SQLite).
#
# Called by start.sh. Safe to repeat: containers that are already healthy are left running.
# AATM_INFRA_AUTOSTART=false skips it. A failure is a warning, so the app can still start and the
# watchdog keeps reporting the missing container.
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ "${AATM_INFRA_AUTOSTART:-true}" != "true" ]]; then
  echo "[AATM] Redis/RabbitMQ autostart skipped (AATM_INFRA_AUTOSTART=false)."
  exit 0
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "[AATM] WARNING: docker not found; Redis and RabbitMQ were not started." >&2
  exit 0
fi
echo "[AATM] Starting Redis and RabbitMQ (docker compose)..."
if docker compose --env-file .env up -d --wait --no-recreate redis rabbitmq; then
  echo "[AATM] Redis and RabbitMQ are healthy."
else
  echo "[AATM] WARNING: Redis/RabbitMQ did not become healthy (docker compose up failed)." >&2
fi
exit 0
