import base64
import binascii
from contextlib import contextmanager
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import certifi
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024
ALLOWED_COVER_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
USER_AGENT = (
    "CDLibrary/1.0 "
    f"({os.environ.get('CD_LIBRARY_CONTACT', 'personal collection manager')})"
)
_musicbrainz_lock = threading.Lock()
_musicbrainz_last_request = 0.0


@contextmanager
def connect_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "cd-library.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_db():
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                catalog_number TEXT NOT NULL DEFAULT '',
                release_id TEXT NOT NULL DEFAULT '',
                metadata_source TEXT NOT NULL DEFAULT '',
                cover_data TEXT NOT NULL DEFAULT '',
                cover_mime TEXT NOT NULL DEFAULT '',
                cover_url TEXT NOT NULL DEFAULT '',
                slot INTEGER UNIQUE CHECK (slot IS NULL OR slot BETWEEN 0 AND 100),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cd_id INTEGER NOT NULL REFERENCES cds(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                duration TEXT NOT NULL DEFAULT '',
                UNIQUE(cd_id, position)
            );
            """
        )


def parse_tracks(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("Track list must be valid JSON.") from error
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Track list must be a list.")
    tracks = []
    for index, track in enumerate(value, 1):
        if not isinstance(track, dict):
            raise ValueError("Each track must be an object.")
        title = str(track.get("title", "")).strip()
        if title:
            tracks.append({
                "position": index,
                "title": title[:300],
                "artist": str(track.get("artist", "")).strip()[:300],
                "duration": str(track.get("duration", "")).strip()[:50],
            })
    return tracks


def parse_cover(payload):
    encoded = str(payload.get("cover_data", "") or "")
    mime = str(payload.get("cover_mime", "") or "").lower()
    if not encoded:
        return "", ""
    if mime not in ALLOWED_COVER_TYPES:
        raise ValueError("Cover image must be JPEG, PNG, WebP, or GIF.")
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Cover image data is invalid.") from error
    if not binary or len(binary) > MAX_COVER_BYTES:
        raise ValueError("Cover image must be smaller than 5 MB.")
    return base64.b64encode(binary).decode("ascii"), mime


def clean_text(payload, key, limit=300):
    return str(payload.get(key, "") or "").strip()[:limit]


def cd_payload(row, tracks):
    result = dict(row)
    result["tracks"] = [dict(track) for track in tracks]
    result["cover"] = f"/api/cds/{row['id']}/cover" if row["cover_data"] else row["cover_url"]
    result.pop("cover_data", None)
    result.pop("cover_mime", None)
    return result


def get_cd(db, cd_id):
    row = db.execute("SELECT * FROM cds WHERE id = ?", (cd_id,)).fetchone()
    if row is None:
        return None
    tracks = db.execute(
        "SELECT position, title, artist, duration FROM tracks WHERE cd_id = ? ORDER BY position",
        (cd_id,),
    ).fetchall()
    return cd_payload(row, tracks)


def save_cd(payload, cd_id=None):
    title = clean_text(payload, "title")
    if not title:
        raise ValueError("Album title is required.")
    tracks = parse_tracks(payload.get("tracks", []))
    cover_data, cover_mime = parse_cover(payload)
    cover_url = clean_text(payload, "cover_url", 1000) if not cover_data else ""
    fields = (
        title,
        clean_text(payload, "artist"),
        clean_text(payload, "year", 20),
        clean_text(payload, "label"),
        clean_text(payload, "catalog_number", 100),
        clean_text(payload, "release_id", 120),
        clean_text(payload, "metadata_source", 40),
    )
    with connect_db() as db:
        if cd_id is None:
            cur = db.execute(
                """INSERT INTO cds
                (title, artist, year, label, catalog_number, release_id, metadata_source,
                 cover_data, cover_mime, cover_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*fields, cover_data, cover_mime, cover_url),
            )
            cd_id = cur.lastrowid
        else:
            existing = db.execute("SELECT * FROM cds WHERE id = ?", (cd_id,)).fetchone()
            if existing is None:
                return None
            next_cover_data = cover_data or existing["cover_data"]
            next_cover_mime = cover_mime or existing["cover_mime"]
            next_cover_url = cover_url or ("" if cover_data else existing["cover_url"])
            db.execute(
                """UPDATE cds SET title=?, artist=?, year=?, label=?, catalog_number=?,
                release_id=?, metadata_source=?, cover_data=?, cover_mime=?, cover_url=?
                WHERE id=?""",
                (*fields, next_cover_data, next_cover_mime, next_cover_url, cd_id),
            )
            db.execute("DELETE FROM tracks WHERE cd_id = ?", (cd_id,))
        db.executemany(
            "INSERT INTO tracks (cd_id, position, title, artist, duration) VALUES (?, ?, ?, ?, ?)",
            [(cd_id, track["position"], track["title"], track["artist"], track["duration"])
             for track in tracks],
        )
        return get_cd(db, cd_id)


def api_request(url, headers=None):
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    tls_context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=12, context=tls_context) as response:
        return json.loads(response.read().decode("utf-8"))


def musicbrainz_search(title="", artist="", year="", query=""):
    if query:
        search_query = query
    else:
        criteria = []
        if title:
            criteria.append(f'release:"{title.replace(chr(34), chr(92) + chr(34))}"')
        if artist:
            criteria.append(f'artist:"{artist.replace(chr(34), chr(92) + chr(34))}"')
        if year:
            criteria.append(f'date:{year}')
        search_query = " AND ".join(criteria)
    url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode(
        {"query": search_query, "fmt": "json", "limit": 15}
    )
    data = musicbrainz_request(url)
    results = []
    for release in data.get("releases", []):
        results.append({
            "source": "MusicBrainz",
            "release_id": release.get("id", ""),
            "title": release.get("title", ""),
            "artist": musicbrainz_artists(release.get("artist-credit", [])),
            "year": (release.get("date", "") or "")[:4],
            "label": "",
            "catalog_number": "",
            "tracks": [],
            "cover_url": f"https://coverartarchive.org/release/{release.get('id')}/front-250"
            if release.get("id") else "",
        })
    return results


def musicbrainz_artists(credits):
    return ", ".join(
        item.get("artist", {}).get("name", item.get("name", ""))
        for item in credits
        if isinstance(item, dict)
    )


def musicbrainz_request(url):
    global _musicbrainz_last_request
    with _musicbrainz_lock:
        delay = 1.0 - (time.monotonic() - _musicbrainz_last_request)
        if delay > 0:
            time.sleep(delay)
        _musicbrainz_last_request = time.monotonic()
        return api_request(url)


def musicbrainz_release(release_id):
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", release_id):
        raise ValueError("MusicBrainz release ID is invalid.")
    url = f"https://musicbrainz.org/ws/2/release/{release_id}?inc=artists+recordings+labels&fmt=json"
    release = musicbrainz_request(url)
    tracks = []
    for medium in release.get("media", []):
        for track in medium.get("tracks", []):
            recording = track.get("recording", {})
            tracks.append({
                "title": track.get("title") or recording.get("title", ""),
                "artist": musicbrainz_artists(track.get("artist-credit", [])),
                "duration": track.get("length", recording.get("length", "")),
            })
    return {
        "source": "MusicBrainz",
        "release_id": release.get("id", release_id),
        "title": release.get("title", ""),
        "artist": musicbrainz_artists(release.get("artist-credit", [])),
        "year": (release.get("date", "") or "")[:4],
        "label": ", ".join(
            item.get("label", {}).get("name", "")
            for item in release.get("label-info", [])
            if item.get("label")
        ),
        "catalog_number": ", ".join(
            item.get("catalog-number", "")
            for item in release.get("label-info", [])
            if item.get("catalog-number")
        ),
        "tracks": tracks,
        "cover_url": f"https://coverartarchive.org/release/{release_id}/front-250",
    }


def discogs_search(title="", artist="", year="", query=""):
    params = {"type": "release", "per_page": 15}
    if title:
        params["release_title"] = title
    if artist:
        params["artist"] = artist
    if year:
        params["year"] = year
    if query and not title and not artist and not year:
        params["q"] = query
    data = discogs_request(
        "/database/search",
        params,
    )
    results = data.get("results", [])
    if not results and artist and not title and not query:
        fallback_params = {
            "q": artist,
            "type": "release",
            "per_page": 15,
        }
        if year:
            fallback_params["year"] = year
        data = discogs_request("/database/search", fallback_params)
    return [
        {
            "source": "Discogs",
            "release_id": str(release.get("id", "")),
            "title": release.get("title", ""),
            "artist": "",
            "year": str(release.get("year", "") or ""),
            "label": ", ".join(release.get("label", []) or []),
            "catalog_number": "",
            "tracks": [],
            "cover_url": release.get("cover_image", "") or release.get("thumb", ""),
        }
        for release in data.get("results", [])
    ]


def discogs_request(endpoint, params=None):
    token = os.environ.get("DISCOGS_TOKEN", "").strip()
    key = os.environ.get("DISCOGS_CONSUMER_KEY", "").strip()
    secret = os.environ.get("DISCOGS_CONSUMER_SECRET", "").strip()
    if token:
        headers = {"Authorization": f"Discogs token={token}"}
    elif key and secret:
        params = {**(params or {}), "key": key, "secret": secret}
        headers = {}
    elif key or secret:
        raise RuntimeError(
            "Set both DISCOGS_CONSUMER_KEY and DISCOGS_CONSUMER_SECRET."
        )
    else:
        raise RuntimeError(
            "Discogs lookup needs credentials. Set both DISCOGS_CONSUMER_KEY and "
            "DISCOGS_CONSUMER_SECRET, or set DISCOGS_TOKEN."
        )
    url = f"https://api.discogs.com{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return api_request(url, headers)


def discogs_release(release_id):
    if not release_id.isdigit():
        raise ValueError("Discogs release ID must be numeric.")
    data = discogs_request(f"/releases/{release_id}")
    return {
        "source": "Discogs",
        "release_id": str(data.get("id", release_id)),
        "title": data.get("title", ""),
        "artist": ", ".join(item.get("name", "") for item in data.get("artists", [])),
        "year": str(data.get("year", "") or ""),
        "label": ", ".join(item.get("name", "") for item in data.get("labels", [])),
        "catalog_number": ", ".join(
            item.get("catno", "")
            for item in data.get("labels", [])
            if item.get("catno")
        ),
        "tracks": [
            {
                "title": track.get("title", ""),
                "artist": ", ".join(item.get("name", "") for item in track.get("artists", [])),
                "duration": track.get("duration", ""),
            }
            for track in data.get("tracklist", [])
            if track.get("type_", "track") == "track"
        ],
        "cover_url": data.get("images", [{}])[0].get("uri", "")
        if data.get("images") else "",
    }


class CdLibraryHandler(BaseHTTPRequestHandler):
    server_version = "CDLibrary/1.0"

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args))

    def send_bytes(self, content, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload, status=200):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(content, "application/json; charset=utf-8", status)

    def send_error_json(self, message, status=400):
        self.send_json({"error": message}, status)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid request size.") from error
        if size < 0 or size > MAX_REQUEST_BYTES:
            raise ValueError("Request is too large.")
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must be valid JSON.") from error
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            try:
                content = (APP_DIR / "static" / "index.html").read_bytes()
                self.send_bytes(content, "text/html; charset=utf-8")
            except OSError as error:
                self.send_error_json(f"Could not read application page: {error}", 500)
            return
        if path == "/favicon.svg":
            try:
                content = (APP_DIR / "static" / "favicon.svg").read_bytes()
                self.send_bytes(content, "image/svg+xml")
            except OSError as error:
                self.send_error_json(f"Could not read page icon: {error}", 500)
            return
        if path == "/media/PD-F958.png":
            try:
                content = (APP_DIR / "media" / "PD-F958.png").read_bytes()
                self.send_bytes(content, "image/png")
            except OSError as error:
                self.send_error_json(f"Could not read CD changer image: {error}", 500)
            return
        if path == "/api/cds":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            with connect_db() as db:
                rows = db.execute(
                    """SELECT DISTINCT c.* FROM cds c LEFT JOIN tracks t ON t.cd_id=c.id
                    WHERE ? = '' OR c.title LIKE ? OR c.artist LIKE ? OR t.title LIKE ? OR t.artist LIKE ?
                    ORDER BY c.title COLLATE NOCASE, c.artist COLLATE NOCASE""",
                    (query, f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%"),
                ).fetchall()
                self.send_json([get_cd(db, row["id"]) for row in rows])
            return
        if path == "/api/tracks":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            pattern = f"%{query}%"
            with connect_db() as db:
                rows = db.execute(
                    """SELECT t.id, t.position, t.title, t.artist AS track_artist,
                    t.duration, c.id AS cd_id, c.title AS album, c.artist AS album_artist,
                    c.year, c.slot,
                    CASE WHEN c.cover_data != '' THEN '/api/cds/' || c.id || '/cover'
                    ELSE c.cover_url END AS cover
                    FROM tracks t JOIN cds c ON c.id = t.cd_id
                    WHERE ? = '' OR t.title LIKE ? OR t.artist LIKE ?
                    OR c.title LIKE ? OR c.artist LIKE ? OR c.year LIKE ?
                    ORDER BY c.title COLLATE NOCASE, t.position, t.title COLLATE NOCASE""",
                    (query, pattern, pattern, pattern, pattern, pattern),
                ).fetchall()
            self.send_json([
                {
                    **dict(row),
                    "artist": row["track_artist"] or row["album_artist"],
                }
                for row in rows
            ])
            return
        if path == "/api/slots":
            with connect_db() as db:
                rows = db.execute(
                    """SELECT id, title, artist, year, label, catalog_number,
                    release_id, metadata_source, slot, cover_url,
                    CASE WHEN cover_data != '' THEN 1 ELSE 0 END AS has_cover
                    FROM cds WHERE slot IS NOT NULL"""
                ).fetchall()
            occupied = {}
            with connect_db() as db:
                for row in rows:
                    cd = dict(row)
                    tracks = db.execute(
                        """SELECT position, title, artist, duration FROM tracks
                        WHERE cd_id = ? ORDER BY position""",
                        (row["id"],),
                    ).fetchall()
                    cd["tracks"] = [dict(track) for track in tracks]
                    cd["cover"] = (
                        f"/api/cds/{row['id']}/cover"
                        if row["has_cover"] else row["cover_url"]
                    )
                    cd.pop("cover_url")
                    cd.pop("has_cover")
                    occupied[row["slot"]] = cd
            self.send_json([
                {"slot": number, "cd": occupied.get(number)}
                for number in range(101)
            ])
            return
        match = re.fullmatch(r"/api/cds/(\d+)", path)
        if match:
            with connect_db() as db:
                result = get_cd(db, int(match.group(1)))
            self.send_json(result if result else {"error": "CD not found."}, 200 if result else 404)
            return
        match = re.fullmatch(r"/api/cds/(\d+)/cover", path)
        if match:
            with connect_db() as db:
                row = db.execute(
                    "SELECT cover_data, cover_mime FROM cds WHERE id = ?",
                    (int(match.group(1)),),
                ).fetchone()
            if row is None or not row["cover_data"]:
                self.send_error_json("Cover image not found.", 404)
            else:
                self.send_bytes(
                    base64.b64decode(row["cover_data"]),
                    row["cover_mime"],
                )
            return
        match = re.fullmatch(r"/api/metadata/([a-z]+)", path)
        if match:
            self.search_metadata(match.group(1), parsed.query)
            return
        match = re.fullmatch(r"/api/metadata/musicbrainz/([^/]+)", path)
        if match:
            self.read_musicbrainz_release(match.group(1))
            return
        match = re.fullmatch(r"/api/metadata/discogs/([^/]+)", path)
        if match:
            self.read_discogs_release(match.group(1))
            return
        self.send_error_json("Not found.", 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/cds":
            try:
                result = save_cd(self.read_json())
                self.send_json(result, 201)
            except ValueError as error:
                self.send_error_json(str(error))
            return
        match = re.fullmatch(r"/api/slots/(-?\d+)", path)
        if match:
            self.assign_slot(int(match.group(1)))
            return
        self.send_error_json("Not found.", 404)

    def do_PUT(self):
        match = re.fullmatch(r"/api/cds/(\d+)", urllib.parse.urlsplit(self.path).path)
        if not match:
            self.send_error_json("Not found.", 404)
            return
        try:
            result = save_cd(self.read_json(), int(match.group(1)))
            self.send_json(result if result else {"error": "CD not found."}, 200 if result else 404)
        except ValueError as error:
            self.send_error_json(str(error))

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        match = re.fullmatch(r"/api/cds/(\d+)", path)
        if match:
            with connect_db() as db:
                cur = db.execute("DELETE FROM cds WHERE id = ?", (int(match.group(1)),))
            self.send_json({"deleted": True} if cur.rowcount else {"error": "CD not found."},
                           200 if cur.rowcount else 404)
            return
        match = re.fullmatch(r"/api/slots/(-?\d+)", path)
        if match:
            slot = int(match.group(1))
            if not 0 <= slot <= 100:
                self.send_error_json("Slot must be between 0 and 100.")
                return
            with connect_db() as db:
                cur = db.execute("UPDATE cds SET slot = NULL WHERE slot = ?", (slot,))
            self.send_json({"slot": slot, "unloaded": cur.rowcount > 0})
            return
        self.send_error_json("Not found.", 404)

    def assign_slot(self, slot):
        if not 0 <= slot <= 100:
            self.send_error_json("Slot must be between 0 and 100.")
            return
        try:
            payload = self.read_json()
            cd_id = int(payload.get("cd_id"))
        except (ValueError, TypeError) as error:
            self.send_error_json(str(error) if isinstance(error, ValueError) else "Choose a valid CD.")
            return
        try:
            with connect_db() as db:
                db.execute("BEGIN IMMEDIATE")
                cd = db.execute("SELECT id, slot FROM cds WHERE id = ?", (cd_id,)).fetchone()
                if cd is None:
                    self.send_error_json("CD not found.", 404)
                    return
                if cd["slot"] is not None and cd["slot"] != slot and not bool(payload.get("move")):
                    self.send_error_json(f"This CD is already loaded in slot {cd['slot']}.")
                    return
                occupied = db.execute("SELECT id FROM cds WHERE slot = ?", (slot,)).fetchone()
                if occupied is not None and occupied["id"] != cd_id and not bool(payload.get("replace")):
                    self.send_error_json(f"Slot {slot} is already occupied.")
                    return
                if occupied is not None and occupied["id"] != cd_id:
                    db.execute("UPDATE cds SET slot = NULL WHERE id = ?", (occupied["id"],))
                db.execute("UPDATE cds SET slot = ? WHERE id = ?", (slot, cd_id))
                self.send_json({"slot": slot, "cd": get_cd(db, cd_id)})
        except sqlite3.IntegrityError:
            self.send_error_json(
                "The slot assignment conflicts with another change. Refresh and try again.", 409
            )

    def search_metadata(self, source, query_string):
        parameters = urllib.parse.parse_qs(query_string, keep_blank_values=True)
        title = parameters.get("title", [""])[0].strip()
        artist = parameters.get("artist", [""])[0].strip()
        year = parameters.get("year", [""])[0].strip()
        query = parameters.get("q", [""])[0].strip()
        search_text = " ".join(value for value in (title, artist, year, query) if value)
        if len(search_text) < 2:
            self.send_error_json("Enter at least two characters to search.")
            return
        if any(len(value) > 200 for value in (title, artist, year, query)):
            self.send_error_json("Search text is too long.")
            return
        if year and not re.fullmatch(r"\d{4}", year):
            self.send_error_json("Release year must be a four-digit year.")
            return
        try:
            if source == "musicbrainz":
                self.send_json(musicbrainz_search(title, artist, year, query))
            elif source == "discogs":
                self.send_json(discogs_search(title, artist, year, query))
            else:
                self.send_error_json("Unsupported metadata source.", 404)
        except urllib.error.HTTPError as error:
            self.provider_error(source, error)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            self.send_error_json(f"{source.title()} could not be reached: {error}", 502)
        except RuntimeError as error:
            self.send_error_json(str(error), 503)

    def read_discogs_release(self, release_id):
        try:
            self.send_json(discogs_release(release_id))
        except urllib.error.HTTPError as error:
            self.provider_error("Discogs", error)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            self.send_error_json(f"Discogs could not be reached: {error}", 502)
        except RuntimeError as error:
            self.send_error_json(str(error), 503)
        except ValueError as error:
            self.send_error_json(str(error))

    def read_musicbrainz_release(self, release_id):
        try:
            self.send_json(musicbrainz_release(release_id))
        except urllib.error.HTTPError as error:
            self.provider_error("MusicBrainz", error)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            self.send_error_json(f"MusicBrainz could not be reached: {error}", 502)
        except ValueError as error:
            self.send_error_json(str(error))

    def provider_error(self, source, error):
        if source.lower() == "discogs" and error.code == 401:
            self.send_error_json(
                "Discogs rejected the configured credentials. Check the Discogs key, secret, or token.",
                502,
            )
        else:
            self.send_error_json(
                f"{source} returned an error (HTTP {error.code}).",
                502,
            )


def main():
    initialize_db()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), CdLibraryHandler)
    server.daemon_threads = True
    print(f"CD Library listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping CD Library.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
