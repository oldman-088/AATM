#!/usr/bin/env bash
set -euo pipefail
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [[ -f "$UNIT_DIR/aatm.service" ]]; then
  systemctl --user disable aatm.service
  rm -- "$UNIT_DIR/aatm.service"
  systemctl --user daemon-reload
fi
printf 'AATM autostart removed; running processes were not stopped.\n'
