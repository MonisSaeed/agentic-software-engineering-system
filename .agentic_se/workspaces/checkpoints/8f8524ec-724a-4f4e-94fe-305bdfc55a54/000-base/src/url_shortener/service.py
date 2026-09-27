from __future__ import annotations

import re
import hashlib
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .repository import LinkRecord, ShortenerRepository


_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,32}$")


class InvalidLink(ValueError):
    pass


class LinkConflict(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class LinkNotFound(LookupError):
    pass


class LinkExpired(LookupError):
    pass


class ShortenerService:
    def __init__(self, repository: ShortenerRepository) -> None:
        self.repository = repository

    def create_link(
        self,
        url: str,
        custom_code: str | None = None,
        expires_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[LinkRecord, bool]:
        if not isinstance(url, str) or not 1 <= len(url) <= 2048:
            raise InvalidLink("url must contain between 1 and 2048 characters")
        try:
            parsed = urlsplit(url)
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            valid_port = False
            parsed = urlsplit("")
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not valid_port:
            raise InvalidLink("url must be an absolute HTTP or HTTPS URL")
        if parsed.username or parsed.password:
            raise InvalidLink("URLs containing credentials are not accepted")
        if custom_code is not None and not _CODE_PATTERN.fullmatch(custom_code):
            raise InvalidLink("custom_code must be 4-32 letters, numbers, '_' or '-'")
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
            raise InvalidLink("Idempotency-Key must contain 1-128 characters")
        normalized_expiry = self._normalize_expiry(expires_at)
        stored_key = (
            hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            if idempotency_key is not None
            else None
        )
        try:
            return self.repository.create(url, custom_code, normalized_expiry, stored_key)
        except ValueError as error:
            if "idempotency key" in str(error):
                raise IdempotencyConflict(str(error)) from error
            raise
        except sqlite3.IntegrityError as error:
            if custom_code is not None:
                raise LinkConflict("custom_code is already in use") from error
            raise

    @staticmethod
    def _normalize_expiry(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise InvalidLink("expires_at must be an ISO 8601 timestamp") from error
        if parsed.tzinfo is None:
            raise InvalidLink("expires_at must include a timezone")
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= datetime.now(timezone.utc):
            raise InvalidLink("expires_at must be in the future")
        return parsed.isoformat()

    def redirect_target(self, code: str) -> str:
        record = self.repository.find(code)
        if record is None:
            raise LinkNotFound(code)
        if record.expires_at is not None:
            expiry = datetime.fromisoformat(record.expires_at)
            if datetime.now(timezone.utc) >= expiry:
                raise LinkExpired(code)
        self.repository.record_click(code, datetime.now(timezone.utc).isoformat())
        return record.url

    def analytics(self, code: str) -> dict[str, object]:
        result = self.repository.analytics(code)
        if result is None:
            raise LinkNotFound(code)
        return result