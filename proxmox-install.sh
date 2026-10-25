#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP="Groovebox"
GITHUB_OWNER="ksiansf"
GITHUB_REPO="CD-library"
DEFAULT_CORES=1
DEFAULT_MEMORY=1024
DEFAULT_DISK=8
DEFAULT_PORT=8080
TEMP_DIR="$(mktemp -d)"
CT_CREATED=0
UPDATE_MODE=0
UPDATE_SERVICE_STOPPED=0

cleanup() {
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

on_error() {
  local status=$?
  printf 'Installation failed at line %s (exit code %s).\n' "${BASH_LINENO[0]:-unknown}" "$status" >&2
  if [[ "$UPDATE_MODE" -eq 1 && "$UPDATE_SERVICE_STOPPED" -eq 1 ]]; then
    printf 'Attempting to start the existing Groovebox service after the failed update...\n' >&2
    if ! pct exec "$CTID" -- bash -c 'cd /opt/groovebox && docker compose up -d'; then
      printf 'Warning: automatic service restart failed. Inspect CT %s and its Docker logs.\n' "$CTID" >&2
    fi
  fi
  if [[ "$CT_CREATED" -eq 1 ]]; then
    printf 'The partially installed container was left in place (CTID %s) to avoid deleting data.\n' "$CTID" >&2
    printf 'Inspect it with: pct status %s\n' "$CTID" >&2
  fi
  exit "$status"
}
trap on_error ERR

fail() {
  printf 'Error: %s\n' "$*" >&2
  if [[ "$CT_CREATED" -eq 1 ]]; then
    printf 'The partially installed container was left in place (CTID %s) to avoid deleting data.\n' "$CTID" >&2
    printf 'Inspect it with: pct status %s\n' "$CTID" >&2
  fi
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

prompt_value() {
  local prompt="$1" default="$2" value
  read -r -p "$prompt [$default]: " value
  printf '%s' "${value:-$default}"
}

find_groovebox_containers() {
  local id status config hostname description
  local -a ids=()
  mapfile -t ids < <(pct list | awk 'NR > 1 {print $1}')
  FOUND_CTIDS=()

  for id in "${ids[@]}"; do
    status="$(pct status "$id" 2>/dev/null | awk '{print $2}')"
    if [[ "$status" == "running" ]]; then
      if pct exec "$id" -- test -f /opt/groovebox/compose.yaml 2>/dev/null &&
        pct exec "$id" -- test -f /opt/groovebox/app.py 2>/dev/null; then
        FOUND_CTIDS+=("$id")
      fi
      continue
    fi

    config="$(pct config "$id" 2>/dev/null || true)"
    hostname="$(printf '%s\n' "$config" | awk -F': ' '$1 == "hostname" {print $2; exit}')"
    description="$(printf '%s\n' "$config" | awk -F': ' '$1 == "description" {print $2; exit}')"
    if [[ "$hostname" == "groovebox" || "$description" == "Groovebox CD Library managed installation" ]]; then
      FOUND_CTIDS+=("$id")
    fi
  done
}

[[ "$EUID" -eq 0 ]] || fail "Run this script in the Proxmox host shell as root."
[[ -r /etc/pve/.version ]] || fail "This does not appear to be a Proxmox VE host."

for command in pct pvesh pveam pvesm curl tar awk sort grep; do
  require_command "$command"
done

printf '\n%s Proxmox LXC installer\n' "$APP"
printf 'This updates an existing Groovebox LXC when found; otherwise it creates a new Debian 12 LXC.\n\n'

BRANCH="$(prompt_value 'Git branch to install' 'main')"
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ && "$BRANCH" != /* && "$BRANCH" != *..* ]] ||
  fail "Invalid branch name."

find_groovebox_containers
if (( ${#FOUND_CTIDS[@]} > 1 )); then
  printf 'Found multiple Groovebox containers: %s\n' "${FOUND_CTIDS[*]}"
  CTID="$(prompt_value 'Container ID to update' '')"
  printf '%s\n' "${FOUND_CTIDS[@]}" | grep -Fxq "$CTID" ||
    fail "Select one of the detected Groovebox container IDs."
  UPDATE_MODE=1
elif (( ${#FOUND_CTIDS[@]} == 1 )); then
  CTID="${FOUND_CTIDS[0]}"
  UPDATE_MODE=1
fi

if [[ "$UPDATE_MODE" -eq 1 ]]; then
  printf 'Found existing Groovebox container %s. Updating it in place; no new container will be created.\n' "$CTID"
else
  NEXT_ID="$(pvesh get /cluster/nextid)"
  CTID="$(prompt_value 'Container ID' "$NEXT_ID")"
  [[ "$CTID" =~ ^[0-9]{3,6}$ ]] || fail "Container ID must contain 3 to 6 digits."
  pct status "$CTID" >/dev/null 2>&1 && fail "Container ID $CTID is already in use."

  HOSTNAME="$(prompt_value 'Container hostname' 'groovebox')"
  [[ "$HOSTNAME" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || fail "Invalid hostname."
  CORES="$(prompt_value 'CPU cores' "$DEFAULT_CORES")"
  MEMORY="$(prompt_value 'Memory in MiB' "$DEFAULT_MEMORY")"
  DISK="$(prompt_value 'Root disk size in GiB' "$DEFAULT_DISK")"
  PORT="$(prompt_value 'Web access port' "$DEFAULT_PORT")"
  BRIDGE="$(prompt_value 'Network bridge' 'vmbr0')"
  [[ "$CORES" =~ ^[1-9][0-9]*$ ]] || fail "CPU cores must be a positive integer."
  [[ "$MEMORY" =~ ^[1-9][0-9]*$ ]] || fail "Memory must be a positive integer."
  [[ "$DISK" =~ ^[1-9][0-9]*$ ]] || fail "Disk size must be a positive integer."
  [[ "$PORT" =~ ^[0-9]{1,5}$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail "Port must be between 1 and 65535."
  [[ "$BRIDGE" =~ ^[A-Za-z0-9_.-]+$ ]] || fail "Invalid network bridge name."

  DISCOGS_KEY=""
  DISCOGS_SECRET=""
  read -r -p "Configure Discogs credentials now? [y/N]: " CONFIGURE_DISCOGS
  if [[ "$CONFIGURE_DISCOGS" =~ ^[Yy]$ ]]; then
    while true; do
      read -r -p "Discogs consumer key: " DISCOGS_KEY
      [[ "$DISCOGS_KEY" =~ ^[A-Za-z0-9_-]+$ ]] && break
      printf 'The consumer key must be non-empty and contain only letters, digits, underscore, or hyphen.\n' >&2
    done
    while true; do
      read -r -p "Discogs consumer secret: " DISCOGS_SECRET
      [[ "$DISCOGS_SECRET" =~ ^[A-Za-z0-9_-]+$ ]] && break
      printf 'The consumer secret must be non-empty and contain only letters, digits, underscore, or hyphen.\n' >&2
    done
  fi
fi

printf 'Downloading Groovebox source from https://github.com/%s/%s (%s)...\n' "$GITHUB_OWNER" "$GITHUB_REPO" "$BRANCH"
ARCHIVE_URL="https://codeload.github.com/${GITHUB_OWNER}/${GITHUB_REPO}/tar.gz/refs/heads/${BRANCH}"
curl --fail --location --silent --show-error --retry 3 "$ARCHIVE_URL" -o "$TEMP_DIR/source.tar.gz" ||
  fail "Could not download the GitHub source archive. Check that https://github.com/${GITHUB_OWNER}/${GITHUB_REPO} is public and branch '${BRANCH}' exists."
mkdir "$TEMP_DIR/source"
tar -xzf "$TEMP_DIR/source.tar.gz" --strip-components=1 -C "$TEMP_DIR/source"
[[ -f "$TEMP_DIR/source/compose.yaml" && -f "$TEMP_DIR/source/Dockerfile" &&
  -f "$TEMP_DIR/source/app.py" && -d "$TEMP_DIR/source/static" ]] ||
  fail "The selected GitHub branch does not contain a complete Groovebox project at its root."

tar -czf "$TEMP_DIR/groovebox.tar.gz" \
  --exclude='./.git' --exclude='./.env' --exclude='./data' --exclude='./.venv' \
  -C "$TEMP_DIR/source" .

if [[ "$UPDATE_MODE" -eq 1 ]]; then
  if [[ "$(pct status "$CTID" | awk '{print $2}')" != "running" ]]; then
    printf 'Starting existing Groovebox container %s for update...\n' "$CTID"
    pct start "$CTID"
  fi
  pct exec "$CTID" -- bash -c 'test -f /opt/groovebox/compose.yaml && test -f /opt/groovebox/app.py' ||
    fail "CT $CTID does not contain a Groovebox installation at /opt/groovebox."

  printf 'Stopping Groovebox and creating a pre-update data backup...\n'
  UPDATE_SERVICE_STOPPED=1
  pct exec "$CTID" -- bash -c '
    set -e
    cd /opt/groovebox
    mkdir -p data
    docker compose stop
    install -d -m 0700 /var/backups/groovebox
    backup="/var/backups/groovebox/data-$(date +%F-%H%M%S)-$$.tar.gz"
    tar -czf "$backup" data
    chmod 0600 "$backup"
    printf "%s\n" "$backup"
  ' > "$TEMP_DIR/backup-path.txt"
  BACKUP_PATH="$(tail -n 1 "$TEMP_DIR/backup-path.txt")"
  printf 'Pre-update data backup: CT %s:%s\n' "$CTID" "$BACKUP_PATH"

  pct push "$CTID" "$TEMP_DIR/groovebox.tar.gz" /root/groovebox-update.tar.gz
  pct exec "$CTID" -- bash -c '
    set -e
    tar -xzf /root/groovebox-update.tar.gz -C /opt/groovebox
    rm /root/groovebox-update.tar.gz
    cd /opt/groovebox
    bash ./install.sh
  '
  UPDATE_SERVICE_STOPPED=0
  PORT="$(pct exec "$CTID" -- bash -c 'sed -n "s/^CD_LIBRARY_PORT=//p" /opt/groovebox/.env | head -n 1')"
  PORT="${PORT:-8080}"
  printf 'Updated existing Groovebox container %s; its .env and data/ were preserved.\n' "$CTID"
else
  pveam update
  TEMPLATE="$(pveam available --section system | awk '$1 == "system" && $2 ~ /^debian-12-standard_/ {print $2}' | sort -V | tail -n 1)"
  [[ -n "$TEMPLATE" ]] || fail "No Debian 12 standard template is available from the configured Proxmox repositories."

  TEMPLATE_STORAGE="$(pvesm status --content vztmpl | awk 'NR > 1 && $3 == "active" {print $1; exit}')"
  ROOTFS_STORAGE="$(pvesm status --content rootdir | awk 'NR > 1 && $3 == "active" {print $1; exit}')"
  [[ -n "$TEMPLATE_STORAGE" ]] || fail "No active storage accepts container templates (vztmpl)."
  [[ -n "$ROOTFS_STORAGE" ]] || fail "No active storage accepts container root filesystems (rootdir)."

  mapfile -t ROOTFS_STORAGES < <(pvesm status --content rootdir | awk 'NR > 1 && $3 == "active" {print $1}')
  printf '\nActive storage options for the container root filesystem:\n'
  pvesm status --content rootdir
  ROOTFS_STORAGE="$(prompt_value 'Root filesystem storage ID' "$ROOTFS_STORAGE")"
  printf '%s\n' "${ROOTFS_STORAGES[@]}" | grep -Fxq "$ROOTFS_STORAGE" ||
    fail "Storage '$ROOTFS_STORAGE' is not active or does not support container root filesystems."

  UNPRIVILEGED=1
  read -r -p "Create an unprivileged container? [Y/n]: " UNPRIVILEGED_ANSWER
  if [[ "$UNPRIVILEGED_ANSWER" =~ ^[Nn]$ ]]; then
    UNPRIVILEGED=0
    printf 'Warning: privileged containers provide weaker isolation from the Proxmox host.\n' >&2
  fi

  printf '\nDownloading Debian template %s...\n' "$TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"

  read -r -s -p "Set a root password for the new container: " ROOT_PASSWORD
  printf '\n'
  [[ -n "$ROOT_PASSWORD" ]] || fail "A non-empty root password is required."
  read -r -s -p "Confirm root password: " ROOT_PASSWORD_CONFIRM
  printf '\n'
  [[ "$ROOT_PASSWORD" == "$ROOT_PASSWORD_CONFIRM" ]] || fail "Passwords do not match."
  unset ROOT_PASSWORD_CONFIRM

  printf '\nCreating Debian LXC %s...\n' "$CTID"
  create_container() {
    pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
      --hostname "$HOSTNAME" \
      --description "Groovebox CD Library managed installation" \
      --ostype debian \
      --cores "$CORES" \
      --memory "$MEMORY" \
      --rootfs "${ROOTFS_STORAGE}:${DISK}" \
      --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
      --unprivileged "$1" \
      --features nesting=1,keyctl=1 \
      --onboot 1
  }

  if ! create_container "$UNPRIVILEGED"; then
    if pct status "$CTID" >/dev/null 2>&1; then
      CT_CREATED=1
      fail "Proxmox reported an error creating CT $CTID, but the CT still exists. Inspect it before retrying."
    fi
    if [[ -e "/etc/pve/lxc/${CTID}.conf" ]]; then
      fail "Proxmox could not create CT $CTID, and a container configuration remains at /etc/pve/lxc/${CTID}.conf. Inspect it before retrying."
    fi

    if [[ "$UNPRIVILEGED" -eq 1 ]]; then
      printf '\nUnprivileged container creation failed before CT %s was created.\n' "$CTID" >&2
      printf 'A rootfs extraction permission error can indicate this host/storage cannot create the mapped unprivileged rootfs.\n' >&2
      read -r -p "Retry as a privileged container? This weakens host isolation. [y/N]: " RETRY_PRIVILEGED
      if [[ "$RETRY_PRIVILEGED" =~ ^[Yy]$ ]]; then
        UNPRIVILEGED=0
        printf 'Retrying CT %s as privileged; review the isolation warning before continuing.\n' "$CTID" >&2
        if ! create_container "$UNPRIVILEGED"; then
          if pct status "$CTID" >/dev/null 2>&1; then
            CT_CREATED=1
            fail "Proxmox reported an error creating CT $CTID, but the CT still exists. Inspect it before retrying."
          fi
          fail "Proxmox could not create CT $CTID, even as a privileged container. Check thin-pool capacity and host rootfs permissions; see the installer troubleshooting section in the README."
        fi
      else
        fail "Proxmox could not create the unprivileged CT. No privileged retry was made. Check thin-pool capacity and host rootfs permissions, then retry."
      fi
    else
      fail "Proxmox could not create CT $CTID. Check thin-pool capacity and host rootfs permissions; see the installer troubleshooting section in the README."
    fi
  fi
  CT_CREATED=1
  pct start "$CTID"

  printf 'Setting the container root password...\n'
  printf 'root:%s\n' "$ROOT_PASSWORD" | pct exec "$CTID" -- chpasswd
  unset ROOT_PASSWORD

  printf 'Copying application files into the container...\n'
  pct exec "$CTID" -- mkdir -p /opt/groovebox
  pct push "$CTID" "$TEMP_DIR/groovebox.tar.gz" /root/groovebox.tar.gz
  {
    printf 'CD_LIBRARY_PORT=%s\n' "$PORT"
    if [[ -n "$DISCOGS_KEY" ]]; then
      printf 'DISCOGS_CONSUMER_KEY=%s\n' "$DISCOGS_KEY"
      printf 'DISCOGS_CONSUMER_SECRET=%s\n' "$DISCOGS_SECRET"
    fi
  } > "$TEMP_DIR/groovebox.env"
  unset DISCOGS_KEY DISCOGS_SECRET
  chmod 600 "$TEMP_DIR/groovebox.env"
  pct push "$CTID" "$TEMP_DIR/groovebox.env" /root/groovebox.env
  pct exec "$CTID" -- bash -c 'tar -xzf /root/groovebox.tar.gz -C /opt/groovebox && mv /root/groovebox.env /opt/groovebox/.env && chmod 600 /opt/groovebox/.env && rm /root/groovebox.tar.gz'

  cat > "$TEMP_DIR/install-in-container.sh" <<'INNER_SCRIPT'
#!/usr/bin/env bash
set -Eeuo pipefail

. /etc/os-release
[[ "$ID" == "debian" && "$VERSION_ID" == "12" ]] || {
  echo "Expected Debian 12 inside the new container." >&2
  exit 1
}

apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
  "$VERSION_CODENAME" "$(dpkg --print-architecture)" \
  > /etc/apt/sources.list.d/docker.sources
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
cd /opt/groovebox
mkdir -p data
docker compose config --quiet
docker compose up --build --detach
docker compose ps
INNER_SCRIPT

  pct push "$CTID" "$TEMP_DIR/install-in-container.sh" /root/install-groovebox.sh
  printf 'Installing Docker and starting Groovebox inside the container...\n'
  pct exec "$CTID" -- bash /root/install-groovebox.sh
  pct exec "$CTID" -- rm /root/install-groovebox.sh
fi

IP_ADDRESS="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
printf '\n%s installation completed.\n' "$APP"
printf 'Container ID: %s\n' "$CTID"
if [[ "$UPDATE_MODE" -eq 0 ]]; then
  printf 'Hostname: %s\n' "$HOSTNAME"
fi
printf 'Address: http://%s:%s\n' "${IP_ADDRESS:-<LXC-IP>}" "$PORT"
if [[ "$UPDATE_MODE" -eq 0 && "$CONFIGURE_DISCOGS" =~ ^[Yy]$ ]]; then
  printf 'Discogs consumer credentials were saved to /opt/groovebox/.env with owner-only permissions.\n'
elif [[ "$UPDATE_MODE" -eq 0 ]]; then
  printf 'Discogs credentials were skipped; you can add them later in /opt/groovebox/.env.\n'
else
  printf 'Existing .env configuration and collection data were preserved.\n'
fi
printf 'The app has no built-in login. Restrict this port to trusted network devices.\n'
printf 'Future app updates: copy the new source into /opt/groovebox in this LXC, back up data/, then run bash /opt/groovebox/install.sh.\n'
