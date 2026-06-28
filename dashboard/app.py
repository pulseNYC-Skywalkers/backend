"""Milestone 5 — biller dashboard (FastAPI, zero-build, runs offline).

Chosen over a Next.js app deliberately: it ships inside the same Python service,
needs no Node toolchain, and reads straight from DuckDB — so the whole pipeline
demos with one `pip install`. The biller value is identical: a worklist of
decisions, filters, and — the headline feature — every extracted fact is
highlighted at its exact character span in the original note/assessment, so a
biller can see *where* each number came from. Nothing on screen is ungrounded.

    python -m dashboard.app          # http://127.0.0.1:8050
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from pipeline.decide import rules
from store.dao import Store

app = FastAPI(title="Wound-Care Billing — Biller View")

HTML_PATH = __import__("pathlib").Path(__file__).resolve().parent / "index.html"


def _store() -> Store:
    return Store()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_PATH.read_text()


@app.get("/api/summary")
def summary() -> JSONResponse:
    s = _store()
    try:
        cov = s.coverage_report()
        decisions = s.decision_counts()
        cfg = rules.load_config()
        n_canonical = len(s.all_canonical())
        n_flag = sum(1 for c in s.all_canonical() if c.get("needs_review"))
        return JSONResponse({
            "coverage": cov, "decisions": decisions,
            "rules_version": cfg["rules_version"], "payers": cfg["payers"],
            "patients": n_canonical, "needs_review": n_flag,
        })
    finally:
        s.close()


@app.get("/api/patients")
def patients(decision: str | None = None, q: str | None = None) -> JSONResponse:
    s = _store()
    try:
        decisions = {d["internal_id"]: d for d in s.all_decisions()}
        out: list[dict[str, Any]] = []
        for c in s.all_canonical():
            d = decisions.get(c["internal_id"], {})
            if decision and d.get("decision") != decision:
                continue
            name = f"{c.get('first_name') or ''} {c.get('last_name') or ''}".strip()
            if q and q.lower() not in (name.lower() + " " + (c.get("patient_id") or "").lower()):
                continue
            pw = c.get("primary_wound") or {}
            facts = pw.get("facts") or {}
            out.append({
                "internal_id": c["internal_id"], "patient_id": c.get("patient_id"),
                "facility_id": c.get("facility_id"), "name": name,
                "payer": c.get("primary_payer_code"),
                "decision": d.get("decision", "—"),
                "wound_type": (facts.get("wound_type") or {}).get("value"),
                "location": (facts.get("location") or {}).get("value"),
                "area_cm2": pw.get("area_cm2"),
                "needs_review": c.get("needs_review"),
            })
        return JSONResponse({"patients": out, "count": len(out)})
    finally:
        s.close()


def _source_text(s: Store, source_type: str, source_id: str) -> str:
    rtype = "notes" if source_type == "note" else "assessments"
    payload = s.get_raw(rtype, source_id) or []
    for rec in payload:
        if str(rec.get("id")) == str(source_id):
            return (rec.get("note_text") if source_type == "note"
                    else rec.get("raw_json")) or ""
    if payload:
        rec = payload[0]
        return (rec.get("note_text") if source_type == "note" else rec.get("raw_json")) or ""
    return ""


@app.get("/api/patient/{internal_id}")
def patient_detail(internal_id: int) -> JSONResponse:
    s = _store()
    try:
        canonical = s.get_canonical(internal_id)
        if not canonical:
            return JSONResponse({"error": "not found"}, status_code=404)
        decision = next((d for d in s.all_decisions() if d["internal_id"] == internal_id), {})

        sources: list[dict[str, Any]] = []
        for extr in s.extractions_for(internal_id):
            text = _source_text(s, extr["source_type"], extr["source_id"])
            spans = []
            for field, f in (extr["facts"] or {}).items():
                if f and f.get("span"):
                    spans.append({"field": field, "value": f["value"],
                                  "start": f["span"][0], "end": f["span"][1],
                                  "confidence": f["confidence"]})
            sources.append({
                "source_type": extr["source_type"], "source_id": extr["source_id"],
                "tier": extr["tier"], "confidence": extr["confidence"],
                "needs_review": extr["needs_review"], "text": text, "spans": spans,
            })
        return JSONResponse(jsonable_encoder({
            "canonical": canonical, "decision": decision, "sources": sources,
        }))
    finally:
        s.close()


@app.post("/api/redecide")
def redecide(payload: dict[str, Any]) -> JSONResponse:
    """Biller toggles payer eligibility -> re-run Decide ONLY (no fetch/extract).
    Demonstrates the pure-function decision engine live."""
    s = _store()
    try:
        cfg = rules.load_config()
        if "billable" in payload:
            cfg["payers"]["billable"] = payload["billable"]
        if "not_billable" in payload:
            cfg["payers"]["not_billable"] = payload["not_billable"]
        if "conditional" in payload:
            cfg["payers"]["conditional"] = payload["conditional"]
        cfg["rules_version"] = payload.get("rules_version", cfg["rules_version"] + "*")
        try:
            cfg = _revalidate(cfg)
        except rules.ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        for c in s.all_canonical():
            s.upsert_decision(rules.decide(c, cfg))
        return JSONResponse({"rules_version": cfg["rules_version"],
                             "decisions": s.decision_counts()})
    finally:
        s.close()


def _revalidate(cfg: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, str] = {}
    for bucket in ("billable", "conditional", "not_billable"):
        for code in cfg["payers"].get(bucket, []):
            if code in seen:
                raise rules.ConfigError(f"payer {code} in both {seen[code]} and {bucket}")
            seen[code] = bucket
    return cfg


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8050, log_level="info")


if __name__ == "__main__":
    main()
