# Wound-Care Billing Pipeline

A resilient pipeline that ingests messy clinical notes from a flaky EHR API,
extracts wound facts without hallucinating, and lets a non-technical biller
control the billing decisions.

> "The API is down 70% of the time and an LLM hallucinates. So we built a
> pipeline where neither can corrupt a billing decision. We cache raw data so
> downtime only costs time, we ground every extracted fact to its exact spot in
> the note so nothing is invented, and a deterministic, biller-configurable rule
> engine, not the model, makes the call."

## Architecture

```
FETCH ─▶ EXTRACT ─▶ RESOLVE ─▶ DECIDE ─▶ PRESENT
resilient  tiered +    aggregate   config-driven   Next.js +
client +   grounded    notes +     rule engine,    source-highlight
DuckDB     facts       wounds      3 states        dashboard
```

Each stage is decoupled and reads the stored result of the previous one, so the
whole pipeline is **resumable and reproducible: same input → same output.**

## Status

| Milestone | Scope | State |
|---|---|---|
| **1. Resilient fetch + raw store** | flaky-API client, DuckDB cache, resumable `fetch_state` | ✅ **Done** |
| 2. Tiered extraction | raw_json/regex + LangExtract/Gemini, grounded-or-null |  ✅ **Done** |
| 3. Resolve | multi-note/multi-wound aggregation per patient |  ✅ **Done** |
| 4. Decide | config-driven 3-state rule engine, templated reasons |  ✅ **Done** |
| 5. Dashboard | Next.js biller view + source-highlight |  ✅ **Done** |
| 6. Incremental + eval + observability polish | `since` watermark, golden set | ⬜ |

### Milestone 1 — verified results (against the REAL hackathon API)

Run end-to-end against the live `https://hackathon.prod.pulsefoundry.ai`:

```
coverage: 100.00% (1203/1203 complete, 0 outstanding, 0 quarantined)
raw records: patients=3 diagnoses=300 coverage=300 notes=300 assessments=300

requests_sent : 1706
throttled_429 :  503   (29.5% — matches the API's ~30% throttle)
real_failures :    0
success       : 1203
gave_up       :    0   ← 100% completeness despite the flaky API
```

300 patients across 3 facilities (real sizes are non-uniform: **120 / 90 / 90**).
The same code runs against the bundled mock by leaving `EHR_API_BASE_URL` at its
default — a config flip, nothing else.

- **Idempotent re-run:** running again does zero work (nothing pending, no re-fetch).
- **Resumable:** simulate a killed run (mark N tasks incomplete) → restart processes
  *only* those and restores completeness.
- **Circuit breaker:** 3-state logic (closed → open → half_open → closed) verified,
  and **429s never trip it** — only real failures do.
- **Edge cases:** `python -m evaluation.edge_cases` → **20/20** ingestion edge cases
  pass (deterministic, via `httpx.MockTransport`). See `EDGE_CASES.md`.

> **Real-data finding (why persisting raw matters):** the live API's payloads
> differ from API.md's idealized examples — assessment `raw_json` is a *nested
> survey* (structured Q&A **or** free-text "Wound narrative"), there are **4**
> note types (incl. undocumented "Wound (IDT)" / "Wound Care Progress Note"), and
> wound types include "Abscess". Because Milestone 1 caches the raw bytes, we
> discovered this **without re-fetching**, and Milestone 2 builds against reality.

## Quick start

```bash
# 1. Install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env

# 2a. EITHER run against the bundled mock (offline): start it in terminal A
.venv/bin/python -m mock_api.server          # faithful PCC stand-in, ~30% 429 + ~2% 5xx

# 2b. OR point at the real hackathon API (no server needed):
#     set EHR_API_BASE_URL=https://hackathon.prod.pulsefoundry.ai in .env

# 3. Run the resilient fetcher — caches all 300 patients to DuckDB
.venv/bin/python -m orchestration.runner            # run / resume
.venv/bin/python -m orchestration.runner --reset    # wipe state and start fresh
.venv/bin/python -m orchestration.runner --stats    # print status + coverage %

# 4. Inspect the raw cache (DuckDB queries raw JSON directly)
.venv/bin/python -m scripts.inspect_raw [optional_db_path]

# 5. Validate resilience + ingestion edge cases
.venv/bin/python -m scripts.test_breaker            # circuit-breaker invariants
.venv/bin/python -m evaluation.edge_cases           # 20/20 §1 ingestion edge cases
```

Point at any EHR API by setting `EHR_API_BASE_URL` (and `EHR_API_KEY`). The client
code is identical — only config changes. A contract change is handled by adding a
new adapter version (`EHR_ADAPTER_VERSION`) in `pipeline/fetch/ehr_adapter.py`.

## How the resilience works

| Signal | Meaning | Response |
|---|---|---|
| **429** | Normal throttling (~30% of requests) | Backoff + jitter, honor `Retry-After` (capped-backoff fallback if absent), ease global rate. **Never trips the breaker.** |
| **5xx / timeout / conn refused** | Endpoint actually unhealthy | Counts toward the breaker; trips it after N *consecutive* real failures. |
| **401** | Auth/credential expiry | Transient: run the refresh hook, retry. Never a data failure. |
| **Malformed / truncated body** | Data issue, not an outage | Retry a few times; if persistent → **quarantine**, keep going. |
| **200 but empty / null** | Legitimately no records | Mark `no_data` (complete, but distinct from "fetched with data"). |
| **Schema-invalid payload** | Bad record set | **Quarantine**; partial-bad batches keep their valid records. |
| **Unknown field (contract drift)** | API changed | Warn + keep the record (forward-compatible); add an adapter version when needed. |

- **Decoupled fetch/process:** the rest of the pipeline reads from DuckDB, never
  the live API. Downtime is a *latency* problem, never a *correctness* one.
- **Resumable work-queue (`fetch_state`):** `pending → fetched / no_data / failed /
  quarantined`. The queue self-expands (`patient lists → 4 resources/patient`) and
  self-reconciles on restart (idempotent enqueue). Resumable statuses are retried;
  `fetched`/`no_data` are terminal.
- **Honest coverage %:** a partial run reports exactly how complete it is, even if
  the API was down for part of the window.
- **Exactly-once fetch:** each resource is cached once; re-runs never re-hit the API.
- **Versioned adapter:** `pipeline/fetch/ehr_adapter.py` localizes the API contract
  (URLs, the two-ID model, schema validation) so a contract change is a swap.

## Project layout

```
backend/
├── pipeline/
│   ├── fetch/         # resilient client, circuit breaker, rate limiter, EHR adapter
│   ├── extract/       # (Milestone 2) tiered extractor + LangExtract/Gemini
│   ├── resolve/       # (Milestone 3) per-patient aggregation
│   └── decide/        # (Milestone 4) config-driven rule engine
├── store/             # DuckDB schema + DAO boundary (raw, fetch_state, quarantine)
├── config/            # env-driven settings (single config seam)
├── observability/     # structured logging + counters
├── orchestration/     # resumable fetch runner
├── mock_api/          # faithful PCC mock server + deterministic data generator
├── evaluation/        # edge-case validation harness (httpx.MockTransport)
├── scripts/           # raw inspection + breaker test
└── dashboard/         # (Milestone 5) Next.js biller view
```

See `AGENTS.md` for design rationale, `EDGE_CASES.md` for the edge-case coverage
matrix, and `API.md` for the upstream contract.
