# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Combine the FOCUS (LF) and ClickHouse (Meta/AMD) extracts with the
sheet's own lookup tables into a single static HTML report for one month.

Reproduction fidelity differs by section, and the report says so explicitly
rather than implying a false apples-to-apples match:

- LF is reproduced exactly from FOCUS (see ci_reports/focus_extract.py).
- Meta and AMD are reproduced by *methodology*, matching HUD's own
  ClickHouse query -- but not necessarily by byte value against a previously
  published month. HUD's own dashboard is timezone-sensitive in how it
  builds its date-range query params, which can silently drop the first day
  of a manually-run export depending on the browser timezone of whoever ran
  it. This extractor always computes the correct, full calendar month
  instead of reproducing that artifact -- see "Meta/AMD figures are
  intentionally recomputed" in specs/monthly-ci-report.md for how this was
  verified.

AMD's cost is entirely derived (ClickHouse's `cost` column is always 0 for
AMD-owned rows) -- see ci_reports/amd_cost.py for the GPU-hours x multiplier
formula. A runner_type the vendor/architecture lookup doesn't recognize
still has its (known) cost counted, bucketed under an explicit "N/A" row
rather than dropped -- so a mapping-table gap never excludes a whole month.
A runner_type the GPU label mapping doesn't recognize has no derivable cost
at all, so those rows are excluded from the AMD total (which the report
discloses) rather than counted as $0. Either way, the gap itself is listed
under Coverage gaps.
"""

import html
import json
import math
import re
import sys
from pathlib import Path

from ci_reports.amd_cost import compute_amd_cost, load_gpu_label_mappings

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
MAPPINGS_ROOT = Path(__file__).resolve().parent / "mappings"

# Matches AWS FOCUS ChargeDescription lines for EC2 On-Demand instance-hours,
# e.g. "$8.61696 per On Demand Linux m8g.48xlarge Instance Hour" -- captures
# (os, instance_type). Charge lines that aren't instance-hours (S3, data
# transfer, ...) have no assignable OS/architecture and are excluded from
# the by-architecture and by-OS pivots, same as the sheet's own pivots.
INSTANCE_HOUR_RE = re.compile(r"per On Demand (\w+) ([a-z0-9]+\.[a-z0-9]+) Instance Hour")

# Same fixed bucket -> color map as the trend chart's ARCH_COLORS in
# ci_reports/site/index.html, so a given architecture/OS reads as the same
# color everywhere on the site rather than each pie assigning colors by its
# own sorted order (which could put a different hue on "CUDA" in every
# table). Keep this in sync with index.html's copy by hand -- there's no
# shared module between the Python report renderer and the client-side
# trend chart's JS.
#
# The 7 real architectures get the dataviz skill's 7 semantic hues (see
# index.html's ARCH_COLORS for the full rationale); 'Linux' -- which never
# appears in the architecture pivot, only the OS one -- takes the 8th and
# last hue in that palette, since it's the OS pivot's own dominant/default
# bucket. The remaining "not attributable" buckets share one neutral gray
# family, distinguished only by lightness.
ARCH_COLORS = {
    "CUDA": "#008300",
    "x86_64 (Intel)": "#2a78d6",
    "x86_64 (AMD)": "#eda100",
    "ROCM": "#e34948",
    "ARM64 (AWS Graviton)": "#1baf7a",
    "Windows": "#e87ba4",
    "macOS": "#4a3aa7",
    "Linux": "#eb6834",
    "x86_64 (Unknown)": "#6b6a66",
    "ARM64 (Unknown)": "#898781",
    "Other": "#a8a69f",
    "N/A": "#c3c2b7",
}


def color_for(label):
    return ARCH_COLORS.get(label, ARCH_COLORS["Other"])


def load_lookup_tables():
    lookup_dir = MAPPINGS_ROOT
    with open(lookup_dir / "instance_vendor_translations.json") as f:
        translations = json.load(f)
    runner_to_arch = {row["runner"]: row["architecture"] for row in translations}
    # Rows whose runner starts with "windows." tag the same instance family
    # (e.g. g4dn, g5) as architecture "Windows" regardless of the underlying
    # GPU -- built from non-Windows rows only so it agrees with the
    # OS-based override used for LF (see bucket_lf_by_architecture()).
    family_to_arch = {
        row["instance_family"]: row["architecture"]
        for row in translations
        if not row["runner"].startswith("windows.") and row["instance_family"] != "N/A"
    }
    gpu_mappings = load_gpu_label_mappings(lookup_dir / "gpu_label_mappings.json")
    return runner_to_arch, family_to_arch, gpu_mappings


# `c-mt-`/`c-lf-` canary fleets and `mt-rel-`/`lf-rel-` release-channel fleets
# run the same instance types as their plain `mt-`/`lf-` counterparts, just
# under a different environment prefix -- the vendor/architecture lookup only
# has one row per instance type, so these prefixes are stripped before
# falling back to "(unmapped)".
ENV_PREFIXES = ("c-mt-rel-", "c-mt-", "mt-rel-", "lf-rel-", "mt-", "lf-")


def resolve_arch(runner_type, runner_to_arch):
    if runner_type in runner_to_arch:
        return runner_to_arch[runner_type]
    for prefix in ENV_PREFIXES:
        if runner_type.startswith(prefix):
            base = runner_type[len(prefix) :]
            if base in runner_to_arch:
                return runner_to_arch[base]
    return None


# Label used in the architecture/OS pivots for rows whose cost is known but
# whose runner_type/instance family isn't in the vendor/architecture lookup
# yet -- shown as an explicit row so the gap is visible in the report itself,
# not just in the Coverage gaps section, and so the pivot's total still adds
# up to the section's real grand total.
UNMAPPED_ARCH_LABEL = "N/A"


def bucket_meta(rows, runner_to_arch):
    """Sums Meta's real `cost` column by architecture. Returns
    (arch_totals, grand_total, unmapped_runner_types)."""
    arch_totals = {}
    unmapped = []
    grand_total = 0.0
    for row in rows:
        cost = float(row["cost"])
        grand_total += cost
        arch = resolve_arch(row["runner_type"], runner_to_arch)
        if arch is None:
            unmapped.append(row["runner_type"])
            arch = UNMAPPED_ARCH_LABEL
        arch_totals[arch] = arch_totals.get(arch, 0.0) + cost
    return arch_totals, grand_total, sorted(set(unmapped))


def bucket_amd(rows, runner_to_arch, gpu_mappings):
    """Derives AMD's cost via amd_cost.compute_amd_cost() (ClickHouse's own
    `cost` column is always 0 for these rows) and sums by architecture.

    A runner_type missing from the GPU label mapping has no derivable cost
    at all (not "$0" -- unknown), so those rows are kept out of arch_totals/
    grand_total rather than bucketed with a fabricated number; their row
    count and total duration are returned separately so callers can disclose
    that the AMD total is a floor, not a complete figure, for months with
    this gap. A runner_type with derivable cost but no architecture mapping
    still has its real cost counted, just bucketed under N/A.

    Returns (arch_totals, grand_total, unmapped_gpu_labels, unmapped_arch,
    unresolved_gpu_duration)."""
    arch_totals = {}
    unmapped_gpu = []
    unmapped_arch = []
    grand_total = 0.0
    unresolved_gpu_duration = 0.0
    for row in rows:
        runner_type = row["runner_type"]
        duration = float(row["duration"])
        derived = compute_amd_cost(runner_type, duration, gpu_mappings)
        if derived is None:
            unmapped_gpu.append(runner_type)
            unresolved_gpu_duration += duration
            continue
        cost = derived["total_cost"]
        grand_total += cost
        arch = resolve_arch(runner_type, runner_to_arch)
        if arch is None:
            unmapped_arch.append(runner_type)
            arch = UNMAPPED_ARCH_LABEL
        arch_totals[arch] = arch_totals.get(arch, 0.0) + cost
    return (
        arch_totals,
        grand_total,
        sorted(set(unmapped_gpu)),
        sorted(set(unmapped_arch)),
        unresolved_gpu_duration,
    )


def bucket_runtime_by_arch(rows, runner_to_arch):
    """Sums a ClickHouse vendor's raw `duration` column by architecture --
    the Runtime-side counterpart to bucket_meta()/bucket_amd(), which bucket
    the (Meta-real, AMD-derived) `cost` column instead. Used for both Meta
    and AMD rows since both carry the same runner_type/duration shape.
    Unlike AMD's cost bucketing, this needs no GPU pricing multiplier -- the
    raw duration is already the Runtime figure -- so every row counts,
    including runner_types with no derivable AMD cost."""
    arch_totals = {}
    for row in rows:
        duration = float(row["duration"])
        arch = resolve_arch(row["runner_type"], runner_to_arch) or UNMAPPED_ARCH_LABEL
        arch_totals[arch] = arch_totals.get(arch, 0.0) + duration
    return arch_totals


def normalize_os_name(raw_os):
    """ClickHouse's `os` column is lowercase ("linux", "windows", "macos").
    str.capitalize() alone would turn "macos" into "Macos" -- wrong spelling
    and a miss against ARCH_COLORS' "macOS" entry -- so macOS gets its own
    case, and everything else still just gets capitalized."""
    if raw_os.lower() == "macos":
        return "macOS"
    return raw_os.capitalize()


def bucket_meta_by_os(rows):
    """Sums Meta's real `cost` column by the `os` column ClickHouse already
    carries -- no lookup table needed. Returns (os_totals, grand_total)."""
    os_totals = {}
    grand_total = 0.0
    for row in rows:
        cost = float(row["cost"])
        grand_total += cost
        os_name = normalize_os_name(row["os"])
        os_totals[os_name] = os_totals.get(os_name, 0.0) + cost
    return os_totals, grand_total


def bucket_amd_by_os(rows, gpu_mappings):
    """Derives AMD's cost via amd_cost.compute_amd_cost() and sums by the
    `os` column. Returns (os_totals, grand_total)."""
    os_totals = {}
    grand_total = 0.0
    for row in rows:
        derived = compute_amd_cost(row["runner_type"], float(row["duration"]), gpu_mappings)
        if derived is None:
            continue
        cost = derived["total_cost"]
        grand_total += cost
        os_name = normalize_os_name(row["os"])
        os_totals[os_name] = os_totals.get(os_name, 0.0) + cost
    return os_totals, grand_total


def bucket_lf(financials, family_to_arch):
    """Parses LF's FOCUS `ChargeDescription -> cost` map into architecture
    and OS totals. Only "Instance Hour" charge lines carry an assignable OS
    and instance family; every other charge type (S3, data transfer, EBS,
    ...) has neither, so it's bucketed under "Other" in both pivots rather
    than dropped -- keeping both pivots' totals equal to LF's overall total.
    Returns (arch_totals, os_totals, unmapped_instance_families)."""
    arch_totals = {}
    os_totals = {}
    unmapped = []
    for description, cost in financials.items():
        match = INSTANCE_HOUR_RE.search(description)
        if not match:
            arch_totals["Other"] = arch_totals.get("Other", 0.0) + cost
            os_totals["Other"] = os_totals.get("Other", 0.0) + cost
            continue
        os_name, instance_type = match.groups()
        family = instance_type.split(".")[0]

        if os_name == "Windows":
            arch = "Windows"
        else:
            arch = family_to_arch.get(family)
            if arch is None:
                unmapped.append(family)
                arch = UNMAPPED_ARCH_LABEL
        arch_totals[arch] = arch_totals.get(arch, 0.0) + cost
        os_totals[os_name] = os_totals.get(os_name, 0.0) + cost
    return arch_totals, os_totals, sorted(set(unmapped))


def bucket_lf_runtime_by_arch(runtime, family_to_arch):
    """Sums LF's FOCUS runtime map by architecture, for the Runtime-by-arch
    stack -- unlike bucket_lf(), non-"Instance Hour" charge lines (S3, data
    transfer, EBS, ...) are dropped rather than bucketed under "Other".
    Those lines carry GB/requests/etc, not instance-hours, so summing them
    with real instance-hours would swamp the chart with an incomparable
    number; the sheet's own Architecture pivot excludes them for the same
    reason. This means the result does not sum to lf_runtime_total by
    design -- that total still includes every usage type."""
    arch_totals = {}
    for description, quantity in runtime.items():
        match = INSTANCE_HOUR_RE.search(description)
        if not match:
            continue
        os_name, instance_type = match.groups()
        family = instance_type.split(".")[0]

        if os_name == "Windows":
            arch = "Windows"
        else:
            arch = family_to_arch.get(family) or UNMAPPED_ARCH_LABEL
        arch_totals[arch] = arch_totals.get(arch, 0.0) + quantity
    return arch_totals


def combine_totals(*totals_dicts):
    combined = {}
    for totals in totals_dicts:
        for key, value in totals.items():
            combined[key] = combined.get(key, 0.0) + value
    return combined


def render_pie_svg(title, totals, size=200):
    total = sum(totals.values())
    if total <= 0:
        return f"<div class='pie-block'><h4>{html.escape(title)}</h4><p>No data.</p></div>"

    cx = cy = size / 2
    r = size / 2 - 4
    angle = -90.0
    slices = []
    legend_items = []
    nonzero = [(label, value) for label, value in totals.items() if value > 0]
    for label, value in sorted(nonzero, key=lambda kv: -kv[1]):
        color = color_for(label)
        # A single 100% slice has identical start/end points (a full circle
        # in arc terms), which an SVG arc command draws as a zero-length path
        # -- nothing renders. Draw a plain circle for that one-slice case
        # instead of an arc.
        if len(nonzero) == 1:
            slices.append(f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="{color}"/>')
            legend_items.append(
                f'<li><span class="swatch" style="background:{color}"></span>'
                f"{html.escape(label)} ({value / total * 100:.1f}%)</li>"
            )
            break
        sweep = (value / total) * 360.0
        x1 = cx + r * math.cos(math.radians(angle))
        y1 = cy + r * math.sin(math.radians(angle))
        angle += sweep
        x2 = cx + r * math.cos(math.radians(angle))
        y2 = cy + r * math.sin(math.radians(angle))
        large_arc = 1 if sweep > 180 else 0
        slices.append(
            f'<path d="M{cx},{cy} L{x1:.2f},{y1:.2f} '
            f'A{r:.2f},{r:.2f} 0 {large_arc} 1 {x2:.2f},{y2:.2f} Z" fill="{color}"/>'
        )
        legend_items.append(
            f'<li><span class="swatch" style="background:{color}"></span>'
            f"{html.escape(label)} ({value / total * 100:.1f}%)</li>"
        )

    svg = f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">{"".join(slices)}</svg>'
    return f"""
    <div class="pie-block">
      <h4>{html.escape(title)}</h4>
      <div class="pie-row">
        {svg}
        <ul class="legend">{"".join(legend_items)}</ul>
      </div>
    </div>
    """


def render_pivot_table(title, column_label, totals, grand_total, row_keys):
    """row_keys is the common set of keys shown across every vendor's table
    in a section, so a key absent from this vendor's totals still gets a
    row, at $0.00, rather than being omitted. Display order is this table's
    own spend, descending -- the shared row *set* keeps the tables
    comparable without forcing them all into one table's order."""
    ordered_keys = sorted(row_keys, key=lambda k: -totals.get(k, 0.0))
    rows_html = "\n".join(
        f"<tr><td>{html.escape(key)}</td><td>${totals.get(key, 0.0):,.2f}</td></tr>"
        for key in ordered_keys
    )
    return f"""
    <h4>{html.escape(title)}</h4>
    <table>
      <thead><tr><th>{html.escape(column_label)}</th><th>Cost</th></tr></thead>
      <tbody>
        {rows_html}
        <tr class="total"><td>Total</td><td>${grand_total:,.2f}</td></tr>
      </tbody>
    </table>
    """


def render_pivot_section(section_title, column_label, per_vendor_totals, note=None):
    """Renders one Architecture- or OS-style section: a row of Vendor->Total
    tables (LF/Meta/AMD/Combined) followed by a row of matching pie charts,
    reproducing the sheet's own table+pie-per-vendor layout. An optional
    `note` renders as a caption under the section heading (used by the OS
    section to call out that Linux is the default OS).

    Every vendor's table lists the same rows -- the union of keys across all
    vendors, so a key one vendor has $0 of (e.g. AMD has no Windows spend)
    still shows a $0.00 row there for comparison against the other tables,
    instead of just not appearing. A key none of the vendors have at all
    (never billed this month) is never in that union, so it's never shown --
    "hidden" falls out of the union rather than needing a separate check.
    Each table's own row *order* is its own spend, descending (see
    render_pivot_table) -- only the row set is shared, not the order, so
    e.g. AMD's table still reads biggest-to-smallest for AMD even though
    that differs from LF's or Combined's ordering.
    """
    row_keys = {key for totals in per_vendor_totals.values() for key in totals}

    tables = "\n".join(
        f'<div class="pivot-cell">{render_pivot_table(vendor, column_label, totals, sum(totals.values()), row_keys)}</div>'
        for vendor, totals in per_vendor_totals.items()
    )
    pies = "\n".join(
        f'<div class="pivot-cell">{render_pie_svg(vendor, totals)}</div>'
        for vendor, totals in per_vendor_totals.items()
    )
    note_html = f'<p class="section-note">{html.escape(note)}</p>' if note else ""
    return f"""
    <h3>{html.escape(section_title)}</h3>
    {note_html}
    <div class="pivot-row">{tables}</div>
    <div class="pivot-row">{pies}</div>
    """


def render_coverage_gaps(gaps):
    """Lists mapping-table gaps for visibility. This is informational only --
    a gap no longer blocks a month's report or snapshot entry from being
    produced. Cost that's known but unmapped to an architecture is still
    counted, just bucketed under the "N/A" row in the pivots above; cost
    that can't be derived at all (AMD's GPU label mapping) is called out by
    the "AMD runner_type not in GPU label mapping" line and excluded from
    the AMD total, which render_report() discloses separately."""
    if not all(items == [] for items in gaps.values()):
        items_html = "\n".join(
            f"<li><strong>{html.escape(label)}:</strong> {len(items)} runner_type(s) -- "
            f"{html.escape(', '.join(items[:10]))}{' ...' if len(items) > 10 else ''}</li>"
            for label, items in gaps.items()
            if items
        )
        return f"<h3>Coverage gaps</h3><ul>{items_html}</ul>"
    return "<h3>Coverage gaps</h3><p>None.</p>"


def compute_month_totals(year_month):
    """Loads one month's FOCUS + ClickHouse extracts and buckets them by
    architecture/OS -- the shared computation behind both render_report()
    (the per-month HTML page) and snapshot.py (the cross-month trend line).

    Shows as much data as is available: a runner_type/instance family
    missing from ci_reports/mappings/ no longer excludes the whole month --
    its cost (when known) is bucketed under the "N/A" architecture instead,
    and the "gaps" dict below lists what's missing for disclosure."""
    month_dir = DATA_ROOT / year_month
    ch_dir = month_dir / "clickhouse"

    with open(month_dir / "focus_totals.json") as f:
        focus = json.load(f)
    lf_total = sum(focus["financials"].values())
    lf_runtime_total = sum(focus["runtime"].values())
    provenance = focus.get("x_Provenance", "focus_export")

    with open(ch_dir / "meta_by_runner_type.json") as f:
        meta_rows = json.load(f)
    with open(ch_dir / "amd_by_runner_type.json") as f:
        amd_rows = json.load(f)

    runner_to_arch, family_to_arch, gpu_mappings = load_lookup_tables()

    meta_arch, meta_total, meta_unmapped_arch = bucket_meta(meta_rows, runner_to_arch)
    (
        amd_arch,
        amd_total,
        amd_unmapped_gpu,
        amd_unmapped_arch,
        amd_unresolved_gpu_duration,
    ) = bucket_amd(amd_rows, runner_to_arch, gpu_mappings)
    lf_arch, lf_os, lf_unmapped_family = bucket_lf(focus["financials"], family_to_arch)
    meta_os, _ = bucket_meta_by_os(meta_rows)
    amd_os, _ = bucket_amd_by_os(amd_rows, gpu_mappings)
    combined_total = lf_total + meta_total + amd_total

    meta_runtime_total = sum(float(row["duration"]) for row in meta_rows)
    amd_runtime_total = sum(float(row["duration"]) for row in amd_rows)
    combined_runtime_total = lf_runtime_total + meta_runtime_total + amd_runtime_total

    # LF's slice only counts Instance Hour charge lines (see
    # bucket_lf_runtime_by_arch), so combined_runtime_arch does not sum to
    # combined_runtime_total -- it covers instance-hours only, matching the
    # sheet's own Architecture pivot, while the total covers every usage type.
    lf_runtime_arch = bucket_lf_runtime_by_arch(focus["runtime"], family_to_arch)
    meta_runtime_arch = bucket_runtime_by_arch(meta_rows, runner_to_arch)
    amd_runtime_arch = bucket_runtime_by_arch(amd_rows, runner_to_arch)
    combined_runtime_arch = combine_totals(lf_runtime_arch, meta_runtime_arch, amd_runtime_arch)

    combined_arch = combine_totals(lf_arch, meta_arch, amd_arch)
    combined_os = combine_totals(lf_os, meta_os, amd_os)

    gaps = {
        "Meta runner_type not in vendor/architecture lookup": meta_unmapped_arch,
        "AMD runner_type not in GPU label mapping (cost not derived)": amd_unmapped_gpu,
        "AMD runner_type not in vendor/architecture lookup": amd_unmapped_arch,
        "LF instance family not in vendor/architecture lookup": lf_unmapped_family,
    }
    has_gaps = any(gaps.values())

    return {
        "provenance": provenance,
        "lf_total": lf_total,
        "meta_total": meta_total,
        "amd_total": amd_total,
        "combined_total": combined_total,
        "lf_runtime_total": lf_runtime_total,
        "meta_runtime_total": meta_runtime_total,
        "amd_runtime_total": amd_runtime_total,
        "combined_runtime_total": combined_runtime_total,
        "combined_runtime_arch": combined_runtime_arch,
        "lf_arch": lf_arch,
        "meta_arch": meta_arch,
        "amd_arch": amd_arch,
        "combined_arch": combined_arch,
        "lf_os": lf_os,
        "meta_os": meta_os,
        "amd_os": amd_os,
        "combined_os": combined_os,
        "gaps": gaps,
        "has_gaps": has_gaps,
        "amd_unresolved_gpu_duration": amd_unresolved_gpu_duration,
    }


def render_report(year_month):
    totals = compute_month_totals(year_month)
    lf_total = totals["lf_total"]
    meta_total = totals["meta_total"]
    amd_total = totals["amd_total"]
    combined_total = totals["combined_total"]
    gaps = totals["gaps"]
    amd_unresolved_gpu_duration = totals["amd_unresolved_gpu_duration"]
    amd_cost_note = (
        f"<p><em>Note: {amd_unresolved_gpu_duration:,.1f} duration-unit(s) of AMD "
        "usage have no GPU pricing multiplier yet and are excluded from the AMD "
        "total above -- see Coverage gaps.</em></p>"
        if amd_unresolved_gpu_duration > 0
        else ""
    )

    arch_section = render_pivot_section(
        "Spend by architecture",
        "Architecture",
        {
            "LF": totals["lf_arch"],
            "Meta": totals["meta_arch"],
            "AMD": totals["amd_arch"],
            "Combined": totals["combined_arch"],
        },
    )
    os_section = render_pivot_section(
        "Spend by OS",
        "OS",
        {
            "LF": totals["lf_os"],
            "Meta": totals["meta_os"],
            "AMD": totals["amd_os"],
            "Combined": totals["combined_os"],
        },
        note="Linux is the default OS -- the other rows are OS exceptions.",
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PyTorch Foundation CI Report -- {html.escape(year_month)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.8rem; text-align: left; }}
  tr.total td {{ font-weight: bold; border-top: 2px solid #333; }}
  .banner {{ background: #fff3cd; border: 1px solid #ffe69c; padding: 1rem; border-radius: 4px; }}
  .grand-total {{ font-size: 1.3rem; font-weight: bold; }}
  .pivot-row {{ display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 1rem; }}
  .pivot-cell {{ flex: 1 1 260px; min-width: 240px; }}
  .pie-row {{ display: flex; align-items: center; gap: 0.75rem; }}
  .legend {{ list-style: none; padding: 0; margin: 0; font-size: 0.85rem; }}
  .legend li {{ display: flex; align-items: center; gap: 0.4rem; margin-bottom: 0.2rem; }}
  .swatch {{ display: inline-block; width: 0.8rem; height: 0.8rem; border-radius: 2px; flex-shrink: 0; }}
  .section-note {{ font-size: 0.85rem; color: #666; margin-top: -0.25rem; }}
</style>
</head>
<body>
<h1>PyTorch Foundation CI Report -- {html.escape(year_month)}</h1>

<div class="banner">
  <strong>Data trust:</strong> Data from the PyTorch Foundation AWS account
  (LF, below) is billing-grade and trusted. Figures from PyTorch HUD (Meta)
  and AMD-provided pricing are <strong>estimates</strong>, not confirmed
  real costs -- we don't have ground truth for either. This report does
  not include self-hosted runner costs provided directly by member
  companies.
  <br><br>
  <strong>Reproduction fidelity:</strong> LF is reproduced exactly from the
  AWS FOCUS export. Meta and AMD are recomputed live from ClickHouse using
  the same query methodology as HUD's own dashboard, for the full calendar
  month. A previously published month's figures may differ slightly because
  HUD's own dashboard can silently drop the first day of a manually-run
  export depending on the browser timezone of whoever generated it, and
  because published exports were sometimes taken before that month's data
  had fully landed -- this report does not reproduce either artifact. See
  the project spec's "Meta/AMD figures are intentionally recomputed"
  section for how this was verified.
</div>

<p class="grand-total">Combined total (list-price-equivalent): ${combined_total:,.2f}</p>

<h2>LF (from AWS FOCUS export -- trusted)</h2>
<p>Total: ${lf_total:,.2f}</p>

<h2>Meta (from ClickHouse -- estimated)</h2>
<p>Estimated total: ${meta_total:,.2f}</p>

<h2>AMD (from ClickHouse duration x AMD-provided multiplier -- estimated)</h2>
<p>Estimated total: ${amd_total:,.2f}</p>
{amd_cost_note}

{arch_section}

{os_section}

{render_coverage_gaps(gaps)}

</body>
</html>
"""


def main():
    if len(sys.argv) != 2:
        print("usage: python -m ci_reports.render <YYYY-MM>", file=sys.stderr)
        sys.exit(1)
    year_month = sys.argv[1]

    report_html = render_report(year_month)
    out_path = DATA_ROOT / year_month / "report.html"
    out_path.write_text(report_html)
    print(f"{year_month}: wrote {out_path}")


if __name__ == "__main__":
    main()
