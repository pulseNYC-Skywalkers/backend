"""Export billing rules documentation to Excel.

    python -m scripts.export_rules
    python -m scripts.export_rules -o billing_rules_v1.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dashboard.rules_export import rules_export_filename, rules_to_xlsx


def main() -> None:
    ap = argparse.ArgumentParser(description="Export billing rules to Excel")
    ap.add_argument("-o", "--output", help="output .xlsx path")
    args = ap.parse_args()

    out = Path(args.output) if args.output else Path(rules_export_filename())
    out.write_bytes(rules_to_xlsx())
    print(f"Wrote {out.resolve()}")


if __name__ == "__main__":
    main()
