"""Export billing rules documentation to Excel."""

from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from pipeline.decide.rules import load_config

_TITLE = Font(bold=True, size=12)
_HEADER = Font(bold=True)

# Exact payer rows (Code, Name, Rule) — order as in demo doc
_PAYERS = [
    ("MCB", "Medicare Part B", "Billable — bill if everything else checks out"),
    ("MCA", "Medicare Part A", "Conditional — always needs biller review"),
    ("MCD", "Medicaid", "Conditional — always needs biller review"),
    (
        "HMO",
        "Managed care",
        "Not billable — bundled, wound care not separately billable",
    ),
]

_DECISION_STEPS = [
    ("1.", "Payer in not_billable (HMO)", "do_not_bill"),
    ("2.", "require_active_coverage AND no active coverage", "do_not_bill"),
    (
        "3.",
        "needs_review from Resolve/Extract OR missing required wound fields",
        "flag_for_review",
    ),
    ("4.", "Wound area ≤ min_wound_area_cm2", "flag_for_review"),
    ("5.", "Payer in conditional (MCA, MCD)", "flag_for_review"),
    ("6.", "require_active_diagnosis AND no active diagnosis", "flag_for_review"),
    ("7.", "Otherwise (typically MCB + all checks pass)", "bill"),
]

_OUTCOMES = [
    ("Ready to bill", "MCB, active coverage & diagnosis, full wound docs", "Submit claim"),
    (
        "Needs review",
        "MCA/MCD, incomplete docs, conflicts, missing diagnosis",
        "Review & optionally approve in dashboard",
    ),
    ("Don't bill", "HMO, or lapsed coverage", "Skip — hard stop"),
]

_UPSTREAM_INTRO = (
    "Resolve/Extract can set needs_review before Decide runs:"
)
_UPSTREAM_BULLETS = [
    "Cross-source disagreement (e.g. note vs assessment measurements)",
    "Multiple wounds in one note",
    "No wound facts extracted",
    "Missing required fields at extraction time",
]
_UPSTREAM_NOTE = (
    'Those flow into flag_for_review with reasons like '
    '"Review: length_cm: sources disagree [...]".'
)

_DEMO_PATIENTS = [
    ("FA-001", "HMO", "Don't bill (rule 1)"),
    ("FA-003", "MCA", "Needs review (rule 5 — conditional payer)"),
    ("FA-007", "MCB", "Ready to bill (rule 7 — if docs complete)"),
]

_BILLER_OVERRIDES = [
    "Approve a flagged case → effective decision can move to bill",
    "Mark fields as optional → waives missing-field blockers",
    "Can't override do_not_bill (HMO) — must change payer config",
]


def _autofit(ws: Worksheet, max_col: int) -> None:
    for col_idx in range(1, max_col + 1):
        letter = get_column_letter(col_idx)
        max_len = max(
            len(str(ws.cell(row=r, column=col_idx).value or ""))
            for r in range(1, ws.max_row + 1)
        )
        ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 72)


def _title(ws: Worksheet, text: str) -> None:
    ws.append([text])
    ws.cell(row=ws.max_row, column=1).font = _TITLE


def _header_row(ws: Worksheet, cells: list[str]) -> None:
    ws.append(cells)
    for cell in ws[ws.max_row]:
        cell.font = _HEADER


def _blank(ws: Worksheet) -> None:
    ws.append([])


def _build_all_rules_sheet(ws: Worksheet, cfg: dict[str, Any]) -> None:
    version = cfg["rules_version"]
    req_fields = ", ".join(cfg.get("required_wound_fields", []))

    _title(ws, f"Config ({version})")
    _blank(ws)

    _title(ws, "Payer buckets")
    _header_row(ws, ["Code", "Name", "Rule"])
    for row in _PAYERS:
        ws.append(list(row))
    _blank(ws)

    _title(ws, "Other gates")
    _header_row(ws, ["Setting", "Value", "Meaning"])
    ws.append([
        "require_active_coverage",
        str(cfg.get("require_active_coverage")).lower(),
        "Must have coverage with effective_to == null",
    ])
    ws.append([
        "require_active_diagnosis",
        str(cfg.get("require_active_diagnosis")).lower(),
        "Must have a diagnosis with clinical_status == active",
    ])
    ws.append([
        "required_wound_fields",
        req_fields,
        "All four must be present (grounded facts)",
    ])
    ws.append([
        "min_wound_area_cm2",
        str(cfg.get("min_wound_area_cm2", 0)),
        "Area ≤ threshold → flag (healed/trivial); "
        "0.0 means only zero-area wounds flag",
    ])
    _blank(ws)

    _title(ws, "Decision logic (strict order)")
    ws.append(["Rules are checked top to bottom — first match wins:"])
    _blank(ws)
    _header_row(ws, ["Step", "Condition", "Outcome"])
    for step, cond, outcome in _DECISION_STEPS:
        ws.append([step, cond, f"→ {outcome}"])
    ws.append([
        "",
        "(includes: missing fields, source conflicts, multi-wound notes, etc.)",
        "",
    ])
    _blank(ws)

    _title(ws, "What each outcome means")
    _header_row(ws, ["Outcome", "Typical case", "Biller action"])
    for row in _OUTCOMES:
        ws.append(list(row))
    _blank(ws)

    _title(ws, "Upstream rules (not in YAML, but affect step 3)")
    ws.append([_UPSTREAM_INTRO])
    _blank(ws)
    for bullet in _UPSTREAM_BULLETS:
        ws.append([f"• {bullet}"])
    _blank(ws)
    ws.append([_UPSTREAM_NOTE])
    _blank(ws)

    _title(ws, "Demo patients (from your script)")
    _header_row(ws, ["Patient", "Payer", "Expected decision"])
    for row in _DEMO_PATIENTS:
        ws.append(list(row))
    _blank(ws)

    _title(ws, "Biller overrides (dashboard)")
    ws.append(["Separate from YAML rules:"])
    _blank(ws)
    for line in _BILLER_OVERRIDES:
        ws.append([f"• {line}"])

    _autofit(ws, 3)


def rules_to_xlsx(cfg: dict[str, Any] | None = None) -> bytes:
    cfg = cfg or load_config()
    wb = Workbook()
    ws = wb.active
    ws.title = "Billing rules"
    _build_all_rules_sheet(ws, cfg)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def rules_export_filename() -> str:
    cfg = load_config()
    return f"billing_rules_{cfg['rules_version']}.xlsx"
