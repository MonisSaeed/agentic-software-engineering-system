from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .repository import ShortenerRepository
from .service import (
    IdempotencyConflict,
    InvalidLink,
    LinkConflict,
    LinkExpired,
    LinkNotFound,
    ShortenerService,
)


_CODE_PATH = re.compile(r"^/([A-Za-z0-9_-]{4,32})$")
_ANALYTICS_PATH = re.compile(r"^/api/v1/links/([A-Za-z0-9_-]{4,32})/analytics$")
_MAX_BODY_BYTES = 16_384


class InMemoryRateLimiter:
    def __init__(self, requests_per_minute: int, *, max_clients: int = 10_000) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self.requests_per_minute = requests_per_minute
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._clients: dict[str, tuple[float, int]] = {}

    def allow(self, client_key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            previous = self._clients.get(client_key)
            if previous is None or now - previous[0] >= 60:
                if len(self._clients) >= self.max_clients:
                    self._clients = {
                        key: value for key, value in self._clients.items() if now - value[0] < 60
                    }
                if len(self._clients) >= self.max_clients:
                    return False
                self._clients[client_key] = (now, 1)
                return True
            started, count = previous
            if count >= self.requests_per_minute:
                return False
            self._clients[client_key] = (started, count + 1)
            return True


def make_handler(service: ShortenerService, *, analytics_api_key: str | None, base_url: str):
    create_limiter = InMemoryRateLimiter(60)
    redirect_limiter = InMemoryRateLimiter(120)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ShortenerPrototype/1.0"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, object]:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as error:
                raise InvalidLink("invalid Content-Length") from error
            if length < 0 or length > _MAX_BODY_BYTES:
                raise InvalidLink("request body exceeds the size limit")
            if self.headers.get_content_type() != "application/json":
                raise InvalidLink("Content-Type must be application/json")
            try:
                data = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise InvalidLink("request body must be valid JSON") from error
            if not isinstance(data, dict):
                raise InvalidLink("request body must be a JSON object")
            return data

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/v1/links":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not create_limiter.allow(self.client_address[0]):
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
                return
            try:
                payload = self._read_json()
                if set(payload) - {"url", "custom_code", "expires_at"}:
                    raise InvalidLink("request contains unsupported fields")
                url = payload.get("url")
                custom_code = payload.get("custom_code")
                expires_at = payload.get("expires_at")
                if not isinstance(url, str):
                    raise InvalidLink("url is required and must be a string")
                if custom_code is not None and not isinstance(custom_code, str):
                    raise InvalidLink("custom_code must be a string")
                if expires_at is not None and not isinstance(expires_at, str):
                    raise InvalidLink("expires_at must be an ISO 8601 string")
                record, created = service.create_link(
                    url,
                    custom_code,
                    expires_at,
                    self.headers.get("Idempotency-Key"),
                )
                status = HTTPStatus.CREATED if created else HTTPStatus.OK
                self._json(
                    status,
                    {
                        "code": record.code,
                        "short_url": f"{base_url.rstrip('/')}/{record.code}",
                        "url": record.url,
                        "created_at": record.created_at,
                        "expires_at": record.expires_at,
                    },
                )
            except InvalidLink as error:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_request", "message": str(error)})
            except LinkConflict as error:
                self._json(HTTPStatus.CONFLICT, {"error": "code_conflict", "message": str(error)})
            except IdempotencyConflict as error:
                self._json(HTTPStatus.CONFLICT, {"error": "idempotency_conflict", "message": str(error)})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            analytics_match = _ANALYTICS_PATH.fullmatch(path)
            if analytics_match:
                if not analytics_api_key:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "analytics_not_configured"})
                    return
                supplied = self.headers.get("Authorization", "")
                expected = f"Bearer {analytics_api_key}"
                if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                try:
                    self._json(HTTPStatus.OK, service.analytics(analytics_match.group(1)))
                except LinkNotFound:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return

            code_match = _CODE_PATH.fullmatch(path)
            if not code_match:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not redirect_limiter.allow(self.client_address[0]):
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
                return
            try:
                target = service.redirect_target(code_match.group(1))
            except (LinkNotFound, LinkExpired):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    database: str = ".agentic_se/shortener.sqlite3",
    analytics_api_key: str | None = None,
    base_url: str | None = None,
) -> ThreadingHTTPServer:
    repository = ShortenerRepository(database)
    service = ShortenerService(repository)
    public_base_url = base_url or f"http://{host}:{port}"
    handler = make_handler(
        service,
        analytics_api_key=analytics_api_key,
        base_url=public_base_url,
    )
    return ThreadingHTTPServer((host, port), handler)


def serve_from_environment() -> None:
    host = os.environ.get("SHORTENER_HOST", "127.0.0.1")
    port = int(os.environ.get("SHORTENER_PORT", "8000"))
    database = os.environ.get("SHORTENER_DATABASE", ".agentic_se/shortener.sqlite3")
    api_key = os.environ.get("SHORTENER_ANALYTICS_API_KEY")
    server = create_server(host, port, database=database, analytics_api_key=api_key)
    print(f"URL shortener listening on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()