# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Turns one month's raw CI-metrics extract (see ci_metrics_extract.py) into
a snapshot entry, and maintains data/ci_metrics_snapshot.json -- a sibling of
trend_snapshot.json, kept separate so neither file's schema churns the other.

`aggregate_month()` is a pure function of the three extracted row lists so it
can be unit-tested with synthetic rows, without ClickHouse.

**Aligned repo set.** The three sources cover very different repo sets
(runtime: most repos; jobs: fewer; PRs: fewest), so a ratio like hours-per-PR
is only meaningful over repos present in every source *that has data for the
month*. The default "aligned" scope is that intersection; "pytorch/pytorch"
is the one named repo scope the page offers on top. A source with no rows at
all for a month (e.g. runtime before `runner_cost` begins) is left out of the
intersection rather than emptying it, and its hours-based figures are null --
never zero -- so a missing month reads as "no data", not "no usage".

**Runtime is hours, not dollars.** `runner_cost` records AMD cost as 0 and
FOCUS (the LF bill) has no workflow dimension, so a per-workflow dollar
breakdown would not reconcile with the Financials total. Hours are the one
unit comparable across every owning account.

Like trend_snapshot.json, each month is captured once and then treated as
immutable (re-run with --force to recompute deliberately), so historical
points don't shift on every rebuild."""

import json
import sys
from pathlib import Path

from ci_reports.render import discover_months

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_PATH = DATA_ROOT / "ci_metrics_snapshot.json"
EXTRACT_SUBDIR = Path("clickhouse") / "ci_metrics"

# Bump when the entry shape changes so existing entries backfill on the next
# normal run instead of silently lacking the new fields.
SCHEMA_VERSION = 2

NAMED_REPO_SCOPE = "pytorch/pytorch"

# Workflows kept per scope per month; the remainder is summed into one
# "other" figure so the list still adds up to the scope's total. A workflow
# absent from the previous month's kept list is reported as NEW by the page.
TOP_WORKFLOWS = 100

OUTCOMES = ("success", "failure", "cancelled", "skipped", "other")

# Conclusions that are failures in all but name. Anything else outside
# OUTCOMES (action_required, neutral, empty, ...) lands in "other" rather
# than being mislabeled as skipped.
FAILURE_CONCLUSIONS = ("timed_out", "startup_failure")

# workflow_job.workflow_event values grouped for display. pull_request_target
# runs on the same PRs as pull_request; everything else not named here (a
# long tail of repository_dispatch, label, issues, ...) is "other".
TRIGGER_BUCKETS = {
    "pull_request": "pull_request",
    "pull_request_target": "pull_request",
    "push": "push",
    "schedule": "schedule",
    "workflow_dispatch": "workflow_dispatch",
}
OTHER_TRIGGER = "other"


def _round_floats(value, ndigits=4):
    """Rounds every float in a nested entry, so the snapshot the site
    downloads isn't padded with 15-digit noise."""
    if isinstance(value, float):
        return round(value, ndigits)
    if isinstance(value, dict):
        return {k: _round_floats(v, ndigits) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_floats(v, ndigits) for v in value]
    return value


def _ratio(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def _scope_entry(repos, prs, jobs, runtime, has_runtime):
    """Totals over `repos` only. `repos` is a set of repo names."""
    prs = [r for r in prs if r["repo"] in repos]
    jobs = [r for r in jobs if r["repo"] in repos]
    runtime = [r for r in runtime if r["repo"] in repos]

    authors = set()
    for r in prs:
        authors.update(r["authors"])

    job_count = sum(int(r["jobs"]) for r in jobs)
    job_hours = sum(float(r["hours"]) for r in jobs)
    rerun_hours = sum(float(r["rerun_hours"]) for r in jobs)

    hours_by_trigger = {}
    for r in jobs:
        bucket = TRIGGER_BUCKETS.get(r["event"], OTHER_TRIGGER)
        hours_by_trigger[bucket] = hours_by_trigger.get(bucket, 0.0) + float(r["hours"])

    entry = {
        "repo_count": len(repos),
        "prs_opened": sum(int(r["opened"]) for r in prs),
        "prs_merged": sum(int(r["merged"]) for r in prs),
        "contributors": len(authors),
        "runs": sum(int(r["runs"]) for r in jobs),
        "jobs": job_count,
        "rerun_jobs": sum(int(r["rerun_jobs"]) for r in jobs),
        "job_hours": job_hours,
        "rerun_hours": rerun_hours,
        "hours_by_trigger": hours_by_trigger,
        "hours": None,
        "hours_by_outcome": None,
        "workflows": None,
        "workflows_other_hours": None,
    }

    if has_runtime:
        by_outcome = {o: 0.0 for o in OUTCOMES}
        by_workflow = {}
        for r in runtime:
            hours = float(r["hours"])
            conclusion = r["conclusion"]
            if conclusion in FAILURE_CONCLUSIONS:
                conclusion = "failure"
            outcome = conclusion if conclusion in by_outcome else "other"
            by_outcome[outcome] += hours
            key = (r["repo"], r["workflow_name"])
            by_workflow[key] = by_workflow.get(key, 0.0) + hours
        ranked = sorted(by_workflow.items(), key=lambda kv: -kv[1])
        entry["hours"] = sum(by_outcome.values())
        entry["hours_by_outcome"] = by_outcome
        entry["workflows"] = [[repo, name, hours] for (repo, name), hours in ranked[:TOP_WORKFLOWS]]
        entry["workflows_other_hours"] = sum(h for _, h in ranked[TOP_WORKFLOWS:])

    hours = entry["hours"]
    waste = (
        entry["hours_by_outcome"]["failure"] + entry["hours_by_outcome"]["cancelled"]
        if has_runtime
        else None
    )
    entry["hours_per_pr"] = _ratio(hours, entry["prs_opened"])
    entry["jobs_per_pr"] = _ratio(job_count, entry["prs_opened"])
    entry["hours_per_job"] = _ratio(hours, job_count)
    entry["waste_pct"] = _ratio(waste, hours)
    entry["rerun_share"] = _ratio(rerun_hours, job_hours)
    return entry


def aggregate_month(prs, jobs, runtime):
    """One month's snapshot entry from the three raw extract row lists."""
    has_runtime = bool(runtime)
    source_repos = [
        {r["repo"] for r in rows} for rows in (prs, jobs, runtime) if rows
    ]
    aligned = set.intersection(*source_repos) if source_repos else set()

    entry = {
        "schema_version": SCHEMA_VERSION,
        "has_runtime": has_runtime,
        "scopes": {
            "aligned": _scope_entry(aligned, prs, jobs, runtime, has_runtime),
            NAMED_REPO_SCOPE: _scope_entry({NAMED_REPO_SCOPE}, prs, jobs, runtime, has_runtime),
        },
        "hours_by_repo": None,
        "hours_non_aligned": None,
    }

    if has_runtime:
        by_repo = {}
        non_aligned = 0.0
        for r in runtime:
            hours = float(r["hours"])
            if r["repo"] in aligned:
                by_repo[r["repo"]] = by_repo.get(r["repo"], 0.0) + hours
            else:
                non_aligned += hours
        entry["hours_by_repo"] = by_repo
        entry["hours_non_aligned"] = non_aligned
    return _round_floats(entry)


# Display colors for the monthly report's pies. Same validated categorical
# hues as render.ARCH_COLORS / site/index.html (kept in sync by hand, like
# those two): green/red/amber carry the success/failure/cancelled meaning,
# everything non-attributable falls to the neutral gray.
OUTCOME_COLORS = {
    "success": "#1baf7a",
    "failure": "#e34948",
    "cancelled": "#eda100",
    "skipped": "#a8a69f",
    "other": "#c9c7c0",
}
TRIGGER_COLORS = {
    "pull_request": "#2a78d6",
    "push": "#1baf7a",
    "schedule": "#eda100",
    "workflow_dispatch": "#4a3aa7",
    OTHER_TRIGGER: "#a8a69f",
}


def prev_month(year_month):
    year, month = (int(p) for p in year_month.split("-"))
    year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return f"{year:04d}-{month:02d}"


def pct_change(current, previous):
    """Relative change as a fraction, or None when either side is missing or
    the base is zero (an undefined, not infinite, change)."""
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def workflow_movers(current, previous, limit=15):
    """Workflows ranked by absolute hours added versus the previous month.

    `current`/`previous` are scope entries (or None for a month with no
    entry). A workflow is `new` only when a previous month exists with
    runtime data and the workflow isn't in its kept list -- with no previous
    month there is nothing to be new relative to. A workflow that fell out
    of the kept top-N (see TOP_WORKFLOWS) reads as new, which is accurate in
    practice: it was small enough to rank below the cutoff."""
    if not current or current["workflows"] is None:
        return []
    has_prev = bool(previous) and previous["workflows"] is not None
    prev_hours = (
        {(repo, name): hours for repo, name, hours in previous["workflows"]} if has_prev else {}
    )
    total = current["hours"] or 0.0
    rows = []
    for repo, name, hours in current["workflows"]:
        before = prev_hours.get((repo, name)) if has_prev else None
        rows.append(
            {
                "repo": repo,
                "workflow": name,
                "hours": hours,
                "delta_hours": None if not has_prev else hours - (before or 0.0),
                "delta_pct": pct_change(hours, before),
                "new": has_prev and before is None,
                "share": hours / total if total else None,
            }
        )
    rows.sort(key=lambda r: -(r["delta_hours"] if r["delta_hours"] is not None else r["hours"]))
    return rows[:limit]


def load_extract(year_month):
    """The three raw row lists for a month, or None if it was never
    extracted (ci_metrics_extract hasn't been run for it)."""
    base = DATA_ROOT / year_month / EXTRACT_SUBDIR
    names = (
        "prs_by_repo.json",
        "jobs_by_repo_event.json",
        "runtime_by_repo_workflow_conclusion.json",
    )
    if not all((base / n).exists() for n in names):
        return None
    out = []
    for n in names:
        with open(base / n) as f:
            out.append(json.load(f))
    return tuple(out)


def load_snapshot():
    if SNAPSHOT_PATH.exists():
        with open(SNAPSHOT_PATH) as f:
            return json.load(f)
    return {}


def build_snapshot(force=False):
    snapshot = load_snapshot()
    skipped = []

    for year_month in discover_months():
        current = snapshot.get(year_month)
        if not force and current and current.get("schema_version") == SCHEMA_VERSION:
            continue
        extract = load_extract(year_month)
        if extract is None:
            skipped.append(year_month)
            continue
        snapshot[year_month] = aggregate_month(*extract)

    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)

    return snapshot, skipped


def main():
    force = "--force" in sys.argv[1:]
    snapshot, skipped = build_snapshot(force=force)
    print(f"CI metrics snapshot has {len(snapshot)} month(s): {SNAPSHOT_PATH}")
    if skipped:
        print(
            f"{len(skipped)} month(s) not extracted yet (run "
            f"`python -m ci_reports.ci_metrics_extract <YYYY-MM>`): {', '.join(skipped)}"
        )


if __name__ == "__main__":
    main()
