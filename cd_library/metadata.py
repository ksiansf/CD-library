import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request

import certifi

from . import config

USER_AGENT = (
    "CDLibrary/1.0 "
    f"({os.environ.get('CD_LIBRARY_CONTACT', 'personal collection manager')})"
)
_musicbrainz_lock = threading.Lock()
_musicbrainz_last_request = 0.0

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
    token = config.read_saved_discogs_token() or os.environ.get("DISCOGS_TOKEN", "").strip()
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
