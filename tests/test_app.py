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
        cd_app.DATA_DIR = Path(self.temp_dir.name)
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

    def test_page_is_served_and_discogs_reports_missing_token(self):
        page = urllib.request.urlopen(self.base_url + "/")
        self.assertEqual(page.status, 200)
        page_content = page.read()
        self.assertIn(b"Changer overview", page_content)
        self.assertLess(page_content.index(b'class="regular-slots"'), page_content.index(b'class="special-slot-area"'))
        self.assertIn(b'id="carousel-feature"', page_content)
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
        self.assertIn(b"/media/PD-F958.png", page_content)

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
            },
        ):
            status, error = self.request("/api/metadata/discogs?q=album")
        self.assertEqual(status, 503)
        self.assertIn("credentials", error["error"])

    def test_discogs_consumer_credentials_are_sent_server_side(self):
        with patch.dict(
            os.environ,
            {
                "DISCOGS_TOKEN": "",
                "DISCOGS_CONSUMER_KEY": "consumer key",
                "DISCOGS_CONSUMER_SECRET": "consumer secret",
            },
        ), patch.object(
            cd_app,
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
            cd_app, "api_request", return_value={"results": []}
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
            cd_app,
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
            cd_app,
            "api_request",
            side_effect=[{"results": []}, {"results": []}],
        ) as api_call:
            cd_app.discogs_search(artist="Radiohead", year="1997")

        self.assertIn("q=Radiohead", api_call.call_args_list[1].args[0])
        self.assertIn("year=1997", api_call.call_args_list[1].args[0])

    def test_musicbrainz_search_applies_all_provided_criteria(self):
        with patch.object(
            cd_app, "api_request", return_value={"releases": []}
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
        with patch.object(cd_app, "api_request", return_value=release) as api_call:
            status, data = self.request(f"/api/metadata/musicbrainz/{release_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data["tracks"][0]["title"], "Opening")
        self.assertEqual(data["tracks"][0]["duration"], "180000")
        self.assertEqual(data["label"], "Example Records")
        self.assertIn("inc=artists+recordings+labels", api_call.call_args.args[0])

    def test_online_api_requests_use_trusted_certifi_bundle(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with patch.object(cd_app.urllib.request, "urlopen", return_value=response) as urlopen:
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
