#!/usr/bin/env python3

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

APP_DIR = Path(os.environ.get("APP_DIR", "/opt/groovebox")).resolve()
DATA_DIR = APP_DIR / "data"
REQUEST_PATH = DATA_DIR / "update-request.json"
STATUS_PATH = DATA_DIR / "update-status.json"
API_RELEASE_URL = "https://api.github.com/repos/ksiansf/CD-library/releases/latest"
API_ARCHIVE_URL = "https://api.github.com/repos/ksiansf/CD-library/tarball/"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
PROTECTED_ROOTS = {".git", ".env", ".venv", "data", "__pycache__"}


class UpdateError(RuntimeError):
    pass


def write_status(request_id, state, message):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = STATUS_PATH.with_name(f".{STATUS_PATH.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as status_file:
            json.dump(
                {"state": state, "request_id": request_id, "message": message},
                status_file,
            )
            status_file.flush()
            os.fsync(status_file.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, STATUS_PATH)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def latest_release():
    request = urllib.request.Request(
        API_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Groovebox-host-updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname != "api.github.com":
                raise UpdateError("GitHub returned an unexpected release URL.")
            content = response.read(1024 * 1024 + 1)
    except UpdateError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise UpdateError("Could not contact GitHub to check the latest release.") from error
    if len(content) > 1024 * 1024:
        raise UpdateError("GitHub returned an oversized release response.")
    try:
        release = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise UpdateError("GitHub returned an invalid release response.") from error
    if not isinstance(release, dict):
        raise UpdateError("GitHub returned an invalid release response.")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag
    ):
        raise UpdateError("GitHub returned an invalid release tag.")
    return tag


def download_archive(tag):
    url = API_ARCHIVE_URL + urllib.parse.quote(tag, safe="")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Groovebox-host-updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname not in {
                "api.github.com",
                "codeload.github.com",
            }:
                raise UpdateError("GitHub returned an unexpected archive URL.")
            content = response.read(MAX_ARCHIVE_BYTES + 1)
    except UpdateError:
        raise
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise UpdateError("Could not download the GitHub release archive.") from error
    if len(content) > MAX_ARCHIVE_BYTES:
        raise UpdateError("The GitHub release archive is too large.")
    return content


def extract_archive(content, destination):
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:gz")
    except tarfile.TarError as error:
        raise UpdateError("The GitHub release archive is invalid.") from error

    top_level = None
    extracted_paths = set()
    extracted_size = 0
    with archive:
        for member in archive:
            name = member.name
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or "\x00" in name
                or re.match(r"^[A-Za-z]:", name)
            ):
                raise UpdateError("The GitHub release archive contains an unsafe path.")
            parts = PurePosixPath(name).parts
            if any(part in {"", ".", ".."} for part in parts):
                raise UpdateError("The GitHub release archive contains an unsafe path.")
            if top_level is None:
                top_level = parts[0]
            if parts[0] != top_level:
                raise UpdateError("The GitHub release archive has an invalid layout.")
            relative = PurePosixPath(*parts[1:])
            if not relative.parts:
                continue
            if relative.parts[0] in PROTECTED_ROOTS:
                continue
            if not (member.isdir() or member.isfile()):
                raise UpdateError("The GitHub release archive contains an unsafe file.")
            target = destination.joinpath(*relative.parts)
            if target in extracted_paths:
                raise UpdateError("The GitHub release archive contains duplicate paths.")
            extracted_paths.add(target)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            extracted_size += member.size
            if extracted_size > MAX_EXTRACTED_BYTES:
                raise UpdateError("The expanded GitHub release archive is too large.")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise UpdateError("The GitHub release archive contains an unreadable file.")
            with source, target.open("xb") as output_file:
                shutil.copyfileobj(source, output_file)
            os.chmod(target, member.mode & 0o755)

    if not all(
        (destination / required).exists()
        for required in ("app.py", "Dockerfile", "compose.yaml", "cd_library", "static")
    ):
        raise UpdateError("The GitHub release does not contain a complete application.")


def reject_source_symlinks():
    for root, directories, files in os.walk(APP_DIR, followlinks=False):
        relative = Path(root).relative_to(APP_DIR)
        if relative.parts and relative.parts[0] in PROTECTED_ROOTS:
            directories.clear()
            continue
        for name in directories + files:
            path = Path(root) / name
            if path.is_symlink():
                raise UpdateError("The application directory contains a symbolic link.")
        if not relative.parts:
            directories[:] = [
                name for name in directories if name not in PROTECTED_ROOTS
            ]


def restore_application(snapshot):
    for path in APP_DIR.iterdir():
        if path.name in PROTECTED_ROOTS:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    shutil.copytree(
        snapshot,
        APP_DIR,
        dirs_exist_ok=True,
        copy_function=shutil.copy2,
    )


def apply_release(tag, archive_content, temporary_directory):
    staging = Path(temporary_directory) / "release"
    staging.mkdir()
    extract_archive(archive_content, staging)
    reject_source_symlinks()
    snapshot = Path(temporary_directory) / "previous"
    shutil.copytree(
        APP_DIR,
        snapshot,
        ignore=shutil.ignore_patterns(*PROTECTED_ROOTS),
    )
    shutil.copytree(
        staging,
        APP_DIR,
        dirs_exist_ok=True,
        copy_function=shutil.copy2,
    )
    (APP_DIR / "VERSION").write_text(tag + "\n", encoding="utf-8")
    try:
        subprocess.run(
            ["bash", "./install.sh"],
            cwd=APP_DIR,
            check=True,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError):
        restore_application(snapshot)
        restart = subprocess.run(
            ["docker", "compose", "up", "--build", "--detach"],
            cwd=APP_DIR,
            check=False,
            timeout=120,
        )
        if restart.returncode:
            print("Could not restart the previous Groovebox container.", file=sys.stderr)
        raise


def process_request():
    if not REQUEST_PATH.is_file():
        return
    try:
        with REQUEST_PATH.open("r", encoding="utf-8") as request_file:
            payload = json.load(request_file)
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        request_id = str(uuid.UUID(request_id))
    except (OSError, json.JSONDecodeError, ValueError, AttributeError) as error:
        REQUEST_PATH.unlink(missing_ok=True)
        write_status("", "failed", "The update request was invalid.")
        raise UpdateError("The update request was invalid.") from error
    REQUEST_PATH.unlink()
    write_status(request_id, "updating", "Downloading and installing the release.")

    try:
        tag = latest_release()
        version_path = APP_DIR / "VERSION"
        try:
            current_version = version_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise UpdateError("Could not read the installed application version.") from error
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", current_version):
            raise UpdateError("The installed application version is invalid.")
        if tag == current_version:
            write_status(request_id, "no_update", "The application is already up to date.")
            return
        archive_content = download_archive(tag)
        with tempfile.TemporaryDirectory(prefix="groovebox-update-") as temporary_directory:
            apply_release(tag, archive_content, temporary_directory)
    except (
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
        TimeoutError,
        urllib.error.URLError,
        UpdateError,
    ) as error:
        print(f"Groovebox update failed: {error}", file=sys.stderr, flush=True)
        write_status(
            request_id,
            "failed",
            "Update failed. Check the host service log for details.",
        )
        raise
    write_status(request_id, "completed", f"Updated to release {tag}.")


def main():
    sys.path.insert(0, str(APP_DIR))
    process_request()


if __name__ == "__main__":
    main()
