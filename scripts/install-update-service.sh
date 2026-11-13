#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER_DIR="/usr/local/lib"

[[ "$EUID" -eq 0 ]] || {
  printf 'Run this script as root.\n' >&2
  exit 1
}
command -v systemctl >/dev/null 2>&1 || {
  printf 'systemctl is required to install the Groovebox update service.\n' >&2
  exit 1
}
[[ -f "$APP_DIR/scripts/groovebox_update.py" ]] || {
  printf 'The Groovebox update helper is missing from %s.\n' "$APP_DIR" >&2
  exit 1
}

mkdir -p "$APP_DIR/data" "$HELPER_DIR"
install -m 0755 "$APP_DIR/scripts/groovebox_update.py" \
  "$HELPER_DIR/groovebox-update.py"
install -m 0644 /dev/null "$APP_DIR/data/.update-helper-installed"
if [[ ! -e "$APP_DIR/data/.update-trigger" ]]; then
  install -m 0644 /dev/null "$APP_DIR/data/.update-trigger"
fi

cat > /etc/systemd/system/groovebox-update.service <<UNIT
[Unit]
Description=Update Groovebox from its latest GitHub release
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
Environment="APP_DIR=$APP_DIR"
ExecStart=/usr/bin/python3 $HELPER_DIR/groovebox-update.py
UNIT

cat > /etc/systemd/system/groovebox-update.path <<UNIT
[Unit]
Description=Watch for Groovebox update requests

[Path]
PathChanged=$APP_DIR/data/.update-trigger
Unit=groovebox-update.service

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now groovebox-update.path
printf 'Groovebox host update service is enabled.\n'
