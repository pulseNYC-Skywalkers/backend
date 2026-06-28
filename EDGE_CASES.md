# Edge-Case Coverage Matrix

Maps every edge case from the spec to its handling and current status.

**Legend:** ✅ implemented & validated now · 🧪 covered by an automated test
(`evaluation/edge_cases.py`, 20/20 passing) · 🟡 designed, lands in the named
milestone.

Milestone 1 (resilient fetch + raw store) is **complete and validated against the
live API**. Extraction / Resolve / Decide / Dashboard are Milestones 2–5.

---

## 1. Ingestion / API resilience  — ✅ all implemented (M1)

| Edge case | Status | How |
|---|---|---|
| Malformed / truncated payload | ✅ 🧪 | Retry a few times (data issue, not outage); persistent → `quarantine`, keep going. `client.py` + `mark_quarantined`. |
| 429 throttling mid-run | ✅ 🧪 | Backoff + jitter, honor `Retry-After`, adaptive global rate-limiter; **does not** trip the breaker. |
| True outage (5xx / timeout / refused) | ✅ 🧪 | 3-state circuit breaker (`circuit_breaker.py`); pending IDs stay resumable. |
| API down for the whole window | ✅ | Resumable `fetch_state`; partial run reports an honest `coverage %`. |
| API contract change (renamed field / new enum / format) | ✅ 🧪 | On-arrival schema validation flags unknown fields (warn, keep); versioned adapter (`EHR_ADAPTER_VERSION`). **Found live:** real note types & `raw_json` differ from API.md. |
| Auth / credential expiry mid-run (401) | ✅ 🧪 | Detect 401 → `on_auth_refresh` hook → retry; treated as transient, not data failure. |
| Pagination gaps (list drops a page) | ✅ | Per-facility count check (opt-in; real sizes are non-uniform 120/90/90). Mechanism in `runner.py`. |
| 200-but-empty / null record | ✅ 🧪 | `mark_no_data` — complete but distinct from "fetched with data"; never looks done-with-data. |
| No `Retry-After` header on a 429 | ✅ 🧪 | Capped exponential backoff fallback. |

## 2. Extraction / LLM — 🟡 Milestone 2

| Edge case | Status | Plan |
|---|---|---|
| Unseen note format | 🟡 M2 | Schema-enforced output; missing required → low confidence → `flag_for_review`. (We already cache **4** real note formats to build against.) |
| Hallucination / low confidence | 🟡 M2 | Grounded-or-null (LangExtract source spans); rules decide, not the LLM; templated reasons. |
| LLM provider rate-limited / down | 🟡 M2 | Same resilient-client pattern reused for the LLM; hash-cache; tiered extraction skips the LLM for structured data. |
| Invalid JSON / broken schema from model | 🟡 M2 | Controlled generation + one bounded stricter retry → else flag. |
| Empty / garbage note | 🟡 M2 | No fields → flag; never fabricate. |
| Conflict within one note | 🟡 M2 | Surface conflict, flag (don't auto-pick). |
| Note longer than context window | 🟡 M2 | Chunk + multi-pass (LangExtract `extraction_passes`). |
| Negation ("no signs of infection") | 🟡 M2 | Grounding + polarity check vs source. |
| Missing / ambiguous units (cm vs mm) | 🟡 M2 | Normalize + range/sanity-check; flag if ambiguous. **Real data has terse units** ("4.3x4.4x1.3cm"). |
| Non-deterministic extraction | 🟡 M2 | Temperature 0; extract once + hash-cache; pin model for golden eval. |

## 3. Deduplication / Resolve — 🟡 Milestone 3

| Edge case | Status | Plan |
|---|---|---|
| Duplicate patients / notes | 🟡 M3 | Deterministic canonical key; idempotent merge; double-billing flag. (Real API: 1 record per `patient_id` → dedup is an idempotent no-op, built as such.) |
| Name/ID spelling variants | 🟡 M3 | Fuzzy match name + DOB + facility; uncertain → review. |
| Multiple wounds on one patient | 🟡 M3 | Highest severity = primary; aggregate the rest. **Real HP notes already contain multi-wound text.** |
| Conflicting provider data | 🟡 M3 | Show both, flag. |
| Re-run spawns duplicates | ✅(design) 🟡 M3 | Deterministic key → idempotent (the fetch layer is already idempotent). |

## 4. Decision / Rules engine — 🟡 Milestone 4

| Edge case | Status | Plan |
|---|---|---|
| Missing eligibility-blocking field | 🟡 M4 | `rules_config`: required missing → flag; biller can downgrade to optional. |
| Borderline / ambiguous eligibility | 🟡 M4 | Third state `flag_for_review`. |
| Invalid config (typo / unknown field) | 🟡 M4 | Validate `rules_config` on load; reject with a clear error. |
| Config drift vs existing decisions | 🟡 M4 | Every decision stores its `rules_version`; re-derive on demand. |
| Biller toggles a flag | 🟡 M4 | Pure function of `(patient, config)` → re-run Decide only; zero API/LLM. |
| Contradictory rules | 🟡 M4 | Config validation + explicit precedence. |

## 5. Scale — 300 → 10,000 — ✅ architecture ready / 🟡 substitutions

| Dimension | Status | How |
|---|---|---|
| API throughput | ✅ | Concurrent idempotent work-queue behind one adaptive rate-limiter; `since` incremental (🟡 M6). |
| LLM cost & quota | 🟡 M2 | Tiered extraction (>50% cut), hash-cache, batch, budget cap. |
| Storage / DB | ✅(seam) | DAO boundary → DuckDB→Postgres/warehouse is a config swap. |
| Memory | ✅ | Stream/batch per task; never load the full set. |
| Dashboard at 10k rows | 🟡 M5 | Virtualized rows + server-side filter/sort. |
| Wall-clock / parallelism | ✅ | Stateless workers; add concurrency/workers. |
| Concurrency control | ✅ | One shared rate-limiter; idempotency makes retries safe. |

## 6. Stale data / freshness — 🟡 mostly Milestone 6

| Edge case | Status | Plan |
|---|---|---|
| Source changed between runs | ✅(seam) 🟡 M6 | `last_modified_at` captured now; `since` watermark + `source_hash` re-extract in M6. |
| Cached extraction stale | 🟡 M2/M6 | `source_hash` change → cache miss → re-extract. |
| Decision stale (config changed) | 🟡 M4 | `rules_version` + re-derive (no re-fetch). |
| Coverage/eligibility lapsed | 🟡 M4 | Freshness threshold → flag for re-check. |
| Patient discharged/transferred/deceased | 🟡 M4 | Detect status change on sync; drop from active billing. |
| Dashboard shows old state | 🟡 M5 | "Last synced / last decided" + auto-recompute on toggle. |

## 7. Containerization / Deployment / Ops — ✅ ready / 🟡 packaging

| Edge case | Status | How |
|---|---|---|
| Reproducible environment | 🟡 | Pinned `requirements.txt` now; Docker + compose to add. |
| Secrets | ✅ | Injected via env (`config/settings.py`); never baked/committed (`.gitignore`). |
| Container restart mid-run | ✅ | `fetch_state` + DuckDB persist → restart resumes (validated by the resume test). |
| Idempotent restart | ✅ | `fetch_state` + exactly-once fetch → repeats nothing done. |
| OOM / resource limits | ✅ | Per-task batch processing; no full-set load. |
| Horizontal scaling | ✅(design) | Stateless workers; all state in the shared store. |
| Health / readiness checks | ✅ | `/health` exposed (mock); client uses it as a probe target. |
| Silent stage failure | ✅ | Per-stage counters + `fetch_events` audit + coverage %. |

## 8. Real-world / domain / compliance — ✅ partial / 🟡 by milestone

| Edge case | Status | How |
|---|---|---|
| PHI / HIPAA | ✅(posture) 🟡 | Synthetic data; secrets via env; de-identify-before-LLM stance documented (M2). |
| Audit trail | ✅ | `fetch_events` logs every attempt + outcome; decisions log `rules_version` (M4). |
| Timezone & date formats | ✅(captured) 🟡 M2 | `last_modified_at` stored raw; normalize/flag ambiguous in extraction. |
| Concurrent biller config edits | 🟡 M4 | Config versioning + optimistic locking. |
| Insurance nuances (secondary, prior-auth) | 🟡 M4 | Coverage logic beyond active/inactive; flag prior-auth. |
| Double-billing across runs | 🟡 M3 | Idempotency + double-billing flag. |
| Wound lifecycle (healed = not billable) | 🟡 M3/M4 | Track wound state across assessments. |
| Impossible values (negative/200cm) | 🟡 M2 | Range/sanity checks → reject + flag. |
| No eligible patients / empty result | 🟡 M5 | Graceful empty state. |
| LLM cost runaway | 🟡 M2 | Cost-per-extraction tracking + hard budget cap. |
