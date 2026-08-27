# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Offline test for amd_cost.compute_amd_cost() against a synthetic mapping
table -- deliberately not the real AMD-supplied multipliers (those live in
ci_reports/mappings/, committed) and a fixed made-up duration rather than
anything pulled from ClickHouse, since real durations would drift as more
CI runs land and this test would then be checking arithmetic against a
moving target instead of the formula itself."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ci_reports.amd_cost import compute_amd_cost, count_gpus, match_gpu_label

SYNTHETIC_MAPPINGS = [
    {"label": "widget9000", "gpu": "Synthetic Widget 9000", "multiplier": 2.0},
    {"label": "sprocket", "gpu": "Synthetic Sprocket", "multiplier": 0.5},
    {"label": "linux.rocm.gpu", "gpu": "Synthetic Catch-All", "multiplier": 1.0},
]


def test_specific_label_beats_catch_all():
    result = compute_amd_cost("linux.rocm.gpu.widget9000.2", 10.0, SYNTHETIC_MAPPINGS)
    assert result["model"] == "Synthetic Widget 9000"
    assert result["gpu_count"] == 2
    assert result["gpu_hours"] == 20.0
    assert result["total_cost"] == 40.0


def test_catch_all_used_when_no_specific_label_matches():
    result = compute_amd_cost("linux.rocm.gpu.mi210.1", 5.0, SYNTHETIC_MAPPINGS)
    assert result["model"] == "Synthetic Catch-All"
    assert result["gpu_count"] == 1
    assert result["total_cost"] == 5.0


def test_no_trailing_count_defaults_to_one_gpu():
    result = compute_amd_cost("linux.rocm.gpu.sprocket", 8.0, SYNTHETIC_MAPPINGS)
    assert result["gpu_count"] == 1
    assert result["gpu_hours"] == 8.0
    assert result["total_cost"] == 4.0


def test_trailing_non_numeric_suffix_does_not_break_count():
    result = compute_amd_cost(
        "linux.rocm.gpu.widget9000.1.test-control", 3.0, SYNTHETIC_MAPPINGS
    )
    assert result["gpu_count"] == 1


def test_unmapped_runner_returns_none_instead_of_guessing():
    assert match_gpu_label("some-totally-unknown-runner", SYNTHETIC_MAPPINGS) is None
    assert compute_amd_cost("some-totally-unknown-runner", 1.0, SYNTHETIC_MAPPINGS) is None


def test_count_gpus_helper_directly():
    assert count_gpus("linux.rocm.gpu.widget9000.4", "widget9000") == 4
    assert count_gpus("linux.rocm.gpu.widget9000", "widget9000") == 1


if __name__ == "__main__":
    test_specific_label_beats_catch_all()
    test_catch_all_used_when_no_specific_label_matches()
    test_no_trailing_count_defaults_to_one_gpu()
    test_trailing_non_numeric_suffix_does_not_break_count()
    test_unmapped_runner_returns_none_instead_of_guessing()
    test_count_gpus_helper_directly()
    print("OK")
