# AGENTS.md — repo guide & conventions

Wound-care billing pipeline. Built around three threats and three answers:

1. **API down ~70%** → decouple fetch from process; cache raw data. Downtime
   costs time, never correctness.
2. **LLMs hallucinate** → ground every extracted fact to its exact span in the
   note; ungrounded values are dropped ("grounded or null").
3. **A model shouldn't decide claims** → a deterministic, biller-configurable
   rule engine makes the call. The LLM only extracts facts and advises.

## Conventions

- **Config seam:** nothing reads `os.environ` directly — everything goes through
  `config/settings.py`. Pointing at a real EHR API or swapping the DB is a config
  change, not a code change.
- **Storage seam:** the pipeline talks to `store/dao.py` (`Store`), never DuckDB
  directly. This is what makes "DuckDB → Postgres/warehouse later" a substitution.
- **Stages are decoupled:** each stage reads the persisted output of the previous
  one (`raw_records` → `extractions` → `canonical_patient` → `decisions`). Same
  input → same output.
- **429 ≠ failure:** only 5xx / timeouts / connection errors are "real failures"
  that feed the circuit breaker. Never trip the breaker on throttling.
- **Determinism:** the mock dataset is fully seeded (`MOCK_SEED`); rule decisions
  are pure functions of `(canonical_patient, rules_config)`.

## Module map

| Path | Responsibility |
|---|---|
| `config/settings.py` | All tunables (fetch resilience, store path, mock behavior, LLM). |
| `store/schema.sql` | DuckDB schema: `raw_records`, `fetch_state`, `fetch_events`. |
| `store/dao.py` | `Store` — the only thing that touches DuckDB. |
| `observability/metrics.py` | `get_logger`, thread-safe `COUNTERS`. |
| `pipeline/fetch/circuit_breaker.py` | 3-state breaker (closed/open/half_open). |
| `pipeline/fetch/rate_limiter.py` | Adaptive (AIMD) global rate limiter. |
| `pipeline/fetch/client.py` | `ResilientClient` — backoff/jitter, Retry-After, 429-vs-breaker, 401-refresh, malformed-retry. |
| `pipeline/fetch/ehr_adapter.py` | **Versioned PCC adapter**: URLs, two-ID model, pydantic schema validation. The only place that knows the API shape. |
| `orchestration/runner.py` | `FetchRunner` — resumable, self-expanding, validating work-queue. |
| `mock_api/server.py` | Faithful PCC mock (FastAPI) injecting 429/5xx/malformed/empty/drift. |
| `mock_api/data_gen.py` | Deterministic dataset calibrated to the **real** API (see findings below). |
| `evaluation/edge_cases.py` | 20/20 ingestion edge-case harness (httpx.MockTransport). |
| `scripts/inspect_raw.py` | DuckDB querying raw JSON directly; parses real nested `raw_json`. |
| `scripts/test_breaker.py` | Proves breaker invariants. |

## Real-API findings (calibrated from live responses)

API.md's examples were idealized. The live API actually returns:
- **Facility sizes are non-uniform:** 101=120, 102=90, 103=90 (300 total). Internal
  `id` is global 1..300; string `patient_id` is FA-/FB-/FC-###.
- **Two-layer identity:** diagnoses/coverage keyed by **string** `patient_id`;
  notes/assessments keyed by **integer** `id`. The adapter resolves both.
- **4 note types** (not 3): `Wound (SPN)`, `HP Skin & Wound Note`,
  `Wound Care Progress Note`, `Wound (IDT)` (Envive). HP notes can hold >1 wound.
- **`assessment.raw_json` is a nested survey**, in two variants: structured Q&A
  sections (LOCATION/WOUND/DRAINAGE/WOUND_BED) **or** a free-text "Wound narrative".
- **Coverage** is a single record; `payer_type` is `Medicare`/`Medicaid`/`Managed Care`.
- Wound types include `Abscess`, `Burn`, etc. beyond the doc's examples.

Because M1 persists raw bytes, all of this was discovered without re-fetching, and
the mock `data_gen.py` is calibrated to match it.

## Data flow (Milestone 1)

```
runner.bootstrap()  enqueue facilities task; reconcile children from cached parents
runner.run()        loop: pending_tasks → ResilientClient.get → upsert_raw + mark_fetched
                          facilities → enqueue patient-list tasks
                          patients   → enqueue 4 tasks/patient (diagnoses,coverage,notes,assessments)
```

`fetch_state.status ∈ {pending, fetched, failed}`. `pending_tasks()` returns
`pending ∪ failed`, so restarts resume automatically.

## Running

See `README.md` "Quick start". TL;DR: start `mock_api.server`, then
`python -m orchestration.runner`.

## Scope discipline

Build only what earns a rubric line. Cross-provider dedup is *designed, not built*
(the API returns one record per patient → it would demo as a no-op); Resolve is
made idempotent so it's correct whether or not duplicates exist. LangGraph/dlt are
optional showcases, not MVP plumbing.
