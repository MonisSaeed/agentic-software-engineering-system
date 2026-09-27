from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .runtime import ExecutionEvent, WorkflowContext


_SECRET_KEY = re.compile(r"(secret|password|token|api[_-]?key|credential)", re.IGNORECASE)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~-]+"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password)\b\s*[:=]\s*['\"]?[^'\"\s,;]+"),
)


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


class SQLiteRunStore:
    """Small durable run/event store for local prototypes and demonstrations."""

    def __init__(self, database: str | Path = ".agentic_se/runs.sqlite3") -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    graph_revision INTEGER NOT NULL,
                    context_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    kind TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run_idx ON events(run_id, sequence);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_event(self, event: ExecutionEvent, context: WorkflowContext) -> None:
        safe_context = _redact(
            {
                "workflow_name": context.workflow_name,
                "run_id": context.run_id,
                "graph_revision": context.graph_revision,
                "state": context.state,
                "outputs": context.outputs,
                "attempts": context.attempts,
                "lineage": context.lineage,
                "metadata": context.metadata,
            }
        )
        payload = _redact(asdict(event))
        event_json = json.dumps(payload, ensure_ascii=True, default=str)
        context_json = json.dumps(safe_context, ensure_ascii=True, default=str)
        status = {
            "run_started": "running",
            "run_completed": "completed",
            "safe_stop": "safe_stopped",
        }.get(event.kind, "running")
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO runs
                   (run_id, workflow_name, status, started_at, ended_at, graph_revision, context_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     status=excluded.status,
                     ended_at=CASE WHEN excluded.status IN ('completed', 'safe_stopped')
                                   THEN excluded.ended_at ELSE runs.ended_at END,
                     graph_revision=excluded.graph_revision,
                     context_json=excluded.context_json""",
                (
                    event.run_id,
                    event.workflow_name,
                    status,
                    event.timestamp,
                    event.timestamp if status in ("completed", "safe_stopped") else None,
                    event.graph_revision,
                    context_json,
                ),
            )
            connection.execute(
                "INSERT INTO events (run_id, kind, timestamp, payload_json) VALUES (?, ?, ?, ?)",
                (event.run_id, event.kind, event.timestamp, event_json),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["context"] = json.loads(result.pop("context_json"))
        return result

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, workflow_name, status, started_at, ended_at, graph_revision "
                "FROM runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, kind, timestamp, payload_json FROM events "
                "WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "kind": row["kind"],
                "timestamp": row["timestamp"],
                **json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def metrics(self) -> dict[str, Any]:
        with self._connect() as connection:
            runs = connection.execute("SELECT status, started_at, ended_at FROM runs").fetchall()
            events = connection.execute("SELECT run_id, kind, timestamp FROM events ORDER BY sequence").fetchall()
        completed = [run for run in runs if run["status"] == "completed"]
        terminal = [run for run in runs if run["status"] in ("completed", "safe_stopped", "failed")]
        successful = len(completed)
        success_rate = successful / len(terminal) if terminal else 0.0
        retry_count = sum(row["kind"] == "retry_scheduled" for row in events)
        rollback_count = sum(row["kind"] == "rollback" for row in events)
        by_run: dict[str, list[tuple[str, str]]] = {}
        for event in events:
            by_run.setdefault(event["run_id"], []).append((event["kind"], event["timestamp"]))
        mttr: list[float] = []
        for timeline in by_run.values():
            failure_at = next(
                (stamp for kind, stamp in timeline if kind in ("step_failed", "safe_stop")), None
            )
            end_at = next(
                (stamp for kind, stamp in reversed(timeline) if kind in ("run_completed", "safe_stop")), None
            )
            if failure_at and end_at:
                mttr.append(max(0.0, (datetime.fromisoformat(end_at) - datetime.fromisoformat(failure_at)).total_seconds()))
        latencies = [
            max(0.0, (datetime.fromisoformat(run["ended_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds())
            for run in terminal
            if run["ended_at"]
        ]
        return {
            "runs_total": len(runs),
            "runs_completed": successful,
            "runs_safe_stopped": sum(run["status"] == "safe_stopped" for run in runs),
            "success_rate": success_rate,
            "retry_count": retry_count,
            "rollback_count": rollback_count,
            "mttr_seconds": sum(mttr) / len(mttr) if mttr else 0.0,
            "end_to_end_latency_seconds": sum(latencies) / len(latencies) if latencies else 0.0,
        }

    def __call__(self, event: ExecutionEvent, context: WorkflowContext) -> None:
        self.record_event(event, context)
