#!/usr/bin/env bash
# AATM — install the per-user autostart unit (E5). One command, never needs sudo.
#
# After a reboot or a power cut the stack comes back by itself: the unit waits for the network,
# runs scripts/ubuntu/autostart.sh (which retries start-all.sh and then runs the doctor once), and
# lingers so the user manager starts at boot rather than only at login.
#
#   scripts/ubuntu/install-autostart.sh              # install, enable, and turn linger on if needed
#   scripts/ubuntu/install-autostart.sh --no-linger  # start at login only; leave linger as it is
#   scripts/ubuntu/install-autostart.sh --desktop    # no systemd --user: use the autostart file
#
# Remove it with scripts/ubuntu/uninstall-autostart.sh. Running processes are never stopped by either.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_NAME="aatm.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
LINGER=1
DESKTOP_FALLBACK=0

for arg in "$@"; do
  case "$arg" in
    --no-linger) LINGER=0 ;;
    --desktop) DESKTOP_FALLBACK=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "install-autostart: unknown argument '$arg' (options: --no-linger, --desktop)" >&2; exit 2 ;;
  esac
done

write_desktop_entry() {
  mkdir -p "$DESKTOP_DIR"
  cat > "$DESKTOP_DIR/aatm.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=AATM
Comment=Start the AATM stack after login (autostart fallback, E5)
Exec=$ROOT_DIR/scripts/ubuntu/autostart.sh
Terminal=false
X-GNOME-Autostart-enabled=true
DESKTOP
  printf 'AATM autostart fallback written to %s (starts at graphical login only; no linger, no retries by systemd).\n' \
    "$DESKTOP_DIR/aatm.desktop"
}

systemd_user_available() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user is-system-running >/dev/null 2>&1 && return 0
  systemctl --user daemon-reload >/dev/null 2>&1   # the real test: is a user manager reachable?
}

# systemd runs ExecStart directly, so the script the unit points at has to be executable.
if [[ ! -f "$ROOT_DIR/scripts/ubuntu/autostart.sh" ]]; then
  echo "install-autostart: scripts/ubuntu/autostart.sh is missing — the repo looks incomplete." >&2
  exit 1
fi
if [[ ! -x "$ROOT_DIR/scripts/ubuntu/autostart.sh" ]]; then
  chmod +x "$ROOT_DIR/scripts/ubuntu/autostart.sh"
  echo "install-autostart: made scripts/ubuntu/autostart.sh executable (the unit runs it directly)."
fi

if ! systemd_user_available; then
  echo "install-autostart: no usable systemd --user manager, so $UNIT_NAME was not installed." >&2
  echo "  Either start the user manager (loginctl enable-linger $USER, then: systemctl --user daemon-reload)," >&2
  echo "  or install the desktop-autostart fallback instead:" >&2
  echo "      $0 --desktop" >&2
  if [[ $DESKTOP_FALLBACK -eq 1 ]]; then
    write_desktop_entry
    exit 0
  fi
  exit 3
fi

if [[ $DESKTOP_FALLBACK -eq 1 ]]; then
  write_desktop_entry
  exit 0
fi

mkdir -p "$UNIT_DIR"
# systemd expands percent specifiers even in quoted arguments.
escaped=${ROOT_DIR//%/%%}
escaped=${escaped//\\/\\\\}
escaped=${escaped//\"/\\\"}
cat > "$UNIT_DIR/$UNIT_NAME" <<UNIT
[Unit]
Description=AATM desktop stack (starts after boot or login, waits for the network)
# Intent and ordering for a system manager. The user manager has no network-online.target, so this
# alone would not wait for anything: the real wait for a default route is inside autostart.sh.
Wants=network-online.target
After=network-online.target
# autostart.sh does its own retrying; do not let systemd rate-limit the attempts away.
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory="$escaped"
Environment=AATM_AUTOSTART=1
# Waits for a default route, then runs start-all.sh, which ends with one doctor pass (report only).
ExecStart="$escaped/scripts/ubuntu/autostart.sh"
# A cold start builds nothing but does wait for containers and for readiness; give it room.
TimeoutStartSec=infinity
Restart=no

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"

if systemctl --user is-enabled "$UNIT_NAME" >/dev/null 2>&1; then
  printf 'AATM autostart: %s is enabled (unit %s).\n' "$UNIT_NAME" "$UNIT_DIR/$UNIT_NAME"
else
  echo "install-autostart: WARNING: $UNIT_NAME was written but systemctl --user is-enabled did not confirm it." >&2
  echo "  Check with: systemctl --user status $UNIT_NAME" >&2
fi

# Linger is what makes "after a power cut" real: without it the user manager, and so this unit,
# only starts once somebody logs in.
if command -v loginctl >/dev/null 2>&1; then
  if loginctl show-user "${USER:-$(id -un)}" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
    printf 'AATM autostart: linger is already on, so the stack starts at boot.\n'
  elif [[ $LINGER -eq 1 ]]; then
    if loginctl enable-linger "${USER:-$(id -un)}" >/dev/null 2>&1; then
      printf 'AATM autostart: linger enabled for %s (undo: loginctl disable-linger %s).\n' "${USER:-$(id -un)}" "${USER:-$(id -un)}"
    else
      echo "install-autostart: WARNING: could not enable linger. Run it yourself, or the stack starts at login only:" >&2
      echo "      loginctl enable-linger ${USER:-$(id -un)}" >&2
    fi
  else
    echo "install-autostart: linger left as it was (--no-linger): the stack starts at login, not at boot." >&2
  fi
else
  echo "install-autostart: loginctl not found, so linger was not checked — the unit still starts at login." >&2
fi

printf 'Verify with: systemctl --user cat %s ; systemctl --user start %s ; tail -f %s/logs/autostart.log\n' \
  "$UNIT_NAME" "$UNIT_NAME" "$ROOT_DIR"
