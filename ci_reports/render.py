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


def known_architectures():
    """Every architecture label the vendor/architecture lookup table can
    produce, read straight from its 'architecture' column. Used so the
    Architecture pivot always lists every architecture the mapping table
    knows about -- even one with $0 this month -- rather than only whatever
    happens to be in per_vendor_totals' union (which drops a bucket entirely
    the moment no vendor billed anything for it that month)."""
    with open(MAPPINGS_ROOT / "instance_vendor_translations.json") as f:
        translations = json.load(f)
    return {row["architecture"] for row in translations}


def load_architecture_sources():
    """Hand-maintained {architecture: {"location": [...], "funded_by": [...],
    "note": str}} from ci_reports/mappings/architecture_sources.json -- where
    each architecture's capacity is hosted and who pays for it. Unlike the
    vendor/architecture lookup, this can't be derived from FOCUS or
    ClickHouse (neither retains a host/region column, and ClickHouse's
    owning_account has only 3 values, so it can't express member-donated
    capacity like Intel/Google/IBM), so it's static and reviewed by hand."""
    with open(MAPPINGS_ROOT / "architecture_sources.json") as f:
        rows = json.load(f)
    return {row["architecture"]: row for row in rows}


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
#
# `ephemeral.` is test-infra's own prefix for the ephemeral variant of a
# scale-config.yml runner_type (same instance_type, is_ephemeral: true) --
# see pytorch/test-infra's scale-runners lambda, which generates both a
# plain and an `ephemeral.`-prefixed entry per instance type. `c-` alone
# (no `mt`/`lf` provider) is ci-infra's bare canary marker for an OSDC
# runner label with no provider segment -- see the `c` field in
# pytorch/ci-infra's osdc/docs/runner_naming_convention.md. `canary.` is an
# older Meta canary-fleet prefix predating the `c-`/`c-mt-` scheme above,
# no longer used for new labels but still seen on historical data.
ENV_PREFIXES = (
    "c-mt-rel-",
    "c-mt-",
    "mt-rel-",
    "lf-rel-",
    "mt-",
    "lf-",
    "ephemeral.",
    "c-",
    "canary.",
)


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


def bucket_lf_runtime_by_os(runtime):
    """Sums LF's FOCUS runtime map by OS, the OS-side counterpart to
    bucket_lf_runtime_by_arch() -- same "Instance Hour" charge lines only,
    dropping every other charge type for the same reason (GB/requests/etc.
    aren't comparable to instance-hours)."""
    os_totals = {}
    for description, quantity in runtime.items():
        match = INSTANCE_HOUR_RE.search(description)
        if not match:
            continue
        os_name, _instance_type = match.groups()
        os_totals[os_name] = os_totals.get(os_name, 0.0) + quantity
    return os_totals


def bucket_runtime_by_os(rows):
    """Sums a ClickHouse vendor's raw `duration` column by the `os` column
    -- the OS-side counterpart to bucket_runtime_by_arch(). Used for both
    Meta and AMD rows."""
    os_totals = {}
    for row in rows:
        duration = float(row["duration"])
        os_name = normalize_os_name(row["os"])
        os_totals[os_name] = os_totals.get(os_name, 0.0) + duration
    return os_totals


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


def redistribute_intel_jobs_to_xpu(arch_totals, runtime_arch_totals, intel_rows, runner_to_arch):
    """Carves Intel's job-name-regex-matched cost/duration (see
    clickhouse_extract.INTEL_JOB_REGEX) out of whichever architecture bucket
    each row's runner_type would otherwise resolve to, and into an explicit
    "XPU" bucket -- rather than adding it on top of that bucket, which would
    double-count it. These rows are generic CPU/Windows runners (already
    counted under e.g. "x86_64 (Intel)"/"Windows") that happen to also run
    Intel XPU software tests; the job-name regex is the only way to find
    that slice, since the runner_type itself doesn't say so.

    A row whose runner_type already resolves to XPU on its own (e.g.
    linux.client.xpu) is skipped -- it's already in the XPU bucket via the
    vendor/architecture lookup, so redistributing it again would double-add
    it instead of double-counting it.

    Mutates and returns both dicts."""
    for row in intel_rows:
        arch = resolve_arch(row["runner_type"], runner_to_arch) or UNMAPPED_ARCH_LABEL
        if arch == "XPU":
            continue
        cost = float(row["cost"])
        duration = float(row["duration"])
        arch_totals[arch] = arch_totals.get(arch, 0.0) - cost
        arch_totals["XPU"] = arch_totals.get("XPU", 0.0) + cost
        runtime_arch_totals[arch] = runtime_arch_totals.get(arch, 0.0) - duration
        runtime_arch_totals["XPU"] = runtime_arch_totals.get("XPU", 0.0) + duration
    return arch_totals, runtime_arch_totals


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


def render_pivot_table(
    title,
    column_label,
    totals,
    grand_total,
    row_keys,
    value_label="Cost",
    format_value=None,
    extra_columns=None,
):
    """row_keys is the common set of keys shown across every vendor's table
    in a section, so a key absent from this vendor's totals still gets a
    row, at zero, rather than being omitted. Display order is this table's
    own spend, descending -- the shared row *set* keeps the tables
    comparable without forcing them all into one table's order.

    `value_label`/`format_value` let a caller reuse this for a non-cost
    metric (e.g. the Runtime section's instance-hours) without every cost
    table having to know about that -- defaults reproduce the original
    dollar formatting exactly.

    `extra_columns` is an optional list of (header, fn(row_key) -> str) for
    extra per-row columns after the primary key column (e.g. Location,
    Funded By) -- left as None (the default) reproduces today's two-column
    output byte-for-byte, so every call site but the Combined architecture
    tables is unaffected. Extra columns are left blank in the Total row --
    they describe a single row_key, not an aggregate."""
    if format_value is None:
        format_value = lambda v: f"${v:,.2f}"
    extra_columns = extra_columns or []
    ordered_keys = sorted(row_keys, key=lambda k: -totals.get(k, 0.0))
    extra_headers_html = "".join(f"<th>{html.escape(header)}</th>" for header, _ in extra_columns)
    rows_html = "\n".join(
        f"<tr><td>{html.escape(key)}</td>"
        + "".join(f"<td>{html.escape(fn(key))}</td>" for _, fn in extra_columns)
        + f"<td>{format_value(totals.get(key, 0.0))}</td></tr>"
        for key in ordered_keys
    )
    extra_blanks_html = "<td></td>" * len(extra_columns)
    return f"""
    <h4>{html.escape(title)}</h4>
    <table>
      <thead><tr><th>{html.escape(column_label)}</th>{extra_headers_html}<th>{html.escape(value_label)}</th></tr></thead>
      <tbody>
        {rows_html}
        <tr class="total"><td>Total</td>{extra_blanks_html}<td>{format_value(grand_total)}</td></tr>
      </tbody>
    </table>
    """


def render_pivot_section(
    section_title,
    column_label,
    per_vendor_totals,
    note=None,
    known_keys=None,
    value_label="Cost",
    format_value=None,
    annotate_vendors=None,
):
    """Renders one Architecture- or OS-style section: a row of Vendor pie
    charts (LF/Meta/AMD/Combined) followed by a row of matching Vendor->Total
    tables, graphs-above-tables like the Trend page. An optional
    `note` renders as a caption under the section heading (used by the OS
    section to call out that Linux is the default OS).

    Every vendor's table lists the same rows -- the union of keys across all
    vendors (plus `known_keys`, when given), so a key one vendor has $0 of
    (e.g. AMD has no Windows spend) still shows a $0.00 row there for
    comparison against the other tables, instead of just not appearing.
    Without `known_keys`, a key none of the vendors have at all this month
    is never in that union, so it's never shown -- pass `known_keys` (the
    Architecture section does, with known_architectures()) when the row set
    should always include architectures the mapping table knows about even
    when every vendor billed $0 for them this month. Each table's own row
    *order* is its own spend, descending (see render_pivot_table) -- only
    the row set is shared, not the order, so e.g. AMD's table still reads
    biggest-to-smallest for AMD even though that differs from LF's or
    Combined's ordering.

    `annotate_vendors` is an optional set of vendor names (e.g. {"Combined"})
    that get Location/Funded By columns from architecture_sources.json --
    only the Architecture section's call sites pass this, and only for the
    Combined table (the per-vendor LF/Meta/AMD tables already carry the
    vendor as their title, and four side-by-side 4-column tables don't fit).
    A row missing from the file renders "--" rather than a guess.
    """
    row_keys = {key for totals in per_vendor_totals.values() for key in totals}
    if known_keys:
        row_keys |= set(known_keys)

    annotate_vendors = annotate_vendors or set()
    sources = load_architecture_sources() if annotate_vendors else {}

    def source_columns(field):
        return lambda key: ", ".join(sources.get(key, {}).get(field, [])) or "—"

    tables = "\n".join(
        f'<div class="pivot-cell">{render_pivot_table(vendor, column_label, totals, sum(totals.values()), row_keys, value_label, format_value, extra_columns=[("Location", source_columns("location")), ("Funded By", source_columns("funded_by"))] if vendor in annotate_vendors else None)}</div>'
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
    <div class="pivot-row">{pies}</div>
    <div class="pivot-row">{tables}</div>
    """


def render_coverage_gaps(gaps):
    """Lists mapping-table gaps for visibility. This is informational only --
    a gap no longer blocks a month's report or snapshot entry from being
    produced. Cost that's known but unmapped to an architecture is still
    counted, just bucketed under the "N/A" row in the pivots above; cost
    that can't be derived at all (AMD's GPU label mapping) is called out by
    the "AMD runner_type not in GPU label mapping" line and excluded from
    the AMD total, which render_report() discloses separately."""
    mappings_link = '<p><a href="../../mappings/index.html">Review all mappings &amp; gaps &rarr;</a></p>'
    if not all(items == [] for items in gaps.values()):
        items_html = "\n".join(
            f"<li><strong>{html.escape(label)}:</strong> {len(items)} runner_type(s) -- "
            f"{html.escape(', '.join(items[:10]))}{' ...' if len(items) > 10 else ''}</li>"
            for label, items in gaps.items()
            if items
        )
        return f"<h3>Coverage gaps</h3><ul>{items_html}</ul>{mappings_link}"
    return f"<h3>Coverage gaps</h3><p>None.</p>{mappings_link}"


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
    with open(ch_dir / "intel_jobs_lf.json") as f:
        intel_jobs_lf = json.load(f)
    with open(ch_dir / "intel_jobs_excluding_lf.json") as f:
        intel_jobs_meta = json.load(f)

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

    # OS is independent of the XPU carve-out below (that only reallocates
    # between architecture buckets), so these don't need the same
    # redistribution pass lf_runtime_arch/meta_runtime_arch get.
    lf_runtime_os = bucket_lf_runtime_by_os(focus["runtime"])
    meta_runtime_os = bucket_runtime_by_os(meta_rows)
    amd_runtime_os = bucket_runtime_by_os(amd_rows)
    combined_runtime_os = combine_totals(lf_runtime_os, meta_runtime_os, amd_runtime_os)

    # Carve XPU out of whichever bucket the Intel-jobs regex rows would
    # otherwise land in -- a reallocation within each vendor's own total, not
    # new money, so lf_total/meta_total/combined_total are unaffected by
    # construction. See redistribute_intel_jobs_to_xpu().
    lf_arch, lf_runtime_arch = redistribute_intel_jobs_to_xpu(
        lf_arch, lf_runtime_arch, intel_jobs_lf, runner_to_arch
    )
    meta_arch, meta_runtime_arch = redistribute_intel_jobs_to_xpu(
        meta_arch, meta_runtime_arch, intel_jobs_meta, runner_to_arch
    )

    combined_runtime_arch = combine_totals(lf_runtime_arch, meta_runtime_arch, amd_runtime_arch)

    combined_arch = combine_totals(lf_arch, meta_arch, amd_arch)
    combined_os = combine_totals(lf_os, meta_os, amd_os)

    # Architectures with cost/hours this month (or known to the mapping
    # table at all) but no row in architecture_sources.json -- disclosed the
    # same way as any other mapping gap, so a "--" Location/Funded By cell
    # never reads as a confirmed "nowhere"/"no one". Not populated for
    # truly-unrecognized architecture labels (those are already covered by
    # the gap categories above and end up in "N/A", which does have a row).
    known_arch_keys = known_architectures() | set(combined_arch) | {"Other", "N/A"}
    missing_sources = sorted(known_arch_keys - set(load_architecture_sources()))

    gaps = {
        "Meta runner_type not in vendor/architecture lookup": meta_unmapped_arch,
        "AMD runner_type not in GPU label mapping (cost not derived)": amd_unmapped_gpu,
        "AMD runner_type not in vendor/architecture lookup": amd_unmapped_arch,
        "LF instance family not in vendor/architecture lookup": lf_unmapped_family,
        "Architecture not in the architecture-sources table": missing_sources,
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
        "lf_runtime_arch": lf_runtime_arch,
        "meta_runtime_arch": meta_runtime_arch,
        "amd_runtime_arch": amd_runtime_arch,
        "combined_runtime_os": combined_runtime_os,
        "lf_runtime_os": lf_runtime_os,
        "meta_runtime_os": meta_runtime_os,
        "amd_runtime_os": amd_runtime_os,
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


def discover_months():
    """Every month with a FOCUS extract on disk -- same test snapshot.py uses
    to decide a month is present at all."""
    return sorted(
        p.name for p in DATA_ROOT.iterdir() if p.is_dir() and (p / "focus_totals.json").exists()
    )


def collect_all_gaps():
    """Unions compute_month_totals()'s "gaps" dict across every month found
    under data/, so the Mappings page can show every runner_type/instance
    family that's ever gone unmapped -- not just whichever month happens to
    be rendered last. A gap that only showed up in one past month (e.g. a
    runner_type retired since) still belongs here: the lookup table is still
    missing it, so it's still worth resolving even if it means zero cost is
    being mis-bucketed this month.

    Returns {category: {item: sorted [year_month, ...]}}."""
    all_gaps = {}
    for year_month in discover_months():
        try:
            totals = compute_month_totals(year_month)
        except (FileNotFoundError, KeyError):
            continue
        for category, items in totals["gaps"].items():
            months_by_item = all_gaps.setdefault(category, {})
            for item in items:
                months_by_item.setdefault(item, []).append(year_month)
    return all_gaps


def resolve_runner_key(runner_type, runner_to_arch):
    """Same match order as resolve_arch(), but returns the matched
    translation-table key instead of its architecture -- used to mark a
    Meta/AMD runner_type "seen" against its own table row rather than just
    resolving what it costs."""
    if runner_type in runner_to_arch:
        return runner_type
    for prefix in ENV_PREFIXES:
        if runner_type.startswith(prefix):
            base = runner_type[len(prefix) :]
            if base in runner_to_arch:
                return base
    return None


def collect_label_last_seen():
    """For every row of instance_vendor_translations.json, the most recent
    month (from discover_months()) its runner/instance family actually
    showed up in a month's raw data -- so the Mappings page can show "last
    active" next to each row, the same way collect_all_gaps() shows "Seen
    in" for gap items.

    Meta/AMD rows are matched by their literal `runner_type` string (via
    resolve_runner_key(), the same ENV_PREFIXES-stripping resolve_arch()
    uses) against the `runner` column. LF rows have no runner_type at all --
    FOCUS only carries a ChargeDescription that resolves to an instance
    family (see bucket_lf()) -- so those are matched by `instance_family`
    instead, split by the same Windows/non-Windows OS branch bucket_lf()
    uses (a windows.* row's instance_family names the same family as its
    non-Windows counterpart, e.g. windows.g5.4xlarge.nvidia.gpu -> "g5", so
    without the OS branch a Linux g5 FOCUS line would also light up the
    Windows row and vice versa).

    Several rows share one instance_family (e.g. every linux.g5.* size), so
    a single FOCUS line marks all of them seen -- FOCUS resolves a family,
    not a specific label, and this is inherent to the data, not a bug here.

    Returns {row_key: "YYYY-MM"}, where row_key is the literal runner string
    for a Meta/AMD-style match, or ("family", instance_family, is_windows)
    for an LF-style match -- is_windows mirrors the same Windows/non-Windows
    split family_to_arch/bucket_lf() use, not the raw FOCUS OS string,
    since a non-Windows family match doesn't otherwise care which specific
    OS (Linux, SUSE, RHEL, ...) FOCUS reported. Rows never observed in any
    month are simply absent."""
    last_seen = {}

    def mark(key, year_month):
        if key not in last_seen or year_month > last_seen[key]:
            last_seen[key] = year_month

    for year_month in discover_months():
        month_dir = DATA_ROOT / year_month
        ch_dir = month_dir / "clickhouse"
        try:
            with open(month_dir / "focus_totals.json") as f:
                focus = json.load(f)
            with open(ch_dir / "meta_by_runner_type.json") as f:
                meta_rows = json.load(f)
            with open(ch_dir / "amd_by_runner_type.json") as f:
                amd_rows = json.load(f)
            with open(ch_dir / "intel_jobs_lf.json") as f:
                intel_jobs_lf = json.load(f)
            with open(ch_dir / "intel_jobs_excluding_lf.json") as f:
                intel_jobs_meta = json.load(f)
        except (FileNotFoundError, KeyError):
            continue

        runner_to_arch, _family_to_arch, _gpu_mappings = load_lookup_tables()

        for row in (*meta_rows, *amd_rows, *intel_jobs_lf, *intel_jobs_meta):
            matched = resolve_runner_key(row["runner_type"], runner_to_arch)
            if matched is not None:
                mark(matched, year_month)

        for description in focus["financials"]:
            match = INSTANCE_HOUR_RE.search(description)
            if not match:
                continue
            os_name, instance_type = match.groups()
            family = instance_type.split(".")[0]
            mark(("family", family, os_name == "Windows"), year_month)

    return last_seen


def render_mappings_page():
    """Renders a standalone page listing every row of the vendor/architecture
    and GPU label lookup tables -- the spreadsheet-tab equivalent the old
    sheet had for eyeballing the raw mapping data -- plus a Coverage gaps
    table up top highlighting every runner_type/instance family seen in a
    month's data that the lookup tables don't recognize, so gaps are visible
    in the same place as the tables they belong in, not just buried in each
    month's report."""
    with open(MAPPINGS_ROOT / "instance_vendor_translations.json") as f:
        translations = json.load(f)
    with open(MAPPINGS_ROOT / "gpu_label_mappings.json") as f:
        gpu_mappings_raw = json.load(f)
    with open(MAPPINGS_ROOT / "architecture_sources.json") as f:
        architecture_sources_raw = json.load(f)

    all_gaps = collect_all_gaps()
    has_gaps = any(all_gaps.values())
    last_seen = collect_label_last_seen()
    seen_months = discover_months()

    def render_seen_in(months):
        months = sorted(months)
        if len(months) <= 3:
            return ", ".join(months)
        return f"{len(months)} months, {months[0]} → {months[-1]}"

    def last_seen_for(row):
        if row["runner"] in last_seen:
            return last_seen[row["runner"]]
        if row["instance_family"] == "N/A":
            return None
        key = ("family", row["instance_family"], row["runner"].startswith("windows."))
        return last_seen.get(key)

    gaps_rows = "\n".join(
        f"<tr><td>{html.escape(category)}</td><td>{html.escape(item)}</td>"
        f"<td>{html.escape(render_seen_in(months))}</td></tr>"
        for category, months_by_item in all_gaps.items()
        for item, months in sorted(months_by_item.items())
    )
    gaps_section = (
        f"""
    <h2>Coverage gaps</h2>
    <p>Every runner_type/instance family seen in a month's data that the
    tables below don't recognize -- resolving one here removes it from this
    list and from the "N/A" bucket in that month's report.</p>
    <table class="gaps-table">
      <thead><tr><th>Gap</th><th>Runner type / instance family</th><th>Seen in</th></tr></thead>
      <tbody>{gaps_rows}</tbody>
    </table>
    """
        if has_gaps
        else "<h2>Coverage gaps</h2><p>None.</p>"
    )

    translation_rows = "\n".join(
        f"<tr><td>{html.escape(row['runner'])}</td>"
        f"<td>{html.escape(row['instance_family'])}</td>"
        f"<td>{html.escape(row['vendor'])}</td>"
        f"<td>{html.escape(row['model'])}</td>"
        f"<td>{html.escape(row['architecture'])}</td>"
        f"<td>{html.escape(last_seen_for(row) or '—')}</td></tr>"
        for row in sorted(translations, key=lambda r: r["runner"])
    )

    gpu_rows = "\n".join(
        f"<tr><td>{html.escape(row['label'])}</td>"
        f"<td>{html.escape(row['gpu'])}</td>"
        f"<td>{row['multiplier']}</td></tr>"
        for row in sorted(gpu_mappings_raw, key=lambda r: r["label"])
    )

    architecture_sources_rows = "\n".join(
        f"<tr><td>{html.escape(row['architecture'])}</td>"
        f"<td>{html.escape(', '.join(row['location']))}</td>"
        f"<td>{html.escape(', '.join(row['funded_by']))}</td>"
        f"<td>{html.escape(row.get('note', ''))}</td></tr>"
        for row in sorted(architecture_sources_raw, key=lambda r: r["architecture"])
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PyTorch Foundation CI Reports -- Mappings</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: min(1800px, 96vw); margin: 2rem auto; padding: 0 1rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; font-size: 0.9rem; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.8rem; text-align: left; }}
  thead th {{ background: #f4f4f4; position: sticky; top: 0; }}
  .gaps-table tr {{ background: #fff3cd; }}
  .gaps-table thead th {{ background: #ffe69c; }}
  .nav {{ margin-bottom: 1rem; }}
  input.filter {{ padding: 0.4rem; margin-bottom: 0.5rem; width: 100%; max-width: 320px; box-sizing: border-box; }}
</style>
</head>
<body>
<p class="nav"><a href="../index.html">&larr; Back to Trend</a></p>
<h1>Mappings</h1>
<p>The vendor/architecture and GPU label lookup tables used to bucket every
month's report, for reviewing coverage in one place instead of only inside
a given month's report.</p>

{gaps_section}

<h2>Vendor / architecture lookup</h2>
<p>Last seen reflects only the months present in this machine's local
<code>data/</code>{
    f" ({seen_months[0]} &rarr; {seen_months[-1]})" if seen_months else ""
} -- "—" means never observed there, not necessarily retired. A row whose
instance family is shared by several labels (e.g. every linux.g5.* size)
shows the same month for all of them, since FOCUS resolves to a family, not
a specific label.</p>
<input class="filter" id="filter-translations" type="text" placeholder="Filter by runner, vendor, model, or architecture...">
<table id="table-translations">
  <thead><tr><th>Runner</th><th>Instance family</th><th>Vendor</th><th>Model</th><th>Architecture</th><th>Last seen</th></tr></thead>
  <tbody>{translation_rows}</tbody>
</table>

<h2>GPU label mappings (AMD cost multiplier)</h2>
<input class="filter" id="filter-gpu" type="text" placeholder="Filter by label or GPU...">
<table id="table-gpu">
  <thead><tr><th>Label</th><th>GPU</th><th>Multiplier</th></tr></thead>
  <tbody>{gpu_rows}</tbody>
</table>

<h2>Architecture sources</h2>
<p>Where each architecture's capacity is hosted and who funds it -- hand
maintained (see ci_reports/mappings/architecture_sources.json), since
neither the FOCUS nor ClickHouse extract retains a host/funder column. A
Location or Funded By value of "Unknown" means we don't yet know, not that
none applies.</p>
<input class="filter" id="filter-sources" type="text" placeholder="Filter by architecture, location, or funder...">
<table id="table-sources">
  <thead><tr><th>Architecture</th><th>Location</th><th>Funded By</th><th>Note</th></tr></thead>
  <tbody>{architecture_sources_rows}</tbody>
</table>

<script>
  function wireFilter(inputId, tableId) {{
    const input = document.getElementById(inputId);
    const rows = document.querySelectorAll(`#${{tableId}} tbody tr`);
    input.addEventListener('input', () => {{
      const needle = input.value.toLowerCase();
      rows.forEach(row => {{
        row.style.display = row.textContent.toLowerCase().includes(needle) ? '' : 'none';
      }});
    }});
  }}
  wireFilter('filter-translations', 'table-translations');
  wireFilter('filter-gpu', 'table-gpu');
  wireFilter('filter-sources', 'table-sources');
</script>

</body>
</html>
"""


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
        known_keys=known_architectures(),
        annotate_vendors={"Combined"},
    )
    runtime_arch_section = render_pivot_section(
        "Runtime by architecture",
        "Architecture",
        {
            "LF": totals["lf_runtime_arch"],
            "Meta": totals["meta_runtime_arch"],
            "AMD": totals["amd_runtime_arch"],
            "Combined": totals["combined_runtime_arch"],
        },
        known_keys=known_architectures(),
        note="Figures are instance-hours. LF's slice only counts Instance "
        "Hour charge lines -- usage billed in other units (data transfer, "
        "storage, requests, etc.) isn't attributable to an architecture "
        "and is excluded here.",
        value_label="Hours",
        format_value=lambda v: f"{v:,.1f}",
        annotate_vendors={"Combined"},
    )
    runtime_os_section = render_pivot_section(
        "Runtime by OS",
        "OS",
        {
            "LF": totals["lf_runtime_os"],
            "Meta": totals["meta_runtime_os"],
            "AMD": totals["amd_runtime_os"],
            "Combined": totals["combined_runtime_os"],
        },
        note="Linux is the default OS -- the other rows are OS exceptions. "
        "Figures are instance-hours; LF's slice only counts Instance Hour "
        "charge lines, same as Runtime by architecture above.",
        value_label="Hours",
        format_value=lambda v: f"{v:,.1f}",
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
  body {{ font-family: system-ui, sans-serif; max-width: min(1800px, 96vw); margin: 2rem auto; padding: 0 1rem; }}
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
  .toggle {{ margin: 1rem 0; }}
  .toggle button {{ padding: 0.4rem 1rem; border: 1px solid #999; background: #f4f4f4; cursor: pointer; font-size: 0.95rem; }}
  .toggle button:first-child {{ border-radius: 4px 0 0 4px; }}
  .toggle button:last-child {{ border-radius: 0 4px 4px 0; border-left: none; }}
  .toggle button.active {{ background: #2a78d6; color: #fff; border-color: #2a78d6; }}
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
  AWS billing export. Meta and AMD are recomputed for the full calendar
  month using a consistent query methodology, so a previously published
  figure for the same month may differ slightly from an earlier one-off
  export.
</div>

<p class="grand-total">Combined total (list-price-equivalent): ${combined_total:,.2f}</p>

<h2>LF (from AWS FOCUS export -- trusted)</h2>
<p>Total: ${lf_total:,.2f}</p>

<h2>Meta (from ClickHouse -- estimated)</h2>
<p>Estimated total: ${meta_total:,.2f}</p>

<h2>AMD (from ClickHouse duration x AMD-provided multiplier -- estimated)</h2>
<p>Estimated total: ${amd_total:,.2f}</p>
{amd_cost_note}

<div class="toggle">
  <button id="btn-financials" class="active">Financials</button>
  <button id="btn-runtime">Runtime</button>
</div>

<div id="financials-view">
{arch_section}

{os_section}
</div>

<div id="runtime-view" style="display:none">
{runtime_arch_section}

{runtime_os_section}
</div>

{render_coverage_gaps(gaps)}

<script>
  const btnFin = document.getElementById('btn-financials');
  const btnRun = document.getElementById('btn-runtime');
  const finView = document.getElementById('financials-view');
  const runView = document.getElementById('runtime-view');

  function showFinancials() {{
    btnFin.classList.add('active');
    btnRun.classList.remove('active');
    finView.style.display = '';
    runView.style.display = 'none';
  }}

  function showRuntime() {{
    btnRun.classList.add('active');
    btnFin.classList.remove('active');
    runView.style.display = '';
    finView.style.display = 'none';
  }}

  btnFin.addEventListener('click', showFinancials);
  btnRun.addEventListener('click', showRuntime);

  if (new URLSearchParams(window.location.search).get('view') === 'runtime') {{
    showRuntime();
  }}
</script>

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
