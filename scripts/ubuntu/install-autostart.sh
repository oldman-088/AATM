#!/usr/bin/env bash
# Install a per-user service; never needs sudo.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
# systemd expands percent specifiers even in quoted arguments.
escaped=${ROOT_DIR//%/%%}
escaped=${escaped//\\/\\\\}
escaped=${escaped//\"/\\\"}
cat > "$UNIT_DIR/aatm.service" <<UNIT
[Unit]
Description=AATM desktop stack
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory="$escaped"
ExecStart="$escaped/scripts/ubuntu/start-all.sh"
RemainAfterExit=yes

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable aatm.service
printf 'AATM autostart enabled for the next user login.\n'
