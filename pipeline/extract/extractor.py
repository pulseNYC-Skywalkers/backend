"""Milestone 2 — tiered, grounded wound-fact extraction.

Why tiered? Most facts can be read deterministically and for free; we only spend
an LLM call when cheaper tiers come up short. This is also the cost-control story
for scaling 300 -> 10k patients.

  Tier 1  structured   parse assessment.raw_json survey (Q&A)      conf 0.95
  Tier 2  regex        parse free-text note_text / narrative       conf 0.75
  Tier 3  llm          Gemini, GROUNDED-OR-NULL, only when needed  conf <=0.9
                       (gated on GOOGLE_API_KEY; off by default)

Every value is grounded to a [start,end] char span in a named source. If a value
can't be located in the source, it is dropped (None) — never invented. Required
fields missing, impossible values, or cross-tier disagreement -> needs_review.
"""

from __future__ import annotations

import json
import re
from typing import Any

from config import settings
from observability.metrics import COUNTERS, get_logger
from pipeline.extract.grounding import fact, find_span, source_hash
from store.dao import Store

log = get_logger("extract")

EXTRACTOR_VERSION = "extract_v1"

# Required for a clean billable fact set; anything missing -> flag_for_review.
REQUIRED_FIELDS = ("wound_type", "location", "length_cm", "width_cm")

WOUND_TYPES = ["Pressure", "Diabetic", "Venous", "Arterial", "Surgical", "Burn", "Abscess"]
LOCATIONS = ["Sacrum", "Right hip", "Left hip", "Right heel", "Left heel", "Left foot",
             "Right foot", "Left plantar", "Right plantar", "Left upper arm", "Right upper arm",
             "Abdominal wall", "Coccyx", "Left lower leg", "Right lower leg", "heel"]
DRAINAGE_TYPES = ["serosanguineous", "serosang", "serous", "sanguineous", "purulent"]
DRAINAGE_AMOUNTS = ["none", "scant", "light", "moderate", "heavy", "minimal", "min"]
PERIWOUND = ["intact", "erythematous", "macerated", "indurated", "callused", "erythema"]

# Sanity bounds (cm). Outside this -> impossible value -> reject + flag.
_MIN_CM, _MAX_CM = 0.0, 100.0


# ---------------------------------------------------------------------------
# Tier 1 — structured assessment survey
# ---------------------------------------------------------------------------
_Q2FIELD = {
    "location": "location",
    "laterality": "laterality",
    "wound type": "wound_type",
    "stage": "stage",
    "length (cm)": "length_cm",
    "width (cm)": "width_cm",
    "depth (cm)": "depth_cm",
    "drainage type": "drainage_type",
    "drainage amount": "drainage_amount",
    "granulation %": "granulation_pct",
    "slough %": "slough_pct",
    "periwound": "periwound",
}
_NUMERIC = {"length_cm", "width_cm", "depth_cm", "granulation_pct", "slough_pct"}


def _coerce_number(field_name: str, value: Any) -> Any:
    if field_name not in _NUMERIC:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_structured(raw_json_text: str, source: str) -> tuple[dict[str, Any], list[str]]:
    """Parse the nested Q&A survey. Returns (facts, narrative_text_or_empty)."""
    facts: dict[str, Any] = {}
    narratives: list[str] = []
    try:
        survey = json.loads(raw_json_text)
    except Exception:
        return facts, narratives

    for section in survey.get("sections", []) or []:
        for q in section.get("questions", []) or []:
            question = str(q.get("question", "")).strip().lower()
            answer = q.get("answer")
            if question == "wound narrative" and answer:
                narratives.append(str(answer))
                continue
            field_name = _Q2FIELD.get(question)
            if not field_name or answer in (None, "", "N/A", "n/a"):
                continue
            value = _coerce_number(field_name, answer)
            if value is None:
                continue
            if field_name in _NUMERIC and field_name.endswith("_cm") and not (_MIN_CM < value < _MAX_CM):
                continue  # impossible measurement -> drop (grounded-or-null)
            span = find_span(raw_json_text, answer)
            f = fact(value, span, source, 0.95)
            if f:
                facts[field_name] = f
    return facts, narratives


# ---------------------------------------------------------------------------
# Tier 2 — regex over free text (notes, or an assessment narrative answer)
# ---------------------------------------------------------------------------
_LABELLED = {
    "length_cm": re.compile(r"length[:\s]+([\d.]+)\s*cm", re.I),
    "width_cm": re.compile(r"width[:\s]+([\d.]+)\s*cm", re.I),
    "depth_cm": re.compile(r"depth[:\s]+([\d.]+)\s*cm", re.I),
}
_TRIPLE = re.compile(r"([\d.]+)\s*x\s*([\d.]+)\s*x\s*([\d.]+)\s*cm", re.I)
_DOUBLE = re.compile(r"([\d.]+)\s*(?:cm)?\s*x\s*([\d.]+)\s*cm", re.I)
_DEPTH_ALT = re.compile(r"([\d.]+)\s*cm\s*deep", re.I)
_STAGE = re.compile(r"stage[:\s]*([1-4]|unstageable)", re.I)
_GRAN = re.compile(r"([\d.]+)\s*%\s*gran", re.I)
_SLOUGH = re.compile(r"([\d.]+)\s*%\s*slough", re.I)
_MM = re.compile(r"([\d.]+)\s*x\s*([\d.]+)\s*mm", re.I)


def _num_fact(text: str, m: re.Match, group: int, source: str) -> dict[str, Any] | None:
    try:
        val = float(m.group(group))
    except (TypeError, ValueError):
        return None
    if not (_MIN_CM < val < _MAX_CM):
        return None
    span = m.span(group)
    return fact(val, span, source, 0.75)


def extract_regex(text: str, source: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if not text:
        return facts

    # Measurements: prefer the compact L x W x D triple, else labelled, else double.
    m = _TRIPLE.search(text)
    if m:
        for fname, g in (("length_cm", 1), ("width_cm", 2), ("depth_cm", 3)):
            f = _num_fact(text, m, g, source)
            if f:
                facts[fname] = f
    else:
        for fname, pat in _LABELLED.items():
            lm = pat.search(text)
            if lm:
                f = _num_fact(text, lm, 1, source)
                if f:
                    facts[fname] = f
        if "length_cm" not in facts:
            dm = _DOUBLE.search(text)
            if dm:
                for fname, g in (("length_cm", 1), ("width_cm", 2)):
                    f = _num_fact(text, dm, g, source)
                    if f:
                        facts[fname] = f
        if "depth_cm" not in facts:
            da = _DEPTH_ALT.search(text)
            if da:
                f = _num_fact(text, da, 1, source)
                if f:
                    facts["depth_cm"] = f

    # Units edge case: a measurement given in mm -> normalise to cm, flag-worthy.
    if "length_cm" not in facts:
        mm = _MM.search(text)
        if mm:
            for fname, g in (("length_cm", 1), ("width_cm", 2)):
                try:
                    val = round(float(mm.group(g)) / 10.0, 2)
                except (TypeError, ValueError):
                    continue
                facts[fname] = fact(val, mm.span(g), source, 0.6)

    # Categorical fields: keyword search -> grounded span.
    for fname, vocab in (("wound_type", WOUND_TYPES), ("location", LOCATIONS),
                         ("drainage_type", DRAINAGE_TYPES), ("drainage_amount", DRAINAGE_AMOUNTS),
                         ("periwound", PERIWOUND)):
        for term in vocab:
            span = find_span(text, term)
            if span:
                facts[fname] = fact(term, span, source, 0.7)
                break

    sm = _STAGE.search(text)
    if sm:
        facts["stage"] = fact(f"Stage {sm.group(1)}", sm.span(1), source, 0.7)
    gm = _GRAN.search(text)
    if gm:
        f = _num_fact(text, gm, 1, source)
        if f:
            facts["granulation_pct"] = f
    slm = _SLOUGH.search(text)
    if slm:
        f = _num_fact(text, slm, 1, source)
        if f:
            facts["slough_pct"] = f
    return facts


def _count_second_wound(text: str) -> bool:
    """HP notes sometimes document a second wound ('Heel wound also eval')."""
    if not text:
        return False
    markers = ("also eval", "second wound", "additional wound", "heel wound")
    return sum(text.lower().count(m) for m in markers) > 0


# ---------------------------------------------------------------------------
# Tier 3 — LLM (Gemini), grounded-or-null. Gated on GOOGLE_API_KEY.
# ---------------------------------------------------------------------------
def extract_llm(text: str, source: str) -> dict[str, Any]:
    """Only runs when a key is configured AND cheaper tiers fell short.

    The model is asked to return, for each field, the *verbatim substring* it read
    the value from. We then verify that substring exists in `text`; if it doesn't,
    we discard the value. The model can advise but cannot invent.
    """
    if not settings.LLM.google_api_key:
        return {}
    try:  # pragma: no cover - optional dependency / network
        import google.generativeai as genai
    except Exception:
        log.warning("google-generativeai not installed; skipping LLM tier")
        return {}

    try:  # pragma: no cover
        genai.configure(api_key=settings.LLM.google_api_key)
        model = genai.GenerativeModel(settings.LLM.model)
        prompt = (
            "Extract wound facts from the clinical note. Return STRICT JSON: a map "
            "of field -> {value, quote} where `quote` is the exact verbatim substring "
            "from the note the value came from. Fields: wound_type, location, stage, "
            "length_cm, width_cm, depth_cm, drainage_type, drainage_amount. Omit any "
            "field not explicitly present. Note:\n" + text
        )
        resp = model.generate_content(prompt, generation_config={"temperature": 0})
        data = json.loads(re.sub(r"```(json)?", "", resp.text).strip())
    except Exception as exc:  # pragma: no cover
        log.warning("LLM extraction failed: %s", exc)
        return {}

    facts: dict[str, Any] = {}
    for fname, obj in (data or {}).items():
        if not isinstance(obj, dict):
            continue
        quote = obj.get("quote")
        value = obj.get("value")
        span = find_span(text, quote) if quote else None
        if span is None:  # ungrounded -> hallucination -> drop
            COUNTERS.incr("llm_ungrounded_dropped")
            continue
        facts[fname] = fact(value, span, source, min(0.9, float(obj.get("confidence", 0.85))))
    return facts


# ---------------------------------------------------------------------------
# Orchestration: merge tiers for one source, then sanity/required checks
# ---------------------------------------------------------------------------
def _merge(*layers: dict[str, Any]) -> dict[str, Any]:
    """Higher tier wins per-field (structured > regex > llm), but a present fact
    never gets overwritten by a missing one."""
    out: dict[str, Any] = {}
    for layer in layers:
        for k, v in layer.items():
            if v and k not in out:
                out[k] = v
    return out


def _review_check(facts: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    missing = [f for f in REQUIRED_FIELDS if f not in facts]
    if missing:
        reasons.append(f"missing required field(s): {', '.join(missing)}")
    return (len(reasons) > 0), reasons


def _agg_confidence(facts: dict[str, Any]) -> float:
    vals = [f["confidence"] for f in facts.values() if f]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def extract_one_assessment(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of observation dicts (usually one) for an assessment."""
    raw_json_text = rec.get("raw_json") or ""
    source = f"assessment:{rec['id']}"
    structured, narratives = extract_structured(raw_json_text, source)
    # If the survey was a free-text narrative, fall back to regex on it.
    if narratives and len(structured) < len(REQUIRED_FIELDS):
        for nar in narratives:
            # ground the narrative back into the raw_json string
            base = raw_json_text.find(nar)
            reg = extract_regex(nar, source)
            if base >= 0:  # shift spans to raw_json coordinates
                for f in reg.values():
                    f["span"] = [f["span"][0] + base, f["span"][1] + base]
            structured = _merge(structured, reg)
    needs, reasons = _review_check(structured)
    return [{
        "source_type": "assessment", "source_id": str(rec["id"]), "wound_index": 0,
        "tier": "structured", "facts": structured, "confidence": _agg_confidence(structured),
        "needs_review": needs, "review_reasons": reasons,
        "source_hash": source_hash(raw_json_text),
    }]


def extract_one_note(rec: dict[str, Any]) -> list[dict[str, Any]]:
    text = rec.get("note_text") or ""
    source = f"note:{rec['id']}"
    primary = extract_regex(text, source)
    # Spend the LLM only when regex left required fields missing.
    if any(f not in primary for f in REQUIRED_FIELDS):
        primary = _merge(primary, extract_llm(text, source))
    needs, reasons = _review_check(primary)

    out = [{
        "source_type": "note", "source_id": str(rec["id"]), "wound_index": 0,
        "tier": "regex", "facts": primary, "confidence": _agg_confidence(primary),
        "needs_review": needs, "review_reasons": reasons,
        "source_hash": source_hash(text),
    }]
    if _count_second_wound(text):  # multi-wound note: under-specified -> flag
        out[0]["needs_review"] = True
        out[0]["review_reasons"] = reasons + ["multiple wounds documented in one note"]
    return out


def run(store: Store | None = None, force: bool = False) -> dict[str, int]:
    own = store is None
    store = store or Store()
    stats = {"notes": 0, "assessments": 0, "skipped_unchanged": 0, "needs_review": 0}

    # Build patient_id (string) lookup from cached patient lists for joins.
    sid_by_iid: dict[int, tuple[str | None, str | None]] = {}
    for prow in store.iter_raw("patients"):
        for p in prow["payload"] or []:
            sid_by_iid[p["id"]] = (p.get("patient_id"), str(p.get("facility_id")))

    for source_type, fetch in (("assessments", extract_one_assessment),
                               ("notes", extract_one_note)):
        for row in store.iter_raw(source_type):
            for rec in row["payload"] or []:
                iid = rec.get("patient_id")  # notes/assessments key on internal id
                if not isinstance(iid, int):
                    continue
                src_id = str(rec["id"])
                text = (rec.get("raw_json") if source_type == "assessments"
                        else rec.get("note_text")) or ""
                h = source_hash(text)
                if not force and store.existing_extraction_hash(
                        iid, source_type[:-1], src_id) == h:
                    stats["skipped_unchanged"] += 1
                    continue
                pid_str, fac = sid_by_iid.get(iid, (None, None))
                for obs in fetch(rec):
                    store.upsert_extraction(
                        internal_id=iid, patient_id=pid_str, facility_id=fac,
                        source_type=obs["source_type"], source_id=obs["source_id"],
                        wound_index=obs["wound_index"], tier=obs["tier"],
                        facts=obs["facts"], confidence=obs["confidence"],
                        needs_review=obs["needs_review"], review_reasons=obs["review_reasons"],
                        source_hash=obs["source_hash"], extractor_version=EXTRACTOR_VERSION,
                    )
                    key = "assessments" if source_type == "assessments" else "notes"
                    stats[key] += 1
                    if obs["needs_review"]:
                        stats["needs_review"] += 1

    log.info("extraction complete: %s", stats)
    if own:
        store.close()
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Tiered grounded wound-fact extraction")
    ap.add_argument("--force", action="store_true", help="re-extract even if source unchanged")
    run(force=ap.parse_args().force)
