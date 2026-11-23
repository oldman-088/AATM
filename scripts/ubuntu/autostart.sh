#!/usr/bin/env bash
# AATM — what the autostart unit runs after a boot or a power cut (E5).
#
# start-all.sh assumes a person is at the machine: the network is up and the stack is expected to
# work now. At boot none of that is guaranteed, so this wrapper waits for connectivity, retries the
# start with a backoff, and then runs the doctor once (report only — it never repairs from here).
# Everything it does is logged to logs/autostart.log, so a boot nobody watched still leaves evidence.
#
#   scripts/ubuntu/autostart.sh          # what systemd runs
#
# Tunables (environment):
#   AATM_AUTOSTART_RETRIES          start attempts, default 5
#   AATM_AUTOSTART_RETRY_DELAY      seconds between attempts, default 30
#   AATM_AUTOSTART_NETWORK_TIMEOUT  seconds to wait for a default route, default 180 (0 = do not wait)
#   AATM_AUTOSTART_PROBE            command that must succeed for "the network is up" (default below)
#   AATM_START_ALL                  the script to run (default: scripts/ubuntu/start-all.sh)
#   AATM_AUTOSTART_LOG              where to log (default: logs/autostart.log)
#
# The consistency check is not repeated here: start-all.sh runs `aatm doctor` once after every
# successful start (E17), including when it is started by this wrapper, and appends it to
# logs/doctor.log. A boot where nothing started has no state worth checking.
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RETRIES="${AATM_AUTOSTART_RETRIES:-5}"
RETRY_DELAY="${AATM_AUTOSTART_RETRY_DELAY:-30}"
NETWORK_TIMEOUT="${AATM_AUTOSTART_NETWORK_TIMEOUT:-180}"
START_ALL="${AATM_START_ALL:-$ROOT_DIR/scripts/ubuntu/start-all.sh}"
LOG_FILE="${AATM_AUTOSTART_LOG:-$ROOT_DIR/logs/autostart.log}"
log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" | tee -a "$LOG_FILE" >/dev/null 2>&1 || true
  printf '%s\n' "$1"
}

PROBE="${AATM_AUTOSTART_PROBE:-}"

have_network() {
  # A default route is the cheapest proof that the machine can reach anything: /proc/net/route is
  # there on every Linux, with 00000000 in the Destination column for the default route. No tools,
  # no DNS, no outbound request, and nothing to spoof. AATM_AUTOSTART_PROBE replaces it (tests).
  if [[ -n "$PROBE" ]]; then
    bash -c "$PROBE"
    return $?
  fi
  [[ -r /proc/net/route ]] && awk 'NR > 1 && $2 == "00000000" { found = 1 } END { exit found ? 0 : 1 }' /proc/net/route
}

wait_for_network() {
  (( NETWORK_TIMEOUT <= 0 )) && return 0
  local waited=0
  while ! have_network; do
    (( waited >= NETWORK_TIMEOUT )) && return 1
    sleep 2
    waited=$((waited + 2))
  done
  return 0
}

if [[ ! -f "$START_ALL" ]]; then
  log "AATM autostart: cannot find $START_ALL — nothing to start."
  exit 1
fi

if ! wait_for_network; then
  # Continue anyway: a link that has not come up yet often comes up during the first attempt, and
  # start-all.sh reports what it could not do instead of waiting silently forever.
  log "AATM autostart: no default route after ${NETWORK_TIMEOUT}s — trying anyway."
fi

attempt=1
while (( attempt <= RETRIES )); do
  log "AATM autostart: attempt $attempt/$RETRIES — $START_ALL"
  if bash "$START_ALL" >>"$LOG_FILE" 2>&1; then
    log "AATM autostart: the stack started on attempt $attempt (start-all.sh reported the doctor pass)."
    exit 0
  fi
  (( attempt >= RETRIES )) && break
  log "AATM autostart: attempt $attempt failed; retrying in ${RETRY_DELAY}s."
  sleep "$RETRY_DELAY"
  attempt=$((attempt + 1))
done

log "AATM autostart: start-all.sh still failing after $RETRIES attempts — leaving it for a person."
log "AATM autostart: look at $LOG_FILE and run: bash $START_ALL"
exit 1
