"""SQLite journal that prevents replayed leases from repeating local side effects."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class JournalEntry:
    job_id: str
    idempotency_key: str
    capability: str
    lease_generation: int
    status: str
    result: dict[str, Any] | None
    result_digest: str | None


class ExecutionJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self.path = path
        self.connection = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS executions (
                idempotency_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                lease_generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                result_digest TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_executions_job_id ON executions(job_id);
            CREATE TABLE IF NOT EXISTS execution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        self._secure_database_files()

    def _secure_database_files(self) -> None:
        for candidate in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def close(self) -> None:
        self.connection.close()
        self._secure_database_files()

    def get(self, idempotency_key: str) -> JournalEntry | None:
        row = self.connection.execute(
            """
            SELECT job_id, idempotency_key, capability, lease_generation, status,
                   result_json, result_digest
            FROM executions WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if not row:
            return None
        result = json.loads(row[5]) if row[5] else None
        return JournalEntry(
            job_id=row[0],
            idempotency_key=row[1],
            capability=row[2],
            lease_generation=row[3],
            status=row[4],
            result=result,
            result_digest=row[6],
        )

    def start(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        capability: str,
        lease_generation: int,
    ) -> JournalEntry | None:
        """Record a start, returning an existing execution when this is a replay."""

        existing = self.get(idempotency_key)
        if existing:
            self.record_event(existing, "lease_replayed", {"job_id": job_id})
            return existing
        timestamp = utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO executions (
                    idempotency_key, job_id, capability, lease_generation, status,
                    started_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    idempotency_key,
                    job_id,
                    capability,
                    lease_generation,
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO execution_events (
                    job_id, idempotency_key, event_type, details_json, created_at
                ) VALUES (?, ?, 'started', '{}', ?)
                """,
                (job_id, idempotency_key, timestamp),
            )
        return None

    def complete(self, idempotency_key: str, result: dict[str, Any]) -> JournalEntry:
        serialized = json.dumps(result, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        timestamp = utc_now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE executions
                SET status = 'completed', result_json = ?, result_digest = ?,
                    completed_at = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (serialized, digest, timestamp, timestamp, idempotency_key),
            )
        entry = self.get(idempotency_key)
        if not entry:
            raise RuntimeError("Journal completion updated no execution.")
        self.record_event(entry, "completed", {"result_digest": digest})
        return entry

    def fail(self, idempotency_key: str, *, error_code: str) -> None:
        entry = self.get(idempotency_key)
        if not entry:
            return
        timestamp = utc_now()
        with self.connection:
            self.connection.execute(
                """
                UPDATE executions SET status = 'failed', updated_at = ?
                WHERE idempotency_key = ?
                """,
                (timestamp, idempotency_key),
            )
        self.record_event(entry, "failed", {"error_code": error_code})

    def record_event(
        self, entry: JournalEntry, event_type: str, details: dict[str, Any] | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO execution_events (
                    job_id, idempotency_key, event_type, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entry.job_id,
                    entry.idempotency_key,
                    event_type,
                    json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
                    utc_now(),
                ),
            )
