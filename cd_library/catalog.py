import base64
import binascii
from contextlib import contextmanager
import http.client
import json
import sqlite3

from . import config, covers

@contextmanager
def connect_db():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DATA_DIR / "cd-library.sqlite3", timeout=10)
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


def migrate_remote_covers():
    with connect_db() as db:
        rows = db.execute(
            """SELECT id, cover_url FROM cds
            WHERE cover_data = '' AND cover_url != '' ORDER BY id"""
        ).fetchall()

    migrated = 0
    failed = 0
    for row in rows:
        try:
            image_data, mime_type = covers.download_cover_image(row["cover_url"])
        except (http.client.HTTPException, OSError, TimeoutError, ValueError) as error:
            failed += 1
            print(f"Could not migrate cover for CD {row['id']}: {error}")
            continue

        encoded = base64.b64encode(image_data).decode("ascii")
        with connect_db() as db:
            cursor = db.execute(
                """UPDATE cds SET cover_data = ?, cover_mime = ?, cover_url = ''
                WHERE id = ? AND cover_data = '' AND cover_url = ?""",
                (encoded, mime_type, row["id"], row["cover_url"]),
            )
        migrated += cursor.rowcount

    if rows:
        print(
            f"Cover migration finished: {migrated} downloaded, "
            f"{failed} still using their saved URLs."
        )
    return migrated, failed


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
    if mime not in config.ALLOWED_COVER_TYPES:
        raise ValueError("Cover image must be JPEG, PNG, WebP, or GIF.")
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Cover image data is invalid.") from error
    if not binary or len(binary) > config.MAX_COVER_BYTES:
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
