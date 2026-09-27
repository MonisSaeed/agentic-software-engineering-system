import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from url_shortener.http import InMemoryRateLimiter, create_server


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class ShortenerHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = f"{self.directory.name}/shortener.sqlite3"
        self.server = create_server(
            port=0,
            database=self.database,
            analytics_api_key="test-key",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.no_redirect = build_opener(NoRedirect())

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def post_link(self, body, headers=None):
        request = Request(
            f"{self.base_url}/api/v1/links",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        return urlopen(request)

    def test_create_redirect_and_protected_analytics(self):
        response = self.post_link({"url": "https://example.com/article", "custom_code": "article1"})
        self.assertEqual(response.status, 201)
        created = json.loads(response.read())

        with self.assertRaises(HTTPError) as redirect_error:
            self.no_redirect.open(f"{self.base_url}/article1")
        self.assertEqual(redirect_error.exception.code, 302)
        self.assertEqual(redirect_error.exception.headers["Location"], "https://example.com/article")

        unauthorized = Request(f"{self.base_url}/api/v1/links/article1/analytics")
        with self.assertRaises(HTTPError) as auth_error:
            urlopen(unauthorized)
        self.assertEqual(auth_error.exception.code, 401)

        authorized = Request(
            f"{self.base_url}/api/v1/links/article1/analytics",
            headers={"Authorization": "Bearer test-key"},
        )
        analytics = json.loads(urlopen(authorized).read())
        self.assertEqual(analytics["total_clicks"], 1)
        self.assertEqual(sum(analytics["daily_clicks"].values()), 1)
        self.assertTrue(created["short_url"].endswith("/article1"))

    def test_invalid_url_and_duplicate_code_are_rejected(self):
        with self.assertRaises(HTTPError) as invalid:
            self.post_link({"url": "file:///etc/passwd"})
        self.assertEqual(invalid.exception.code, 422)

        self.post_link({"url": "https://example.com", "custom_code": "taken1"})
        with self.assertRaises(HTTPError) as duplicate:
            self.post_link({"url": "https://other.example", "custom_code": "taken1"})
        self.assertEqual(duplicate.exception.code, 409)

    def test_idempotent_create_returns_same_link(self):
        headers = {"Idempotency-Key": "request-123"}
        first = self.post_link({"url": "https://example.com"}, headers)
        first_payload = json.loads(first.read())
        second = self.post_link({"url": "https://example.com"}, headers)
        second_payload = json.loads(second.read())
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 200)
        self.assertEqual(first_payload["code"], second_payload["code"])
        with self.assertRaises(HTTPError) as conflict:
            self.post_link({"url": "https://different.example"}, headers)
        self.assertEqual(conflict.exception.code, 409)

    def test_health_and_in_memory_rate_limiter(self):
        health = json.loads(urlopen(f"{self.base_url}/healthz").read())
        self.assertEqual(health["status"], "ok")
        limiter = InMemoryRateLimiter(1)
        self.assertTrue(limiter.allow("127.0.0.1"))
        self.assertFalse(limiter.allow("127.0.0.1"))

    def test_expired_link_is_not_redirected(self):
        self.post_link({"url": "https://example.com", "custom_code": "oldlink"})
        with sqlite3.connect(self.database) as connection:
            expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            connection.execute(
                "UPDATE short_links SET expires_at = ? WHERE code = ?",
                (expired_at, "oldlink"),
            )
        with self.assertRaises(HTTPError) as missing:
            self.no_redirect.open(f"{self.base_url}/oldlink")
        self.assertEqual(missing.exception.code, 404)


if __name__ == "__main__":
    unittest.main()