from __future__ import annotations

import secrets
import sqlite3
import string
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_CODE_ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True)
class LinkRecord:
    code: str
    url: str
    created_at: str
    expires_at: str | None


class ShortenerRepository:
    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS short_links (
                    code TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    idempotency_key TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS click_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL REFERENCES short_links(code),
                    clicked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS click_events_code_time_idx
                    ON click_events(code, clicked_at);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> LinkRecord:
        return LinkRecord(row["code"], row["target_url"], row["created_at"], row["expires_at"])

    def create(
        self,
        url: str,
        custom_code: str | None,
        expires_at: str | None,
        idempotency_key: str | None,
        *,
        max_collisions: int = 5,
    ) -> tuple[LinkRecord, bool]:
        if idempotency_key:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT * FROM short_links WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            if existing is not None:
                record = self._record(existing)
                if record.url != url or record.expires_at != expires_at:
                    raise ValueError("idempotency key was already used for a different request")
                return record, False

        candidate = custom_code
        for _ in range(1 if custom_code else max_collisions):
            if candidate is None:
                candidate = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(7))
            created_at = datetime.now(timezone.utc).isoformat()
            try:
                with self._connect() as connection:
                    connection.execute(
                        "INSERT INTO short_links (code, target_url, created_at, expires_at, idempotency_key) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (candidate, url, created_at, expires_at, idempotency_key),
                    )
                    row = connection.execute(
                        "SELECT * FROM short_links WHERE code = ?", (candidate,)
                    ).fetchone()
                return self._record(row), True
            except sqlite3.IntegrityError:
                if idempotency_key:
                    with self._connect() as connection:
                        existing = connection.execute(
                            "SELECT * FROM short_links WHERE idempotency_key = ?",
                            (idempotency_key,),
                        ).fetchone()
                    if existing is not None:
                        record = self._record(existing)
                        if record.url != url or record.expires_at != expires_at:
                            raise ValueError("idempotency key was already used for a different request")
                        return record, False
                if custom_code:
                    raise
                candidate = None
        raise RuntimeError("could not allocate a unique short code after bounded retries")

    def find(self, code: str) -> LinkRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM short_links WHERE code = ?", (code,)).fetchone()
        return self._record(row) if row else None

    def record_click(self, code: str, clicked_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO click_events (code, clicked_at) VALUES (?, ?)",
                (code, clicked_at),
            )

    def analytics(self, code: str) -> dict[str, object] | None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM short_links WHERE code = ?", (code,)
            ).fetchone()
            if exists is None:
                return None
            total = connection.execute(
                "SELECT COUNT(*) AS total FROM click_events WHERE code = ?", (code,)
            ).fetchone()["total"]
            daily = connection.execute(
                "SELECT substr(clicked_at, 1, 10) AS day, COUNT(*) AS clicks "
                "FROM click_events WHERE code = ? GROUP BY day ORDER BY day",
                (code,),
            ).fetchall()
        return {"code": code, "total_clicks": total, "daily_clicks": {row["day"]: row["clicks"] for row in daily}}