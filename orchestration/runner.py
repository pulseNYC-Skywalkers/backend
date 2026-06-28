"""Fetch orchestrator — resumable, self-expanding, schema-validating work-queue.

The queue starts with one `patients` task per facility constant (101/102/103 —
the PCC contract has no /facilities endpoint) and expands:
    patients -> 4 patient-scoped tasks each (diagnoses, coverage, notes, assessments)

Per-resource handling (the §1 ingestion edge cases):
    * malformed/truncated body  -> client retries, then QUARANTINE (keep going)
    * schema-invalid payload     -> QUARANTINE the record set
    * 200-but-empty / null       -> mark no_data (complete, but not "has data")
    * short patient list         -> warn (possible pagination gap)
    * contract drift (new field) -> warn, keep the record (forward-compatible)

Everything persists in `fetch_state`, so a killed run resumes; a partial run
reports an honest coverage %. Each resource is fetched exactly once and cached.

Usage:
    python -m orchestration.runner            # run / resume
    python -m orchestration.runner --reset     # wipe state and start fresh
    python -m orchestration.runner --stats     # print status + coverage
"""

from __future__ import annotations

import argparse
import asyncio

import httpx

from config import settings
from observability.metrics import COUNTERS, get_logger
from pipeline.fetch.client import ResilientClient
from pipeline.fetch.ehr_adapter import PATIENT_SUBRESOURCES, get_adapter
from store.dao import Store

log = get_logger("runner")


class FetchRunner:
    def __init__(self, store: Store, client: ResilientClient, adapter) -> None:
        self.store = store
        self.client = client
        self.adapter = adapter

    # --- queue bootstrap / reconciliation (idempotent) ---------------------
    def bootstrap(self) -> None:
        for fac in settings.FETCH.facility_ids:
            self.store.enqueue_task(
                resource_type="patients",
                resource_id=str(fac),
                url=self.adapter.patients_url(fac),
                facility_id=str(fac),
            )
        # Re-derive children from any already-cached patient lists (self-healing).
        for (fac_str,) in self.store.query(
            "SELECT resource_id FROM raw_records WHERE resource_type='patients'"
        ):
            payload = self.store.get_raw("patients", fac_str)
            for p in payload or []:
                self._enqueue_patient_tasks(p, fac_str)

    def _enqueue_patient_tasks(self, patient_rec: dict, facility_id: str) -> None:
        keys = self.adapter.patient_keys(patient_rec)
        sid = keys["patient_id_str"]
        for sub in PATIENT_SUBRESOURCES:
            key = keys[sub]
            self.store.enqueue_task(
                resource_type=sub,
                resource_id=str(key),
                url=self.adapter.subresource_url(sub, key),
                patient_id=sid,
                facility_id=facility_id,
            )

    # --- per-task processing ----------------------------------------------
    async def _process(self, task: dict, sem: asyncio.Semaphore) -> None:
        async with sem:
            result = await self.client.get(task["task_key"], task["url"])

        rtype = task["resource_type"]

        # Transport-level non-success (gave_up / malformed / not_found).
        if not result.ok:
            if result.outcome == "malformed":
                self.store.mark_quarantined(
                    task["task_key"], resource_type=rtype, resource_id=task["resource_id"],
                    reason=result.error or "malformed body",
                    http_status=result.http_status, attempts=result.attempts,
                )
                COUNTERS.incr("quarantined")
                log.warning("QUARANTINE %s (%s)", task["task_key"], result.error)
            else:
                self.store.mark_failed(
                    task["task_key"], error=result.error or result.outcome,
                    http_status=result.http_status, attempts=result.attempts,
                )
                log.warning("FAILED %s (%s)", task["task_key"], result.error)
            return

        # Schema-validate the payload on arrival.
        vr = self.adapter.validate(rtype, result.payload)
        for w in vr.warnings:
            COUNTERS.incr("validation_warnings")
            log.warning("contract-drift %s: %s", task["task_key"], w)

        if not vr.ok:
            self.store.mark_quarantined(
                task["task_key"], resource_type=rtype, resource_id=task["resource_id"],
                reason="; ".join(vr.errors)[:500] or "schema validation failed",
                http_status=result.http_status, attempts=result.attempts,
                raw_excerpt=str(result.payload)[:2000],
            )
            COUNTERS.incr("quarantined")
            log.warning("QUARANTINE %s (schema: %s)", task["task_key"], vr.errors[:1])
            return

        if vr.is_empty:
            self.store.mark_no_data(
                task["task_key"], http_status=result.http_status or 200,
                attempts=result.attempts,
            )
            COUNTERS.incr("no_data")
            return

        # Persist the raw payload and mark fetched.
        self.store.upsert_raw(
            resource_type=rtype, resource_id=task["resource_id"], payload=result.payload,
            patient_id=task.get("patient_id"), facility_id=task.get("facility_id"),
            source_url=task["url"], last_modified_at=result.last_modified_at,
            http_status=result.http_status,
        )
        self.store.mark_fetched(
            task["task_key"], http_status=result.http_status or 200, attempts=result.attempts
        )

        # Expand: a patient list spawns 4 tasks per patient + a count check.
        if rtype == "patients":
            expected = settings.FETCH.expected_patients_per_facility
            if expected and vr.n_records < expected:
                COUNTERS.incr("short_patient_lists")
                log.warning("facility %s returned %d/%d patients (possible pagination gap)",
                            task["resource_id"], vr.n_records, expected)
            for p in vr.records:
                self._enqueue_patient_tasks(p, task["resource_id"])

    async def run(self) -> None:
        self.bootstrap()
        sem = asyncio.Semaphore(settings.FETCH.max_concurrency)
        processed: set[str] = set()
        while True:
            batch = [t for t in self.store.pending_tasks() if t["task_key"] not in processed]
            if not batch:
                break
            log.info("processing %d task(s) (concurrency=%d)",
                     len(batch), settings.FETCH.max_concurrency)
            await asyncio.gather(*(self._process(t, sem) for t in batch))
            processed.update(t["task_key"] for t in batch)


def print_stats(store: Store) -> None:
    cov = store.coverage_report()
    log.info("coverage: %.2f%% (%d/%d complete, %d outstanding, %d quarantined)",
             cov["coverage_pct"], cov["complete"], cov["total_tasks"],
             cov["outstanding"], cov["quarantined"])
    log.info("fetch_state: %s", cov["by_status"])
    for rt in ["patients", *PATIENT_SUBRESOURCES]:
        log.info("  raw[%s] = %d", rt, store.count_raw(rt))
    log.info("counters:\n%s", COUNTERS.render())


async def _main_async(reset: bool) -> None:
    store = Store()
    if reset:
        for t in ("fetch_state", "raw_records", "fetch_events", "quarantine"):
            store.query(f"DELETE FROM {t}")
        log.info("state reset")

    adapter = get_adapter(settings.FETCH.adapter_version, settings.FETCH.base_url)
    timeout = httpx.Timeout(settings.FETCH.request_timeout_seconds)
    headers = {}
    if settings.FETCH.api_key:
        headers["Authorization"] = f"Bearer {settings.FETCH.api_key}"

    async def refresh_auth() -> None:
        # Hook point: refresh OAuth token / mTLS cert here. No-op for the mock.
        log.info("auth refresh requested (no-op for mock)")

    async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                follow_redirects=True) as http:
        client = ResilientClient(
            client=http,
            on_event=lambda tk, att, outcome, st, detail: store.log_event(
                task_key=tk, attempt=att, outcome=outcome, http_status=st, detail=detail
            ),
            on_auth_refresh=refresh_auth,
        )
        runner = FetchRunner(store, client, adapter)
        await runner.run()

    print_stats(store)
    store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Resilient EHR fetch runner")
    parser.add_argument("--reset", action="store_true", help="wipe state and start fresh")
    parser.add_argument("--stats", action="store_true", help="print status and exit")
    args = parser.parse_args()

    if args.stats:
        store = Store()
        print_stats(store)
        store.close()
        return
    asyncio.run(_main_async(reset=args.reset))


if __name__ == "__main__":
    main()
