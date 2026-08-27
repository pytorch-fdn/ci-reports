# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Extract the Meta/AMD/Intel-jobs slices of the monthly report from
ClickHouse's `misc.runner_cost` -- the table backing the manual HUD exports
the sheet currently pastes in by hand (see the sheet's own Export tab for
how each slice maps to a filter here: owning_account for Meta/AMD, a
job-name regex for the Intel-jobs breakout).

Queries ClickHouse Cloud's HTTPS interface directly; there is no native
client installed locally. `FINAL` is applied to every query: `runner_cost`
is a SharedReplacingMergeTree, and identical queries were observed to return
different row counts between runs (background merges) even though summed
totals didn't change -- FINAL keeps re-runs of this extractor stable.

Meta and AMD do not double-count: AMD rows pass the "exclude Owner LF"
filter too, but always carry cost = 0 in ClickHouse (see amd_cost.py for how
AMD's actual cost is derived from duration instead).

A previously published month's Meta/AMD numbers can differ from a fresh
recompute here by more than expected backfill drift alone: HUD's own
dashboard (torchci/pages/cost_analysis.tsx) parses its date-range params
with a plain `dayjs(dateString)`, which is timezone-dependent, then compares
against `misc.runner_cost.date` (day granularity) with a strict `>`/`<`
bound -- so a manual export run from a browser west of UTC can silently drop
the first day of the range entirely. By default this extractor instead
defines the month the same way AWS's own Cost and Usage Report / FOCUS data
does: a UTC, half-open interval (`>= <month>-01 AND < <next-month>-01`),
with no viewer-timezone dependency at all -- see "Meta/AMD figures are
intentionally recomputed" in specs/monthly-ci-report.md for how the HUD bug
was verified.

Set `EXPORT_TZ` (an IANA zone name, e.g. `America/Toronto`) to instead
reproduce HUD's own dashboard math exactly, for comparing a fresh pull
against a specific past manual export: the query then uses `date >
<first-day-local-midnight-in-that-zone, as UTC>` and `date <
<last-day-local-midnight-in-that-zone, as UTC>`, matching
`cost_analysis.tsx`'s `dayjs(startDate).utc()` / `dayjs(endDate).utc()`
construction bit-for-bit. This is a verification aid, not the intended
long-term default -- it reproduces a timezone-dependent artifact in HUD's
own frontend, not a property of the underlying data.

`fetch_by_runner_type()` and `fetch_intel_jobs()` filter to `cost > 0` for
real-cost slices (meta, linux_foundation) to match HUD's own
cost_job_per_runner_type query, which excludes zero-cost and negative-cost
(e.g. skipped-run) rows -- confirmed against the sheet's raw runner-type
export this filter reproduces the exact same set of runner_types, not just
a similar total.
"""

import base64
import calendar
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

INTEL_JOB_REGEX = (
    r"(linux-jammy-cpu-py3.*-gcc11|xpu|nightly-dynamo-benchmarks-|"
    r"periodic-dynamo-benchmarks-cpu-|inductor-cpu-|opbenchmark-|inductor-cpu-)"
)


def date_range_clause(column, year_month, export_tz=None):
    """Builds the `WHERE`-clause date bound for one month. Defaults to a
    UTC, half-open interval -- `>= <month>-01 AND < <next-month>-01` -- the
    same convention AWS's own CUR/FOCUS billing data uses for a calendar
    month (`BillingPeriodStart`/`BillingPeriodEnd`), with no viewer-timezone
    dependency.

    When `export_tz` (an IANA zone name) is given, instead reproduces HUD's
    own dashboard's exact query construction for someone viewing it from
    that timezone: local midnight of the first and last day of the month,
    converted to UTC, compared with a strict `>`/`<` bound -- see this
    module's docstring for why that can drop the month's first day."""
    year, month = (int(p) for p in year_month.split("-"))

    if export_tz is None:
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return (
            f"{column} >= '{year_month}-01' "
            f"AND {column} < '{next_year:04d}-{next_month:02d}-01'"
        )

    last_day = calendar.monthrange(year, month)[1]
    tz = ZoneInfo(export_tz)
    start_utc = datetime(year, month, 1, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    end_utc = datetime(year, month, last_day, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    fmt = "%Y-%m-%d %H:%M:%S"
    return (
        f"{column} > toDateTime('{start_utc.strftime(fmt)}') "
        f"AND {column} < toDateTime('{end_utc.strftime(fmt)}')"
    )


def ch_query(host, user, password, sql):
    """Runs one query against ClickHouse's HTTPS interface and returns the
    parsed `FORMAT JSON` response's `data` rows. Uses a GET with the query
    in the querystring -- matching `curl -G --data-urlencode "query=..."` --
    since a plain POST body makes ClickHouse parse the literal `query=`
    prefix as SQL."""
    url = f"{host.rstrip('/')}/?" + urllib.parse.urlencode({"query": f"{sql} FORMAT JSON"})
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())["data"]


def fetch_by_runner_type(host, user, password, year_month, owning_account, export_tz=None):
    """The ClickHouse equivalent of "HUD Monthly Cost/Duration Per Runner
    Type", scoped to one owning_account (meta / amd / linux_foundation).

    Matches HUD's own query (torchci/clickhouse_queries/cost_job_per_runner_type)
    by adding `cost > 0`, which excludes both zero-cost rows and the small
    number of negative-cost rows (e.g. skipped runs) HUD's dashboard also
    excludes. Skipped for AMD: `cost` is always 0 for AMD-owned rows (its
    real cost is derived from `duration` in amd_cost.py), so this filter
    would incorrectly drop every AMD row rather than a genuine subset.

    `export_tz`: see date_range_clause().

    Groups by `os` alongside `runner_type` so callers can bucket by either
    dimension (architecture via runner_type, or OS directly) from the same
    extract -- a runner_type is expected to carry one os value in practice,
    so this doesn't change per-architecture totals, only adds a column."""
    date_clause = date_range_clause("rc.date", year_month, export_tz)
    cost_filter = "AND rc.cost > 0" if owning_account != "amd" else ""
    sql = f"""
        SELECT runner_type, os, sum(rc.cost) AS cost, sum(rc.duration) AS duration, count() AS rows
        FROM misc.runner_cost AS rc FINAL
        WHERE {date_clause} AND rc.owning_account = '{owning_account}'
          {cost_filter}
        GROUP BY runner_type, os
        ORDER BY runner_type
    """
    return ch_query(host, user, password, sql)


def fetch_intel_jobs(host, user, password, year_month, exclude_lf, export_tz=None):
    """The ClickHouse equivalent of "HUD Monthly Cost Per Job Name -- Intel
    Jobs Only", either excluding or restricted to Owner LF.

    `export_tz`: see date_range_clause()."""
    date_clause = date_range_clause("rc.date", year_month, export_tz)
    account_filter = (
        "owning_account != 'linux_foundation'"
        if exclude_lf
        else "owning_account = 'linux_foundation'"
    )
    sql = f"""
        SELECT job_name, sum(rc.cost) AS cost, sum(rc.duration) AS duration, count() AS rows
        FROM misc.runner_cost AS rc FINAL
        WHERE {date_clause} AND rc.{account_filter}
          AND rc.cost > 0
          AND match(job_name, '{INTEL_JOB_REGEX}')
        GROUP BY job_name
        ORDER BY job_name
    """
    return ch_query(host, user, password, sql)


def main():
    if len(sys.argv) != 2:
        print("usage: python -m ci_reports.clickhouse_extract <YYYY-MM>", file=sys.stderr)
        sys.exit(1)
    year_month = sys.argv[1]

    host = os.environ["CH_HOST"]
    user = os.environ["CH_USER"]
    password = os.environ["CH_PASS"]
    export_tz = os.environ.get("EXPORT_TZ") or None

    out_dir = Path(__file__).resolve().parent.parent / "data" / year_month / "clickhouse"
    out_dir.mkdir(parents=True, exist_ok=True)

    slices = {
        "meta_by_runner_type.json": lambda: fetch_by_runner_type(
            host, user, password, year_month, "meta", export_tz
        ),
        "amd_by_runner_type.json": lambda: fetch_by_runner_type(
            host, user, password, year_month, "amd", export_tz
        ),
        "lf_hud_by_runner_type.json": lambda: fetch_by_runner_type(
            host, user, password, year_month, "linux_foundation", export_tz
        ),
        "intel_jobs_excluding_lf.json": lambda: fetch_intel_jobs(
            host, user, password, year_month, exclude_lf=True, export_tz=export_tz
        ),
        "intel_jobs_lf.json": lambda: fetch_intel_jobs(
            host, user, password, year_month, exclude_lf=False, export_tz=export_tz
        ),
    }

    for filename, fetch in slices.items():
        rows = fetch()
        with open(out_dir / filename, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"{year_month}: wrote {len(rows)} rows -> data/{year_month}/clickhouse/{filename}")


if __name__ == "__main__":
    main()
