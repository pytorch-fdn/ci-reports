# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Offline regression test for focus_extract.compute_totals().

No committed Parquet fixture (the real export is ~230MB/month) -- instead
duckdb writes a tiny synthetic Parquet file covering the risk surface: a
priced EC2 instance-hour row, a non-instance usage row (must still count,
unlike the old ResourceType='instance'-filtered version of this query), a
Credit row (must be excluded), and a row with a NULL ResourceType (observed
in the real export -- must not be dropped by the WHERE clause).
"""

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ci_reports.focus_extract import compute_totals


def build_fixture_parquet(path):
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            SELECT * FROM (VALUES
                ('$14.6064 per On Demand Linux r7a.48xlarge Instance Hour', 'Usage', 'instance', 146.064, 10.0),
                ('$0.045 per GB Data Processed by NAT Gateways', 'Usage', 'bucket', 45.0, 1000.0),
                ('$0.005 per In-use public IPv4 address per hour', 'Usage', NULL, 5.0, 1000.0),
                ('AWS Open Source Promotional Credits, credit id: 123', 'Credit', 'instance', -196.064, 0.0)
            ) t(ChargeDescription, ChargeCategory, ResourceType, ListCost, ConsumedQuantity)
        ) TO '{path}' (FORMAT PARQUET)
        """
    )


def test_compute_totals_includes_non_instance_and_excludes_credits(tmp_path=None):
    path = Path("/tmp/focus_extract_test_fixture.parquet")
    build_fixture_parquet(path)
    try:
        fin_totals, run_totals = compute_totals(str(path))
        fin_totals = {k: float(v) for k, v in fin_totals.items()}
        run_totals = {k: float(v) for k, v in run_totals.items()}

        assert fin_totals["$14.6064 per On Demand Linux r7a.48xlarge Instance Hour"] == 146.064
        assert fin_totals["$0.045 per GB Data Processed by NAT Gateways"] == 45.0
        assert fin_totals["$0.005 per In-use public IPv4 address per hour"] == 5.0
        assert "AWS Open Source Promotional Credits, credit id: 123" not in fin_totals

        assert run_totals["$14.6064 per On Demand Linux r7a.48xlarge Instance Hour"] == 10.0
        assert run_totals["$0.045 per GB Data Processed by NAT Gateways"] == 1000.0
        assert run_totals["$0.005 per In-use public IPv4 address per hour"] == 1000.0
        assert "AWS Open Source Promotional Credits, credit id: 123" not in run_totals
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_compute_totals_includes_non_instance_and_excludes_credits()
    print("OK")
