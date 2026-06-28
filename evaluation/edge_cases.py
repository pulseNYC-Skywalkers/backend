"""Edge-case validation harness for the ingestion layer (§1 of the edge-case doc).

Uses httpx.MockTransport to script exact upstream responses, so every failure
mode is tested DETERMINISTICALLY (no dependence on the flaky API or random
injection). Each check maps to a row in the edge-case table.

Run: python -m evaluation.edge_cases   (exit 0 = all pass)
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from pipeline.fetch.circuit_breaker import CircuitBreaker, State
from pipeline.fetch.client import ResilientClient
from pipeline.fetch.ehr_adapter import PccAdapterV1
from pipeline.fetch.rate_limiter import AdaptiveRateLimiter

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((PASS if cond else FAIL, name, detail))


def _fast_client(handler, breaker: CircuitBreaker | None = None) -> tuple[ResilientClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ResilientClient(
        client=http,
        breaker=breaker or CircuitBreaker(failure_threshold=3, cooldown_seconds=0.05,
                                          halfopen_probes=1),
        limiter=AdaptiveRateLimiter(min_interval=0, start_interval=0, max_interval=0.001),
    )
    # Make retries effectively instant for the harness.
    client.base_backoff = 0.001
    client.max_backoff = 0.005
    client.max_throttle_retries = 10
    return client, http


def _counter():
    box = {"n": 0}

    def nxt() -> int:
        box["n"] += 1
        return box["n"]
    return box, nxt


# ---------------------------------------------------------------------------
# CLIENT-LEVEL edge cases (transport resilience)
# ---------------------------------------------------------------------------
async def test_client_layer() -> None:
    URL = "http://test/pcc/x"

    # 429 throttling mid-run: backoff + Retry-After, then succeed. Breaker stays closed.
    box, nxt = _counter()

    def h_429(req):
        if nxt() <= 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "rl"})
        return httpx.Response(200, json=[{"id": 1, "patient_id": "FA-001"}])
    c, http = _fast_client(h_429)
    r = await c.get("t", URL)
    await http.aclose()
    check("429 throttling -> retried then success", r.ok and r.outcome == "success")
    check("429 does NOT trip breaker", c.breaker.state == State.CLOSED)

    # 429 with NO Retry-After header -> capped backoff fallback, still succeeds.
    box, nxt = _counter()

    def h_429_nohdr(req):
        if nxt() <= 2:
            return httpx.Response(429, json={"detail": "rl"})  # no Retry-After
        return httpx.Response(200, json=[{"id": 1, "patient_id": "FA-001"}])
    c, http = _fast_client(h_429_nohdr)
    r = await c.get("t", URL)
    await http.aclose()
    check("429 without Retry-After -> backoff fallback succeeds", r.ok and r.outcome == "success")

    # True outage (5xx) sustained -> breaker trips -> eventually gives up.
    def h_500(req):
        return httpx.Response(500, json={"detail": "boom"})
    bk = CircuitBreaker(failure_threshold=3, cooldown_seconds=0.02, halfopen_probes=1)
    c, http = _fast_client(h_500, breaker=bk)
    c.max_real_attempts = 5
    r = await c.get("t", URL)
    await http.aclose()
    check("sustained 5xx -> gave_up", (not r.ok) and r.outcome == "gave_up")
    check("sustained 5xx -> breaker opened", bk.state in (State.OPEN, State.HALF_OPEN))

    # Timeout / connection refused -> real failure path -> gives up.
    def h_timeout(req):
        raise httpx.ConnectTimeout("timed out", request=req)
    c, http = _fast_client(h_timeout)
    c.max_real_attempts = 4
    r = await c.get("t", URL)
    await http.aclose()
    check("timeout/conn refused -> gave_up (real failure)", (not r.ok) and r.outcome == "gave_up")

    # Auth/credential expiry (401) -> refresh hook -> retry -> success.
    box, nxt = _counter()

    def h_401(req):
        return (httpx.Response(401, json={"detail": "unauth"}) if nxt() == 1
                else httpx.Response(200, json=[{"id": 1, "patient_id": "FA-001"}]))
    refreshed = {"n": 0}

    async def refresh():
        refreshed["n"] += 1
    http = httpx.AsyncClient(transport=httpx.MockTransport(h_401))
    c = ResilientClient(client=http,
                        breaker=CircuitBreaker(failure_threshold=3, cooldown_seconds=0.05, halfopen_probes=1),
                        limiter=AdaptiveRateLimiter(min_interval=0, start_interval=0, max_interval=0.001),
                        on_auth_refresh=refresh)
    r = await c.get("t", URL)
    await http.aclose()
    check("401 -> token refresh then success", r.ok and refreshed["n"] == 1)

    # Malformed/truncated body -> retried then quarantined.
    def h_bad(req):
        return httpx.Response(200, content=b'[{"id": 1, "patient_id": "FA-0', headers={"content-type": "application/json"})
    c, http = _fast_client(h_bad)
    r = await c.get("t", URL)
    await http.aclose()
    check("malformed body -> outcome 'malformed' (quarantine)", (not r.ok) and r.outcome == "malformed")

    # Malformed once, then valid -> recovers.
    box, nxt = _counter()

    def h_bad_then_ok(req):
        return (httpx.Response(200, content=b'{bad json', headers={"content-type": "application/json"})
                if nxt() == 1 else httpx.Response(200, json=[{"id": 1, "patient_id": "FA-001"}]))
    c, http = _fast_client(h_bad_then_ok)
    r = await c.get("t", URL)
    await http.aclose()
    check("transient malformed -> retry recovers", r.ok and r.outcome == "success")

    # 404 not found.
    def h_404(req):
        return httpx.Response(404, json={"detail": "nf"})
    c, http = _fast_client(h_404)
    r = await c.get("t", URL)
    await http.aclose()
    check("404 -> not_found", (not r.ok) and r.outcome == "not_found")


# ---------------------------------------------------------------------------
# ADAPTER-LEVEL edge cases (schema validation on arrival)
# ---------------------------------------------------------------------------
def test_adapter_layer() -> None:
    a = PccAdapterV1("http://test")

    vr = a.validate("patients", [{"id": 1, "facility_id": 101, "patient_id": "FA-001",
                                  "first_name": "Mary"}])
    check("valid payload -> ok, 1 record, no warnings",
          vr.ok and vr.n_records == 1 and not vr.warnings)

    vr = a.validate("patients", [])
    check("empty array -> is_empty (no_data)", vr.is_empty and vr.ok)

    vr = a.validate("patients", None)
    check("null body -> is_empty", vr.is_empty)

    vr = a.validate("patients", {"not": "an array"})
    check("non-array payload -> quarantine (ok=False)", not vr.ok)

    vr = a.validate("patients", [{"id": 1, "facility_id": 101}])  # missing patient_id
    check("missing required field -> record dropped + error",
          vr.n_records == 0 and len(vr.errors) == 1)

    vr = a.validate("patients", [{"id": 1, "facility_id": 101, "patient_id": "FA-001",
                                  "brand_new_field": "x"}])
    check("unknown field -> contract-drift warning (record kept)",
          vr.n_records == 1 and len(vr.warnings) == 1)

    # Mixed: one good, one bad -> keep the good, surface the error, not quarantined.
    vr = a.validate("diagnoses", [{"id": 1, "patient_id": "FA-001"}, {"id": 2}])
    check("partial bad batch -> keep valid records, report errors",
          vr.ok and vr.n_records == 1 and len(vr.errors) == 1)

    # Two-ID model: notes/assessments keyed by int id; diagnoses/coverage by string.
    keys = a.patient_keys({"id": 7, "patient_id": "FB-007"})
    check("identity resolution: notes use int id, diagnoses use string id",
          keys["notes"] == 7 and keys["diagnoses"] == "FB-007")

    # URL construction matches the PCC contract.
    check("patients URL uses query params + facility_id",
          a.patients_url(101) == "http://test/pcc/patients?facility_id=101")
    check("notes URL uses integer patient_id query param",
          a.subresource_url("notes", 7) == "http://test/pcc/notes?patient_id=7")


def main() -> None:
    asyncio.run(test_client_layer())
    test_adapter_layer()

    width = max(len(n) for _, n, _ in _results)
    n_fail = 0
    print("\nEdge-case validation (§1 Ingestion / API resilience)\n" + "-" * (width + 14))
    for status, name, detail in _results:
        mark = "\u2713" if status == PASS else "\u2717"
        line = f"  [{mark}] {name.ljust(width)}"
        if status == FAIL:
            n_fail += 1
            line += f"   <-- {detail or 'failed'}"
        print(line)
    print("-" * (width + 14))
    print(f"  {len(_results) - n_fail}/{len(_results)} passed")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
