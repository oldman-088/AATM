#!/usr/bin/env bash
# AATM — remove the per-user autostart unit (E5). One command; never needs sudo.
#
# It removes what install-autostart.sh created: the unit, its enable symlink, and the desktop
# fallback. Running processes are never stopped — the stack keeps running until you stop it with
# scripts/ubuntu/stop-all.sh (a unit of Type=oneshot owns the start, not the services it started).
set -euo pipefail
UNIT_NAME="aatm.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

systemd_user_available() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user is-system-running >/dev/null 2>&1 && return 0
  systemctl --user daemon-reload >/dev/null 2>&1
}

removed=0
if systemd_user_available; then
  # disable first so a stale wants/ symlink cannot bring the unit back on the next boot; it is
  # harmless when the unit was already disabled, and that is the usual case.
  systemctl --user disable "$UNIT_NAME" >/dev/null 2>&1 || true
fi

if [[ -f "$UNIT_DIR/$UNIT_NAME" ]]; then
  rm -- "$UNIT_DIR/$UNIT_NAME"
  removed=1
fi
for wants in "$UNIT_DIR/default.target.wants/$UNIT_NAME"; do
  if [[ -L "$wants" || -e "$wants" ]]; then
    rm -- "$wants"
    removed=1
  fi
done
if [[ -f "$DESKTOP_DIR/aatm.desktop" ]] && grep -q "scripts/ubuntu/autostart.sh" "$DESKTOP_DIR/aatm.desktop"; then
  rm -- "$DESKTOP_DIR/aatm.desktop"
  removed=1
  printf 'AATM autostart: removed the desktop fallback %s\n' "$DESKTOP_DIR/aatm.desktop"
fi

if [[ $removed -eq 0 ]] && ! systemd_user_available; then
  printf 'AATM autostart: nothing to remove (no unit in %s, no desktop entry, no systemd --user manager).\n' "$UNIT_DIR"
  exit 0
fi

if systemd_user_available; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
else
  echo "uninstall-autostart: systemd --user is not reachable, so the files were removed without daemon-reload." >&2
fi

printf 'AATM autostart removed; running processes were not stopped (use scripts/ubuntu/stop-all.sh for that).\n'
printf 'Linger was left as it is: undo it yourself only if nothing else needs it — loginctl disable-linger %s\n' \
  "${USER:-$(id -un)}"
