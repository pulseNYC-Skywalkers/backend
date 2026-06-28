"""Deterministic synthetic dataset that faithfully emulates the REAL PCC API.

Calibrated against live responses from https://hackathon.prod.pulsefoundry.ai
(not just API.md, whose examples were idealized). Real-world specifics captured:

  * facility sizes are NON-uniform: 101=120, 102=90, 103=90 (300 total)
  * integer internal `id` is GLOBAL (1..300); string `patient_id` is FA-/FB-/FC-
  * coverage is a single record; payer_type is "Medicare"/"Medicaid"/"Managed Care"
  * FOUR note types, each a different free-text style:
      - "Wound Care Progress Note" / "Wound (SPN)" : shorthand ("Meas 4.3x4.4x1.3cm")
      - "HP Skin & Wound Note"                      : prose, sometimes MULTIPLE wounds
      - "Wound (IDT)"                               : Envive slash-delimited narrative
  * assessment.raw_json is a NESTED survey, in two variants:
      - structured Q&A sections (LOCATION/WOUND/DRAINAGE/WOUND_BED)  -> parseable
      - a single "Wound narrative" free-text answer                  -> needs NLP

Wound facts are embedded as literal substrings so Milestone-2 grounded extraction
can map each value to a character span. Seeded -> identical every run.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from config import settings

# (facility_id, prefix, patient_count) — matches the real API's non-uniform sizes.
FACILITIES = [(101, "FA", 120), (102, "FB", 90), (103, "FC", 90)]

WOUND_TYPES = ["Pressure", "Diabetic", "Venous", "Arterial", "Surgical", "Burn"]
LOCATIONS = ["Sacrum", "Right hip", "Left foot", "Right heel", "Left heel",
             "Left plantar", "Right plantar", "Left upper arm", "Abdominal wall",
             "Coccyx", "Left lower leg"]
LATERALITY = ["Left", "Right", "N/A"]
DRAINAGE_TYPES = ["serous", "serosanguineous", "sanguineous", "purulent", "none"]
DRAINAGE_AMOUNTS = ["none", "scant", "light", "moderate", "heavy"]
PERIWOUND = ["intact", "erythematous", "macerated", "indurated", "callused"]
FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Agnes", "Leon",
         "Linda", "William", "Elizabeth", "David", "Barbara", "Richard", "Susan"]
LAST = ["Smith", "Johnson", "Williams", "Dunbar", "Dawson", "Garcia", "Miller",
        "Davis", "Rodriguez", "Martinez", "Wilson", "Thornton", "Espinoza", "Taylor"]
# payer_code -> (payer_name, payer_type) as seen in real data.
PAYERS = [
    ("MCB", "Medicare Part B", "Medicare"),
    ("MCA", "Medicare Part A", "Medicare"),
    ("MCD", "Medicaid", "Medicaid"),
    ("HMO", "HMO / Managed Care", "Managed Care"),
]
ICD = {
    "Pressure": ("L89.152", "Pressure ulcer of sacral region, stage 2"),
    "Diabetic": ("E11.621", "Type 2 diabetes mellitus with foot ulcer"),
    "Venous": ("I83.009", "Varicose veins of lower extremity with ulcer"),
    "Arterial": ("I70.231", "Atherosclerosis of native arteries with ulceration"),
    "Surgical": ("T81.31XA", "Disruption of external operation wound"),
    "Burn": ("T24.209A", "Burn of unspecified degree of lower limb"),
}


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _make_wound(rng: random.Random) -> dict[str, Any]:
    wtype = rng.choice(WOUND_TYPES)
    loc = rng.choice(LOCATIONS)
    w = {
        "wound_type": wtype,
        "location": loc,
        "laterality": ("Left" if "Left" in loc else "Right" if "Right" in loc
                       else rng.choice(LATERALITY)),
        "stage": (f"Stage {rng.randint(1,4)}" if wtype == "Pressure" else "N/A"),
        "length_cm": round(rng.uniform(0.5, 9.0), 1),
        "width_cm": round(rng.uniform(0.3, 7.0), 1),
        "depth_cm": round(rng.uniform(0.0, 4.0), 1),
        "drainage_type": rng.choice(DRAINAGE_TYPES),
        "drainage_amount": rng.choice(DRAINAGE_AMOUNTS),
        "granulation": rng.randint(20, 80),
        "periwound": rng.choice(PERIWOUND),
    }
    w["slough"] = max(0, 100 - w["granulation"] - rng.randint(0, 10))
    # Realistic documentation gaps -> drive flag_for_review later.
    if rng.random() < 0.18:
        w["depth_cm"] = None
    if rng.random() < 0.10:
        w["drainage_amount"] = None
    return w


# --- note renderers (4 real formats) ------------------------------------------
def _note_shorthand(rng, name, w) -> str:
    depth = f"x{w['depth_cm']}" if w["depth_cm"] is not None else ""
    amt = (w["drainage_amount"] or "min")[:4].capitalize()
    dtype = {"serosanguineous": "serosang"}.get(w["drainage_type"], w["drainage_type"])
    dsg = rng.choice(["Foam", "Collagen", "Hydrocolloid", "Alginate"])
    return (
        f"Wound note - {w['location'].replace(' ', '')}. "
        f"Meas {w['length_cm']}x{w['width_cm']}{depth}cm. "
        f"{amt} {dtype} drainage, odor present. "
        f"Wound bed {w['granulation']}% gran, {w['slough']}% slough. "
        f"Periwound {w['periwound']}. {dsg} dsg changed. "
        f"Pt tolerated well. Dr. {rng.choice(LAST)} notified."
    )


def _note_hp(rng, name, w) -> str:
    depth = f", depth {w['depth_cm']}cm" if w["depth_cm"] is not None else ""
    amt = w["drainage_amount"] or "min"
    base = (
        f"Pt seen for wound eval. {w['wound_type']} {w['location']} measures aprx "
        f"{w['length_cm']} x {w['width_cm']}cm{depth}.\n"
        f"{amt.capitalize()} drainage {w['drainage_type']}. "
    )
    # HP notes sometimes document a SECOND wound (multi-wound edge case).
    if rng.random() < 0.3:
        l2, wd2, d2 = round(rng.uniform(1, 4), 1), round(rng.uniform(1, 3), 1), round(rng.uniform(0.1, 1), 1)
        base += (f"Heel wound also eval - {rng.choice(['L','R'])} heel {l2}x{wd2}, "
                 f"{d2}cm deep, slight serous. ")
    base += "Wounds cleaned w/ NS, covered. Will follow up Mon."
    return base


def _note_idt(rng, name, w) -> str:
    odor = rng.choice(["Odor present.", "No odor noted."])
    return (
        "*Envive Care Conference Review - V 4.0\n"
        f"Wound Status: {w['wound_type']} to {w['location']} / "
        f"Measures {w['length_cm']} cm x {w['width_cm']} cm / Stage: {w['stage']}\n"
        f"Drainage present - {w['drainage_type']}, {w['drainage_amount'] or 'none'}. {odor} "
        "Treatment: Foam dressing change daily."
    )


NOTE_RENDERERS = {
    "Wound Care Progress Note": _note_shorthand,
    "Wound (SPN)": _note_shorthand,
    "HP Skin & Wound Note": _note_hp,
    "Wound (IDT)": _note_idt,
}


def _assessment_raw_json(rng, w, assessment_id) -> str:
    """Real nested survey: ~70% structured Q&A, ~30% free-text narrative."""
    header = {
        "assessmentId": assessment_id,
        "assessmentDate": "2026-05-15",
        "status": "Complete",
    }
    if rng.random() < 0.7:
        depth = w["depth_cm"]
        sections = [
            {"sectionName": "LOCATION", "questions": [
                {"question": "Location", "answer": w["location"]},
                {"question": "Laterality", "answer": w["laterality"]},
            ]},
            {"sectionName": "WOUND", "questions": [
                {"question": "Wound Type", "answer": w["wound_type"]},
                {"question": "Stage", "answer": w["stage"]},
                {"question": "Length (cm)", "answer": w["length_cm"]},
                {"question": "Width (cm)", "answer": w["width_cm"]},
                {"question": "Depth (cm)", "answer": depth if depth is not None else "N/A"},
            ]},
            {"sectionName": "DRAINAGE", "questions": [
                {"question": "Drainage Present", "answer": "Yes" if w["drainage_amount"] else "No"},
                {"question": "Drainage Type", "answer": w["drainage_type"].capitalize()},
                {"question": "Drainage Amount", "answer": (w["drainage_amount"] or "N/A").capitalize()},
            ]},
            {"sectionName": "WOUND_BED", "questions": [
                {"question": "Granulation %", "answer": w["granulation"]},
                {"question": "Slough %", "answer": w["slough"]},
                {"question": "Periwound", "answer": w["periwound"].capitalize()},
            ]},
        ]
    else:
        depth = f" / Depth: {w['depth_cm']} cm" if w["depth_cm"] is not None else ""
        narrative = (
            f"{w['wound_type']} to {w['location']} / "
            f"Measures {w['length_cm']} cm x {w['width_cm']} cm / Stage: {w['stage']}{depth} / "
            f"Drainage: {w['drainage_type']}, {w['drainage_amount'] or 'none'}"
        )
        sections = [{"sectionName": "WOUND_INFO", "questions": [
            {"question": "Wound narrative", "answer": narrative}]}]
    header["sections"] = sections
    return json.dumps(header)


@lru_cache(maxsize=1)
def build_dataset() -> dict[str, Any]:
    cfg = settings.MOCK
    base_time = datetime(2026, 5, 17, 19, 13, 0)

    patients_by_facility: dict[int, list[dict[str, Any]]] = {}
    by_pid_str: dict[str, dict[str, Any]] = {}
    by_internal_id: dict[int, dict[str, Any]] = {}

    internal_id = 0
    for fac_id, prefix, count in FACILITIES:
        # MOCK_PATIENTS_PER_FACILITY can override the real non-uniform sizes.
        n_patients = cfg.patients_per_facility if cfg.patients_per_facility != 100 else count
        patients_by_facility[fac_id] = []
        for n in range(1, n_patients + 1):
            internal_id += 1
            pid_str = f"{prefix}-{n:03d}"
            rng = random.Random(f"{cfg.seed}:{pid_str}")
            wound = _make_wound(rng)
            note_type = rng.choice(list(NOTE_RENDERERS))
            last_mod = base_time - timedelta(days=rng.randint(0, 25), hours=rng.randint(0, 23))
            first, last = rng.choice(FIRST), rng.choice(LAST)
            code, pname, ptype = rng.choices(PAYERS, weights=[0.5, 0.2, 0.2, 0.1])[0]

            patient_summary = {
                "id": internal_id, "facility_id": fac_id, "patient_id": pid_str,
                "first_name": first, "last_name": last,
                "birth_date": f"19{rng.randint(30,55)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
                "gender": rng.choice(["Male", "Female"]),
                "primary_payer_code": code, "last_modified_at": _iso(last_mod),
                "is_new_admission": rng.random() < 0.2,
            }

            icd_code, icd_desc = ICD[wound["wound_type"]]
            diagnoses = [{
                "id": rng.randint(1, 9_000_000), "patient_id": pid_str,
                "icd10_code": icd_code, "icd10_description": icd_desc,
                "clinical_status": "active", "onset_date": "2026-04-10",
                "last_modified_at": _iso(last_mod),
            }]
            if rng.random() < 0.4:
                diagnoses.append({
                    "id": rng.randint(1, 9_000_000), "patient_id": pid_str,
                    "icd10_code": "E11.9",
                    "icd10_description": "Type 2 diabetes mellitus without complications",
                    "clinical_status": "active", "onset_date": "2015-03-01",
                    "last_modified_at": _iso(last_mod),
                })

            coverage = [{
                "id": rng.randint(1, 9_000_000), "patient_id": pid_str,
                "payer_name": pname, "payer_code": code, "payer_type": ptype,
                "effective_from": "2024-01-01T00:00:00", "effective_to": None,
                "last_modified_at": _iso(last_mod),
            }]

            notes = [{
                "id": internal_id, "patient_id": internal_id, "org_id": f"ORG-{fac_id}",
                "pcc_note_id": 10000 + internal_id, "note_type": note_type,
                "effective_date": _iso(last_mod),
                "note_text": NOTE_RENDERERS[note_type](rng, first, wound),
                "created_by": f"RN {last}", "note_label": None,
                "sync_version": 1, "is_current": True,
            }]

            assessments = [{
                "id": internal_id, "patient_id": internal_id, "org_id": f"ORG-{fac_id}",
                "pcc_assessment_id": 20000 + internal_id,
                "assessment_type": "Weekly Wound Information Sheet", "status": "Complete",
                "assessment_date": last_mod.date().isoformat(),
                "completion_date": last_mod.date().isoformat(), "template_id": 5,
                "assessment_type_description": rng.choice(["Admissions", "Quarterly", "Annual"]),
                "raw_json": _assessment_raw_json(rng, wound, 50000 + internal_id),
                "sync_version": 1, "is_current": True,
            }]

            record = {"summary": patient_summary, "diagnoses": diagnoses,
                      "coverage": coverage, "notes": notes, "assessments": assessments,
                      "_wound": wound}
            patients_by_facility[fac_id].append(patient_summary)
            by_pid_str[pid_str] = record
            by_internal_id[internal_id] = record

    return {
        "patients_by_facility": patients_by_facility,
        "by_pid_str": by_pid_str,
        "by_internal_id": by_internal_id,
        "total_patients": internal_id,
    }
