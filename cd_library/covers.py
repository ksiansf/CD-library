import ssl
import urllib.parse
import urllib.request

import certifi

from . import config

_COVER_TIMEOUT_SECONDS = 12


def _validate_cover_url(url):
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ValueError("Cover URL is invalid.")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Cover URL is invalid.") from error
    host = (parsed.hostname or "").lower()
    trusted_host = (
        host == "coverartarchive.org"
        or host.endswith(".discogs.com")
        or host == "discogs.com"
        or host == "archive.org"
        or host.endswith(".archive.org")
    )
    if (
        parsed.scheme != "https"
        or not trusted_host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != 443)
        or not parsed.path
    ):
        raise ValueError("Cover URL is not from a supported image provider.")
    return url


class _TrustedCoverRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        _validate_cover_url(new_url)
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


def download_cover_image(url):
    url = _validate_cover_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "CDLibrary/1.0",
            "Accept": "image/jpeg,image/png,image/webp,image/gif",
        },
    )
    tls_context = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(
        _TrustedCoverRedirectHandler(),
        urllib.request.HTTPSHandler(context=tls_context),
    )
    with opener.open(request, timeout=_COVER_TIMEOUT_SECONDS) as response:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                content_length = int(content_length)
            except ValueError as error:
                raise ValueError("Downloaded cover has an invalid size.") from error
            if content_length < 0:
                raise ValueError("Downloaded cover has an invalid size.")
            if content_length > config.MAX_COVER_BYTES:
                raise ValueError("Cover image must be smaller than 5 MB.")
        image_data = response.read(config.MAX_COVER_BYTES + 1)

    if not image_data or len(image_data) > config.MAX_COVER_BYTES:
        raise ValueError("Cover image must be smaller than 5 MB.")
    mime_type = config.image_mime_type(image_data)
    if mime_type not in config.ALLOWED_COVER_TYPES:
        raise ValueError("Downloaded cover is not a supported image.")
    return image_data, mime_type
