"""Demonstrate DuckDB querying raw JSON payloads directly (schema & queryability).

The PCC payloads are bare JSON arrays; assessments carry a JSON-encoded `raw_json`
string whose real structure is a NESTED survey (structured Q&A sections OR a
free-text "Wound narrative"). We parse both here — a preview of Milestone-2
extraction. Run: python -m scripts.inspect_raw [db_path]
"""

from __future__ import annotations

import collections
import json
import sys

from store.dao import Store


def _wound_type_from_assessment(raw_json: str) -> tuple[str | None, bool]:
    """Pull wound_type + whether depth is present, from either raw_json variant.
    Returns (wound_type, has_depth)."""
    try:
        doc = json.loads(raw_json)
    except Exception:
        return None, False
    qa: dict[str, object] = {}
    narrative = None
    for sec in doc.get("sections", []):
        for q in sec.get("questions", []):
            if q.get("question") == "Wound narrative":
                narrative = str(q.get("answer", ""))
            qa[str(q.get("question"))] = q.get("answer")
    if "Wound Type" in qa:  # structured variant
        depth = qa.get("Depth (cm)")
        has_depth = depth not in (None, "N/A", "")
        return str(qa["Wound Type"]), has_depth
    if narrative:  # free-text variant, e.g. "Pressure to Right hip / Measures ..."
        wt = narrative.split(" to ")[0].strip() if " to " in narrative else None
        return wt, ("Depth:" in narrative)
    return None, False


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    store = Store(db_path) if db_path else Store()

    cov = store.coverage_report()
    print("== coverage ==")
    print(f"  {cov['coverage_pct']}%  complete={cov['complete']}/{cov['total_tasks']}  "
          f"outstanding={cov['outstanding']}  quarantined={cov['quarantined']}")
    print(f"  by_status: {cov['by_status']}")

    print("\n== raw record counts ==")
    for (rt, n) in store.query(
        "SELECT resource_type, COUNT(*) FROM raw_records GROUP BY resource_type ORDER BY resource_type"
    ):
        print(f"  {rt:12s} {n}")

    print("\n== note_type distribution (raw notes JSON array) ==")
    for fmt, n in store.query(
        """
        SELECT json_extract_string(payload, '$[0].note_type') AS fmt, COUNT(*) AS n
        FROM raw_records WHERE resource_type='notes'
        GROUP BY fmt ORDER BY n DESC
        """
    ):
        print(f"  {str(fmt):22s} {n}")

    print("\n== assessment raw_json variant (real nested survey) ==")
    variant = collections.Counter()
    wt_dist = collections.Counter()
    missing_depth = 0
    for (rid,) in store.query("SELECT resource_id FROM raw_records WHERE resource_type='assessments'"):
        payload = store.get_raw("assessments", rid) or []
        if not payload:
            continue
        raw_json = payload[0].get("raw_json", "{}")
        doc = json.loads(raw_json)
        sec_names = {s.get("sectionName") for s in doc.get("sections", [])}
        variant["narrative" if "WOUND_INFO" in sec_names else "structured Q&A"] += 1
        wt, has_depth = _wound_type_from_assessment(raw_json)
        wt_dist[wt or "unknown"] += 1
        if not has_depth:
            missing_depth += 1
    for v, n in variant.most_common():
        print(f"  {v:18s} {n}")

    print("\n== wound_type distribution (parsed from real raw_json) ==")
    for wt, n in wt_dist.most_common():
        print(f"  {str(wt):22s} {n}")

    print(f"\n== assessments missing depth (future flag_for_review cases): {missing_depth} ==")

    print("\n== active Medicare Part B (MCB) patients — primary billing population ==")
    n = store.query(
        """
        SELECT COUNT(DISTINCT patient_id) FROM raw_records
        WHERE resource_type='coverage'
          AND payload::TEXT LIKE '%"payer_code": "MCB"%'
        """
    )[0][0]
    print(f"  MCB patients: {n}")

    print("\n== fetch_events outcome tally (resilience audit trail) ==")
    for outcome, n in store.query(
        "SELECT outcome, COUNT(*) AS n FROM fetch_events GROUP BY outcome ORDER BY n DESC"
    ):
        print(f"  {outcome:16s} {n}")

    qn = store.count_quarantine()
    if qn:
        print(f"\n== quarantine ({qn}) ==")
        for tk, reason in store.query("SELECT task_key, reason FROM quarantine LIMIT 10"):
            print(f"  {tk}: {reason}")

    store.close()


if __name__ == "__main__":
    main()
