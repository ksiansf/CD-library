"""Compatibility entry point for the CD Library application."""

import os
import threading
from http.server import ThreadingHTTPServer

from cd_library import catalog, config, metadata, updates
from cd_library.catalog import (
    cd_payload,
    clean_text,
    connect_db,
    get_cd,
    initialize_db,
    parse_cover,
    parse_tracks,
    save_cd,
)
from cd_library.config import (
    APP_DIR,
    DATA_DIR,
    has_discogs_credentials,
    home_assistant_credentials,
    home_assistant_settings,
    read_saved_discogs_token,
    read_saved_settings,
    save_application_settings,
    validate_home_assistant_address,
)
from cd_library.http import CdLibraryHandler
from cd_library.metadata import (
    api_request,
    discogs_release,
    discogs_search,
    discogs_request,
    musicbrainz_artists,
    musicbrainz_release,
    musicbrainz_request,
    musicbrainz_search,
)

__all__ = [
    "APP_DIR",
    "DATA_DIR",
    "CdLibraryHandler",
    "api_request",
    "catalog",
    "cd_payload",
    "clean_text",
    "config",
    "connect_db",
    "discogs_release",
    "discogs_request",
    "discogs_search",
    "get_cd",
    "has_discogs_credentials",
    "home_assistant_credentials",
    "home_assistant_settings",
    "initialize_db",
    "metadata",
    "musicbrainz_artists",
    "musicbrainz_release",
    "musicbrainz_request",
    "musicbrainz_search",
    "parse_cover",
    "parse_tracks",
    "read_saved_discogs_token",
    "read_saved_settings",
    "save_application_settings",
    "save_cd",
    "validate_home_assistant_address",
    "updates",
]


def main():
    initialize_db()
    threading.Thread(
        target=catalog.migrate_remote_covers,
        name="cover-migration",
        daemon=True,
    ).start()
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
