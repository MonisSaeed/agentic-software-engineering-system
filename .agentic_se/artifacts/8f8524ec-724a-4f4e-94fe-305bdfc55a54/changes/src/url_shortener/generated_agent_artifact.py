"""Deterministic code artifact emitted by the offline SDLC demo."""

from urllib.parse import urlsplit


def is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False
