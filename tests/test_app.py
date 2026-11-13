import base64
import http.client
import json
import os
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import app as cd_app


class CdLibraryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        cd_app.config.DATA_DIR = Path(self.temp_dir.name)
        cd_app.initialize_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), cd_app.CdLibraryHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            response = urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        data = response.read()
        return response.status, json.loads(data) if data else None

    def request_via_http_client(self, path, method="POST", payload=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        try:
            body = json.dumps(payload) if payload is not None else None
            headers = {"Content-Type": "application/json"} if body else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None
        finally:
            connection.close()

    def add_cd(self, title, artist="Test Artist", tracks=None):
        return self.request(
            "/api/cds",
            "POST",
            {"title": title, "artist": artist, "tracks": tracks or []},
        )

    def test_favicon_route_serves_svg_icon(self):
        response = urllib.request.urlopen(self.base_url + "/favicon.svg", timeout=2)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
        self.assertIn("<svg", response.read().decode("utf-8"))

    def test_create_search_and_delete_cd_with_tracks(self):
        status, created = self.add_cd("Night Drive", tracks=[{"title": "Silver Roads"}])
        self.assertEqual(status, 201)
        cd_id = created["id"]

        status, results = self.request("/api/cds?q=Silver")
        self.assertEqual(status, 200)
        self.assertEqual(results[0]["id"], cd_id)
        self.assertEqual(results[0]["tracks"][0]["title"], "Silver Roads")

        self.assertEqual(self.request(f"/api/cds/{cd_id}", "DELETE")[0], 200)
        self.assertEqual(self.request("/api/cds")[1], [])

    def test_cd_library_filters_unloaded_cds_and_keeps_search(self):
        unloaded = self.add_cd("Night Drive", tracks=[{"title": "Silver Roads"}])[1]
        loaded = self.add_cd("Daylight", tracks=[{"title": "Morning Song"}])[1]
        self.request("/api/slots/9", "POST", {"cd_id": loaded["id"]})

        self.assertEqual(
            {cd["id"] for cd in self.request("/api/cds")[1]},
            {unloaded["id"], loaded["id"]},
        )
        self.assertEqual(
            [cd["id"] for cd in self.request("/api/cds?status=unloaded")[1]],
            [unloaded["id"]],
        )
        self.assertEqual(
            [cd["id"] for cd in self.request("/api/cds?status=unloaded&q=Silver")[1]],
            [unloaded["id"]],
        )
        self.assertEqual(
            [cd["id"] for cd in self.request("/api/cds?status=loaded")[1]],
            [loaded["id"]],
        )
        self.assertEqual(self.request("/api/cds?status=none")[1], [])
        self.assertEqual(self.request("/api/cds?status=invalid")[0], 400)

    def test_cd_library_sorts_by_artist_album_year_and_slot(self):
        cds = {}
        for title, artist, year in [
            ("Zulu", "Beta", "2005"),
            ("Alpha", "Alpha", "1998"),
            ("Mike", "Alpha", "2001"),
        ]:
            cds[title] = self.request(
                "/api/cds", "POST", {"title": title, "artist": artist, "year": year}
            )[1]
        self.request("/api/slots/8", "POST", {"cd_id": cds["Zulu"]["id"]})
        self.request("/api/slots/2", "POST", {"cd_id": cds["Alpha"]["id"]})

        self.assertEqual(
            [cd["title"] for cd in self.request("/api/cds?sort=artist")[1]],
            ["Alpha", "Mike", "Zulu"],
        )
        self.assertEqual(
            [cd["title"] for cd in self.request("/api/cds?sort=album")[1]],
            ["Alpha", "Mike", "Zulu"],
        )
        self.assertEqual(
            [cd["title"] for cd in self.request("/api/cds?sort=year")[1]],
            ["Alpha", "Mike", "Zulu"],
        )
        self.assertEqual(
            [cd["slot"] for cd in self.request("/api/cds?sort=slot")[1]],
            [2, 8, None],
        )
        self.assertEqual(self.request("/api/cds?sort=invalid")[0], 400)

    def test_track_library_lists_tracks_and_searches_album_artist_and_track(self):
        _, first = self.request(
            "/api/cds",
            "POST",
            {
                "title": "Night Drive",
                "artist": "The Example",
                "year": "2001",
                "cover_url": "https://covers.example/night-drive.jpg",
                "tracks": [
                    {"title": "Silver Roads", "duration": "3:45"},
                    {"title": "Last Light", "artist": "Guest Singer"},
                ],
            },
        )
        self.request(
            "/api/cds",
            "POST",
            {
                "title": "Daylight",
                "artist": "Another Artist",
                "tracks": [{"title": "Morning Song"}],
            },
        )
        self.request("/api/slots/9", "POST", {"cd_id": first["id"]})

        status, tracks = self.request("/api/tracks")
        self.assertEqual(status, 200)
        self.assertEqual(
            [track["title"] for track in tracks],
            ["Morning Song", "Silver Roads", "Last Light"],
        )
        self.assertEqual(tracks[1]["artist"], "The Example")
        self.assertEqual(tracks[1]["album"], "Night Drive")
        self.assertEqual(tracks[1]["album_artist"], "The Example")
        self.assertEqual(tracks[1]["duration"], "3:45")
        self.assertEqual(tracks[1]["slot"], 9)
        self.assertEqual(tracks[1]["cover"], "https://covers.example/night-drive.jpg")
        self.assertEqual(tracks[2]["artist"], "Guest Singer")
        self.assertEqual(tracks[2]["cover"], "https://covers.example/night-drive.jpg")
        self.assertIsNone(tracks[0]["slot"])

        self.assertEqual(
            [track["title"] for track in self.request("/api/tracks?q=Silver")[1]],
            ["Silver Roads"],
        )
        self.assertEqual(
            [track["title"] for track in self.request("/api/tracks?q=Another+Artist")[1]],
            ["Morning Song"],
        )
        self.assertEqual(
            [track["title"] for track in self.request("/api/tracks?q=Night+Drive")[1]],
            ["Silver Roads", "Last Light"],
        )

    def test_slot_range_and_initial_overview_include_101_positions(self):
        slots = self.request("/api/slots")[1]
        self.assertEqual(len(slots), 101)
        self.assertEqual(slots[0]["slot"], 0)
        self.assertEqual(slots[100]["slot"], 100)
        self.assertEqual(self.request("/api/slots/101", "POST", {"cd_id": 1})[0], 400)
        self.assertEqual(self.request("/api/slots/-1", "DELETE")[0], 400)

    def test_loaded_slot_includes_cover_art_url(self):
        _, cd = self.request(
            "/api/cds",
            "POST",
            {
                "title": "Cover album",
                "cover_url": "https://covers.example/album.jpg",
            },
        )
        self.assertEqual(self.request("/api/slots/7", "POST", {"cd_id": cd["id"]})[0], 200)
        slot = self.request("/api/slots")[1][7]
        self.assertEqual(slot["cd"]["cover"], "https://covers.example/album.jpg")
        self.assertNotIn("cover_url", slot["cd"])

    def test_loaded_slot_includes_album_details_and_track_list(self):
        _, cd = self.request(
            "/api/cds",
            "POST",
            {
                "title": "Detailed album",
                "artist": "Example artist",
                "year": "1998",
                "label": "Example label",
                "catalog_number": "EX-42",
                "tracks": [{"title": "Opening song"}, {"title": "Closing song"}],
            },
        )
        self.assertEqual(self.request("/api/slots/8", "POST", {"cd_id": cd["id"]})[0], 200)
        slot_cd = self.request("/api/slots")[1][8]["cd"]
        self.assertEqual(slot_cd["year"], "1998")
        self.assertEqual(slot_cd["label"], "Example label")
        self.assertEqual([track["title"] for track in slot_cd["tracks"]],
                         ["Opening song", "Closing song"])

    def test_unique_assignment_unload_and_move(self):
        first = self.add_cd("First album")[1]
        second = self.add_cd("Second album")[1]

        self.assertEqual(self.request("/api/slots/0", "POST", {"cd_id": first["id"]})[0], 200)
        self.assertEqual(self.request("/api/slots/0", "POST", {"cd_id": second["id"]})[0], 400)
        self.assertEqual(self.request("/api/slots/1", "POST", {"cd_id": first["id"]})[0], 400)

        status, moved = self.request(
            "/api/slots/100", "POST", {"cd_id": first["id"], "move": True}
        )
        self.assertEqual(status, 200)
        self.assertEqual(moved["slot"], 100)
        self.assertIsNone(self.request("/api/slots")[1][0]["cd"])
        self.assertEqual(self.request("/api/slots/100", "DELETE")[1]["unloaded"], True)
        self.assertIsNone(self.request(f"/api/cds/{first['id']}")[1]["slot"])

    def test_swapping_loaded_cds_keeps_both_loaded(self):
        first = self.add_cd("First album")[1]
        second = self.add_cd("Second album")[1]
        third = self.add_cd("Third album")[1]
        self.request("/api/slots/0", "POST", {"cd_id": first["id"]})
        self.request("/api/slots/2", "POST", {"cd_id": second["id"]})
        self.request("/api/slots/3", "POST", {"cd_id": third["id"]})

        status, result = self.request("/api/slots/0", "POST", {"swap_with": 2})

        self.assertEqual(status, 200)
        self.assertEqual(result, {"slots": [0, 2], "swapped": True})
        cds = {cd["id"]: cd for cd in self.request("/api/cds")[1]}
        self.assertEqual(cds[first["id"]]["slot"], 2)
        self.assertEqual(cds[second["id"]]["slot"], 0)
        self.assertEqual(cds[third["id"]]["slot"], 3)

    def test_swapping_slots_requires_two_different_occupied_slots(self):
        cd = self.add_cd("Loaded album")[1]
        self.request("/api/slots/0", "POST", {"cd_id": cd["id"]})

        self.assertEqual(self.request("/api/slots/0", "POST", {"swap_with": 0})[0], 400)
        self.assertEqual(self.request("/api/slots/0", "POST", {"swap_with": 1})[0], 400)
        self.assertEqual(self.request("/api/slots/0", "POST", {"swap_with": 101})[0], 400)

    def test_replacing_occupied_slot_unloads_previous_cd(self):
        previous = self.add_cd("Previous album")[1]
        replacement = self.add_cd("Replacement album")[1]
        self.assertEqual(
            self.request("/api/slots/0", "POST", {"cd_id": previous["id"]})[0],
            200,
        )

        status, result = self.request(
            "/api/slots/0",
            "POST",
            {"cd_id": replacement["id"], "replace": True},
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["cd"]["id"], replacement["id"])
        cds = {cd["id"]: cd for cd in self.request("/api/cds")[1]}
        self.assertIsNone(cds[previous["id"]]["slot"])
        self.assertEqual(cds[replacement["id"]]["slot"], 0)

    def test_title_required_and_bad_tracks_are_reported(self):
        self.assertEqual(self.request("/api/cds", "POST", {"title": " "})[0], 400)
        status, error = self.request(
            "/api/cds", "POST", {"title": "Bad track data", "tracks": {"title": "not a list"}}
        )
        self.assertEqual(status, 400)
        self.assertIn("Track list must be a list", error["error"])

    def test_cover_upload_validation_and_retrieval(self):
        status, created = self.add_cd(
            "Artwork test",
            tracks=[],
        )
        self.assertEqual(status, 201)
        cd_id = created["id"]
        payload = {
            "title": "Artwork test",
            "cover_data": "aGVsbG8=",
            "cover_mime": "image/png",
        }
        self.assertEqual(self.request(f"/api/cds/{cd_id}", "PUT", payload)[0], 200)
        cover_req = urllib.request.urlopen(f"{self.base_url}/api/cds/{cd_id}/cover")
        self.assertEqual(cover_req.headers["Content-Type"], "image/png")
        self.assertEqual(cover_req.read(), b"hello")
        payload["cover_mime"] = "text/plain"
        self.assertEqual(self.request(f"/api/cds/{cd_id}", "PUT", payload)[0], 400)

    def test_metadata_cover_is_downloaded_and_saved_locally(self):
        image_data = b"\x89PNG\r\n\x1a\nlocal cover"
        with patch(
            "cd_library.covers.download_cover_image",
            return_value=(image_data, "image/png"),
        ) as download:
            status, created = self.request(
                "/api/cds",
                "POST",
                {
                    "title": "Local cover album",
                    "cover_url": "https://i.discogs.com/example.png",
                },
            )

        self.assertEqual(status, 201)
        self.assertTrue(created["cover"].startswith("/api/cds/"))
        self.assertEqual(created["cover_url"], "")
        self.assertNotIn("cover_download_warning", created)
        download.assert_called_once_with("https://i.discogs.com/example.png")
        with cd_app.connect_db() as db:
            stored = db.execute(
                "SELECT cover_data, cover_mime, cover_url FROM cds WHERE id = ?",
                (created["id"],),
            ).fetchone()
        self.assertEqual(base64.b64decode(stored["cover_data"]), image_data)
        self.assertEqual(stored["cover_mime"], "image/png")
        self.assertEqual(stored["cover_url"], "")

        response = urllib.request.urlopen(
            self.base_url + created["cover"], timeout=2
        )
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(response.read(), image_data)

    def test_failed_metadata_cover_download_keeps_url_and_reports_retry(self):
        cover_url = "https://i.discogs.com/example.png"
        with patch(
            "cd_library.covers.download_cover_image",
            side_effect=OSError("provider unavailable"),
        ):
            status, created = self.request(
                "/api/cds",
                "POST",
                {"title": "Remote cover album", "cover_url": cover_url},
            )

        self.assertEqual(status, 201)
        self.assertEqual(created["cover"], cover_url)
        self.assertEqual(created["cover_url"], cover_url)
        self.assertIn("retried", created["cover_download_warning"])

    def test_startup_cover_migration_keeps_failed_urls_for_next_startup(self):
        migrated = cd_app.catalog.save_cd({
            "title": "Migrated cover",
            "cover_url": "https://i.discogs.com/migrated.png",
        })
        pending = cd_app.catalog.save_cd({
            "title": "Pending cover",
            "cover_url": "https://coverartarchive.org/release/example/front",
        })
        image_data = b"\x89PNG\r\n\x1a\nmigrated cover"
        with patch(
            "cd_library.covers.download_cover_image",
            side_effect=[
                (image_data, "image/png"),
                OSError("provider unavailable"),
            ],
        ):
            self.assertEqual(cd_app.catalog.migrate_remote_covers(), (1, 1))

        with cd_app.connect_db() as db:
            migrated_row = db.execute(
                "SELECT cover_data, cover_mime, cover_url FROM cds WHERE id = ?",
                (migrated["id"],),
            ).fetchone()
            pending_row = db.execute(
                "SELECT cover_data, cover_mime, cover_url FROM cds WHERE id = ?",
                (pending["id"],),
            ).fetchone()
        self.assertEqual(base64.b64decode(migrated_row["cover_data"]), image_data)
        self.assertEqual(migrated_row["cover_mime"], "image/png")
        self.assertEqual(migrated_row["cover_url"], "")
        self.assertEqual(pending_row["cover_data"], "")
        self.assertEqual(pending_row["cover_url"], pending["cover_url"])

        with patch(
            "cd_library.covers.download_cover_image",
            return_value=(image_data, "image/png"),
        ):
            self.assertEqual(cd_app.catalog.migrate_remote_covers(), (1, 0))

    def test_cover_downloader_restricts_hosts_and_validates_image_bytes(self):
        with patch("cd_library.covers.urllib.request.build_opener") as build_opener:
            with self.assertRaises(ValueError):
                cd_app.catalog.covers.download_cover_image(
                    "https://127.0.0.1/private.png"
                )
            build_opener.assert_not_called()

            with self.assertRaises(ValueError):
                cd_app.catalog.covers._TrustedCoverRedirectHandler().redirect_request(
                    None, None, 302, "Found", {}, "http://127.0.0.1/private.png"
                )

            response = MagicMock()
            response.__enter__.return_value.headers = {"Content-Length": "3"}
            response.__enter__.return_value.read.return_value = b"bad"
            build_opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "supported image"):
                cd_app.catalog.covers.download_cover_image(
                    "https://i.discogs.com/example.png"
                )

    def test_cover_downloader_rejects_oversized_content_length(self):
        response = MagicMock()
        response.__enter__.return_value.headers = {
            "Content-Length": str(cd_app.config.MAX_COVER_BYTES + 1)
        }
        with patch(
            "cd_library.covers.urllib.request.build_opener"
        ) as build_opener:
            build_opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "smaller than 5 MB"):
                cd_app.catalog.covers.download_cover_image(
                    "https://i.discogs.com/example.png"
                )
        response.__enter__.return_value.read.assert_not_called()

    def test_cover_downloader_enforces_limit_when_content_length_is_missing(self):
        response = MagicMock()
        response.__enter__.return_value.headers = {}
        response.__enter__.return_value.read.return_value = (
            b"\x89PNG\r\n\x1a\n" + b"x" * cd_app.config.MAX_COVER_BYTES
        )
        with patch(
            "cd_library.covers.urllib.request.build_opener"
        ) as build_opener:
            build_opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, "smaller than 5 MB"):
                cd_app.catalog.covers.download_cover_image(
                    "https://i.discogs.com/example.png"
                )

    def test_page_is_served_and_discogs_reports_missing_token(self):
        page = urllib.request.urlopen(self.base_url + "/")
        self.assertEqual(page.status, 200)
        page_content = page.read()
        self.assertIn(b"Changer overview", page_content)
        self.assertIn(b'id="settings-button"', page_content)
        self.assertIn(b'id="settings-panel"', page_content)
        self.assertIn(b'id="discogs-token"', page_content)
        self.assertIn(b'id="home-assistant-address"', page_content)
        self.assertIn(b'id="home-assistant-api-key"', page_content)
        self.assertIn(b'id="home-assistant-settings-form"', page_content)
        self.assertIn(b"refreshHomeAssistantAvailability()", page_content)
        self.assertIn(
            b"[data-playback-action],[data-play-slot],[data-track-list-play-slot],[data-play-track]",
            page_content,
        )
        self.assertIn(b"settings.home_assistant_api_key_configured", page_content)
        self.assertIn(b'id="center-image-settings-form"', page_content)
        self.assertIn(b'id="center-image-file"', page_content)
        self.assertIn(b'/media/center-image', page_content)
        self.assertIn(b".settings-section{width:100%", page_content)
        self.assertIn(b".settings-form-discogs{grid-template-columns:minmax(0,1fr) auto", page_content)
        self.assertIn(b".settings-form-home-assistant{grid-template-columns:minmax(0,300px) minmax(0,1fr) auto", page_content)
        self.assertIn(b".settings-section form>.settings-save{align-self:start;margin-top:23px}", page_content)
        self.assertIn(b"@media(max-width:900px){.settings-section .settings-form-discogs", page_content)
        self.assertIn(b'id="save-discogs-token" class="btn primary" type="submit">Save</button>', page_content)
        self.assertIn(b"saved token cannot be viewed", page_content)
        self.assertIn(b':"cover-grid"', page_content)
        self.assertIn(b'data-slot-layout="cover-grid"', page_content)
        self.assertIn(b">Covers</button>", page_content)
        self.assertNotIn(b'data-slot-layout="grid"', page_content)
        self.assertNotIn(b'"grid","cover-grid","donut"', page_content)
        self.assertIn(b'class="transport-panel" role="group"', page_content)
        self.assertIn(b'class="transport-controls"', page_content)
        self.assertIn(b'class="power-control" role="group" aria-label="Power"', page_content)
        self.assertLess(
            page_content.index(b'class="power-control"'),
            page_content.index(b'class="transport-panel"'),
        )
        self.assertIn(b'data-playback-action="previous_track"', page_content)
        self.assertIn(b'data-playback-action="next_track"', page_content)
        self.assertIn(b'data-playback-action="stop"', page_content)
        self.assertIn(b'data-playback-action="pause"', page_content)
        self.assertIn(b'data-playback-action="play"', page_content)
        self.assertIn(b'data-playback-action="power"', page_content)
        self.assertIn(b'class="transport-button" data-playback-action="stop"><svg', page_content)
        self.assertIn(b'class="transport-button" data-playback-action="pause"><svg', page_content)
        self.assertIn(b'class="transport-button" data-playback-action="play"><svg', page_content)
        self.assertIn(b"grid-template-columns:minmax(0,1fr) auto minmax(0,1fr)", page_content)
        self.assertIn(b"/api/home-assistant/transport/${action}", page_content)
        self.assertIn(b"cover-grid-slot .slot-content{flex:1;flex-direction:row", page_content)
        self.assertIn(b"cover-grid-slot .slot-cover{width:39%;max-width:156px", page_content)
        self.assertIn(b"cover-grid-slot.empty .slot-cover-placeholder{width:39%;max-width:156px", page_content)
        self.assertIn(b"cover-grid-slot .slot-copy{flex:1}", page_content)
        self.assertIn(b".cover-grid{grid-template-columns:repeat(auto-fill,minmax(210px,1fr))", page_content)
        self.assertIn(b".cover-grid-layout .special-slot-area{grid-template-columns:repeat(auto-fill,minmax(210px,1fr))", page_content)
        self.assertIn(b"state.slotLayout===\"cover-grid\"", page_content)
        self.assertLess(page_content.index(b'class="regular-slots"'), page_content.index(b'class="special-slot-area"'))
        self.assertIn(b'id="carousel-feature"', page_content)
        self.assertIn(b'data-play-slot="${item.slot}"', page_content)
        self.assertIn(b'data-select-tracks="${item.slot}"', page_content)
        self.assertIn(b"function openTrackPicker(slot)", page_content)
        self.assertIn(b'data-play-track="${track.position}"', page_content)
        self.assertIn(b'<th scope="col">Play</th>', page_content)
        self.assertIn(b'data-track-list-play-slot="${track.slot}"', page_content)
        self.assertIn(b'data-track-list-play-position="${track.position}"', page_content)
        self.assertIn(b'<th scope="col">Track #</th>', page_content)
        self.assertIn(b'<td>${track.position}</td>', page_content)
        self.assertIn(b'/api/slots/${slot}/play', page_content)
        self.assertIn(b'id="track-sort"', page_content)
        self.assertIn(b'<option value="artist">Artist</option>', page_content)
        self.assertIn(b'<option value="album">Album</option>', page_content)
        self.assertIn(b'<option value="year">Year</option>', page_content)
        self.assertIn(b'<option value="slot">Slot</option>', page_content)
        self.assertIn(b'<option value="track">Track</option>', page_content)
        self.assertIn(b'localStorage.setItem("track-sort",state.trackSort)', page_content)
        self.assertIn(b'data-play-slot="${atTwelve.item.slot}"', page_content)
        self.assertIn(b"/api/slots/${slot}/play", page_content)
        self.assertIn(b"class=\"carousel-feature-header\"", page_content)
        self.assertIn(b"align-items:flex-start;justify-content:center", page_content)
        self.assertIn(b"overflow:hidden;text-align:center}", page_content)
        self.assertIn(b"flex:1;min-height:0;width:100%", page_content)
        self.assertIn(b'aria-label="Track list" tabindex="0"', page_content)
        self.assertIn(b"carouselTrackScroll", page_content)
        self.assertIn(b"width:min(440px,61.1111%)", page_content)
        self.assertIn(b"width:calc(46% - 12px)", page_content)
        self.assertIn(b"id=\"carousel-slot-indicator\"", page_content)
        self.assertIn(b"Pointer to slot", page_content)
        self.assertIn(b"applyCarouselRotation", page_content)
        self.assertIn(b"pointerdown", page_content)
        self.assertIn(b"12 o\xe2\x80\x99clock position", page_content)
        self.assertIn(b"openOccupiedSlotActions", page_content)
        self.assertIn(b'id="slot-destination-select"', page_content)
        self.assertIn(b"Choose a slot. Occupied slots swap CDs; empty slots receive this CD.", page_content)
        self.assertIn(b"slots.map(item=>", page_content)
        self.assertIn(b'target.cd?"Swap CDs":"Move CD"', page_content)
        self.assertNotIn(b'id="swap-slot-select"', page_content)
        self.assertNotIn(b'id="move-slot-select"', page_content)
        self.assertIn(b'id="loaded-filter"', page_content)
        self.assertIn(b'id="unloaded-filter"', page_content)
        self.assertIn(b'id="cd-sort"', page_content)
        for sort_option in (b"Album", b"Artist", b"Year", b"Loaded slot"):
            self.assertIn(sort_option, page_content)
        self.assertIn(b"/media/center-image", page_content)

        image = urllib.request.urlopen(self.base_url + "/media/PD-F958.png")
        self.assertEqual(image.status, 200)
        self.assertEqual(image.headers["Content-Type"], "image/png")
        self.assertTrue(image.read().startswith(b"\x89PNG\r\n\x1a\n"))

        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "",
                "DISCOGS_CONSUMER_SECRET": "",
                "HOME_ASSISTANT_API_KEY": "",
                "HOME_ASSISTANT_ADDRESS": "",
            },
        ):
            status, error = self.request("/api/metadata/discogs?q=album")
        self.assertEqual(status, 503)
        self.assertIn("credentials", error["error"])

    def test_discogs_token_settings_are_saved_without_returning_the_token(self):
        token = "private-discogs-token"
        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "",
                "DISCOGS_CONSUMER_SECRET": "",
                "HOME_ASSISTANT_API_KEY": "",
                "HOME_ASSISTANT_ADDRESS": "",
            },
        ):
            status, initial = self.request("/api/settings")
            self.assertEqual(status, 200)
            self.assertFalse(initial["discogs_configured"])
            self.assertFalse(initial["center_image_configured"])

            status, saved = self.request(
                "/api/settings/discogs",
                "POST",
                {"discogs_token": token},
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved, {"saved": True})
            self.assertNotIn(token, json.dumps(saved))

            status, configuration = self.request("/api/settings")
            self.assertEqual(status, 200)
            self.assertTrue(configuration["discogs_configured"])
            self.assertEqual(configuration["home_assistant_address"], "")
            self.assertFalse(configuration["home_assistant_api_key_configured"])
            self.assertNotIn(token, json.dumps(configuration))

            with patch.object(
                cd_app.metadata,
                "api_request",
                return_value={"results": [{"id": 12, "title": "Test Release"}]},
            ) as api_call:
                results = cd_app.discogs_search(query="Test Release")

        self.assertEqual(results[0]["title"], "Test Release")
        self.assertEqual(
            api_call.call_args.args[1],
            {"Authorization": f"Discogs token={token}"},
        )
        self.assertNotIn(token, api_call.call_args.args[0])
        if os.name == "posix":
            settings_mode = (
                cd_app.config.DATA_DIR / "settings.json"
            ).stat().st_mode & 0o777
            self.assertEqual(settings_mode, 0o600)

    def test_update_check_reports_latest_release_and_helper_availability(self):
        release = {
            "tag_name": "v2.0.0",
            "html_url": "https://github.com/ksiansf/CD-library/releases/tag/v2.0.0",
            "published_at": "2026-11-12T12:00:00Z",
        }
        with patch.object(cd_app.updates, "fetch_latest_release", return_value=release):
            status, result = self.request("/api/updates")

        self.assertEqual(status, 200)
        self.assertEqual(result["current_version"], "new")
        self.assertEqual(result["latest_version"], "v2.0.0")
        self.assertTrue(result["update_available"])
        self.assertFalse(result["update_helper_available"])
        (cd_app.config.DATA_DIR / cd_app.updates.UPDATE_HELPER_MARKER).touch()
        with patch.object(cd_app.updates, "fetch_latest_release", return_value=release):
            status, result = self.request("/api/updates")
        self.assertEqual(status, 200)
        self.assertTrue(result["update_helper_available"])

    def test_settings_page_shows_update_controls(self):
        page = urllib.request.urlopen(self.base_url + "/")
        page_content = page.read()
        self.assertIn(b'id="check-updates"', page_content)
        self.assertIn(b'id="install-update"', page_content)
        self.assertIn(b'id="updates-status"', page_content)

    def test_update_request_queues_fixed_release_only_when_available(self):
        marker = cd_app.config.DATA_DIR / cd_app.updates.UPDATE_HELPER_MARKER
        marker.touch()
        (cd_app.config.DATA_DIR / cd_app.updates.UPDATE_TRIGGER_FILE).touch()
        with patch.object(
            cd_app.updates,
            "check_for_updates",
            return_value={"update_available": True},
        ):
            status, queued = self.request("/api/updates", "POST", {})
            self.assertEqual(status, 202)
            self.assertEqual(queued["state"], "queued")
            request_path = cd_app.config.DATA_DIR / "update-request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["request_id"], queued["request_id"])
            self.assertEqual(
                (cd_app.config.DATA_DIR / cd_app.updates.UPDATE_TRIGGER_FILE)
                .read_text(encoding="utf-8"),
                queued["request_id"],
            )
            status, error = self.request("/api/updates", "POST", {})
        self.assertEqual(status, 409)
        self.assertIn("already", error["error"])

    def test_update_request_rejects_missing_helper_and_current_release(self):
        with patch.object(
            cd_app.updates,
            "check_for_updates",
            return_value={"update_available": True},
        ):
            status, error = self.request("/api/updates", "POST", {})
        self.assertEqual(status, 503)
        self.assertIn("not installed", error["error"])

        (cd_app.config.DATA_DIR / cd_app.updates.UPDATE_HELPER_MARKER).touch()
        with patch.object(
            cd_app.updates,
            "check_for_updates",
            return_value={"update_available": False},
        ):
            status, error = self.request("/api/updates", "POST", {})
        self.assertEqual(status, 409)
        self.assertIn("up to date", error["error"])

    def test_discogs_token_settings_reject_empty_tokens(self):
        status, error = self.request(
            "/api/settings/discogs",
            "POST",
            {"discogs_token": "  "},
        )
        self.assertEqual(status, 400)
        self.assertIn("token", error["error"].lower())

    def test_home_assistant_settings_save_address_and_key_without_returning_key(self):
        discogs_token = "existing-discogs-token"
        home_assistant_key = "private-home-assistant-key"
        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "",
                "DISCOGS_CONSUMER_SECRET": "",
                "HOME_ASSISTANT_API_KEY": "",
                "HOME_ASSISTANT_ADDRESS": "",
            },
        ):
            self.assertEqual(
                self.request(
                    "/api/settings/discogs",
                    "POST",
                    {"discogs_token": discogs_token},
                )[0],
                200,
            )
            status, saved = self.request(
                "/api/settings/home-assistant",
                "POST",
                {
                    "home_assistant_address": "http://192.168.0.5:8123/",
                    "home_assistant_api_key": home_assistant_key,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(saved, {"saved": True})
            self.assertNotIn(home_assistant_key, json.dumps(saved))

            status, settings = self.request("/api/settings")
            self.assertEqual(status, 200)
            self.assertEqual(settings["home_assistant_address"], "http://192.168.0.5:8123")
            self.assertTrue(settings["home_assistant_api_key_configured"])
            self.assertNotIn(home_assistant_key, json.dumps(settings))
            self.assertTrue(settings["discogs_configured"])

            saved_settings = cd_app.read_saved_settings()
            self.assertEqual(saved_settings["discogs_token"], discogs_token)
            self.assertEqual(
                saved_settings["home_assistant_api_key"],
                home_assistant_key,
            )

            status, updated = self.request(
                "/api/settings/home-assistant",
                "POST",
                {
                    "home_assistant_address": "https://homeassistant.example",
                    "home_assistant_api_key": "",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(updated, {"saved": True})
            self.assertEqual(
                cd_app.read_saved_settings()["home_assistant_api_key"],
                home_assistant_key,
            )

    def test_center_image_upload_replaces_the_default_image(self):
        image_data = (cd_app.config.APP_DIR / "media" / "PD-F958.png").read_bytes()
        status, saved = self.request(
            "/api/settings/center-image",
            "POST",
            {
                "image_data": base64.b64encode(image_data).decode("ascii"),
                "image_mime": "image/png",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved, {"saved": True})

        settings_status, settings = self.request("/api/settings")
        self.assertEqual(settings_status, 200)
        self.assertTrue(settings["center_image_configured"])

        image = urllib.request.urlopen(self.base_url + "/media/center-image")
        self.assertEqual(image.status, 200)
        self.assertEqual(image.headers["Content-Type"], "image/png")
        self.assertEqual(image.read(), image_data)

    def test_center_image_upload_rejects_mismatched_image_content(self):
        status, error = self.request(
            "/api/settings/center-image",
            "POST",
            {
                "image_data": base64.b64encode(b"not an image").decode("ascii"),
                "image_mime": "image/png",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("content", error["error"].lower())

    def test_home_assistant_settings_reject_unsafe_or_invalid_addresses(self):
        for address in (
            "",
            "ftp://homeassistant.local",
            "http://user:password@homeassistant.local",
            "http://homeassistant.local?token=leak",
            "http://homeassistant.local:99999",
        ):
            with self.subTest(address=address):
                status, error = self.request(
                    "/api/settings/home-assistant",
                    "POST",
                    {
                        "home_assistant_address": address,
                        "home_assistant_api_key": "",
                    },
                )
                self.assertEqual(status, 400)
                self.assertIn("address", error["error"].lower())

    def test_play_slot_sends_server_side_home_assistant_request(self):
        cd_id = self.add_cd("Playback test")[1]["id"]
        self.request("/api/slots/0", "POST", {"cd_id": cd_id})
        api_key = "private-home-assistant-key"
        with patch.object(
            cd_app.config, "home_assistant_credentials",
            return_value=("http://homeassistant.local:8123", api_key),
        ), patch.object(urllib.request, "urlopen") as urlopen:
            upstream_response = MagicMock()
            upstream_response.__enter__.return_value.read.return_value = b""
            urlopen.return_value = upstream_response
            status, result = self.request_via_http_client("/api/slots/0/play")

        self.assertEqual(status, 200)
        self.assertEqual(
            result,
            {"played": True, "slot": 0, "track_number": 0},
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://homeassistant.local:8123/api/services/script/play_cd_track",
        )
        self.assertEqual(request.get_method(), "POST")
        authorization = request.get_header("Authorization")
        self.assertTrue(authorization.startswith("Bearer "))
        self.assertTrue(authorization.endswith(api_key))
        self.assertEqual(
            request.get_header("Content-type"),
            "application/json",
        )
        self.assertEqual(
            json.loads(request.data),
            {"cd_position": 0, "track_number": 0},
        )
        self.assertNotIn(api_key, json.dumps(result))

    def test_play_slot_requires_an_occupied_valid_slot_and_home_assistant_settings(self):
        cd_id = self.add_cd("Playback test")[1]["id"]
        self.request("/api/slots/2", "POST", {"cd_id": cd_id})
        self.assertEqual(self.request("/api/slots/1/play", "POST")[0], 400)
        self.assertEqual(self.request("/api/slots/101/play", "POST")[0], 400)
        self.assertEqual(self.request("/api/slots/-1/play", "POST")[0], 400)

        with patch.object(
            cd_app.config, "home_assistant_credentials", return_value=("", "")
        ):
            status, error = self.request("/api/slots/2/play", "POST")

        self.assertEqual(status, 503)
        self.assertIn("Settings", error["error"])

    def test_play_slot_sanitizes_home_assistant_http_errors(self):
        cd_id = self.add_cd("Playback test")[1]["id"]
        self.request("/api/slots/4", "POST", {"cd_id": cd_id})
        api_key = "private-home-assistant-key"
        with patch.object(
            cd_app.config, "home_assistant_credentials",
            return_value=("http://homeassistant.local:8123", api_key),
        ), patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError(
                "http://homeassistant.local:8123", 401, "Unauthorized", {}, None
            ),
        ):
            status, error = self.request_via_http_client("/api/slots/4/play")

        self.assertEqual(status, 502)
        self.assertIn("HTTP 401", error["error"])
        self.assertNotIn(api_key, json.dumps(error))

    def test_play_slot_sends_selected_track_position_to_home_assistant(self):
        cd_id = self.add_cd(
            "Selected track playback",
            tracks=[
                {"title": "First track"},
                {"title": "Second track"},
                {"title": "Third track"},
            ],
        )[1]["id"]
        self.request("/api/slots/7", "POST", {"cd_id": cd_id})
        api_key = "private-home-assistant-key"
        upstream_response = MagicMock()
        upstream_response.__enter__.return_value.read.return_value = b""
        with patch.object(
            cd_app.config,
            "home_assistant_credentials",
            return_value=("http://homeassistant.local:8123", api_key),
        ), patch.object(
            urllib.request,
            "urlopen",
            return_value=upstream_response,
        ) as urlopen:
            status, result = self.request_via_http_client(
                "/api/slots/7/play",
                payload={"track_number": 2},
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            result,
            {"played": True, "slot": 7, "track_number": 2},
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://homeassistant.local:8123/api/services/script/play_cd_track",
        )
        self.assertEqual(
            json.loads(request.data),
            {"cd_position": 7, "track_number": 2},
        )
        authorization = request.get_header("Authorization")
        self.assertTrue(authorization.startswith("Bearer "))
        self.assertTrue(authorization.endswith(api_key))

    def test_play_slot_rejects_nonexistent_or_invalid_track_numbers(self):
        cd_id = self.add_cd(
            "Track validation",
            tracks=[{"title": "First track"}],
        )[1]["id"]
        self.request("/api/slots/5", "POST", {"cd_id": cd_id})

        for payload in (
            {"track_number": 2},
            {"track_number": -1},
            {"track_number": "1"},
            {"track_number": True},
        ):
            with self.subTest(payload=payload):
                status, error = self.request(
                    "/api/slots/5/play",
                    "POST",
                    payload,
                )
                self.assertEqual(status, 400)
                self.assertIn("track", error["error"].lower())

    def test_home_assistant_transport_controls_post_to_configured_scripts(self):
        api_key = "private-home-assistant-key"
        script_names = {
            "stop": "stop",
            "pause": "pause",
            "play": "play",
            "power": "power",
            "next_track": "next_track",
            "previous_track": "previous_track",
        }
        upstream_response = MagicMock()
        upstream_response.__enter__.return_value.read.return_value = b""
        with patch.object(
            cd_app.config,
            "home_assistant_credentials",
            return_value=("http://homeassistant.local:8123", api_key),
        ), patch.object(
            urllib.request,
            "urlopen",
            return_value=upstream_response,
        ) as urlopen:
            for action, script_name in script_names.items():
                with self.subTest(action=action):
                    status, result = self.request_via_http_client(
                        f"/api/home-assistant/transport/{action}"
                    )

                    self.assertEqual(status, 200)
                    self.assertEqual(result, {"action": action, "sent": True})
                    request = urlopen.call_args.args[0]
                    self.assertEqual(
                        request.full_url,
                        f"http://homeassistant.local:8123/api/services/script/{script_name}",
                    )
                    self.assertEqual(request.get_method(), "POST")
                    self.assertIsNone(request.data)
                    self.assertEqual(
                        request.get_header("Authorization"),
                        f"Bearer {api_key}",
                    )
                    self.assertNotIn(api_key, json.dumps(result))

        self.assertEqual(urlopen.call_count, len(script_names))

    def test_discogs_consumer_credentials_are_sent_server_side(self):
        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "consumer key",
                "DISCOGS_CONSUMER_SECRET": "consumer secret",
            },
        ), patch.object(
            cd_app.metadata,
            "api_request",
            return_value={"results": [{"id": 12, "title": "Test Release"}]},
        ) as api_call:
            results = cd_app.discogs_search("Test Release")

        self.assertEqual(results[0]["title"], "Test Release")
        request_url = api_call.call_args.args[0]
        self.assertIn("key=consumer+key", request_url)
        self.assertIn("secret=consumer+secret", request_url)
        self.assertNotIn("consumer+secret", json.dumps(results))
        self.assertEqual(api_call.call_args.args[1], {})

    def test_discogs_search_applies_album_artist_and_year_filters(self):
        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "consumer-key",
                "DISCOGS_CONSUMER_SECRET": "consumer-secret",
            },
        ), patch.object(
            cd_app.metadata, "api_request", return_value={"results": []}
        ) as api_call:
            cd_app.discogs_search(title="Blue Train", artist="John Coltrane", year="1957")

        request_url = api_call.call_args.args[0]
        self.assertIn("release_title=Blue+Train", request_url)
        self.assertIn("artist=John+Coltrane", request_url)
        self.assertIn("year=1957", request_url)

    def test_discogs_artist_only_search_falls_back_to_general_query(self):
        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "consumer-key",
                "DISCOGS_CONSUMER_SECRET": "consumer-secret",
            },
        ), patch.object(
            cd_app.metadata,
            "api_request",
            side_effect=[
                {"results": []},
                {"results": [{"id": 42, "title": "Artist - Example Album"}]},
            ],
        ) as api_call:
            results = cd_app.discogs_search(artist="Radiohead")

        self.assertEqual(results[0]["title"], "Artist - Example Album")
        self.assertEqual(api_call.call_count, 2)
        self.assertIn("artist=Radiohead", api_call.call_args_list[0].args[0])
        self.assertIn("q=Radiohead", api_call.call_args_list[1].args[0])

    def test_discogs_artist_only_fallback_keeps_year_filter(self):
        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "consumer-key",
                "DISCOGS_CONSUMER_SECRET": "consumer-secret",
            },
        ), patch.object(
            cd_app.metadata,
            "api_request",
            side_effect=[{"results": []}, {"results": []}],
        ) as api_call:
            cd_app.discogs_search(artist="Radiohead", year="1997")

        self.assertIn("q=Radiohead", api_call.call_args_list[1].args[0])
        self.assertIn("year=1997", api_call.call_args_list[1].args[0])

    def test_musicbrainz_search_applies_all_provided_criteria(self):
        with patch.object(
            cd_app.metadata, "api_request", return_value={"releases": []}
        ) as api_call:
            cd_app.musicbrainz_search(title="Blue Train", artist="John Coltrane", year="1957")

        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(api_call.call_args.args[0]).query
        )["query"][0]
        self.assertIn('release:"Blue Train"', query)
        self.assertIn('artist:"John Coltrane"', query)
        self.assertIn("date:1957", query)
        self.assertIn(" AND ", query)

    def test_metadata_search_rejects_invalid_year(self):
        status, error = self.request(
            "/api/metadata/discogs?title=Blue+Train&year=57"
        )
        self.assertEqual(status, 400)
        self.assertIn("four-digit year", error["error"])

    def test_musicbrainz_release_lookup_includes_track_list_and_labels(self):
        release_id = "12345678-1234-1234-1234-123456789abc"
        release = {
            "id": release_id,
            "title": "Lookup Album",
            "date": "2001-05-06",
            "artist-credit": [{"artist": {"name": "Lookup Artist"}}],
            "label-info": [{"label": {"name": "Example Records"}, "catalog-number": "EX-01"}],
            "media": [{
                "tracks": [{
                    "title": "Opening",
                    "length": "180000",
                    "artist-credit": [{"artist": {"name": "Lookup Artist"}}],
                }],
            }],
        }
        with patch.object(cd_app.metadata, "api_request", return_value=release) as api_call:
            status, data = self.request(f"/api/metadata/musicbrainz/{release_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data["tracks"][0]["title"], "Opening")
        self.assertEqual(data["tracks"][0]["duration"], "180000")
        self.assertEqual(data["label"], "Example Records")
        self.assertIn("inc=artists+recordings+labels", api_call.call_args.args[0])

    def test_online_api_requests_use_trusted_certifi_bundle(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with patch.object(urllib.request, "urlopen", return_value=response) as urlopen:
            result = cd_app.api_request("https://musicbrainz.org/test")
        self.assertEqual(result, {"ok": True})
        context = urlopen.call_args.kwargs["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_database_survives_database_reinitialization(self):
        created = self.add_cd("Persistent album")[1]
        cd_app.initialize_db()
        self.assertEqual(self.request(f"/api/cds/{created['id']}")[1]["title"], "Persistent album")


if __name__ == "__main__":
    unittest.main()
