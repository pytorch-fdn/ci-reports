# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Checks that LF-hosted runner types (`lf-`, `lf-rel-`, `c-lf-`,
`wincanarylf.`) are kept out of every Meta input in
render.compute_month_totals() -- their cost is already in LF's FOCUS total,
so ClickHouse rows for them tagged `owning_account = 'meta'` would otherwise
be counted twice -- and that their Intel-jobs rows move to the LF side of
the XPU carve-out instead of leaving Meta's buckets negative.

All figures are synthetic round numbers written to a temporary data/
directory; no real cost/usage data is involved."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ci_reports import render
from ci_reports.render import is_lf_hosted, split_lf_hosted

YEAR_MONTH = "2099-01"


def test_is_lf_hosted():
    for runner_type in (
        "lf-l-x86iavx512-8-64",
        "lf-rel-l-x86iavx512-8-64",
        "c-lf-l-x86iavx512-8-64",
        "lf-linux.c7i.8xlarge",
        "wincanarylf.windows.4xlarge",
    ):
        assert is_lf_hosted(runner_type), runner_type
    for runner_type in (
        "linux.2xlarge",
        "mt-l-x86iavx512-8-64",
        "c-mt-l-x86iavx512-8-64",
        "windows.4xlarge",
        "linux.lf.2xlarge",
    ):
        assert not is_lf_hosted(runner_type), runner_type


def test_split_lf_hosted():
    rows = [{"runner_type": "linux.2xlarge"}, {"runner_type": "lf-linux.2xlarge"}]
    kept, lf_hosted = split_lf_hosted(rows)
    assert kept == [{"runner_type": "linux.2xlarge"}]
    assert lf_hosted == [{"runner_type": "lf-linux.2xlarge"}]


def _row(runner_type, cost, duration, **extra):
    return {"runner_type": runner_type, "os": "linux", "cost": cost, "duration": duration, **extra}


def _write_month(data_root):
    month_dir = data_root / YEAR_MONTH
    ch_dir = month_dir / "clickhouse"
    ch_dir.mkdir(parents=True)
    focus = {
        "financials": {"$0.40 per On Demand Linux c5.2xlarge Instance Hour": 1000.0},
        "runtime": {"$0.40 per On Demand Linux c5.2xlarge Instance Hour": 2500.0},
    }
    meta = [
        _row("linux.2xlarge", 300.0, 100.0),
        _row("lf-linux.2xlarge", 200.0, 50.0),
        _row("wincanarylf.windows.4xlarge", 100.0, 25.0),
    ]
    intel_meta = [
        _row("linux.2xlarge", 30.0, 10.0, job_name="inductor-cpu-test"),
        _row("lf-linux.2xlarge", 20.0, 5.0, job_name="inductor-cpu-test"),
    ]
    files = {
        month_dir / "focus_totals.json": focus,
        ch_dir / "meta_by_runner_type.json": meta,
        ch_dir / "amd_by_runner_type.json": [],
        ch_dir / "intel_jobs_lf.json": [],
        ch_dir / "intel_jobs_excluding_lf.json": intel_meta,
    }
    for path, payload in files.items():
        path.write_text(json.dumps(payload))


def test_compute_month_totals_excludes_lf_hosted_from_meta():
    original_root = render.DATA_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        render.DATA_ROOT = Path(tmp)
        try:
            _write_month(Path(tmp))
            totals = render.compute_month_totals(YEAR_MONTH)
        finally:
            render.DATA_ROOT = original_root

    assert totals["lf_total"] == 1000.0
    assert totals["meta_total"] == 300.0
    assert totals["combined_total"] == 1300.0
    assert totals["meta_runtime_total"] == 100.0
    assert sum(totals["meta_os"].values()) == 300.0

    # Meta's Intel slice carves only its own (non-LF-hosted) row into XPU;
    # the LF-hosted Intel row is carved out of LF's FOCUS buckets instead.
    assert totals["meta_arch"]["XPU"] == 30.0
    assert totals["lf_arch"]["XPU"] == 20.0
    assert sum(totals["combined_arch"].values()) == totals["combined_total"]
    for key in ("lf_arch", "meta_arch", "combined_arch", "meta_runtime_arch"):
        for arch, value in totals[key].items():
            assert value >= 0, f"{key}[{arch}] went negative: {value}"


if __name__ == "__main__":
    test_is_lf_hosted()
    test_split_lf_hosted()
    test_compute_month_totals_excludes_lf_hosted_from_meta()
    print("ok")
