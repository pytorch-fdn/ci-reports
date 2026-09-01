# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Builds the cross-month trend snapshot the site's landing page reads.

Each month's combined total is captured and then treated as immutable, even
if ClickHouse's live Meta/AMD data for that month changes later (e.g. more
runs landing) -- otherwise every historical point on the trend line would
shift on every rebuild, and the chart wouldn't be reproducible across runs.
Re-run with --force to recompute every month deliberately.

Each entry also carries "combined_arch" (cost per architecture bucket) and
"combined_runtime_arch" (duration per architecture bucket), both from
render.compute_month_totals, so the site's trend chart can render a stacked
bar per month for both metrics instead of just the combined total.
"combined_runtime_arch" covers instance-hours only (matching the sheet's own
Architecture pivot) and does not sum to "runtime_total", which covers every
usage type -- see bucket_lf_runtime_by_arch in render.py.

A month is only treated as complete -- and skipped on a normal run -- once
it has no coverage gap *and* already carries "combined_arch" and
"combined_runtime_arch". Those conditions exist so a schema addition like
this one backfills every existing entry the first time build_snapshot runs
after it, rather than silently leaving old entries without the new field.

The one exception is a month snapshotted while it still had a mapping-table
gap (has_gaps=True, see render.compute_month_totals) -- those are always
re-checked on a normal run (no --force needed) so that backfilling
ci_reports/mappings/ later automatically fixes that month's point without
disturbing already-complete months. A gap no longer excludes a month from
the snapshot at all; unmapped cost is bucketed under "N/A" and shown, not
hidden -- see render.py."""

import json
import sys
from pathlib import Path

from ci_reports.render import compute_month_totals, discover_months

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_PATH = DATA_ROOT / "trend_snapshot.json"


def load_snapshot():
    if SNAPSHOT_PATH.exists():
        with open(SNAPSHOT_PATH) as f:
            return json.load(f)
    return {}


def build_snapshot(force=False):
    snapshot = load_snapshot()
    failed = []

    for year_month in discover_months():
        already_complete = (
            year_month in snapshot
            and not snapshot[year_month].get("has_gaps")
            and "combined_arch" in snapshot[year_month]
            and "combined_runtime_arch" in snapshot[year_month]
        )
        if not force and already_complete:
            continue
        try:
            totals = compute_month_totals(year_month)
        except (FileNotFoundError, KeyError) as e:
            failed.append((year_month, f"{type(e).__name__}: {e}"))
            continue
        snapshot[year_month] = {
            "financials_total": totals["combined_total"],
            "runtime_total": totals["combined_runtime_total"],
            "provenance": totals["provenance"],
            "has_gaps": totals["has_gaps"],
            "combined_arch": totals["combined_arch"],
            "combined_runtime_arch": totals["combined_runtime_arch"],
        }

    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)

    return snapshot, failed


def main():
    force = "--force" in sys.argv[1:]
    snapshot, failed = build_snapshot(force=force)

    gapped = [m for m, v in snapshot.items() if v.get("has_gaps")]
    print(f"Snapshot has {len(snapshot)} month(s): {SNAPSHOT_PATH}")
    if gapped:
        print(
            f"{len(gapped)} month(s) still have a coverage gap (shown with "
            f"an N/A bucket, re-checked on next run): {', '.join(sorted(gapped))}"
        )
    if failed:
        print(f"\n{len(failed)} month(s) failed to compute (missing/bad data):")
        for year_month, detail in failed:
            print(f"  {year_month}: {detail}")


if __name__ == "__main__":
    main()
