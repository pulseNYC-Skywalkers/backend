"""Milestone 5 — biller dashboard (consumer-friendly, grounded sources).

Non-technical billers get a plain-English Q&A summary (LLM when configured).
Technical grounding (char-span highlights) stays available in a collapsible section.
Biller can approve flag_for_review cases or mark fields optional -> moves to bill list.

    python -m dashboard.app          # http://127.0.0.1:8050
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import FastAPI, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, Response

from dashboard.export import build_export_rows, export_filename, rows_to_csv, rows_to_xlsx
from dashboard.rules_export import rules_export_filename, rules_to_xlsx
from dashboard.summarizer import analyze_review, summarize_patient
from observability.metrics import get_logger
from pipeline.decide import rules
from store.dao import Store

app = FastAPI(title="Wound-Care Billing — Biller View")
log = get_logger("dashboard")

HTML_PATH = __import__("pathlib").Path(__file__).resolve().parent / "index.html"

# One shared DB connection for the dashboard process (avoids lock contention
# from opening a new connection on every HTTP request).
_db: Store | None = None
_db_lock = threading.Lock()


def get_store(*, write: bool = False) -> Store:
    global _db
    with _db_lock:
        if _db is None or (write and _db._read_only):
            if _db is not None:
                _db.close()
            _db = Store(read_only=not write)
        return _db


def _effective_decision(decision: dict[str, Any], override: dict[str, Any] | None) -> str:
    return Store._effective_decision_value(decision, override)


def _patient_payload(s: Store, internal_id: int) -> dict[str, Any] | None:
    canonical = s.get_canonical(internal_id)
    if not canonical:
        return None
    decision = next((d for d in s.all_decisions() if d["internal_id"] == internal_id), {})
    override = s.get_override(internal_id)
    cfg = rules.load_config()
    optional = (override or {}).get("optional_fields") or []
    review = analyze_review(canonical, decision, cfg, optional)
    eff = _effective_decision(decision, override)
    summary = summarize_patient(canonical, decision, review, eff, override)

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
            "facts": extr.get("facts") or {},
        })

    return {
        "canonical": canonical,
        "decision": decision,
        "override": override,
        "system_decision": decision.get("decision"),
        "effective_decision": eff,
        "review": review,
        "summary": summary,
        "sources": sources,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_PATH.read_text()


@app.get("/api/summary")
def summary() -> JSONResponse:
    s = get_store()
    cov = s.coverage_report()
    system_decisions = s.decision_counts()
    effective = s.effective_decision_counts()
    cfg = rules.load_config()
    overrides = s.all_overrides()
    n_approved = sum(1 for o in overrides.values() if o.get("approved"))
    return JSONResponse({
        "coverage": cov,
        "decisions": effective,
        "system_decisions": system_decisions,
        "rules_version": cfg["rules_version"],
        "payers": cfg["payers"],
        "patients": len(s.all_canonical()),
        "biller_approved": n_approved,
    })


@app.get("/api/export/rules")
def export_rules() -> Response:
    """Download billing rules documentation as Excel."""
    filename = rules_export_filename()
    return Response(
        content=rules_to_xlsx(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export")
def export_billing(format: str = Query("xlsx")) -> Response:
    """Download all billing decisions as CSV or Excel."""
    fmt = format.lower()
    if fmt not in ("csv", "xlsx"):
        return JSONResponse({"error": "format must be csv or xlsx"}, status_code=400)

    try:
        s = get_store()
        rows = build_export_rows(s)
    except Exception as exc:
        log.exception("export failed")
        return JSONResponse({"error": str(exc)}, status_code=500)

    filename = export_filename(fmt)

    if fmt == "xlsx":
        return Response(
            content=rows_to_xlsx(rows),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return Response(
        content=rows_to_csv(rows).encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/patients")
def patients(decision: str | None = None, q: str | None = None) -> JSONResponse:
    s = get_store()
    decisions = {d["internal_id"]: d for d in s.all_decisions()}
    overrides = s.all_overrides()
    out: list[dict[str, Any]] = []
    for c in s.all_canonical():
        d = decisions.get(c["internal_id"], {})
        ov = overrides.get(c["internal_id"])
        eff = _effective_decision(d, ov)
        if decision and eff != decision:
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
            "decision": eff,
            "system_decision": d.get("decision", "—"),
            "biller_approved": bool(ov and ov.get("approved")),
            "wound_type": (facts.get("wound_type") or {}).get("value"),
            "location": (facts.get("location") or {}).get("value"),
            "area_cm2": pw.get("area_cm2"),
            "needs_review": eff == "flag_for_review",
        })
    return JSONResponse({"patients": out, "count": len(out)})


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
    s = get_store()
    payload = _patient_payload(s, internal_id)
    if not payload:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(jsonable_encoder(payload))


@app.post("/api/patient/{internal_id}/approve")
def approve_patient(internal_id: int, body: dict[str, Any]) -> JSONResponse:
    """Biller approves a flagged case and/or marks fields as optional."""
    s = get_store(write=True)
    canonical = s.get_canonical(internal_id)
    if not canonical:
        return JSONResponse({"error": "not found"}, status_code=404)
    decision = next((d for d in s.all_decisions() if d["internal_id"] == internal_id), {})
    if decision.get("decision") == "do_not_bill":
        return JSONResponse({
            "error": "This patient is marked do-not-bill (e.g. bundled insurance). "
                     "Change payer rules in config instead of per-patient approval.",
        }, status_code=400)

    optional_fields = body.get("optional_fields") or []
    approved = bool(body.get("approved", True))
    note = body.get("biller_note") or ""

    s.upsert_override(
        internal_id=internal_id,
        patient_id=canonical.get("patient_id"),
        approved=approved,
        optional_fields=optional_fields,
        biller_note=note,
    )
    override = s.get_override(internal_id)
    cfg = rules.load_config()
    review = analyze_review(canonical, decision, cfg, optional_fields)
    eff = _effective_decision(decision, override)
    biller_summary = summarize_patient(canonical, decision, review, eff, override)

    return JSONResponse(jsonable_encoder({
        "ok": True,
        "effective_decision": eff,
        "system_decision": decision.get("decision"),
        "review": review,
        "summary": biller_summary,
        "decisions": s.effective_decision_counts(),
    }))


@app.post("/api/redecide")
def redecide(payload: dict[str, Any]) -> JSONResponse:
    """Global payer-rule change -> re-run Decide for all patients."""
    s = get_store(write=True)
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
    return JSONResponse({
        "rules_version": cfg["rules_version"],
        "decisions": s.effective_decision_counts(),
        "system_decisions": s.decision_counts(),
    })


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
