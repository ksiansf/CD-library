import base64
import http.client
import json
import re
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi
from http.server import BaseHTTPRequestHandler

from . import catalog, config, covers, metadata, updates as update_service


def save_cd(payload, cd_id=None):
    payload = dict(payload)
    warning = ""
    if not payload.get("cover_data") and payload.get("cover_url"):
        try:
            image_data, mime_type = covers.download_cover_image(payload["cover_url"])
            payload["cover_data"] = base64.b64encode(image_data).decode("ascii")
            payload["cover_mime"] = mime_type
        except (http.client.HTTPException, OSError, TimeoutError, ValueError) as error:
            print(f"Could not save album cover locally: {error}")
            warning = (
                "The CD was saved, but its cover could not be downloaded locally. "
                "It will be retried when the application starts."
            )

    result = catalog.save_cd(payload, cd_id)
    if result is not None and warning and not result["cover"].startswith("/api/cds/"):
        result["cover_download_warning"] = warning
    return result


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
        if size < 0 or size > config.MAX_REQUEST_BYTES:
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
                content = (config.APP_DIR / "static" / "index.html").read_bytes()
                self.send_bytes(content, "text/html; charset=utf-8")
            except OSError as error:
                self.send_error_json(f"Could not read application page: {error}", 500)
            return
        if path == "/favicon.svg":
            try:
                content = (config.APP_DIR / "static" / "favicon.svg").read_bytes()
                self.send_bytes(content, "image/svg+xml")
            except OSError as error:
                self.send_error_json(f"Could not read page icon: {error}", 500)
            return
        if path == "/media/PD-F958.png":
            try:
                content = (config.APP_DIR / "media" / "PD-F958.png").read_bytes()
                self.send_bytes(content, "image/png")
            except OSError as error:
                self.send_error_json(f"Could not read CD changer image: {error}", 500)
            return
        if path == "/media/center-image":
            image_path = config.center_image_path()
            try:
                if image_path.is_file():
                    content = image_path.read_bytes()
                    mime_type = config.image_mime_type(content)
                    if not mime_type:
                        self.send_error_json("Stored center image is invalid.", 500)
                        return
                else:
                    content = (config.APP_DIR / "media" / "PD-F958.png").read_bytes()
                    mime_type = "image/png"
                self.send_bytes(content, mime_type)
            except OSError as error:
                print(f"Could not read center image: {error}")
                self.send_error_json("Could not read the center image.", 500)
            return
        if path == "/api/settings":
            try:
                settings = config.home_assistant_settings()
                settings["discogs_configured"] = config.has_discogs_credentials()
                settings["center_image_configured"] = config.center_image_path().is_file()
            except (OSError, RuntimeError) as error:
                print(f"Could not read application settings: {error}")
                self.send_error_json("Could not read application settings.", 500)
                return
            self.send_json({
                "discogs_configured": settings["discogs_configured"],
                "home_assistant_address": settings["address"],
                "home_assistant_api_key_configured": settings["api_key_configured"],
                "center_image_configured": settings["center_image_configured"],
            })
            return
        if path == "/api/updates":
            try:
                self.send_json(update_service.check_for_updates())
            except update_service.ReleaseCheckError as error:
                print(f"Could not check for application updates: {error}")
                self.send_error_json(str(error), 502)
            except RuntimeError as error:
                print(f"Could not read application version: {error}")
                self.send_error_json(str(error), 500)
            return
        if path == "/api/updates/status":
            if not update_service.helper_is_available():
                self.send_json({"state": "unavailable"})
                return
            try:
                self.send_json(update_service.read_status())
            except RuntimeError as error:
                print(f"Could not read application update status: {error}")
                self.send_error_json(str(error), 500)
            return
        if path == "/api/cds":
            parameters = urllib.parse.parse_qs(parsed.query)
            query = parameters.get("q", [""])[0].strip()
            status = parameters.get("status", ["all"])[0].strip()
            sort = parameters.get("sort", ["album"])[0].strip()
            if status not in {"all", "loaded", "unloaded", "none"}:
                self.send_error_json("CD status must be 'all', 'loaded', 'unloaded', or 'none'.")
                return
            order_by = {
                "album": "c.title COLLATE NOCASE, c.artist COLLATE NOCASE",
                "artist": "c.artist COLLATE NOCASE, c.title COLLATE NOCASE",
                "year": "c.year = '', c.year COLLATE NOCASE, c.title COLLATE NOCASE",
                "slot": "c.slot IS NULL, c.slot, c.title COLLATE NOCASE",
            }
            if sort not in order_by:
                self.send_error_json("CD sort must be 'album', 'artist', 'year', or 'slot'.")
                return
            with catalog.connect_db() as db:
                rows = db.execute(
                    """SELECT DISTINCT c.* FROM cds c LEFT JOIN tracks t ON t.cd_id=c.id
                    WHERE (? = 'all' OR (? = 'loaded' AND c.slot IS NOT NULL)
                    OR (? = 'unloaded' AND c.slot IS NULL))
                    AND (? = '' OR c.title LIKE ? OR c.artist LIKE ? OR t.title LIKE ? OR t.artist LIKE ?)
                    ORDER BY """ + order_by[sort],
                    (
                        status,
                        status,
                        status,
                        query,
                        f"%{query}%",
                        f"%{query}%",
                        f"%{query}%",
                        f"%{query}%",
                    ),
                ).fetchall()
                self.send_json([catalog.get_cd(db, row["id"]) for row in rows])
            return
        if path == "/api/tracks":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            pattern = f"%{query}%"
            with catalog.connect_db() as db:
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
            with catalog.connect_db() as db:
                rows = db.execute(
                    """SELECT id, title, artist, year, label, catalog_number,
                    release_id, metadata_source, slot, cover_url,
                    CASE WHEN cover_data != '' THEN 1 ELSE 0 END AS has_cover
                    FROM cds WHERE slot IS NOT NULL"""
                ).fetchall()
            occupied = {}
            with catalog.connect_db() as db:
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
            with catalog.connect_db() as db:
                result = catalog.get_cd(db, int(match.group(1)))
            self.send_json(result if result else {"error": "CD not found."}, 200 if result else 404)
            return
        match = re.fullmatch(r"/api/cds/(\d+)/cover", path)
        if match:
            with catalog.connect_db() as db:
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
        if path == "/api/updates":
            try:
                payload = self.read_json()
            except ValueError as error:
                self.send_error_json(str(error))
                return
            if payload:
                self.send_error_json("Update requests do not accept options.")
                return
            if not update_service.helper_is_available():
                self.send_error_json("The host update service is not installed.", 503)
                return
            try:
                release = update_service.check_for_updates()
            except update_service.ReleaseCheckError as error:
                print(f"Could not check for application updates: {error}")
                self.send_error_json(str(error), 502)
                return
            except RuntimeError as error:
                print(f"Could not read application version: {error}")
                self.send_error_json(str(error), 500)
                return
            if not release["update_available"]:
                self.send_error_json("The application is already up to date.", 409)
                return
            try:
                request_id = update_service.enqueue_update()
            except update_service.UpdateRequestError as error:
                self.send_error_json(str(error), 409)
                return
            except (OSError, RuntimeError) as error:
                print(f"Could not queue application update: {error}")
                self.send_error_json("Could not queue the application update.", 500)
                return
            self.send_json({"request_id": request_id, "state": "queued"}, 202)
            return
        match = re.fullmatch(
            r"/api/home-assistant/transport/(play|pause|stop|power|next_track|previous_track)",
            path,
        )
        if match:
            action = match.group(1)
            error = self.call_home_assistant_script(action)
            if error:
                self.send_error_json(error[0], error[1])
            else:
                self.send_json({"action": action, "sent": True})
            return
        match = re.fullmatch(r"/api/slots/(-?\d+)/play", path)
        if match:
            try:
                payload = self.read_json()
            except ValueError as error:
                self.send_error_json(str(error))
                return
            self.play_slot_on_home_assistant(int(match.group(1)), payload)
            return
        if path == "/api/settings/discogs":
            try:
                payload = self.read_json()
            except ValueError as error:
                self.send_error_json(str(error))
                return
            token = payload.get("discogs_token")
            if not isinstance(token, str) or not token.strip():
                self.send_error_json("Enter a Discogs personal access token.")
                return
            if len(token.strip()) > 4096:
                self.send_error_json("Discogs token must be 4096 characters or fewer.")
                return
            try:
                config.save_application_settings({"discogs_token": token.strip()})
            except OSError as error:
                print(f"Could not securely store Discogs settings: {error}")
                self.send_error_json("Could not securely store the Discogs token.", 500)
                return
            except RuntimeError as error:
                print(f"Could not read existing application settings: {error}")
                self.send_error_json("Could not read existing application settings.", 500)
                return
            self.send_json({"saved": True})
            return
        if path == "/api/settings/center-image":
            try:
                payload = self.read_json()
                encoded, mime_type = catalog.parse_cover({
                    "cover_data": payload.get("image_data"),
                    "cover_mime": payload.get("image_mime"),
                })
                image_data = base64.b64decode(encoded)
                if config.image_mime_type(image_data) != mime_type:
                    raise ValueError("Image content does not match its file type.")
                config.save_center_image(image_data)
            except ValueError as error:
                self.send_error_json(str(error))
                return
            except OSError as error:
                print(f"Could not securely store center image: {error}")
                self.send_error_json("Could not securely store the center image.", 500)
                return
            self.send_json({"saved": True})
            return
        if path == "/api/settings/home-assistant":
            try:
                payload = self.read_json()
                address_value = payload.get("home_assistant_address")
                api_key_value = payload.get("home_assistant_api_key", "")
                if not isinstance(address_value, str):
                    raise ValueError("Enter a valid Home Assistant address.")
                address = config.validate_home_assistant_address(address_value)
                if not isinstance(api_key_value, str):
                    raise ValueError("Home Assistant API key must be text.")
                api_key = api_key_value.strip()
                if len(api_key) > 4096:
                    raise ValueError("Home Assistant API key must be 4096 characters or fewer.")
                updates = {"home_assistant_address": address}
                if api_key:
                    updates["home_assistant_api_key"] = api_key
                config.save_application_settings(updates)
            except ValueError as error:
                self.send_error_json(str(error))
                return
            except OSError as error:
                print(f"Could not securely store Home Assistant settings: {error}")
                self.send_error_json("Could not securely store Home Assistant settings.", 500)
                return
            except RuntimeError as error:
                print(f"Could not read existing application settings: {error}")
                self.send_error_json("Could not read existing application settings.", 500)
                return
            self.send_json({"saved": True})
            return
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

    def play_slot_on_home_assistant(self, slot, request_payload):
        if not 0 <= slot <= 100:
            self.send_error_json("Slot must be between 0 and 100.")
            return
        track_number = request_payload.get("track_number", 0)
        if (
            not isinstance(track_number, int)
            or isinstance(track_number, bool)
            or track_number < 0
        ):
            self.send_error_json("Track number must be a non-negative integer.")
            return
        try:
            with catalog.connect_db() as db:
                occupied = db.execute(
                    "SELECT id FROM cds WHERE slot = ?", (slot,)
                ).fetchone()
                if occupied is not None and track_number > 0:
                    track = db.execute(
                        "SELECT 1 FROM tracks WHERE cd_id = ? AND position = ?",
                        (occupied["id"], track_number),
                    ).fetchone()
                else:
                    track = True
        except sqlite3.Error as error:
            print(f"Could not check Home Assistant playback slot: {error}")
            self.send_error_json("Could not check the selected slot.", 500)
            return
        if occupied is None:
            self.send_error_json(f"Slot {slot} is empty.")
            return
        if track is None:
            self.send_error_json(
                f"Track {track_number} does not exist in slot {slot}."
            )
            return

        playback_payload = {"cd_position": slot, "track_number": track_number}
        error = self.call_home_assistant_script("play_cd_track", playback_payload)
        if error:
            self.send_error_json(error[0], error[1])
            return
        self.send_json({
            "played": True,
            "slot": slot,
            "track_number": track_number,
        })

    def call_home_assistant_script(self, script_name, payload=None):
        try:
            address, api_key = config.home_assistant_credentials()
            if not address or not api_key:
                return (
                    "Configure the Home Assistant address and API key in Settings.",
                    503,
                )
            address = config.validate_home_assistant_address(address)
        except (OSError, RuntimeError) as error:
            print(f"Could not read Home Assistant settings: {error}")
            return "Could not read Home Assistant settings.", 500
        except ValueError as error:
            return str(error), 503

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {api_key}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{address}/api/services/script/{script_name}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            tls_context = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(
                request, timeout=12, context=tls_context
            ) as response:
                response.read()
        except urllib.error.HTTPError as error:
            return (
                f"Home Assistant rejected the request (HTTP {error.code}).",
                502,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"Could not send Home Assistant play request: {error}")
            return "Could not reach Home Assistant.", 502
        return None

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
            with catalog.connect_db() as db:
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
            with catalog.connect_db() as db:
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
        except ValueError as error:
            self.send_error_json(str(error))
            return
        if "swap_with" in payload:
            try:
                other_slot = int(payload["swap_with"])
            except (ValueError, TypeError):
                self.send_error_json("Choose a valid slot to swap with.")
                return
            if not 0 <= other_slot <= 100 or other_slot == slot:
                self.send_error_json("Choose a different slot between 0 and 100.")
                return
            try:
                with catalog.connect_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    occupied = {
                        row["slot"]: row["id"]
                        for row in db.execute(
                            "SELECT id, slot FROM cds WHERE slot IN (?, ?)",
                            (slot, other_slot),
                        ).fetchall()
                    }
                    if len(occupied) != 2:
                        self.send_error_json("Both slots must contain a CD to swap.")
                        return
                    db.execute(
                        "UPDATE cds SET slot = NULL WHERE slot IN (?, ?)",
                        (slot, other_slot),
                    )
                    db.execute("UPDATE cds SET slot = ? WHERE id = ?", (slot, occupied[other_slot]))
                    db.execute(
                        "UPDATE cds SET slot = ? WHERE id = ?",
                        (other_slot, occupied[slot]),
                    )
                    self.send_json({"slots": [slot, other_slot], "swapped": True})
            except sqlite3.IntegrityError:
                self.send_error_json(
                    "The slot assignment conflicts with another change. Refresh and try again.", 409
                )
            return
        try:
            cd_id = int(payload.get("cd_id"))
        except (ValueError, TypeError) as error:
            self.send_error_json(
                str(error) if isinstance(error, ValueError) else "Choose a valid CD."
            )
            return
        try:
            with catalog.connect_db() as db:
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
                self.send_json({"slot": slot, "cd": catalog.get_cd(db, cd_id)})
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
                self.send_json(metadata.musicbrainz_search(title, artist, year, query))
            elif source == "discogs":
                self.send_json(metadata.discogs_search(title, artist, year, query))
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
            self.send_json(metadata.discogs_release(release_id))
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
            self.send_json(metadata.musicbrainz_release(release_id))
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
