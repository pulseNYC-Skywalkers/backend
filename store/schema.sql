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
