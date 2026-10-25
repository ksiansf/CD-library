#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="cd-library"
BACKUP_DIR="/var/backups/groovebox"
CONTAINER_STOPPED=0

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

restart_stopped_container() {
  local status=$?
  trap - ERR
  if [[ "$CONTAINER_STOPPED" -eq 1 ]] && docker inspect "$SERVICE" >/dev/null 2>&1; then
    printf 'Installation failed; attempting to restart the previous container.\n' >&2
    if ! docker start "$SERVICE" >/dev/null; then
      printf 'Warning: the previous container could not be restarted. Check Docker status and logs.\n' >&2
    fi
  fi
  exit "$status"
}
trap restart_stopped_container ERR

[[ "$EUID" -eq 0 ]] || fail "Run this script as root, for example: sudo bash install.sh"

[[ -f "$APP_DIR/compose.yaml" && -f "$APP_DIR/Dockerfile" &&
  -f "$APP_DIR/app.py" && -d "$APP_DIR/static" ]] ||
  fail "Run this script from a complete Groovebox project directory."

[[ -r /etc/os-release ]] || fail "Cannot identify this operating system."
. /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == "12" ]] ||
  fail "This installer currently supports Debian 12 (Bookworm) only."

printf 'Installing Docker Engine and the Compose plugin...\n'
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

printf 'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
  "$VERSION_CODENAME" "$(dpkg --print-architecture)" \
  > /etc/apt/sources.list.d/docker.sources

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker compose version

cd "$APP_DIR"
mkdir -p "$APP_DIR/data"
docker compose config --quiet

shopt -s nullglob dotglob
data_contents=("$APP_DIR"/data/*)
if (( ${#data_contents[@]} > 0 )); then
  running_container="$(docker ps --filter "name=^/${SERVICE}$" --format '{{.ID}}')"
  if [[ -n "$running_container" ]]; then
    printf 'Stopping the existing Groovebox container for a consistent backup...\n'
    docker stop "$running_container" >/dev/null
    CONTAINER_STOPPED=1
  fi

  install -d -m 0700 "$BACKUP_DIR"
  backup_file="$BACKUP_DIR/data-$(date +%F-%H%M%S)-$$.tar.gz"
  tar -czf "$backup_file" -C "$APP_DIR" data
  chmod 0600 "$backup_file"
  printf 'Data backup created: %s\n' "$backup_file"
fi

printf 'Building and starting Groovebox...\n'
docker compose up --build --detach
CONTAINER_STOPPED=0
docker compose ps

printf '\nGroovebox deployment is ready. Open http://<LXC-IP>:8080 from your LAN.\n'
printf 'If CD_LIBRARY_PORT is set in .env, use that host port instead.\n'
if [[ -f "$APP_DIR/.env" ]]; then
  printf 'Existing .env configuration was preserved.\n'
else
  printf 'No .env file was changed or created; Compose defaults are in use.\n'
fi
printf 'When existing data is present, backups are saved under %s; copy them off the LXC.\n' "$BACKUP_DIR"
