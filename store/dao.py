"""Data-access boundary over DuckDB.

The rest of the pipeline talks to this class, never to DuckDB directly. That seam
is what makes "DuckDB -> Postgres/warehouse later" a config change, not a rewrite.

DuckDB connections are not thread-safe to share, but a single connection is fine
when callers serialize writes. The fetch runner does its concurrent network I/O
off-DB and funnels persistence through one writer, so we keep one connection here
guarded by a lock for safety.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

import duckdb

from config import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class Store:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.STORE.duckdb_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = duckdb.connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._con.execute(SCHEMA_PATH.read_text())

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # --- context manager sugar ---
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # fetch_state work-queue
    # ------------------------------------------------------------------
    def enqueue_task(
        self,
        *,
        resource_type: str,
        resource_id: str,
        url: str,
        patient_id: str | None = None,
        facility_id: str | None = None,
    ) -> str:
        """Insert a pending task if it does not already exist. Idempotent."""
        task_key = f"{resource_type}:{resource_id}"
        with self._lock:
            self._con.execute(
                """
                INSERT INTO fetch_state
                    (task_key, resource_type, resource_id, patient_id, facility_id, url, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT (task_key) DO NOTHING
                """,
                [task_key, resource_type, resource_id, patient_id, facility_id, url],
            )
        return task_key

    def pending_tasks(self) -> list[dict[str, Any]]:
        """Tasks still needing work. `fetched` and `no_data` are terminal-complete;
        `pending`/`failed`/`quarantined` are resumable (the latter retried in case
        a truncated body or transient contract issue has since resolved)."""
        with self._lock:
            rows = self._con.execute(
                """
                SELECT task_key, resource_type, resource_id, patient_id, facility_id, url, attempts
                FROM fetch_state
                WHERE status IN ('pending', 'failed', 'quarantined')
                ORDER BY enqueued_at
                """
            ).fetchall()
            cols = [d[0] for d in self._con.description]
        return [dict(zip(cols, r)) for r in rows]

    def mark_fetched(self, task_key: str, *, http_status: int, attempts: int) -> None:
        with self._lock:
            self._con.execute(
                """
                UPDATE fetch_state
                SET status='fetched', attempts=?, last_http_status=?, last_error=NULL, updated_at=now()
                WHERE task_key=?
                """,
                [attempts, http_status, task_key],
            )

    def mark_failed(self, task_key: str, *, error: str, http_status: int | None, attempts: int) -> None:
        with self._lock:
            self._con.execute(
                """
                UPDATE fetch_state
                SET status='failed', attempts=?, last_http_status=?, last_error=?, updated_at=now()
                WHERE task_key=?
                """,
                [attempts, http_status, error[:1000], task_key],
            )

    def mark_no_data(self, task_key: str, *, http_status: int, attempts: int) -> None:
        """200 OK but no records: legitimately complete, but flagged distinctly so
        it never looks like 'fetched with data'."""
        with self._lock:
            self._con.execute(
                """
                UPDATE fetch_state
                SET status='no_data', attempts=?, last_http_status=?, last_error=NULL, updated_at=now()
                WHERE task_key=?
                """,
                [attempts, http_status, task_key],
            )

    def mark_quarantined(
        self, task_key: str, *, resource_type: str, resource_id: str,
        reason: str, http_status: int | None, attempts: int,
        raw_excerpt: str | None = None,
    ) -> None:
        with self._lock:
            self._con.execute(
                """
                UPDATE fetch_state
                SET status='quarantined', attempts=?, last_http_status=?, last_error=?, updated_at=now()
                WHERE task_key=?
                """,
                [attempts, http_status, reason[:1000], task_key],
            )
            self._con.execute(
                """
                INSERT INTO quarantine
                    (task_key, resource_type, resource_id, reason, http_status, raw_excerpt)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [task_key, resource_type, resource_id, reason[:1000], http_status,
                 (raw_excerpt or "")[:2000]],
            )

    # ------------------------------------------------------------------
    # raw_records
    # ------------------------------------------------------------------
    def upsert_raw(
        self,
        *,
        resource_type: str,
        resource_id: str,
        payload: Any,
        patient_id: str | None = None,
        facility_id: str | None = None,
        source_url: str | None = None,
        last_modified_at: str | None = None,
        http_status: int | None = None,
    ) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO raw_records
                    (resource_type, resource_id, patient_id, facility_id,
                     source_url, last_modified_at, http_status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (resource_type, resource_id) DO UPDATE SET
                    patient_id=excluded.patient_id,
                    facility_id=excluded.facility_id,
                    source_url=excluded.source_url,
                    last_modified_at=excluded.last_modified_at,
                    fetched_at=now(),
                    http_status=excluded.http_status,
                    payload=excluded.payload
                """,
                [
                    resource_type,
                    resource_id,
                    patient_id,
                    facility_id,
                    source_url,
                    last_modified_at,
                    http_status,
                    json.dumps(payload),
                ],
            )

    def get_raw(self, resource_type: str, resource_id: str) -> Any | None:
        with self._lock:
            row = self._con.execute(
                "SELECT payload FROM raw_records WHERE resource_type=? AND resource_id=?",
                [resource_type, resource_id],
            ).fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------------
    # fetch_events (observability audit trail)
    # ------------------------------------------------------------------
    def log_event(
        self,
        *,
        task_key: str,
        attempt: int,
        outcome: str,
        http_status: int | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO fetch_events (task_key, attempt, outcome, http_status, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                [task_key, attempt, outcome, http_status, (detail or "")[:1000]],
            )

    # ------------------------------------------------------------------
    # reporting helpers
    # ------------------------------------------------------------------
    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._con.execute(
                "SELECT status, COUNT(*) FROM fetch_state GROUP BY status"
            ).fetchall()
        return {status: int(n) for status, n in rows}

    def coverage_report(self) -> dict[str, Any]:
        """Completeness of the fetch. `fetched` and `no_data` are complete;
        anything else is outstanding. This is the honest 'coverage %' we report
        even when the API was down for part of the window."""
        counts = self.status_counts()
        complete = counts.get("fetched", 0) + counts.get("no_data", 0)
        total = sum(counts.values())
        outstanding = total - complete
        pct = round(100.0 * complete / total, 2) if total else 0.0
        return {
            "total_tasks": total,
            "complete": complete,
            "outstanding": outstanding,
            "coverage_pct": pct,
            "by_status": counts,
            "quarantined": counts.get("quarantined", 0),
        }

    def count_quarantine(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0])

    def count_raw(self, resource_type: str | None = None) -> int:
        with self._lock:
            if resource_type:
                row = self._con.execute(
                    "SELECT COUNT(*) FROM raw_records WHERE resource_type=?",
                    [resource_type],
                ).fetchone()
            else:
                row = self._con.execute("SELECT COUNT(*) FROM raw_records").fetchone()
        return int(row[0])

    def query(self, sql: str, params: Iterable[Any] | None = None) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, list(params) if params else None).fetchall()
