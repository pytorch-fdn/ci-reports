# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Extract the "CI Metrics" slices of the monthly report from ClickHouse:
what is driving CI cost/runtime up -- more work (PRs, contributors, jobs),
more CI per unit of work, outcomes and reruns (failed/cancelled/rerun jobs), or a shift in
mix (workflow, repo, trigger).

Three slices per month, each grouped by repo so ci_metrics.py can restrict
every figure to the same repo set (a repo present in all three sources) --
`runner_cost`, `workflow_job` and `pull_request` cover very different repo
sets, and a ratio like hours-per-PR is meaningless across mismatched ones:

- `prs_by_repo.json`: PRs opened/merged and the distinct non-bot author
  logins, from `default.pull_request`.
- `jobs_by_repo_event.json`: jobs, runs and reruns by trigger event, from
  `default.workflow_job`.
- `runtime_by_repo_workflow_conclusion.json`: runtime hours by workflow and
  job outcome, from `misc.runner_cost`.

Reuses clickhouse_extract's `ch_query()` and UTC half-open month bound (see
its docstring for why). `FINAL` is applied to every query: all three tables
are ReplacingMergeTree variants, and un-FINALed reads return different row
counts between runs.

Runtime here is `runner_cost.duration` (hours), summed across every
`owning_account` -- unlike cost, duration is meaningful for AMD (whose `cost`
is always 0 upstream) and for member-self-hosted runner types, so hours are
the one unit that is comparable across all three sources. It will not equal
the Runtime tab's billed instance-hours, which come from the FOCUS export for
the LF account only."""

import json
import os
import sys
from pathlib import Path

from ci_reports.clickhouse_extract import ch_query, date_range_clause

# Accounts that open PRs automatically. `user.type = 'Bot'` catches GitHub
# App accounts; these catch the ones that appear as ordinary users.
BOT_LOGINS = (
    "pytorchbot",
    "pytorchmergebot",
    "pytorchupdatebot",
    "facebook-github-bot",
    "dependabot[bot]",
    "dependabot",
)

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
OUT_SUBDIR = Path("clickhouse") / "ci_metrics"


def _sql_list(values):
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def prs_by_repo_sql(year_month):
    # pull_request.created_at is a String (ISO-8601), unlike the DateTime
    # columns elsewhere, so it's parsed before the month bound is applied.
    clause = date_range_clause("parseDateTimeBestEffortOrNull(created_at)", year_month)
    return f"""
        SELECT base.repo.full_name AS repo,
               count() AS opened,
               countIf(merged) AS merged,
               groupUniqArrayIf(
                   user.login,
                   user.type != 'Bot' AND user.login NOT IN ({_sql_list(BOT_LOGINS)})
               ) AS authors
        FROM default.pull_request FINAL
        WHERE {clause}
        GROUP BY repo
        ORDER BY repo
    """


def jobs_by_repo_event_sql(year_month):
    clause = date_range_clause("created_at", year_month)
    # Job-hours only count jobs with a sane started_at/completed_at pair: an
    # unfinished or never-started job carries the epoch (or completed < started)
    # and would otherwise contribute a huge negative/positive duration.
    valid = "started_at > '2000-01-01' AND completed_at > started_at"
    return f"""
        SELECT repository_full_name AS repo,
               workflow_event AS event,
               count() AS jobs,
               uniqExact(run_id) AS runs,
               countIf(run_attempt > 1) AS rerun_jobs,
               sumIf(dateDiff('second', started_at, completed_at), {valid}) / 3600 AS hours,
               sumIf(dateDiff('second', started_at, completed_at),
                     {valid} AND run_attempt > 1) / 3600 AS rerun_hours
        FROM default.workflow_job FINAL
        WHERE {clause}
        GROUP BY repo, event
        ORDER BY repo, event
    """


def runtime_by_repo_workflow_conclusion_sql(year_month):
    clause = date_range_clause("rc.date", year_month)
    return f"""
        SELECT group_repo AS repo, workflow_name, conclusion,
               sum(duration) AS hours, count() AS jobs
        FROM misc.runner_cost AS rc FINAL
        WHERE {clause}
        GROUP BY repo, workflow_name, conclusion
        ORDER BY repo, workflow_name, conclusion
    """


SLICES = {
    "prs_by_repo.json": prs_by_repo_sql,
    "jobs_by_repo_event.json": jobs_by_repo_event_sql,
    "runtime_by_repo_workflow_conclusion.json": runtime_by_repo_workflow_conclusion_sql,
}


def extract_month(host, user, password, year_month):
    out_dir = DATA_ROOT / year_month / OUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, build_sql in SLICES.items():
        rows = ch_query(host, user, password, build_sql(year_month))
        with open(out_dir / filename, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"{year_month}: wrote {len(rows)} rows -> data/{year_month}/{OUT_SUBDIR}/{filename}")


def main():
    if len(sys.argv) < 2:
        print("usage: python -m ci_reports.ci_metrics_extract <YYYY-MM> [<YYYY-MM> ...]", file=sys.stderr)
        sys.exit(1)

    host = os.environ["CH_HOST"]
    user = os.environ["CH_USER"]
    password = os.environ["CH_PASS"]

    for year_month in sys.argv[1:]:
        extract_month(host, user, password, year_month)


if __name__ == "__main__":
    main()
