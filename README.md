# Groovebox — CD Library & Changer Manager

Groovebox is a self-hosted web application for cataloging a CD collection and
tracking discs in a 101-position changer. It is designed for use on a trusted
home network and can be deployed with Docker Compose.

The application records which CD is assigned to each changer slot (0–100); it
does not control the physical changer. There are no user accounts or built-in
authentication, so restrict access to trusted devices and networks.

## Features

- Add, edit, search, and remove CDs. Unloading a CD keeps it in the library.
- Store album metadata, ordered track lists, and cover art.
- Enter information manually or search MusicBrainz and Discogs, then review
  and fill the entry form from a selected release.
- Search the local CD library by album, artist, or track.
- Open the **Track Library** tab to browse every track across all CDs. Search
  by track title, track artist, album, album artist, or year. The table includes
  artist, album, year, duration, and changer slot.
- View the CD library in Gallery or Detailed view. Hover over a CD or loaded
  slot to see its track list.
- Load and unload CDs, move a loaded CD, or replace the CD assigned to a slot.
  Slot 0 is shown separately in the load picker.
- Choose light or dark theme.
- Store the catalog and uploaded cover art in a persistent SQLite data
  directory.

## Proxmox deployment

### Quick install from the Proxmox host

The Proxmox helper runs on the **Proxmox host shell as root**. It checks for
an existing Groovebox LXC first: when one is found, it updates that container
in place instead of creating another. Only when no existing Groovebox
installation is detected does it create a new Debian 12 LXC, install Docker
and Compose, and start the app.

After the updated `proxmox-install.sh` has been pushed to the `main` branch
of the public GitHub repository, run this command in the Proxmox host shell
as root. You can [view the installer source on GitHub](https://github.com/ksiansf/CD-library/blob/main/proxmox-install.sh).

```sh
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ksiansf/CD-library/main/proxmox-install.sh)"
```

The script prompts for the source branch. For a new install, it also prompts
for container ID, hostname, CPU, memory, disk, web port, network bridge,
root-filesystem storage, and root password. New containers default to
unprivileged with Nesting and keyctl and DHCP; you can choose a privileged
container if needed, but that weakens isolation from the Proxmox host. The
new-install path also offers Discogs credential setup; the consumer key and
secret are visible as you type and saved in `/opt/groovebox/.env`.

When updating, the script stops Groovebox, creates a timestamped backup of
`data/` under `/var/backups/groovebox/` inside the LXC, and updates the app
files. The existing `.env` and `data/` are left in place, preserving Discogs
settings, the collection, artwork, and recorded changer slots. If multiple
Groovebox installations are detected, the script asks which CTID to update.
It does not create a new container when an existing Groovebox is found.

Run remote install scripts only from a repository you trust; this command
downloads and executes the script as root on the Proxmox host.

Run the same Proxmox command again to update an existing Groovebox to the
selected branch. Alternatively, update manually inside the LXC as described
in [Updates](#updates).

### Manual installation

The following walkthrough runs the application’s Docker container inside a
Debian Proxmox LXC container. Docker inside an LXC requires nesting and depends
on the Proxmox kernel, storage driver, and security configuration. If Docker
does not start reliably in your LXC, use the Debian VM alternative below;
Docker-in-a-VM is generally the more predictable Docker deployment.

### 1. Create a Debian LXC in Proxmox

In the Proxmox web interface:

1. Download a current Debian 12 (Bookworm) standard container template from
   **local storage → CT Templates → Templates**.
2. Select **Create CT** and choose a container ID and hostname, for example
   `groovebox`.
3. Set a strong root password, or configure an SSH public key.
4. Select the Debian 12 template.
5. Allocate at least **1 CPU core**, **1 GB RAM**, and **8 GB root disk**.
   Increase disk space if you expect to upload many large cover images.
6. Configure a network interface attached to your LAN bridge. DHCP with a
   router reservation or a static address makes the app easy to find later.
7. Create the container as **unprivileged** unless your Proxmox environment
   has a specific requirement for a privileged container.
8. Before starting it, select the CT and open **Options → Features → Edit**.
   Enable **Nesting**. Enable **keyctl** if it is available in your Proxmox
   version. These options allow Docker to run inside the LXC; they do not
   guarantee every Docker storage driver will work on every host.
9. Start the CT and open its console, or connect using SSH.

Keep the LXC on a trusted LAN. The app has no login screen. Use the Proxmox
firewall and/or the Debian firewall to allow the app port only from devices
that should access the collection.

### 2. Prepare the Debian LXC

The automated installer runs inside a prepared Debian 12 (Bookworm) LXC; it
does not create the LXC on the Proxmox host. It installs Docker Engine and the
Compose plugin from Docker's official Debian repository, then builds and
starts Groovebox. The LXC must have internet access, working DNS, and the
**Nesting** feature enabled. Docker inside LXC depends on Proxmox host kernel
and storage configuration; use the Debian VM alternative below if Docker does
not run reliably in your LXC.

### Troubleshooting LXC creation

If `pct create` fails while extracting the Debian template with an error such
as `tar: /var/lib/lxc/<CTID>/rootfs: Cannot open: Permission denied`, check
the selected root-filesystem storage and its host permissions. The Proxmox
installer lets you select an active storage that supports container root
filesystems. At the installer prompt, pressing Enter for unprivileged
container creation selects **yes**. If that attempt fails before a CT is created, the
installer offers an explicit retry as a privileged container. Accepting this
reduces isolation from the Proxmox host; the installer never changes container
privilege mode without confirmation. If creation also fails in privileged
mode, changing privilege mode is not the cause and will not help.

Also check thin-pool capacity before retrying. Warnings that thin-volume sizes
exceed the pool or that the volume group has no free space can indicate a
Proxmox host storage-capacity problem even when `pvesm status` reports
available thin-provisioned space. Choosing a privileged container will **not**
fix a full thin pool. On the Proxmox host, inspect storage, thin-pool usage,
and the mount permissions:

```sh
pvesm status --content rootdir
lvs -a -o vg_name,lv_name,lv_size,data_percent,metadata_percent
df -h /var/lib/lxc
ls -ld /var/lib/lxc
```

Choose a root-filesystem storage with sufficient real free capacity and
working host permissions, or resolve the host storage issue before retrying.
If a failed attempt may have left a partial CT, inspect its status and config
before reusing its ID:

```sh
pct status <CTID>
ls -l /etc/pve/lxc/<CTID>.conf
```

Do not remove an existing CT or storage volume unless you have confirmed it is
only an abandoned failed install and contains no data you need.

### 3. Copy the application into the LXC

First create the destination directory in the LXC as `root`:

```sh
mkdir -p /opt/groovebox
```

Copy the application files from your development computer to that directory.
Use WinSCP, or run this command from PowerShell (replace the example address
and local path with yours):

```powershell
scp -r "C:\path\to\CD library\app.py" `
  "C:\path\to\CD library\compose.yaml" `
  "C:\path\to\CD library\Dockerfile" `
  "C:\path\to\CD library\requirements.txt" `
  "C:\path\to\CD library\install.sh" `
  "C:\path\to\CD library\static" `
  root@192.168.1.50:/opt/groovebox/
```

The directory copied to `/opt/groovebox` should contain at least:

```text
app.py
compose.yaml
Dockerfile
requirements.txt
install.sh
static/
```

Do not copy your Windows `.venv`, `.git`, `.env`, or local `data/` directory
into the LXC. Keep any existing LXC `.env` and `data/` in place. The Docker
image installs its own Python dependencies; the install script does not
overwrite the LXC's existing settings or collection data.

### 4. Configure Discogs (optional)

Metadata lookup from MusicBrainz does not require an account. Discogs lookup
needs either a consumer key and consumer secret or a personal access token.
To configure Discogs after installation (or if you skipped the credentials
prompt in the Proxmox installer), create/edit the server-side `.env` file next
to `compose.yaml`:

```sh
cd /opt/groovebox
nano .env
```

Example contents:

```dotenv
DISCOGS_CONSUMER_KEY=your_consumer_key
DISCOGS_CONSUMER_SECRET=your_consumer_secret
# Alternatively, use DISCOGS_TOKEN instead of the consumer key and secret.
# DISCOGS_TOKEN=your_personal_access_token
CD_LIBRARY_CONTACT=you@example.com
CD_LIBRARY_PORT=8080
```

Set `CD_LIBRARY_CONTACT` to contact information suitable for the MusicBrainz
API User-Agent. If you do not need Discogs, leave the credential variables
unset or blank. Protect the `.env` file because it contains credentials:

```sh
chmod 600 .env
docker compose up -d
```

Keep `.env` private; do not commit it to source control or share it. The
credentials are passed to the container and are not sent to the browser.
The Proxmox installer accepts consumer keys and secrets containing letters,
digits, underscores, and hyphens; for other credential formats, configure
them manually in `.env`.

### 5. Install and start Groovebox

Connect to the LXC and run the installer as `root`:

```sh
cd /opt/groovebox
bash ./install.sh
```

The script checks that it is running as root in Debian 12 and that the
application files are present. It installs and starts Docker, checks the
Compose configuration, creates `data/` if needed, then builds and launches
the app. On a fresh install, the app starts with an empty catalog. When
re-running it with existing data, the script stops the running app for a
consistent backup, saves the data directory under
`/var/backups/groovebox/`, then rebuilds and starts the app. Existing `.env`
and `data/` contents are preserved.

Check the service status and logs if needed:

```sh
cd /opt/groovebox
docker compose ps
docker compose logs --tail=100 cd-library
```

Open the app from a device on the same network:

```text
http://<LXC-IP>:8080
```

For example, if the LXC address is `192.168.1.50`, open
`http://192.168.1.50:8080`.

`CD_LIBRARY_PORT` in `.env` changes the host-side port. For example, setting
`CD_LIBRARY_PORT=8090` in `.env` publishes the app on
`http://<LXC-IP>:8090`; the application still listens on port `8080` inside
Docker. Run `bash ./install.sh` again to apply changed Compose settings.

If the page is unreachable, check `docker compose ps` and
`docker compose logs --tail=100 cd-library`, confirm the LXC has the expected
LAN address, and verify that TCP port `8080` (or your configured host port) is
allowed by the Proxmox and guest firewalls.

### Debian VM alternative

If Docker fails in the LXC because of kernel, storage, or nesting restrictions,
create a small Debian 12 VM in Proxmox instead. Assign it a LAN address, copy
the project files to `/opt/groovebox`, then follow the configuration and
installation steps above. The installer also works in a Debian 12 VM. The
browser address, Compose configuration, and backup procedure are the same.

## Updates

The catalog database in `data/` stores your CDs, tracks, cover art, and the
changer slot assigned to each CD. That slot assignment is Groovebox's record
of what should be loaded in the physical changer; the application does not
control or inspect the changer itself. Keeping `data/` intact therefore
preserves both your collection and its recorded loaded-slot state.

Before each update, make a backup and stop the service so the database is not
being written during the backup:

```sh
cd /opt/groovebox
docker compose stop
tar -czf /root/groovebox-before-update-$(date +%F-%H%M%S).tar.gz data
```

Copy that archive to storage outside the LXC before proceeding, so it is still
available if the LXC itself has a problem.

Using the transfer method in step 3 above, copy the new application files from
your development computer to `/opt/groovebox`, overwriting only application
files such as `app.py`, `static/`, `Dockerfile`, `requirements.txt`,
`compose.yaml`, and `install.sh`. Do **not** copy your development `data/`,
`.env`, or `.venv/` over the LXC files. Keep the existing `data/` and `.env`;
review any intentional Compose changes before replacing `compose.yaml`.

Run the installer to make a consistent pre-update backup and rebuild:

```sh
cd /opt/groovebox
bash ./install.sh
docker compose logs --tail=100 cd-library
```

When `data/` is non-empty, the script creates a timestamped archive in
`/var/backups/groovebox/` before starting the updated app. Copy that backup
off the LXC before relying on it for disaster recovery. Open Groovebox and
confirm your CDs and recorded loaded-slot assignments are still present. Do
not remove `data/` or run `docker compose down -v` during an update. The
Compose file uses a host bind mount (`./data:/data`), so rebuilding the app
container does not remove the collection. Keep the backup until the updated
app is confirmed working.

If an update fails, stop the service and keep the backup. Restore the previous
application files and, if the update changed the database format, restore the
matching pre-update `data/` backup as well before starting the old version.
Do not restore an old database into a newer app, or use a pre-update database
with a newer app whose data migrations have already changed it, unless that
upgrade path is known to be compatible.

The Compose service uses `restart: unless-stopped`, so Docker will start the
app again when the Docker daemon or LXC restarts.

## Backups and restore

The `data/` directory contains the SQLite database and uploaded cover images.
Back it up regularly. For a consistent manual backup, stop the service, copy
the directory to storage outside the LXC, then start the service again:

```sh
cd /opt/groovebox
docker compose stop
tar -czf /root/groovebox-data-$(date +%F).tar.gz data
docker compose start
```

Copy the resulting archive off the LXC to storage that is also protected by
your regular backup plan. Proxmox LXC backups are useful as an additional
recovery layer, but keep an independent copy of important data.

To restore a data archive, stop the service, preserve the current data
directory, extract the backup into `/opt/groovebox`, and restart:

```sh
cd /opt/groovebox
docker compose stop
mv data "data-before-restore-$(date +%F-%H%M%S)"
tar -xzf /root/groovebox-data-YYYY-MM-DD.tar.gz
docker compose start
```

The archive created above contains the `data/` directory at its top level.
Keep the backup private: it contains the CD catalog and uploaded artwork.

## Security and network access

- Groovebox does **not** provide user accounts, passwords, or built-in
  authentication. Do not expose it directly to the public internet.
- Restrict the published port to trusted devices using the Proxmox firewall,
  your router/firewall, and the Debian guest firewall as appropriate.
- For access while away from home, connect through a VPN. If using a reverse
  proxy, configure authentication and HTTPS there, and do not publish the
  app's unauthenticated port to the internet.
- Store Discogs credentials in the server-side `.env` file only. Restrict file
  permissions and protect backups.

## Metadata providers

- [MusicBrainz API documentation](https://musicbrainz.org/doc/MusicBrainz_API):
  release search and track metadata. HTTPS certificate verification is
  enabled using the `certifi` CA bundle.
- [Discogs API documentation](https://www.discogs.com/developers):
  release search and full release details. Configure Discogs consumer
  credentials or a personal access token as described above.
- Metadata lookup requires internet access from the app host. Local catalog
  management, searching, and changer assignments do not depend on either
  metadata provider being available.

## Run locally

Requires Python 3.10 or newer. From PowerShell in the project directory:

```powershell
& .\.venv\bin\python.exe -m pip install -r requirements.txt
& .\.venv\bin\python.exe app.py
```

Open `http://localhost:8080`. The app reads credentials from `.env`
automatically; environment variables already set in the shell take precedence.
By default, local application data is written to `.\data`. Set `DATA_DIR` to
choose another persistent directory and `PORT` to change the listening port.

## Tests

```powershell
& .\.venv\bin\python.exe -m unittest discover -s tests -v
```

## Project files

- `app.py` — HTTP API, metadata lookups, and SQLite persistence.
- `static/index.html` — web interface.
- `install.sh` — Debian 12 Docker setup, data backup, and app deployment.
- `proxmox-install.sh` — Proxmox host helper to create an LXC and deploy the app.
- `Dockerfile` and `compose.yaml` — container build, service, port, environment,
  and persistent data mount.
- `data/` — runtime database and uploaded artwork; preserve for upgrades and
  include in backups.
