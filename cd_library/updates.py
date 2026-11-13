import json
import os
import re
import ssl
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

import certifi

from . import config

GITHUB_RELEASE_URL = "https://api.github.com/repos/ksiansf/CD-library/releases/latest"
MAX_RELEASE_RESPONSE_BYTES = 1024 * 1024
UPDATE_HELPER_MARKER = ".update-helper-installed"
_enqueue_lock = threading.Lock()


class ReleaseCheckError(RuntimeError):
    pass


class UpdateRequestError(RuntimeError):
    pass


def current_version():
    try:
        version = (config.APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("Could not read the installed application version.") from error
    if not version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version):
        raise RuntimeError("The installed application version is invalid.")
    return version


def fetch_latest_release():
    request = urllib.request.Request(
        GITHUB_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Groovebox-release-check",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=10,
            context=ssl.create_default_context(cafile=certifi.where()),
        ) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname != "api.github.com":
                raise ReleaseCheckError("GitHub returned an unexpected release URL.")
            content = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except ReleaseCheckError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise ReleaseCheckError("Could not check GitHub for a published release.") from error
    if len(content) > MAX_RELEASE_RESPONSE_BYTES:
        raise ReleaseCheckError("GitHub returned an oversized release response.")
    try:
        release = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReleaseCheckError("GitHub returned an invalid release response.") from error
    if not isinstance(release, dict):
        raise ReleaseCheckError("GitHub returned an invalid release response.")

    tag = release.get("tag_name")
    release_url = release.get("html_url")
    published_at = release.get("published_at")
    if (
        not isinstance(tag, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag)
        or not isinstance(release_url, str)
        or not isinstance(published_at, str)
    ):
        raise ReleaseCheckError("GitHub returned incomplete release information.")
    try:
        parsed_url = urllib.parse.urlsplit(release_url)
    except ValueError as error:
        raise ReleaseCheckError("GitHub returned an unexpected release link.") from error
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or parsed_url.path != f"/ksiansf/CD-library/releases/tag/{tag}"
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ReleaseCheckError("GitHub returned an unexpected release link.")
    return {
        "tag_name": tag,
        "html_url": release_url,
        "published_at": published_at,
    }


def helper_is_available():
    return (config.DATA_DIR / UPDATE_HELPER_MARKER).is_file()


def check_for_updates():
    release = fetch_latest_release()
    version = current_version()
    return {
        "current_version": version,
        "latest_version": release["tag_name"],
        "published_at": release["published_at"],
        "release_url": release["html_url"],
        "update_available": release["tag_name"] != version,
        "update_helper_available": helper_is_available(),
    }


def read_status():
    status_path = config.DATA_DIR / "update-status.json"
    try:
        with status_path.open("r", encoding="utf-8") as status_file:
            status = json.load(status_file)
    except FileNotFoundError:
        return {"state": "idle"}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Could not read update status.") from error
    if not isinstance(status, dict) or status.get("state") not in {
        "queued",
        "updating",
        "completed",
        "failed",
        "no_update",
    }:
        raise RuntimeError("Stored update status is invalid.")
    return {
        key: status[key]
        for key in ("state", "request_id", "message")
        if key in status and isinstance(status[key], str)
    }


def _write_json_atomically(path, payload, exclusive=False):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=config.DATA_DIR
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output_file:
            json.dump(payload, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(temporary_path, 0o600)
        if exclusive:
            os.link(temporary_path, path)
            os.unlink(temporary_path)
        else:
            os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def enqueue_update():
    if not helper_is_available():
        raise UpdateRequestError("The host update service is not installed.")
    with _enqueue_lock:
        status = read_status()
        if status["state"] in {"queued", "updating"}:
            raise UpdateRequestError("An application update is already in progress.")

        request_id = str(uuid.uuid4())
        request_path = config.DATA_DIR / "update-request.json"
        if request_path.exists():
            raise UpdateRequestError("An application update is already queued.")
        _write_json_atomically(
            config.DATA_DIR / "update-status.json",
            {
                "state": "queued",
                "request_id": request_id,
                "message": "Update request queued.",
            },
        )
        try:
            _write_json_atomically(
                request_path, {"request_id": request_id}, exclusive=True
            )
        except FileExistsError as error:
            raise UpdateRequestError(
                "An application update is already queued."
            ) from error
        return request_id
