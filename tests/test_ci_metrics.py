# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Offline checks for the CI Metrics aggregation (ci_reports/ci_metrics.py)
and its extract SQL (ci_reports/ci_metrics_extract.py), using small synthetic
row lists shaped like the extractor's output -- no ClickHouse, no real
figures. Covers repo alignment, bot exclusion, author dedup, top-N plus other
reconciling to the total, null handling for months without runtime data, and
the month-over-month mover/NEW logic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ci_reports import ci_metrics
from ci_reports.ci_metrics import (
    aggregate_month,
    pct_change,
    prev_month,
    workflow_movers,
)
from ci_reports.ci_metrics_extract import BOT_LOGINS, prs_by_repo_sql


def pr(repo, opened, merged, authors):
    return {"repo": repo, "opened": opened, "merged": merged, "authors": authors}


def job(repo, event, jobs=10, runs=4, rerun_jobs=2, hours=5.0, rerun_hours=1.0):
    return {
        "repo": repo,
        "event": event,
        "jobs": jobs,
        "runs": runs,
        "rerun_jobs": rerun_jobs,
        "hours": hours,
        "rerun_hours": rerun_hours,
    }


def rt(repo, workflow, conclusion, hours):
    return {"repo": repo, "workflow_name": workflow, "conclusion": conclusion, "hours": hours, "jobs": 1}


def sample():
    prs = [
        pr("org/a", 10, 6, ["alice", "bob"]),
        pr("org/b", 4, 3, ["bob", "carol"]),
        pr("org/only-prs", 7, 1, ["dave"]),
    ]
    jobs = [
        job("org/a", "pull_request", hours=6.0, rerun_hours=1.5),
        job("org/a", "push", hours=2.0, rerun_hours=0.0),
        job("org/b", "schedule", hours=3.0),
        job("org/only-jobs", "pull_request"),
    ]
    runtime = [
        rt("org/a", "ci", "success", 8.0),
        rt("org/a", "ci", "failure", 2.0),
        rt("org/b", "nightly", "cancelled", 1.0),
        rt("org/b", "nightly", "success", 3.0),
        rt("org/only-runtime", "ci", "success", 50.0),
    ]
    return prs, jobs, runtime


def test_aligned_scope_is_the_intersection_of_all_sources():
    entry = aggregate_month(*sample())
    aligned = entry["scopes"]["aligned"]
    assert aligned["repo_count"] == 2
    # Only org/a and org/b: org/only-* each appear in a single source.
    assert aligned["prs_opened"] == 14
    assert aligned["jobs"] == 30
    assert aligned["hours"] == 14.0


def test_hours_by_repo_and_non_aligned_reconcile_to_total_runtime():
    prs, jobs, runtime = sample()
    entry = aggregate_month(prs, jobs, runtime)
    total = sum(r["hours"] for r in runtime)
    assert set(entry["hours_by_repo"]) == {"org/a", "org/b"}
    assert abs(sum(entry["hours_by_repo"].values()) + entry["hours_non_aligned"] - total) < 1e-6


def test_contributors_are_deduped_across_repos():
    aligned = aggregate_month(*sample())["scopes"]["aligned"]
    # bob authors in both aligned repos but counts once; dave is outside.
    assert aligned["contributors"] == 3


def test_named_scope_covers_only_that_repo():
    prs, jobs, runtime = sample()
    prs.append(pr(ci_metrics.NAMED_REPO_SCOPE, 5, 2, ["erin"]))
    jobs.append(job(ci_metrics.NAMED_REPO_SCOPE, "push", hours=4.0))
    runtime.append(rt(ci_metrics.NAMED_REPO_SCOPE, "trunk", "success", 9.0))
    scope = aggregate_month(prs, jobs, runtime)["scopes"][ci_metrics.NAMED_REPO_SCOPE]
    assert scope["repo_count"] == 1
    assert scope["prs_opened"] == 5
    assert scope["contributors"] == 1
    assert scope["hours"] == 9.0


def test_outcomes_and_trigger_hours_sum_to_totals():
    aligned = aggregate_month(*sample())["scopes"]["aligned"]
    assert abs(sum(aligned["hours_by_outcome"].values()) - aligned["hours"]) < 1e-6
    assert abs(sum(aligned["hours_by_trigger"].values()) - aligned["job_hours"]) < 1e-6
    # Entries round floats to 4 decimals for the downloaded snapshot.
    assert aligned["waste_pct"] == round(3.0 / 14.0, 4)
    assert abs(aligned["rerun_share"] - aligned["rerun_hours"] / aligned["job_hours"]) < 1e-3


def test_unknown_trigger_and_outcome_fall_into_other_buckets():
    jobs = [job("org/a", "repository_dispatch", hours=2.0), job("org/a", "pull_request_target", hours=3.0)]
    runtime = [
        rt("org/a", "ci", "timed_out", 4.0),
        rt("org/a", "ci", "startup_failure", 1.0),
        rt("org/a", "ci", "neutral", 2.0),
        rt("org/a", "ci", "skipped", 0.5),
    ]
    aligned = aggregate_month([pr("org/a", 1, 1, ["x"])], jobs, runtime)["scopes"]["aligned"]
    assert aligned["hours_by_trigger"] == {"other": 2.0, "pull_request": 3.0}
    outcomes = aligned["hours_by_outcome"]
    # timed_out / startup_failure are failures; unrecognized conclusions are
    # "other", not mislabeled as skipped.
    assert outcomes["failure"] == 5.0
    assert outcomes["other"] == 2.0
    assert outcomes["skipped"] == 0.5


def test_top_workflows_plus_other_sum_to_total():
    original = ci_metrics.TOP_WORKFLOWS
    ci_metrics.TOP_WORKFLOWS = 3
    try:
        runtime = [rt("org/a", f"wf{i}", "success", float(i + 1)) for i in range(8)]
        entry = aggregate_month(
            [pr("org/a", 1, 1, ["x"])], [job("org/a", "push")], runtime
        )
        aligned = entry["scopes"]["aligned"]
        assert len(aligned["workflows"]) == 3
        # Largest first.
        assert [w[1] for w in aligned["workflows"]] == ["wf7", "wf6", "wf5"]
        kept = sum(w[2] for w in aligned["workflows"])
        assert abs(kept + aligned["workflows_other_hours"] - aligned["hours"]) < 1e-6
    finally:
        ci_metrics.TOP_WORKFLOWS = original


def test_month_without_runtime_has_null_hours_not_zero():
    prs, jobs, _ = sample()
    entry = aggregate_month(prs, jobs, [])
    aligned = entry["scopes"]["aligned"]
    assert entry["has_runtime"] is False
    # With no runtime rows the runtime source drops out of the intersection,
    # so the PR/job repos still align instead of the scope emptying.
    assert aligned["repo_count"] == 2
    assert aligned["prs_opened"] == 14
    for key in ("hours", "hours_by_outcome", "workflows", "hours_per_pr", "waste_pct"):
        assert aligned[key] is None, key
    assert entry["hours_by_repo"] is None
    assert entry["hours_non_aligned"] is None


def test_pct_change_edge_cases():
    assert pct_change(110, 100) == 0.1
    assert pct_change(None, 100) is None
    assert pct_change(100, None) is None
    assert pct_change(5, 0) is None
    assert pct_change(0, 4) == -1.0


def test_prev_month_wraps_the_year():
    assert prev_month("2026-03") == "2026-02"
    assert prev_month("2026-01") == "2025-12"


def scope_with(workflows):
    total = sum(w[2] for w in workflows)
    return {"workflows": workflows, "hours": total}


def test_movers_rank_by_added_hours_and_flag_new_workflows():
    previous = scope_with([["org/a", "ci", 10.0], ["org/a", "old", 5.0]])
    current = scope_with([["org/a", "ci", 16.0], ["org/a", "old", 4.0], ["org/a", "fresh", 3.0]])
    rows = workflow_movers(current, previous)
    assert [r["workflow"] for r in rows] == ["ci", "fresh", "old"]
    by_name = {r["workflow"]: r for r in rows}
    assert by_name["ci"]["delta_hours"] == 6.0
    assert by_name["ci"]["delta_pct"] == 0.6
    assert by_name["fresh"]["new"] is True
    assert by_name["ci"]["new"] is False
    assert by_name["old"]["delta_hours"] == -1.0


def test_movers_without_a_previous_month_have_no_deltas_or_new_flags():
    current = scope_with([["org/a", "ci", 16.0]])
    for previous in (None, {"workflows": None, "hours": None}):
        rows = workflow_movers(current, previous)
        assert rows[0]["delta_hours"] is None
        assert rows[0]["new"] is False
    assert workflow_movers(None, None) == []
    assert workflow_movers({"workflows": None, "hours": None}, None) == []


def test_movers_respect_the_limit():
    current = scope_with([["org/a", f"wf{i}", float(i + 1)] for i in range(20)])
    assert len(workflow_movers(current, None, limit=5)) == 5


def test_pr_sql_excludes_bot_accounts():
    sql = prs_by_repo_sql("2026-01")
    assert "user.type != 'Bot'" in sql
    for login in BOT_LOGINS:
        assert f"'{login}'" in sql, login
    # Month bound is half-open and UTC, and created_at (a String) is parsed.
    assert "parseDateTimeBestEffortOrNull(created_at) >= '2026-01-01'" in sql
    assert "< '2026-02-01'" in sql


if __name__ == "__main__":
    test_aligned_scope_is_the_intersection_of_all_sources()
    test_hours_by_repo_and_non_aligned_reconcile_to_total_runtime()
    test_contributors_are_deduped_across_repos()
    test_named_scope_covers_only_that_repo()
    test_outcomes_and_trigger_hours_sum_to_totals()
    test_unknown_trigger_and_outcome_fall_into_other_buckets()
    test_top_workflows_plus_other_sum_to_total()
    test_month_without_runtime_has_null_hours_not_zero()
    test_pct_change_edge_cases()
    test_prev_month_wraps_the_year()
    test_movers_rank_by_added_hours_and_flag_new_workflows()
    test_movers_without_a_previous_month_have_no_deltas_or_new_flags()
    test_movers_respect_the_limit()
    test_pr_sql_excludes_bot_accounts()
    print("OK")
