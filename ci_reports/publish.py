# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Assembles the Cloudflare Pages deploy directory: the committable site
shell (ci_reports/site/) plus the generated, gitignored data (trend
snapshot + one rendered report per month) it reads at runtime.

Kept as a separate build step rather than writing straight into
ci_reports/site/ so the shell's own source never gets mixed with generated
output that contains real cost figures -- ci_reports/site/ stays
committable code; data/site/ (this script's output) stays gitignored, same
as everything else under data/."""

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = REPO_ROOT / "ci_reports" / "site"
DATA_ROOT = REPO_ROOT / "data"
SITE_BUILD = DATA_ROOT / "site"


def publish():
    if SITE_BUILD.exists():
        shutil.rmtree(SITE_BUILD)
    shutil.copytree(SITE_SRC, SITE_BUILD)

    snapshot_path = DATA_ROOT / "trend_snapshot.json"
    if not snapshot_path.exists():
        print(
            "data/trend_snapshot.json not found -- run "
            "`python -m ci_reports.snapshot` first",
            file=sys.stderr,
        )
        sys.exit(1)
    shutil.copy(snapshot_path, SITE_BUILD / "trend_snapshot.json")

    import json

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    reports_dir = SITE_BUILD / "reports"
    reports_dir.mkdir()
    copied = 0
    for year_month in snapshot:
        report_path = DATA_ROOT / year_month / "report.html"
        if not report_path.exists():
            print(f"{year_month}: in snapshot but no report.html -- skipping", file=sys.stderr)
            continue
        month_dir = reports_dir / year_month
        month_dir.mkdir(parents=True)
        shutil.copy(report_path, month_dir / "index.html")
        copied += 1

    print(f"Published {copied} month report(s) to {SITE_BUILD}")


if __name__ == "__main__":
    publish()
