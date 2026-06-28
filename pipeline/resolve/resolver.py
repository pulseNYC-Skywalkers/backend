"""Milestone 3 — Resolve: one canonical record per patient.

A patient can have several sources (an assessment + one or more notes) describing
the same wound, sometimes disagreeing. Resolve aggregates them into a single
billable view:

  * merge the grounded facts across sources, highest-confidence wins per field
  * detect cross-source disagreement (e.g. note depth 0.4 vs assessment 1.3) -> flag
  * choose the PRIMARY wound by severity (stage, then area) when there are several
  * attach demographics, coverage and diagnoses for the Decide stage
  * idempotent: keyed by internal_id; re-running overwrites in place (a deterministic
    key means re-runs never spawn duplicates)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from observability.metrics import get_logger
from pipeline.extract.grounding import source_hash
from store.dao import Store

log = get_logger("resolve")

RESOLVER_VERSION = "resolve_v1"

# Numeric fields where a cross-source disagreement matters for billing.
_CONFLICT_FIELDS = ("length_cm", "width_cm", "depth_cm")


def _stage_num(stage_fact: dict[str, Any] | None) -> int:
    if not stage_fact:
        return 0
    s = str(stage_fact.get("value", ""))
    digits = "".join(c for c in s if c.isdigit())
    return int(digits) if digits else 0


def _area(facts: dict[str, Any]) -> float:
    l = facts.get("length_cm", {}).get("value") if facts.get("length_cm") else None
    w = facts.get("width_cm", {}).get("value") if facts.get("width_cm") else None
    return float(l) * float(w) if l and w else 0.0


def merge_observations(observations: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Merge a group of observations describing one wound. Highest confidence wins
    per field; numeric disagreement across sources is surfaced as a conflict."""
    by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obs in observations:
        for field, f in (obs.get("facts") or {}).items():
            if f:
                by_field[field].append(f)

    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    for field, cands in by_field.items():
        best = max(cands, key=lambda f: f.get("confidence", 0))
        merged[field] = best
        if field in _CONFLICT_FIELDS and len(cands) > 1:
            vals = [float(c["value"]) for c in cands if isinstance(c.get("value"), (int, float))]
            if vals and (max(vals) - min(vals)) > max(0.5, 0.25 * max(vals)):
                conflicts.append(f"{field}: sources disagree {sorted(set(vals))}")
    return merged, conflicts


def run(store: Store | None = None) -> dict[str, int]:
    own = store is None
    store = store or Store()
    stats = {"patients": 0, "with_wound": 0, "needs_review": 0, "multi_wound": 0}

    # demographics keyed by internal id; coverage/diagnoses keyed by string patient_id
    summary_by_iid: dict[int, dict[str, Any]] = {}
    fac_by_iid: dict[int, str] = {}
    for prow in store.iter_raw("patients"):
        for p in prow["payload"] or []:
            summary_by_iid[p["id"]] = p
            fac_by_iid[p["id"]] = str(p.get("facility_id"))

    coverage_by_sid: dict[str, list[dict]] = {}
    for crow in store.iter_raw("coverage"):
        coverage_by_sid[crow["resource_id"]] = crow["payload"] or []
    diag_by_sid: dict[str, list[dict]] = {}
    for drow in store.iter_raw("diagnoses"):
        diag_by_sid[drow["resource_id"]] = drow["payload"] or []

    for iid, summary in summary_by_iid.items():
        pid_str = summary.get("patient_id")
        observations = store.extractions_for(iid)
        review_reasons: list[str] = []

        # Group observations into wounds. Index 0 = primary; index>0 = extra wounds.
        groups: dict[int, list[dict]] = defaultdict(list)
        for obs in observations:
            groups[obs.get("wound_index", 0)].append(obs)
            review_reasons.extend(obs.get("review_reasons") or [])

        wounds: list[dict[str, Any]] = []
        for widx, obs_group in sorted(groups.items()):
            merged, conflicts = merge_observations(obs_group)
            review_reasons.extend(conflicts)
            if merged:
                wounds.append({
                    "facts": merged,
                    "sources": sorted({o["source_type"] + ":" + o["source_id"] for o in obs_group}),
                    "stage_num": _stage_num(merged.get("stage")),
                    "area_cm2": round(_area(merged), 2),
                })

        # Primary = highest severity (stage, then area).
        wounds.sort(key=lambda w: (w["stage_num"], w["area_cm2"]), reverse=True)
        primary = wounds[0] if wounds else None
        if len(wounds) > 1:
            stats["multi_wound"] += 1

        coverage = coverage_by_sid.get(pid_str, [])
        diagnoses = diag_by_sid.get(pid_str, [])
        if not primary:
            review_reasons.append("no wound facts extracted")

        needs_review = len(review_reasons) > 0
        combined_hash = source_hash("|".join(sorted(o["source_hash"] or "" for o in observations)))

        store.upsert_canonical({
            "internal_id": iid, "patient_id": pid_str, "facility_id": fac_by_iid.get(iid),
            "first_name": summary.get("first_name"), "last_name": summary.get("last_name"),
            "primary_payer_code": summary.get("primary_payer_code"),
            "coverage": coverage, "diagnoses": diagnoses,
            "primary_wound": primary, "all_wounds": wounds,
            "n_notes": sum(1 for o in observations if o["source_type"] == "note"),
            "n_assessments": sum(1 for o in observations if o["source_type"] == "assessment"),
            "needs_review": needs_review,
            "review_reasons": sorted(set(review_reasons)),
            "source_hash": combined_hash,
        })
        stats["patients"] += 1
        if primary:
            stats["with_wound"] += 1
        if needs_review:
            stats["needs_review"] += 1

    log.info("resolve complete: %s", stats)
    if own:
        store.close()
    return stats


if __name__ == "__main__":
    run()
