#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP="Groovebox"
DEFAULT_CORES=1
DEFAULT_MEMORY=1024
DEFAULT_DISK=8
DEFAULT_PORT=8080
TEMP_DIR="$(mktemp -d)"
CT_CREATED=0

cleanup() {
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

on_error() {
  local status=$?
  printf 'Installation failed at line %s (exit code %s).\n' "${BASH_LINENO[0]:-unknown}" "$status" >&2
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

[[ "$EUID" -eq 0 ]] || fail "Run this script in the Proxmox host shell as root."
[[ -r /etc/pve/.version ]] || fail "This does not appear to be a Proxmox VE host."

for command in pct pvesh pveam pvesm curl tar awk sort; do
  require_command "$command"
done

printf '\n%s Proxmox LXC installer\n' "$APP"
printf 'This creates a new Debian 12 LXC and installs Docker Compose and Groovebox.\n'
printf 'It does not modify existing containers.\n\n'

while true; do
  read -r -p "Public GitHub repository URL containing Groovebox (https://github.com/owner/repo): " REPO_URL
  if [[ "$REPO_URL" =~ ^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(\.git)?/?$ ]]; then
    REPO_OWNER="${BASH_REMATCH[1]}"
    REPO_NAME="${BASH_REMATCH[2]}"
    break
  fi
  printf 'Enter a GitHub HTTPS repository URL, for example https://github.com/example/groovebox.\n' >&2
done

BRANCH="$(prompt_value 'Git branch to install' 'main')"
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ && "$BRANCH" != /* && "$BRANCH" != *..* ]] ||
  fail "Invalid branch name."

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
    read -r -s -p "Discogs consumer key (input hidden): " DISCOGS_KEY
    printf '\n'
    [[ "$DISCOGS_KEY" =~ ^[A-Za-z0-9_-]+$ ]] && break
    printf 'The consumer key must be non-empty and contain only letters, digits, underscore, or hyphen.\n' >&2
  done
  while true; do
    read -r -s -p "Discogs consumer secret (input hidden): " DISCOGS_SECRET
    printf '\n'
    [[ "$DISCOGS_SECRET" =~ ^[A-Za-z0-9_-]+$ ]] && break
    printf 'The consumer secret must be non-empty and contain only letters, digits, underscore, or hyphen.\n' >&2
  done
fi

pveam update
TEMPLATE="$(pveam available --section system | awk '$1 == "system" && $2 ~ /^debian-12-standard_/ {print $2}' | sort -V | tail -n 1)"
[[ -n "$TEMPLATE" ]] || fail "No Debian 12 standard template is available from the configured Proxmox repositories."

TEMPLATE_STORAGE="$(pvesm status --content vztmpl | awk 'NR > 1 && $3 == "active" {print $1; exit}')"
ROOTFS_STORAGE="$(pvesm status --content rootdir | awk 'NR > 1 && $3 == "active" {print $1; exit}')"
[[ -n "$TEMPLATE_STORAGE" ]] || fail "No active storage accepts container templates (vztmpl)."
[[ -n "$ROOTFS_STORAGE" ]] || fail "No active storage accepts container root filesystems (rootdir)."

printf '\nDownloading Debian template %s...\n' "$TEMPLATE"
pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"

printf 'Downloading Groovebox source from %s (%s)...\n' "$REPO_URL" "$BRANCH"
ARCHIVE_URL="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${BRANCH}"
curl --fail --location --silent --show-error --retry 3 "$ARCHIVE_URL" -o "$TEMP_DIR/source.tar.gz" ||
  fail "Could not download the GitHub source archive. Check that the repository and branch exist and are public."
mkdir "$TEMP_DIR/source"
tar -xzf "$TEMP_DIR/source.tar.gz" --strip-components=1 -C "$TEMP_DIR/source"
[[ -f "$TEMP_DIR/source/compose.yaml" && -f "$TEMP_DIR/source/Dockerfile" &&
  -f "$TEMP_DIR/source/app.py" && -d "$TEMP_DIR/source/static" ]] ||
  fail "The selected GitHub branch does not contain a complete Groovebox project at its root."

tar -czf "$TEMP_DIR/groovebox.tar.gz" \
  --exclude='./.git' --exclude='./.env' --exclude='./data' --exclude='./.venv' \
  -C "$TEMP_DIR/source" .

read -r -s -p "Set a root password for the new container: " ROOT_PASSWORD
printf '\n'
[[ -n "$ROOT_PASSWORD" ]] || fail "A non-empty root password is required."
read -r -s -p "Confirm root password: " ROOT_PASSWORD_CONFIRM
printf '\n'
[[ "$ROOT_PASSWORD" == "$ROOT_PASSWORD_CONFIRM" ]] || fail "Passwords do not match."
unset ROOT_PASSWORD_CONFIRM

printf '\nCreating Debian LXC %s...\n' "$CTID"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$HOSTNAME" \
  --ostype debian \
  --cores "$CORES" \
  --memory "$MEMORY" \
  --rootfs "${ROOTFS_STORAGE}:${DISK}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
  --unprivileged 1 \
  --features nesting=1,keyctl=1 \
  --onboot 1
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

IP_ADDRESS="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
printf '\n%s installation completed.\n' "$APP"
printf 'Container ID: %s\nHostname: %s\n' "$CTID" "$HOSTNAME"
printf 'Address: http://%s:%s\n' "${IP_ADDRESS:-<LXC-IP>}" "$PORT"
if [[ "$CONFIGURE_DISCOGS" =~ ^[Yy]$ ]]; then
  printf 'Discogs consumer credentials were saved to /opt/groovebox/.env with owner-only permissions.\n'
else
  printf 'Discogs credentials were skipped; you can add them later in /opt/groovebox/.env.\n'
fi
printf 'The app has no built-in login. Restrict this port to trusted network devices.\n'
printf 'Future app updates: copy the new source into /opt/groovebox in this LXC, back up data/, then run bash /opt/groovebox/install.sh.\n'
