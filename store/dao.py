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
    def __init__(self, db_path: str | None = None, *, read_only: bool = False) -> None:
        self.db_path = db_path or settings.STORE.duckdb_path
        self._read_only = read_only
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = duckdb.connect(self.db_path, read_only=read_only)
        if not read_only:
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

    def query_dicts(self, sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._con.execute(sql, list(params) if params else None)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------------
    # raw iteration helpers (used by extract / resolve stages)
    # ------------------------------------------------------------------
    def iter_raw(self, resource_type: str) -> list[dict[str, Any]]:
        """All cached rows for a resource type, payload already JSON-decoded."""
        rows = self.query_dicts(
            """
            SELECT resource_id, patient_id, facility_id, last_modified_at, payload
            FROM raw_records WHERE resource_type=? ORDER BY resource_id
            """,
            [resource_type],
        )
        for r in rows:
            r["payload"] = json.loads(r["payload"]) if r["payload"] else None
        return rows

    # ------------------------------------------------------------------
    # extractions (Milestone 2)
    # ------------------------------------------------------------------
    def upsert_extraction(
        self, *, internal_id: int, patient_id: str | None, facility_id: str | None,
        source_type: str, source_id: str, wound_index: int, tier: str,
        facts: Any, confidence: float, needs_review: bool,
        review_reasons: Any, source_hash: str, extractor_version: str,
    ) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO extractions
                    (internal_id, patient_id, facility_id, source_type, source_id,
                     wound_index, tier, facts, confidence, needs_review,
                     review_reasons, source_hash, extractor_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (internal_id, source_type, source_id, wound_index) DO UPDATE SET
                    patient_id=excluded.patient_id, facility_id=excluded.facility_id,
                    tier=excluded.tier, facts=excluded.facts,
                    confidence=excluded.confidence, needs_review=excluded.needs_review,
                    review_reasons=excluded.review_reasons, source_hash=excluded.source_hash,
                    extractor_version=excluded.extractor_version, created_at=now()
                """,
                [internal_id, patient_id, facility_id, source_type, source_id,
                 wound_index, tier, json.dumps(facts), confidence, needs_review,
                 json.dumps(review_reasons), source_hash, extractor_version],
            )

    def extractions_for(self, internal_id: int) -> list[dict[str, Any]]:
        rows = self.query_dicts(
            "SELECT * FROM extractions WHERE internal_id=? ORDER BY source_type, source_id, wound_index",
            [internal_id],
        )
        for r in rows:
            r["facts"] = json.loads(r["facts"]) if r.get("facts") else {}
            r["review_reasons"] = json.loads(r["review_reasons"]) if r.get("review_reasons") else []
        return rows

    def existing_extraction_hash(self, internal_id: int, source_type: str, source_id: str) -> str | None:
        row = self.query(
            "SELECT source_hash FROM extractions WHERE internal_id=? AND source_type=? AND source_id=? LIMIT 1",
            [internal_id, source_type, source_id],
        )
        return row[0][0] if row else None

    # ------------------------------------------------------------------
    # canonical_patient (Milestone 3)
    # ------------------------------------------------------------------
    def upsert_canonical(self, rec: dict[str, Any]) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO canonical_patient
                    (internal_id, patient_id, facility_id, first_name, last_name,
                     primary_payer_code, coverage, diagnoses, primary_wound,
                     all_wounds, n_notes, n_assessments, needs_review,
                     review_reasons, source_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (internal_id) DO UPDATE SET
                    patient_id=excluded.patient_id, facility_id=excluded.facility_id,
                    first_name=excluded.first_name, last_name=excluded.last_name,
                    primary_payer_code=excluded.primary_payer_code,
                    coverage=excluded.coverage, diagnoses=excluded.diagnoses,
                    primary_wound=excluded.primary_wound, all_wounds=excluded.all_wounds,
                    n_notes=excluded.n_notes, n_assessments=excluded.n_assessments,
                    needs_review=excluded.needs_review, review_reasons=excluded.review_reasons,
                    source_hash=excluded.source_hash, resolved_at=now()
                """,
                [rec["internal_id"], rec.get("patient_id"), rec.get("facility_id"),
                 rec.get("first_name"), rec.get("last_name"), rec.get("primary_payer_code"),
                 json.dumps(rec.get("coverage")), json.dumps(rec.get("diagnoses")),
                 json.dumps(rec.get("primary_wound")), json.dumps(rec.get("all_wounds")),
                 rec.get("n_notes", 0), rec.get("n_assessments", 0),
                 rec.get("needs_review", False), json.dumps(rec.get("review_reasons", [])),
                 rec.get("source_hash")],
            )

    def all_canonical(self) -> list[dict[str, Any]]:
        rows = self.query_dicts("SELECT * FROM canonical_patient ORDER BY internal_id")
        for r in rows:
            for col in ("coverage", "diagnoses", "primary_wound", "all_wounds", "review_reasons"):
                r[col] = json.loads(r[col]) if r.get(col) else None
        return rows

    def get_canonical(self, internal_id: int) -> dict[str, Any] | None:
        rows = self.query_dicts("SELECT * FROM canonical_patient WHERE internal_id=?", [internal_id])
        if not rows:
            return None
        r = rows[0]
        for col in ("coverage", "diagnoses", "primary_wound", "all_wounds", "review_reasons"):
            r[col] = json.loads(r[col]) if r.get(col) else None
        return r

    # ------------------------------------------------------------------
    # decisions (Milestone 4)
    # ------------------------------------------------------------------
    def upsert_decision(self, rec: dict[str, Any]) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO decisions
                    (internal_id, patient_id, facility_id, decision, reasons,
                     eligibility, rules_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (internal_id) DO UPDATE SET
                    patient_id=excluded.patient_id, facility_id=excluded.facility_id,
                    decision=excluded.decision, reasons=excluded.reasons,
                    eligibility=excluded.eligibility, rules_version=excluded.rules_version,
                    decided_at=now()
                """,
                [rec["internal_id"], rec.get("patient_id"), rec.get("facility_id"),
                 rec["decision"], json.dumps(rec.get("reasons", [])),
                 json.dumps(rec.get("eligibility", {})), rec.get("rules_version")],
            )

    def all_decisions(self) -> list[dict[str, Any]]:
        rows = self.query_dicts("SELECT * FROM decisions ORDER BY internal_id")
        for r in rows:
            r["reasons"] = json.loads(r["reasons"]) if r.get("reasons") else []
            r["eligibility"] = json.loads(r["eligibility"]) if r.get("eligibility") else {}
        return rows

    def decision_counts(self) -> dict[str, int]:
        rows = self.query("SELECT decision, COUNT(*) FROM decisions GROUP BY decision")
        return {d: int(n) for d, n in rows}

    # ------------------------------------------------------------------
    # watermarks (Milestone 6 — incremental `since`)
    # ------------------------------------------------------------------
    def get_watermark(self, resource_type: str) -> str | None:
        row = self.query("SELECT since_value FROM watermarks WHERE resource_type=?", [resource_type])
        return row[0][0] if row else None

    def set_watermark(self, resource_type: str, since_value: str) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO watermarks (resource_type, since_value) VALUES (?, ?)
                ON CONFLICT (resource_type) DO UPDATE SET
                    since_value=excluded.since_value, updated_at=now()
                """,
                [resource_type, since_value],
            )

    def max_last_modified(self, resource_type: str) -> str | None:
        row = self.query(
            "SELECT MAX(last_modified_at) FROM raw_records WHERE resource_type=?",
            [resource_type],
        )
        return str(row[0][0]) if row and row[0][0] is not None else None

    # ------------------------------------------------------------------
    # biller_overrides (Milestone 5 — human approval)
    # ------------------------------------------------------------------
    def get_override(self, internal_id: int) -> dict[str, Any] | None:
        rows = self.query_dicts(
            "SELECT * FROM biller_overrides WHERE internal_id=?", [internal_id]
        )
        if not rows:
            return None
        r = rows[0]
        r["optional_fields"] = json.loads(r["optional_fields"]) if r.get("optional_fields") else []
        return r

    def all_overrides(self) -> dict[int, dict[str, Any]]:
        rows = self.query_dicts("SELECT * FROM biller_overrides")
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            r["optional_fields"] = json.loads(r["optional_fields"]) if r.get("optional_fields") else []
            out[r["internal_id"]] = r
        return out

    def upsert_override(
        self, *, internal_id: int, patient_id: str | None,
        approved: bool, optional_fields: list[str] | None = None,
        biller_note: str | None = None,
    ) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO biller_overrides
                    (internal_id, patient_id, approved, optional_fields, biller_note, approved_at)
                VALUES (?, ?, ?, ?, ?, CASE WHEN ? THEN now() ELSE NULL END)
                ON CONFLICT (internal_id) DO UPDATE SET
                    patient_id=excluded.patient_id,
                    approved=excluded.approved,
                    optional_fields=excluded.optional_fields,
                    biller_note=excluded.biller_note,
                    approved_at=CASE WHEN excluded.approved THEN now() ELSE biller_overrides.approved_at END,
                    updated_at=now()
                """,
                [internal_id, patient_id, approved,
                 json.dumps(optional_fields or []), biller_note or "", approved],
            )

    def effective_decision_counts(self) -> dict[str, int]:
        """Decision counts after biller approvals (approved review -> bill)."""
        overrides = self.all_overrides()
        counts: dict[str, int] = {"bill": 0, "flag_for_review": 0, "do_not_bill": 0}
        for d in self.all_decisions():
            eff = self._effective_decision_value(d, overrides.get(d["internal_id"]))
            counts[eff] = counts.get(eff, 0) + 1
        return counts

    @staticmethod
    def _effective_decision_value(decision: dict[str, Any], override: dict[str, Any] | None) -> str:
        sys_dec = decision.get("decision", "flag_for_review")
        if override and override.get("approved") and sys_dec == "flag_for_review":
            return "bill"
        return sys_dec
