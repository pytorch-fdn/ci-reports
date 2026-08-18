# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Extract LF AWS list-price spend directly from the FOCUS 1.0 Cost/Usage
Parquet export landing in an S3 bucket the org has read access to (bucket
name supplied via the FOCUS_BUCKET env var -- not hardcoded here since this
repo is public).

FOCUS's `ListCost` column is AWS's own computation of usage x public
on-demand list price, per line item. Reading it directly needs no
region-prefix table, no OS/license-model mapping, no per-service-code
guessing, and it prices every usage type AWS billed, including the long tail
(EBS, data transfer, CloudWatch, ...) that a hand-built Cost Explorer +
Pricing API reconstruction could only partially cover (that earlier approach
was tried first and is preserved in git history, not in this repo's tree).

Verified against the real July 2026 sheet during development: SUM(ListCost)
WHERE ChargeCategory = 'Usage' matched the sheet's "LF Spend from Ternary"
figure to the cent. ChargeCategory = 'Credit' nets to almost exactly the
same amount negated (the two AWS Open Source Promotional Credits) -- excluded
here since ListCost is list price, not post-credit cost, and the sheet's own
totals are built from the positive Usage lines only.

Runtime queries the same ChargeCategory = 'Usage' population as Financials,
just SUM(ConsumedQuantity) instead of SUM(ListCost) -- this matches the real
"LF Raw Ternary Import" tab, which includes non-instance usage (Lambda, API
Gateway, NAT, EBS, ...) under Measure = "Consumed Quantity" too. The sheet's
own Runner/Instance-Family lookup table -- not this extractor -- is what
filters those out of the Architecture pivot. The two CSVs still end up with
different row counts since each drops rows where its own measure is zero
(e.g. free-tier lines: ListCost = 0 but ConsumedQuantity > 0) -- that's a
per-measure filter, not a difference in row population.

Only covers months this FOCUS export exists for. Older months (e.g. 2026-03)
have no Parquet data here.
"""

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb

FOCUS_PREFIX = os.environ.get(
    "FOCUS_PREFIX", "pointfive-focus-export/PointFive-FOCUS-Export/data"
)


def download_focus_parquet(profile, bucket, year_month, dest_dir):
    """Sync one billing_period's Parquet files from the read-only FOCUS
    export bucket. Idempotent -- `aws s3 cp --recursive` only re-downloads
    changed/missing files."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    s3_prefix = f"s3://{bucket}/{FOCUS_PREFIX}/billing_period={year_month}/"
    cmd = ["aws", "--profile", profile, "s3", "cp", s3_prefix, str(dest_dir), "--recursive"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{result.stderr}")
    if not list(dest_dir.glob("*.parquet")):
        raise RuntimeError(
            f"No Parquet files found under {s3_prefix} -- FOCUS export may "
            "not exist for this month yet."
        )


def compute_totals(parquet_glob):
    """Group FOCUS line items by ChargeDescription -- the same key Ternary's
    feed and the sheet's lookup table already use.

    Financials and Runtime are the SAME row population (all ChargeCategory =
    'Usage' lines, any ResourceType) -- confirmed against the real July 2026
    "LF Raw Ternary Import" tab, which lists Lambda invocations, API Gateway
    requests, NAT bytes, EBS IOPS, etc. alongside EC2 instance-hours under
    Measure = "Consumed Quantity", not just instance-hours. (An earlier
    version of this function restricted Runtime to ResourceType = 'instance',
    which silently dropped all of that -- wrong; the sheet's own
    Runner/Instance-Family lookup table is what filters non-instance rows out
    of the Architecture pivot, not the raw import.)

    Financials: SUM(ListCost). Runtime: SUM(ConsumedQuantity).

    Returns (fin_totals, run_totals) dicts of description -> total.
    """
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT ChargeDescription, SUM(ListCost), SUM(ConsumedQuantity)
        FROM read_parquet('{parquet_glob}')
        WHERE ChargeCategory = 'Usage'
        GROUP BY ChargeDescription
        """
    ).fetchall()
    fin_totals = {desc: cost for desc, cost, qty in rows if cost}
    run_totals = {desc: qty for desc, cost, qty in rows if qty}
    return fin_totals, run_totals


def write_csv(path, totals, measure, period):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Charge Description", "Measure", period, "Totals"])
        for description, total in sorted(totals.items(), key=lambda kv: -kv[1]):
            amount = f"{total:.4f}"
            w.writerow([description, measure, amount, amount])


def main():
    if len(sys.argv) != 3:
        print("usage: python -m ci_reports.focus_extract <AWS_PROFILE> <YYYY-MM>", file=sys.stderr)
        sys.exit(1)
    profile, year_month = sys.argv[1], sys.argv[2]
    period = f"{year_month.split('-')[1]}/{year_month.split('-')[0]}"

    bucket = os.environ["FOCUS_BUCKET"]

    out_dir = Path(__file__).resolve().parent.parent / "data" / year_month
    focus_dir = out_dir / "focus_raw"
    download_focus_parquet(profile, bucket, year_month, focus_dir)

    fin_totals, run_totals = compute_totals(str(focus_dir / "*.parquet"))

    write_csv(out_dir / "financials.csv", fin_totals, "Billed Cost", period)
    write_csv(out_dir / "runtime.csv", run_totals, "Consumed Quantity", period)

    # Everything under data/ is gitignored, including this snapshot -- it
    # just lets the CSVs be regenerated locally without re-downloading the
    # raw Parquet if the export bucket's retention ages it out first.
    with open(out_dir / "focus_totals.json", "w") as f:
        json.dump({"financials": fin_totals, "runtime": run_totals}, f, indent=2)

    fin_total = sum(fin_totals.values())
    run_total = sum(run_totals.values())
    print(
        f"{year_month}: Financials total=${fin_total:,.2f}  "
        f"Runtime total={run_total:,.2f} (mixed units: instance-hours, "
        f"GB, requests, ... -- matches the raw-import column, not a "
        f"single unit) (FOCUS)"
    )


if __name__ == "__main__":
    main()
