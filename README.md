# Groovebox — CD Library & Changer Manager

Groovebox is a web application for cataloging CDs, searching album/artist/track metadata, and recording which discs are loaded in a 101-slot changer (slots 0–100). The changer overview shows all occupied and empty positions. It does not control the physical changer.

## Run with Docker Compose

On a host with Docker Compose installed:

1. Copy this project to the host.
2. Set `DISCOGS_CONSUMER_KEY` and `DISCOGS_CONSUMER_SECRET` to the values from your Discogs developer settings. Alternatively, set `DISCOGS_TOKEN` if you have a personal access token. Set `CD_LIBRARY_PORT` to a desired host port if needed.
3. Start the application:

   ```sh
   docker compose up --build -d
   ```

4. Open `http://<server-ip>:8080` (or the configured `CD_LIBRARY_PORT`) from a device on your network.

Compose stores the SQLite database and embedded cover images in `./data`, which is mounted into the container at `/data`. Keep this directory when upgrading or recreating the container. To back it up consistently, stop the service, copy `data`, then start it again:

```sh
docker compose stop
# Copy the data directory to your backup location.
docker compose start
```

To restore, stop the service, replace `data` with the backed-up directory, then start the service. Protect backups because they contain your catalog and uploaded artwork.

### Proxmox and network access

This container can run on a Proxmox host that supports Docker/Compose, commonly inside a Linux VM or LXC configured for Docker. Restrict the published port to your trusted LAN using the host/Proxmox firewall. The app does not provide user accounts or authentication; do not expose it directly to the public internet. If remote access beyond your LAN is needed, use a VPN or a trusted reverse proxy that enforces authentication.

### Metadata providers

- MusicBrainz searches use its public API. Network access is needed only for lookup. HTTPS uses the `certifi` CA bundle with certificate verification enabled. Set `CD_LIBRARY_CONTACT` to contact information in the API User-Agent, as requested by MusicBrainz.
- Discogs lookups accept your consumer key and consumer secret (`DISCOGS_CONSUMER_KEY` and `DISCOGS_CONSUMER_SECRET`) or a personal access token (`DISCOGS_TOKEN`). Keep both values in the server-side `.env`; Compose passes them to the app, and they are never sent to the browser. The app fetches full release details and track listings after a result is selected.
- Provider failures do not affect local catalog, search, or changer functions.

For a Compose `.env` file beside `compose.yaml`, use:

```dotenv
DISCOGS_CONSUMER_KEY=your_consumer_key
DISCOGS_CONSUMER_SECRET=your_consumer_secret
CD_LIBRARY_CONTACT=you@example.com
CD_LIBRARY_PORT=8080
```

Do not commit `.env` or share the credentials. Set `CD_LIBRARY_CONTACT` to an appropriate contact value for the MusicBrainz API User-Agent.

## Run locally

Requires Python 3.10 or newer. Install the dependencies into the project virtual environment before starting. The app reads credentials from `.env` automatically; environment variables already set in the shell take precedence:

```powershell
& .\.venv\bin\python.exe -m pip install -r requirements.txt
& .\.venv\bin\python.exe app.py
```

Open `http://localhost:8080`. By default, application data is written to `.\data`. Set `DATA_DIR` to choose another persistent directory. Set `PORT` to change the listening port.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Included capabilities

- Add, edit, search, and remove CD records with track lists and cover art.
- Search MusicBrainz and Discogs and review release results before saving.
- Search the local collection by album, artist, or track.
- Assign, move, and unload CDs in slots 0–100 while enforcing unique assignments.
- Gallery and detailed CD views, plus a searchable Track Library table showing track and album artists, album, year, duration, and changer slot; persisted light/dark theme selection.
- Persistent SQLite storage suitable for container-volume backups.
