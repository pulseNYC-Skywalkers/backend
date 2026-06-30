"""Plain-language patient summaries for non-technical billers.

Turns the same structured JSON the pipeline already produced into a Q&A brief.
Uses Gemini when GOOGLE_API_KEY is set; otherwise a deterministic template that
covers the same questions in the same order.
"""

from __future__ import annotations

import json
import re
from typing import Any

from config import settings
from pipeline.decide.rules import PAYER_NAMES, load_config

# Human labels for clinical / rule fields.
FIELD_LABELS: dict[str, str] = {
    "wound_type": "Wound type",
    "location": "Body location",
    "laterality": "Side (left/right)",
    "stage": "Wound stage",
    "length_cm": "Length (cm)",
    "width_cm": "Width (cm)",
    "depth_cm": "Depth (cm)",
    "drainage_type": "Drainage type",
    "drainage_amount": "Drainage amount",
    "granulation_pct": "Granulation tissue (%)",
    "slough_pct": "Slough tissue (%)",
    "periwound": "Skin around the wound",
}

DECISION_LABELS = {
    "bill": "Ready to bill",
    "flag_for_review": "Needs your review",
    "do_not_bill": "Do not bill",
}


def _label(field: str) -> str:
    return FIELD_LABELS.get(field, field.replace("_", " ").title())


def _fact_val(facts: dict[str, Any], field: str) -> str | None:
    f = facts.get(field)
    if not f:
        return None
    v = f.get("value")
    return str(v) if v is not None else None


def analyze_review(
    canonical: dict[str, Any],
    decision: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    optional_fields: list[str] | None = None,
) -> dict[str, Any]:
    """What blocked billing, which fields are missing, what the biller can waive."""
    cfg = cfg or load_config()
    optional = set(optional_fields or [])
    required = cfg.get("required_wound_fields", [])
    pw = canonical.get("primary_wound") or {}
    facts = pw.get("facts") or {}
    present = {f for f in required if f in facts}
    missing = [f for f in required if f not in facts and f not in optional]
    waived = [f for f in required if f not in facts and f in optional]

    blockers: list[dict[str, str]] = []
    sys_dec = decision.get("decision", "")
    eligibility = decision.get("eligibility") or {}

    if sys_dec == "do_not_bill":
        blockers.append({
            "type": "payer",
            "title": "Insurance not billable",
            "detail": (decision.get("reasons") or ["This payer cannot be billed separately."])[0],
        })
    elif sys_dec == "flag_for_review":
        for reason in decision.get("reasons") or []:
            rl = reason.lower()
            if "conditionally billable" in rl or "confirmation required" in rl:
                blockers.append({
                    "type": "payer_conditional",
                    "title": "Payer needs your OK",
                    "detail": reason,
                })
            elif "incomplete" in rl or "missing" in rl:
                blockers.append({
                    "type": "missing_fields",
                    "title": "Documentation gap",
                    "detail": reason,
                })
            elif "review:" in rl.lower():
                blockers.append({
                    "type": "extraction",
                    "title": "Charting issue",
                    "detail": reason.replace("Review: ", ""),
                })
            elif "diagnosis" in rl:
                blockers.append({
                    "type": "diagnosis",
                    "title": "Diagnosis missing",
                    "detail": reason,
                })

    # Field checklist for the UI.
    field_checklist = []
    for f in required:
        val = _fact_val(facts, f)
        if val:
            field_checklist.append({"field": f, "label": _label(f), "status": "present", "value": val})
        elif f in optional:
            field_checklist.append({"field": f, "label": _label(f), "status": "waived", "value": None})
        else:
            field_checklist.append({"field": f, "label": _label(f), "status": "missing", "value": None})

    # Also show useful optional fields that ARE present.
    extra_present = []
    for f, fact in facts.items():
        if f not in required and fact:
            extra_present.append({"field": f, "label": _label(f), "value": _fact_val(facts, f)})

    can_approve = sys_dec == "flag_for_review" and (
        len(missing) == 0 or all(
            b["type"] in ("payer_conditional", "missing_fields", "extraction", "diagnosis")
            for b in blockers
        )
    )

    return {
        "system_decision": sys_dec,
        "missing_fields": missing,
        "waived_fields": waived,
        "present_fields": sorted(present),
        "field_checklist": field_checklist,
        "extra_fields": extra_present,
        "blockers": blockers,
        "can_approve": can_approve,
        "payer_code": canonical.get("primary_payer_code"),
        "payer_name": PAYER_NAMES.get(canonical.get("primary_payer_code") or "", ""),
        "eligibility": eligibility,
    }


def _deterministic_summary(
    canonical: dict[str, Any],
    decision: dict[str, Any],
    review: dict[str, Any],
    effective_decision: str,
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    """Q&A brief without LLM — same structure the LLM produces."""
    pw = canonical.get("primary_wound") or {}
    facts = pw.get("facts") or {}
    name = f"{canonical.get('first_name') or ''} {canonical.get('last_name') or ''}".strip()
    payer = canonical.get("primary_payer_code") or "—"
    payer_full = PAYER_NAMES.get(payer, payer)

    sections: list[dict[str, Any]] = []

    # Who is this patient?
    sections.append({
        "title": "Patient",
        "items": [
            {"q": "Who is this patient?", "a": f"{name} ({canonical.get('patient_id')})"},
            {"q": "Which facility?", "a": f"Facility {canonical.get('facility_id')}"},
            {"q": "What insurance do they have?", "a": f"{payer_full} ({payer})"},
        ],
    })

    # Wound
    wound_items = []
    if facts:
        for f in ("wound_type", "location", "length_cm", "width_cm", "depth_cm",
                  "drainage_type", "drainage_amount"):
            v = _fact_val(facts, f)
            if v:
                wound_items.append({"q": f"What is the {_label(f).lower()}?", "a": v})
        if pw.get("area_cm2"):
            wound_items.append({"q": "How big is the wound (area)?",
                                "a": f"{pw['area_cm2']} cm²"})
    else:
        wound_items.append({"q": "Was a wound documented?", "a": "No wound facts were extracted."})
    sections.append({"title": "Wound documentation", "items": wound_items})

    # Diagnosis & coverage
    cov = canonical.get("coverage") or []
    active_cov = next((c for c in cov if not c.get("effective_to")), None)
    dx = canonical.get("diagnoses") or []
    active_dx = next((d for d in dx if str(d.get("clinical_status", "")).lower() == "active"), None)
    sections.append({
        "title": "Clinical & coverage",
        "items": [
            {"q": "Is insurance active?", "a": (
                f"Yes — {active_cov.get('payer_name')}" if active_cov
                else "No active coverage found")},
            {"q": "Is there an active diagnosis?", "a": (
                f"Yes — {active_dx.get('icd10_code')}: {active_dx.get('icd10_description')}"
                if active_dx else "No active diagnosis on file")},
        ],
    })

    # Billing decision
    eff_label = DECISION_LABELS.get(effective_decision, effective_decision)
    dec_items = [
        {"q": "What does the system recommend?", "a": DECISION_LABELS.get(
            review["system_decision"], review["system_decision"])},
        {"q": "What is the current status?", "a": eff_label},
    ]
    if override and override.get("approved"):
        dec_items.append({"q": "Did a biller approve this?", "a": "Yes — moved to the bill list."})
    for reason in (decision.get("reasons") or [])[:4]:
        dec_items.append({"q": "Why?", "a": reason})
    sections.append({"title": "Billing decision", "items": dec_items})

    # Review section (only when flagged)
    if review["system_decision"] == "flag_for_review":
        rev_items = []
        for b in review["blockers"]:
            rev_items.append({"q": b["title"], "a": b["detail"]})
        for fc in review["field_checklist"]:
            if fc["status"] == "missing":
                rev_items.append({
                    "q": f"Missing: {fc['label']}",
                    "a": "Not found in the chart — you can mark this as optional if your policy allows.",
                })
            elif fc["status"] == "waived":
                rev_items.append({
                    "q": f"Waived: {fc['label']}",
                    "a": "Biller marked this field as optional.",
                })
        sections.append({"title": "What needs your attention", "items": rev_items})

    headline = eff_label
    if effective_decision == "bill":
        headline = f"✓ Ready to bill — {name}"
    elif effective_decision == "flag_for_review":
        headline = f"⚠ Review needed — {name}"
    else:
        headline = f"✗ Do not bill — {name}"

    return {
        "headline": headline,
        "sections": sections,
        "source": "template",
    }


def summarize_patient(
    canonical: dict[str, Any],
    decision: dict[str, Any],
    review: dict[str, Any],
    effective_decision: str,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build biller Q&A summary. LLM when available (12s cap); template fallback."""
    fallback = _deterministic_summary(
        canonical, decision, review, effective_decision, override
    )
    if not settings.LLM.google_api_key:
        return fallback

    try:  # pragma: no cover
        import concurrent.futures
        import google.generativeai as genai

        def _llm_call() -> dict[str, Any]:
            genai.configure(api_key=settings.LLM.google_api_key)
            model = genai.GenerativeModel(settings.LLM.model)
            payload = {
                "patient": {
                    "id": canonical.get("patient_id"),
                    "name": f"{canonical.get('first_name')} {canonical.get('last_name')}",
                    "facility": canonical.get("facility_id"),
                    "payer": canonical.get("primary_payer_code"),
                },
                "wound": canonical.get("primary_wound"),
                "coverage": canonical.get("coverage"),
                "diagnoses": canonical.get("diagnoses"),
                "system_decision": review["system_decision"],
                "effective_decision": effective_decision,
                "reasons": decision.get("reasons"),
                "review": review,
                "biller_approved": bool(override and override.get("approved")),
            }
            prompt = (
                "You are helping a non-technical medical biller. Using ONLY the JSON below "
                "(do not invent facts), write a plain-English patient billing brief.\n\n"
                "Return STRICT JSON with this shape:\n"
                '{"headline":"one short line","sections":[{"title":"...","items":[{"q":"question","a":"answer"}]}]}\n\n'
                "Required sections (in order):\n"
                "1. Patient — who, facility, insurance\n"
                "2. Wound documentation — type, location, size, drainage in simple terms\n"
                "3. Clinical & coverage — active insurance? active diagnosis?\n"
                "4. Billing decision — system recommendation, current status, why\n"
                "5. What needs your attention — ONLY if flagged for review; list each blocker "
                "and each MISSING field by plain name. Say which fields the biller can mark optional.\n\n"
                "Use short questions a biller would ask. No jargon, no JSON field names.\n\n"
                f"DATA:\n{json.dumps(payload, default=str)[:12000]}"
            )
            resp = model.generate_content(prompt, generation_config={"temperature": 0})
            text = re.sub(r"```(json)?", "", resp.text).strip()
            data = json.loads(text)
            if "sections" not in data:
                raise ValueError("invalid LLM shape")
            data["source"] = "llm"
            return data

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_llm_call).result(timeout=12)
    except Exception:
        return fallback
