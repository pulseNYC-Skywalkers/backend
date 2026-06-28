"""Local mock EHR API — faithful stand-in for the PCC hackathon API (see API.md).

Same routes, query params, and bare-array responses as
https://hackathon.prod.pulsefoundry.ai, plus deliberately injectable failure
modes so we can VALIDATE the ingestion edge cases locally:
  * ~30% of requests -> 429 (Retry-After 1-5s, sometimes omitted)
  * ~2%  of requests -> 500
  * optional: truncated/invalid JSON, 200-but-empty, unknown-field contract drift

Run:  python -m mock_api.server
Toggle edge cases via MOCK_* env vars (see .env.example / config/settings.py).
"""

from __future__ import annotations

import copy
import random

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from config import settings
from mock_api.data_gen import build_dataset

app = FastAPI(title="Mock EHR API (PCC-compatible)", version="1.0.0")
_cfg = settings.MOCK


@app.middleware("http")
async def flaky_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    roll = random.random()
    if roll < _cfg.throttle_rate:
        headers = {}
        # Sometimes omit Retry-After to exercise the capped-backoff fallback.
        if random.random() > _cfg.drop_retry_after_rate:
            headers["Retry-After"] = str(random.randint(_cfg.retry_after_min,
                                                        _cfg.retry_after_max))
        return JSONResponse(status_code=429,
                            content={"detail": "Rate limit exceeded. Back off and retry."},
                            headers=headers)
    if roll < _cfg.throttle_rate + _cfg.outage_rate:
        return JSONResponse(status_code=500,
                            content={"detail": {"error": "Internal Server Error"}})
    return await call_next(request)


def _inject_and_respond(records: list[dict]) -> Response:
    """Apply optional edge-case mutations to a successful data response."""
    # 200-but-empty.
    if records and random.random() < _cfg.empty_rate:
        return JSONResponse(content=[])
    # Contract drift: add an unknown field to the first record.
    if records and random.random() < _cfg.unknown_field_rate:
        records = copy.deepcopy(records)
        records[0]["_unexpected_new_field"] = "surprise"
    # Truncated / invalid JSON body (still HTTP 200).
    if records and random.random() < _cfg.malformed_rate:
        body = '[{"id": 1, "patient_id": "FA-0'  # deliberately truncated
        return Response(content=body, media_type="application/json", status_code=200)
    return JSONResponse(content=records)


def _since_ok(value: str | None, since: str | None) -> bool:
    if not since or not value:
        return True
    return value >= since


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/pcc/patients")
def patients(facility_id: int = Query(...), since: str | None = None) -> Response:
    data = build_dataset()
    plist = data["patients_by_facility"].get(facility_id)
    if plist is None:
        return JSONResponse(status_code=422,
                            content={"detail": [{"msg": "invalid facility_id",
                                                  "loc": ["query", "facility_id"]}]})
    out = [p for p in plist if _since_ok(p.get("last_modified_at"), since)]
    return _inject_and_respond(out)


@app.get("/pcc/diagnoses")
def diagnoses(patient_id: str = Query(...)) -> Response:
    rec = build_dataset()["by_pid_str"].get(patient_id)
    return _inject_and_respond(rec["diagnoses"] if rec else [])


@app.get("/pcc/coverage")
def coverage(patient_id: str = Query(...)) -> Response:
    rec = build_dataset()["by_pid_str"].get(patient_id)
    return _inject_and_respond(rec["coverage"] if rec else [])


@app.get("/pcc/notes")
def notes(patient_id: int = Query(...), since: str | None = None) -> Response:
    rec = build_dataset()["by_internal_id"].get(patient_id)
    items = [n for n in (rec["notes"] if rec else [])
             if n.get("is_current") and _since_ok(n.get("effective_date"), since)]
    return _inject_and_respond(items)


@app.get("/pcc/assessments")
def assessments(patient_id: int = Query(...), since: str | None = None) -> Response:
    rec = build_dataset()["by_internal_id"].get(patient_id)
    items = [a for a in (rec["assessments"] if rec else [])
             if a.get("is_current") and _since_ok(a.get("assessment_date"), since)]
    return _inject_and_respond(items)


if __name__ == "__main__":
    from urllib.parse import urlparse

    import uvicorn

    parsed = urlparse(settings.FETCH.base_url)
    uvicorn.run("mock_api.server:app", host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 8099, log_level="warning")
