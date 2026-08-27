# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Reimplements the sheet's AMD Financials methodology.

AMD-owned rows in ClickHouse's `misc.runner_cost` always carry `cost = 0` --
AMD's runners aren't billed per-second like a cloud provider, so the sheet
derives a cost from `duration` using GPU-hour pricing ratios AMD supplies
directly (the sheet's own "Label to GPU Mappings" table):

    #GPUs      = numeric suffix on the runner_type label (.1 / .2 / .4 / .8)
    GPU Hours  = Runtime (duration) x #GPUs
    Total Cost = GPU Hours x Model Cost Multiplier

These multipliers are AMD's own *estimated* per-GPU-hour value for each
model, not an invoice or metered cloud rate -- there's no real transaction
to bill against since AMD owns the hardware outright. Treat every AMD cost
figure this module produces as an estimate for cross-vendor comparison, not
an actual, auditable dollar cost.

Model/multiplier lookup is substring matching against `runner_type`, same as
the sheet: specific labels are tried before the generic "linux.rocm.gpu"
prefix, so a more specific label never gets shadowed by the catch-all.

This substring rule is not guaranteed to cover every runner_type that will
ever appear -- a newer GPU generation was observed in the real July 2026
sheet with no explicit row in its own mapping table, and this module
reproduces the *documented* table only. An unmapped runner_type is a real
coverage gap in the lookup data, not something to guess an answer for --
callers must surface it, not silently drop it or fall back to a default
multiplier.
"""

import json

CATCH_ALL_LABEL = "linux.rocm.gpu"


def load_gpu_label_mappings(path):
    with open(path) as f:
        return json.load(f)


def match_gpu_label(runner_type, mappings):
    """Return the matching mapping row for runner_type, or None if unmapped.

    The catch-all label is tried last regardless of its position in the
    mappings list, so it never shadows a more specific label that also
    happens to be a substring of the same runner_type."""
    ordered = sorted(mappings, key=lambda m: m["label"] == CATCH_ALL_LABEL)
    for mapping in ordered:
        if mapping["label"] in runner_type:
            return mapping
    return None


def count_gpus(runner_type, label):
    """#GPUs is the first purely-numeric dot-separated token that appears
    after the token containing the matched label; defaults to 1 when no such
    token exists (e.g. a bare runner_type with no trailing count)."""
    tokens = runner_type.split(".")
    needle = label.split(".")[-1]
    for i, token in enumerate(tokens):
        if needle in token:
            for later in tokens[i + 1 :]:
                if later.isdigit():
                    return int(later)
            return 1
    return 1


def compute_amd_cost(runner_type, duration, mappings):
    """Returns a dict of {gpu_count, model, multiplier, gpu_hours,
    total_cost}, or None if runner_type matches no mapping row -- a coverage
    gap the caller must surface, not paper over."""
    mapping = match_gpu_label(runner_type, mappings)
    if mapping is None:
        return None
    gpu_count = count_gpus(runner_type, mapping["label"])
    gpu_hours = duration * gpu_count
    return {
        "gpu_count": gpu_count,
        "model": mapping["gpu"],
        "multiplier": mapping["multiplier"],
        "gpu_hours": gpu_hours,
        "total_cost": gpu_hours * mapping["multiplier"],
    }
