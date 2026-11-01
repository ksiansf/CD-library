import json
import os
import tempfile
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024
ALLOWED_COVER_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

def center_image_path():
    return DATA_DIR / "center-image"

def image_mime_type(image_data):
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(image_data) >= 12
        and image_data.startswith(b"RIFF")
        and image_data[8:12] == b"WEBP"
    ):
        return "image/webp"
    return ""

def save_center_image(image_data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".center-image-", suffix=".tmp", dir=DATA_DIR
    )
    try:
        with os.fdopen(file_descriptor, "wb") as image_file:
            image_file.write(image_data)
            image_file.flush()
            os.fsync(image_file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, center_image_path())
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise

def read_saved_settings():
    settings_path = DATA_DIR / "settings.json"
    try:
        with settings_path.open("r", encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise RuntimeError("Stored application settings are invalid.") from error
    if not isinstance(settings, dict):
        raise RuntimeError("Stored application settings are invalid.")
    for key in (
        "discogs_token",
        "home_assistant_api_key",
        "home_assistant_address",
    ):
        if not isinstance(settings.get(key, ""), str):
            raise RuntimeError("Stored application settings are invalid.")
    return settings

def read_saved_discogs_token():
    return read_saved_settings().get("discogs_token", "").strip()

def save_application_settings(updates):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings = read_saved_settings()
    settings.update(updates)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".settings-", suffix=".tmp", dir=DATA_DIR
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as settings_file:
            json.dump(settings, settings_file)
            settings_file.flush()
            os.fsync(settings_file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, DATA_DIR / "settings.json")
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise

def home_assistant_settings():
    saved_settings = read_saved_settings()
    return {
        "address": saved_settings.get(
            "home_assistant_address",
            os.environ.get("HOME_ASSISTANT_ADDRESS", ""),
        ).strip(),
        "api_key_configured": bool(
            saved_settings.get("home_assistant_api_key", "").strip()
            or os.environ.get("HOME_ASSISTANT_API_KEY", "").strip()
        ),
    }

def home_assistant_credentials():
    saved_settings = read_saved_settings()
    address = saved_settings.get(
        "home_assistant_address",
        os.environ.get("HOME_ASSISTANT_ADDRESS", ""),
    ).strip()
    api_key = saved_settings.get(
        "home_assistant_api_key",
        os.environ.get("HOME_ASSISTANT_API_KEY", ""),
    ).strip()
    return address, api_key

def validate_home_assistant_address(address):
    address = address.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(address)
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        valid_port = False
        parsed = None
    if (
        not address
        or parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not valid_port
    ):
        raise ValueError("Enter a valid Home Assistant address using HTTP or HTTPS.")
    return address

def has_discogs_credentials():
    return bool(
        read_saved_discogs_token()
        or os.environ.get("DISCOGS_TOKEN", "").strip()
        or (
            os.environ.get("DISCOGS_CONSUMER_KEY", "").strip()
            and os.environ.get("DISCOGS_CONSUMER_SECRET", "").strip()
        )
    )
