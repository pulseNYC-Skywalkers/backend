"""Build biller-friendly export rows from canonical + decisions + overrides."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from store.dao import Store

DECISION_LABEL = {
    "bill": "Ready to bill",
    "flag_for_review": "Needs review",
    "do_not_bill": "Do not bill",
}

# Internal keys (stable order)
EXPORT_COLUMNS = [
    "patient_id",
    "first_name",
    "last_name",
    "facility_id",
    "payer",
    "wound_type",
    "location",
    "length_cm",
    "width_cm",
    "depth_cm",
    "area_cm2",
    "system_decision",
    "effective_decision",
    "biller_approved",
    "reasons",
    "rules_version",
]

# Human-readable Excel / CSV headers (no snake_case — avoids Excel import quirks)
EXPORT_HEADERS = [
    "Patient ID",
    "First Name",
    "Last Name",
    "Facility ID",
    "Payer",
    "Wound Type",
    "Location",
    "Length (cm)",
    "Width (cm)",
    "Depth (cm)",
    "Area (cm²)",
    "System Decision",
    "Effective Decision",
    "Biller Approved",
    "Reasons",
    "Rules Version",
]

# Strip control chars that break Excel XML
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = _ILLEGAL_XML.sub("", str(value))
    return text


def _fact_val(facts: dict[str, Any], field: str) -> str:
    f = facts.get(field)
    if not f:
        return ""
    return _cell(f.get("value"))


def _safe_overrides(store: Store) -> dict[int, dict[str, Any]]:
    try:
        return store.all_overrides()
    except Exception:
        return {}


def build_export_rows(store: Store) -> list[dict[str, str]]:
    decisions = {d["internal_id"]: d for d in store.all_decisions()}
    overrides = _safe_overrides(store)
    rows: list[dict[str, str]] = []

    for c in store.all_canonical():
        iid = c["internal_id"]
        d = decisions.get(iid, {})
        ov = overrides.get(iid)
        eff = Store._effective_decision_value(d, ov)
        pw = c.get("primary_wound") or {}
        facts = (pw.get("facts") or {}) if isinstance(pw, dict) else {}
        sys_dec = d.get("decision", "")
        reasons = d.get("reasons") or []

        rows.append({
            "patient_id": _cell(c.get("patient_id")),
            "first_name": _cell(c.get("first_name")),
            "last_name": _cell(c.get("last_name")),
            "facility_id": _cell(c.get("facility_id")),
            "payer": _cell(c.get("primary_payer_code")),
            "wound_type": _fact_val(facts, "wound_type"),
            "location": _fact_val(facts, "location"),
            "length_cm": _fact_val(facts, "length_cm"),
            "width_cm": _fact_val(facts, "width_cm"),
            "depth_cm": _fact_val(facts, "depth_cm"),
            "area_cm2": _cell(pw.get("area_cm2") if isinstance(pw, dict) else ""),
            "system_decision": _cell(DECISION_LABEL.get(sys_dec, sys_dec)),
            "effective_decision": _cell(DECISION_LABEL.get(eff, eff)),
            "biller_approved": "Yes" if ov and ov.get("approved") else "No",
            "reasons": _cell(" | ".join(str(r) for r in reasons)),
            "rules_version": _cell(d.get("rules_version")),
        })

    rows.sort(key=lambda r: r["patient_id"])
    return rows


def _row_values(row: dict[str, str]) -> list[str]:
    return [row.get(col, "") for col in EXPORT_COLUMNS]


def rows_to_csv(rows: Iterable[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_HEADERS)
    for row in rows:
        writer.writerow(_row_values(row))
    return buf.getvalue()


def rows_to_xlsx(rows: list[dict[str, str]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Billing"

    header_font = Font(bold=True)
    ws.append(EXPORT_HEADERS)
    for cell in ws[1]:
        cell.font = header_font

    for row in rows:
        ws.append(_row_values(row))

    for col_cells in ws.iter_cols(min_row=1, max_row=ws.max_row):
        letter = col_cells[0].column_letter
        max_len = max(len(str(cell.value or "")) for cell in col_cells)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 52)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def export_filename(fmt: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ext = "xlsx" if fmt == "xlsx" else "csv"
    return f"wound_care_billing_{stamp}.{ext}"
