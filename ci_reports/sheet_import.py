# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Backfill LF Financials/Runtime for months before the AWS FOCUS export
existed (2026-07), by importing the existing "LF Raw Ternary Import" /
"LF Raw Ternary Import Runtime" CSV exports instead of recomputing anything.

These sheet exports are already the same wide, one-row-per-charge-description
shape Ternary always produced -- this module just pivots them from
"one column per month" to the single-month shape `render.py` reads
(`data/<YYYY-MM>/focus_totals.json`), the same shape `focus_extract.py`
writes for FOCUS-covered months. `x_Provenance` is set to
`legacy_sheet_import` (see `specs/monthly-ci-report.md`) so a report can show
which months were recomputed vs. imported verbatim.

No AWS Cost Explorer / Pricing API calls -- deliberately not used here;
see COPS-455 for why.
"""

import csv
import json
import re
import sys
from pathlib import Path

from ci_reports.focus_extract import write_csv

# FOCUS Parquet export coverage starts here; months from here on are the
# FOCUS extractor's responsibility, not this importer's -- skipped rather
# than silently overwritten.
FIRST_FOCUS_YEAR_MONTH = "2026-07"

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"

_MONTH_COL_RE = re.compile(r"^(0[1-9]|1[0-2])/(\d{4})$")

# The Ternary export has no ChargeCategory column, so credit/discount lines
# can't be filtered the way focus_extract.py filters them (WHERE
# ChargeCategory = 'Usage'). Match them by charge description instead, so
# sheet-imported months are net of the same promotional credits/discounts
# focus_extract.py excludes -- otherwise a month with a large credit (e.g.
# the AWS Open Source Promotional Credit) would plot near zero on the trend
# chart next to FOCUS months, which are gross/pre-credit.
_CREDIT_DESCRIPTION_RE = re.compile(
    r"^(AWS Open Source Promotional Credit|Enterprise Discount Program Discount)",
    re.IGNORECASE,
)


def _month_col_to_year_month(col):
    month, year = col.split("/")
    return f"{year}-{month}"


def parse_wide_sheet(path):
    """Parse one Ternary wide-format CSV (Charge Description, Measure, one
    column per MM/YYYY, Totals) into {year_month: {charge_description: value}}.

    Skips the sheet's own "Totals" footer row (a per-column checksum, not a
    charge description) and blank cells (a charge description with no usage
    in that month)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    header = rows[0]
    month_cols = [
        (i, _month_col_to_year_month(col))
        for i, col in enumerate(header)
        if _MONTH_COL_RE.match(col)
    ]

    by_month = {year_month: {} for _, year_month in month_cols}
    for row in rows[1:]:
        description = row[0]
        if not description or description == "Totals":
            continue
        if _CREDIT_DESCRIPTION_RE.match(description):
            continue
        for i, year_month in month_cols:
            raw = row[i].strip()
            if not raw:
                continue
            by_month[year_month][description] = float(raw)

    return by_month


def write_month(year_month, fin_totals, run_totals):
    period = f"{year_month.split('-')[1]}/{year_month.split('-')[0]}"
    out_dir = DATA_ROOT / year_month
    out_dir.mkdir(parents=True, exist_ok=True)

    write_csv(out_dir / "financials.csv", fin_totals, "Billed Cost", period)
    write_csv(out_dir / "runtime.csv", run_totals, "Consumed Quantity", period)

    with open(out_dir / "focus_totals.json", "w") as f:
        json.dump(
            {
                "financials": fin_totals,
                "runtime": run_totals,
                "x_Provenance": "legacy_sheet_import",
            },
            f,
            indent=2,
        )


def main():
    if len(sys.argv) != 3:
        print(
            "usage: python -m ci_reports.sheet_import <cost_csv> <runtime_csv>",
            file=sys.stderr,
        )
        sys.exit(1)
    cost_path, runtime_path = sys.argv[1], sys.argv[2]

    fin_by_month = parse_wide_sheet(cost_path)
    run_by_month = parse_wide_sheet(runtime_path)

    for year_month in sorted(set(fin_by_month) | set(run_by_month)):
        if year_month >= FIRST_FOCUS_YEAR_MONTH:
            print(f"{year_month}: skipped, covered by focus_extract.py")
            continue
        fin_totals = fin_by_month.get(year_month, {})
        run_totals = run_by_month.get(year_month, {})
        write_month(year_month, fin_totals, run_totals)
        print(
            f"{year_month}: Financials total=${sum(fin_totals.values()):,.2f}  "
            f"Runtime total={sum(run_totals.values()):,.2f} (mixed units) "
            f"(legacy_sheet_import)"
        )


if __name__ == "__main__":
    main()
