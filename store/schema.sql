-- Wound-Care Billing Pipeline — DuckDB schema (Milestone 1)
-- Raw store mirrors API responses as JSON so DuckDB can query payloads directly,
-- plus a resumable fetch_state work-queue. Later milestones add extractions,
-- canonical_patient, and decisions tables.

-- One row per fetched API resource. Payload kept as raw JSON: "schema &
-- queryability" becomes a strength because we can query inside it with SQL.
CREATE TABLE IF NOT EXISTS raw_records (
    resource_type     TEXT      NOT NULL,   -- facilities | patients | diagnoses | coverage | notes | assessments
    resource_id       TEXT      NOT NULL,   -- patient_id, facility_id, or a list key
    patient_id        TEXT,                 -- set for patient-scoped resources
    facility_id       TEXT,                 -- set where known
    source_url        TEXT,
    last_modified_at  TIMESTAMP,            -- server-reported watermark (for `since` incremental)
    fetched_at        TIMESTAMP DEFAULT now(),
    http_status       INTEGER,
    payload           JSON      NOT NULL,
    PRIMARY KEY (resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_patient   ON raw_records (patient_id);
CREATE INDEX IF NOT EXISTS idx_raw_facility  ON raw_records (facility_id);
CREATE INDEX IF NOT EXISTS idx_raw_modified  ON raw_records (last_modified_at);

-- Resumable work-queue. Kill the run mid-way and restart: only `pending`/`failed`
-- tasks are re-attempted, so API downtime costs wall-clock time, never completeness.
CREATE TABLE IF NOT EXISTS fetch_state (
    task_key          TEXT PRIMARY KEY,     -- "<resource_type>:<resource_id>"
    resource_type     TEXT NOT NULL,
    resource_id       TEXT NOT NULL,
    patient_id        TEXT,
    facility_id       TEXT,
    url               TEXT NOT NULL,
    -- pending | fetched | failed | no_data | quarantined
    status            TEXT NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    last_http_status  INTEGER,
    enqueued_at       TIMESTAMP DEFAULT now(),
    updated_at        TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fetch_status ON fetch_state (status);

-- Append-only audit of fetch attempts: powers retry/throttle counters and the
-- "API down 70%, completeness still 100%" story.
CREATE SEQUENCE IF NOT EXISTS seq_fetch_events START 1;

CREATE TABLE IF NOT EXISTS fetch_events (
    event_id     BIGINT DEFAULT nextval('seq_fetch_events'),
    task_key     TEXT,
    attempt      INTEGER,
    outcome      TEXT,        -- success | throttled_429 | real_failure | gave_up | breaker_open | malformed | auth_refresh
    http_status  INTEGER,
    detail       TEXT,
    ts           TIMESTAMP DEFAULT now()
);

-- Bad payloads set aside for inspection (malformed JSON, schema violations,
-- contract drift). Quarantine, don't crash: keep the run going.
CREATE TABLE IF NOT EXISTS quarantine (
    task_key      TEXT,
    resource_type TEXT,
    resource_id   TEXT,
    reason        TEXT,
    http_status   INTEGER,
    raw_excerpt   TEXT,
    ts            TIMESTAMP DEFAULT now()
);

-- ============================================================================
-- Milestone 2 — EXTRACTION (grounded-or-null wound facts)
-- ============================================================================
-- One row per wound observation extracted from a single source (a note or an
-- assessment). Every fact in `facts` carries its own source char-span so the UI
-- can highlight exactly where each value came from — nothing is invented.
CREATE SEQUENCE IF NOT EXISTS seq_extractions START 1;

CREATE TABLE IF NOT EXISTS extractions (
    extraction_id     BIGINT DEFAULT nextval('seq_extractions'),
    internal_id       INTEGER NOT NULL,   -- patient.id (notes/assessments key)
    patient_id        TEXT,               -- pcc patient_id (FA-001)
    facility_id       TEXT,
    source_type       TEXT NOT NULL,      -- 'assessment' | 'note'
    source_id         TEXT NOT NULL,      -- raw_records.resource_id of the source
    wound_index       INTEGER NOT NULL DEFAULT 0,  -- nth wound documented in that source
    tier              TEXT NOT NULL,      -- 'structured' | 'regex' | 'llm'
    facts             JSON NOT NULL,      -- field -> {value, span:[s,e], confidence}
    confidence        DOUBLE NOT NULL,    -- aggregate confidence for this observation
    needs_review      BOOLEAN NOT NULL DEFAULT FALSE,
    review_reasons    JSON,               -- list[str] explaining any flag
    source_hash       TEXT,               -- hash of source text -> staleness signal
    extractor_version TEXT,
    created_at        TIMESTAMP DEFAULT now(),
    PRIMARY KEY (internal_id, source_type, source_id, wound_index)
);

CREATE INDEX IF NOT EXISTS idx_extr_internal ON extractions (internal_id);

-- ============================================================================
-- Milestone 3 — RESOLVE (one canonical record per patient)
-- ============================================================================
-- Aggregates every note + assessment for a patient into a single billable view:
-- a primary wound (highest severity), all wounds, merged demographics, payer and
-- diagnosis context. Idempotent: keyed by patient, re-run overwrites in place.
CREATE TABLE IF NOT EXISTS canonical_patient (
    internal_id      INTEGER PRIMARY KEY,
    patient_id       TEXT,
    facility_id      TEXT,
    first_name       TEXT,
    last_name        TEXT,
    primary_payer_code TEXT,
    coverage         JSON,    -- list of coverage records (active/inactive)
    diagnoses        JSON,    -- list of icd10 records
    primary_wound    JSON,    -- the highest-severity merged wound + grounding
    all_wounds       JSON,    -- every distinct wound found
    n_notes          INTEGER,
    n_assessments    INTEGER,
    needs_review     BOOLEAN NOT NULL DEFAULT FALSE,
    review_reasons   JSON,
    source_hash      TEXT,    -- combined hash of inputs -> re-resolve on change
    resolved_at      TIMESTAMP DEFAULT now()
);

-- ============================================================================
-- Milestone 4 — DECIDE (config-driven, 3-state billing decision)
-- ============================================================================
-- A decision is a pure function of (canonical_patient, rules_config). It stores
-- the rules_version it was derived under so a config change is re-derivable
-- without re-fetching or re-extracting.
CREATE TABLE IF NOT EXISTS decisions (
    internal_id    INTEGER PRIMARY KEY,
    patient_id     TEXT,
    facility_id    TEXT,
    decision       TEXT NOT NULL,   -- 'bill' | 'do_not_bill' | 'flag_for_review'
    reasons        JSON NOT NULL,   -- list[str] templated, biller-readable
    eligibility    JSON,            -- per-rule pass/fail detail
    rules_version  TEXT,
    decided_at     TIMESTAMP DEFAULT now()
);

-- ============================================================================
-- Milestone 6 — INCREMENTAL (`since` watermark per resource type)
-- ============================================================================
-- Highest last_modified_at successfully ingested per resource type. The next run
-- passes this as `since=` so we poll for changes instead of re-fetching all rows.
CREATE TABLE IF NOT EXISTS watermarks (
    resource_type  TEXT PRIMARY KEY,
    since_value     TEXT,
    updated_at      TIMESTAMP DEFAULT now()
);

-- ============================================================================
-- Milestone 5 — BILLER OVERRIDES (human approval on flag_for_review)
-- ============================================================================
-- When the rule engine flags a case, a biller can mark fields as optional or
-- explicitly approve it for billing. The system decision stays auditable; the
-- effective decision shown in the worklist reflects the override.
CREATE TABLE IF NOT EXISTS biller_overrides (
    internal_id       INTEGER PRIMARY KEY,
    patient_id        TEXT,
    approved          BOOLEAN NOT NULL DEFAULT FALSE,
    optional_fields   JSON,              -- fields biller waived as non-blocking
    biller_note       TEXT,
    approved_at       TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT now()
);
