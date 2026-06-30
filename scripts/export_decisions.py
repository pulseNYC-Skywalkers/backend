"""Export billing decisions to CSV or Excel.

    python -m scripts.export_decisions
    python -m scripts.export_decisions --format xlsx -o billing.xlsx

If the dashboard is running (holds the DuckDB lock), export via the API instead:
    curl -o billing.xlsx "http://127.0.0.1:8050/api/export?format=xlsx"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from dashboard.export import build_export_rows, export_filename, rows_to_csv, rows_to_xlsx
from store.dao import Store


def _export_via_api(base_url: str, fmt: str, out: Path) -> int:
    url = f"{base_url.rstrip('/')}/api/export?format={fmt}"
    try:
        resp = httpx.get(url, timeout=60.0)
    except httpx.ConnectError:
        print(f"Could not connect to dashboard at {base_url}", file=sys.stderr)
        return 1
    if resp.status_code == 404:
        print(
            "Export endpoint not found — restart the dashboard to pick up the latest code:\n"
            "  .venv/bin/python -m dashboard.app",
            file=sys.stderr,
        )
        return 1
    if resp.status_code != 200:
        print(f"Export failed ({resp.status_code}): {resp.text[:500]}", file=sys.stderr)
        return 1
    out.write_bytes(resp.content)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Export billing decisions to CSV or Excel")
    ap.add_argument("--format", choices=("csv", "xlsx"), default="csv")
    ap.add_argument("-o", "--output", help="output path (default: wound_care_billing_<timestamp>.<ext>)")
    ap.add_argument(
        "--api", metavar="URL", default=None,
        help="download via running dashboard (e.g. http://127.0.0.1:8050) — use when DB is locked",
    )
    args = ap.parse_args()

    out = Path(args.output) if args.output else Path(export_filename(args.format))

    if args.api:
        raise SystemExit(_export_via_api(args.api, args.format, out))

    try:
        store = Store(read_only=True)
    except Exception as exc:
        if "lock" in str(exc).lower():
            print(
                "DuckDB is locked (dashboard or another process is using the database).\n"
                "Either stop the dashboard and retry, or export via API:\n"
                "  python -m scripts.export_decisions --api http://127.0.0.1:8050 --format xlsx\n"
                "Or click Export Excel in the dashboard header.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        raise

    try:
        rows = build_export_rows(store)
        if args.format == "xlsx":
            out.write_bytes(rows_to_xlsx(rows))
        else:
            out.write_text(rows_to_csv(rows), encoding="utf-8-sig")
        print(f"Exported {len(rows)} patients -> {out.resolve()}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
