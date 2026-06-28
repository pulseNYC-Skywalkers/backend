"""Central, env-driven configuration.

Every tunable lives here so the rest of the codebase never reads os.environ
directly. This is the single seam for pointing the pipeline at a real EHR API,
swapping the DB path, or dialing the mock server's failure rates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present (no-op when absent; real env vars still win).
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _ints(name: str, default: list[int]) -> list[int]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return [int(x) for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class FetchSettings:
    # Local mock by default. Point at the real PCC mock by setting EHR_API_BASE_URL
    # to https://hackathon.prod.pulsefoundry.ai (see API.md) — code is identical.
    base_url: str = _s("EHR_API_BASE_URL", "http://127.0.0.1:8099")
    api_key: str = _s("EHR_API_KEY", "")
    # Versioned adapter selector, so a contract change is a localized swap.
    adapter_version: str = _s("EHR_ADAPTER_VERSION", "pcc_v1")
    # Facilities are fixed constants in the PCC contract (no /facilities endpoint).
    facility_ids: tuple[int, ...] = tuple(_ints("EHR_FACILITY_IDS", [101, 102, 103]))
    # Expected patients per facility for pagination-gap detection. 0 = disabled
    # (the real API has non-uniform counts: 120/90/90). Set a value only when you
    # have a known per-facility expectation, or rely on cross-run shrink detection.
    expected_patients_per_facility: int = _i("EHR_EXPECTED_PER_FACILITY", 0)
    max_concurrency: int = _i("FETCH_MAX_CONCURRENCY", 8)
    max_attempts: int = _i("FETCH_MAX_ATTEMPTS", 6)
    base_backoff_seconds: float = _f("FETCH_BASE_BACKOFF_SECONDS", 0.5)
    max_backoff_seconds: float = _f("FETCH_MAX_BACKOFF_SECONDS", 30.0)
    # Circuit breaker: trips ONLY on real failures, never on 429 throttling.
    breaker_failure_threshold: int = _i("BREAKER_FAILURE_THRESHOLD", 5)
    breaker_cooldown_seconds: float = _f("BREAKER_COOLDOWN_SECONDS", 10.0)
    breaker_halfopen_probes: int = _i("BREAKER_HALFOPEN_PROBES", 3)
    request_timeout_seconds: float = _f("FETCH_REQUEST_TIMEOUT_SECONDS", 15.0)


@dataclass(frozen=True)
class StoreSettings:
    duckdb_path: str = _s("DUCKDB_PATH", str(PROJECT_ROOT / "data" / "wound_care.duckdb"))


@dataclass(frozen=True)
class MockSettings:
    # Matches API.md: ~30% of requests return 429, Retry-After is 1-5s.
    throttle_rate: float = _f("MOCK_THROTTLE_RATE", 0.30)
    outage_rate: float = _f("MOCK_OUTAGE_RATE", 0.02)
    patients_per_facility: int = _i("MOCK_PATIENTS_PER_FACILITY", 100)
    seed: int = _i("MOCK_SEED", 42)
    retry_after_min: int = _i("MOCK_RETRY_AFTER_MIN", 1)
    retry_after_max: int = _i("MOCK_RETRY_AFTER_MAX", 5)
    # Edge-case injection (local validation only): set to 0 to disable.
    malformed_rate: float = _f("MOCK_MALFORMED_RATE", 0.0)        # truncated/invalid JSON body
    empty_rate: float = _f("MOCK_EMPTY_RATE", 0.0)               # 200 but empty/no data
    drop_retry_after_rate: float = _f("MOCK_DROP_RETRY_AFTER", 0.15)  # 429 without Retry-After
    unknown_field_rate: float = _f("MOCK_UNKNOWN_FIELD_RATE", 0.0)    # contract drift


@dataclass(frozen=True)
class LLMSettings:
    google_api_key: str = _s("GOOGLE_API_KEY", "")
    model: str = _s("LLM_MODEL", "gemini-2.0-flash")


FETCH = FetchSettings()
STORE = StoreSettings()
MOCK = MockSettings()
LLM = LLMSettings()
