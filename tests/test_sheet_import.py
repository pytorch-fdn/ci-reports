# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Offline test for sheet_import.parse_wide_sheet() against a tiny synthetic
wide-format CSV -- not any real Ternary export. Covers the risk surface: a
multi-month row, a row with blank cells in some months (no usage that
month), the sheet's own "Totals" footer row (must be dropped, not treated
as a charge description), and a promotional-credit/discount row (must be
dropped so sheet-imported months are net of credit the same way
focus_extract.py's ChargeCategory = 'Usage' filter excludes them)."""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ci_reports.sheet_import import parse_wide_sheet

SYNTHETIC_ROWS = [
    ["Charge Description", "Measure", "03/2024", "04/2024", "Totals"],
    ["Synthetic widget-hours", "Billed Cost", "12.5", "", "12.5"],
    ["Synthetic sprocket-hours", "Billed Cost", "", "3.25", "3.25"],
    [
        "AWS Open Source Promotional Credit, credit from account: 000000000000",
        "Billed Cost",
        "-5.0",
        "",
        "-5.0",
    ],
    ["Totals", "", "7.5", "3.25", "10.75"],
]


def build_fixture_csv(path):
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(SYNTHETIC_ROWS)


def test_parse_wide_sheet_pivots_by_month_and_drops_totals_row():
    path = Path("/tmp/sheet_import_test_fixture.csv")
    build_fixture_csv(path)
    try:
        by_month = parse_wide_sheet(path)

        assert set(by_month.keys()) == {"2024-03", "2024-04"}
        assert by_month["2024-03"] == {"Synthetic widget-hours": 12.5}
        assert by_month["2024-04"] == {"Synthetic sprocket-hours": 3.25}
        assert "Totals" not in by_month["2024-03"]
        assert "Totals" not in by_month["2024-04"]
        assert not any(
            desc.startswith("AWS Open Source Promotional Credit")
            for desc in by_month["2024-03"]
        )
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_parse_wide_sheet_pivots_by_month_and_drops_totals_row()
    print("OK")
