# Spec: Automated PyTorch Foundation CI Cost Report

Status: **implemented — v1 pipeline (finalize job, static site, Trend
report) is built and running.** This file still records the design
decisions and their rationale, not just a plan; sections describe what was
built, updated in place as the system evolves rather than left to drift
from the code.

This is the single design/spec document for this project: what the system
does, why, and the decisions behind it. Rationale is folded in here rather
than kept in a separate design doc — the project is small enough that two
parallel documents would only drift apart. Verification narratives are
generalized and folded into the relevant section below (with real figures
stripped, per "Public-repo constraints"); open engineering asks and
upstream bugs stay in `TODO.md`. This file is committed and public — see
"Public-repo constraints" below before adding anything to it.

## Goal

Replace the hand-maintained "PyTorch Foundation" Google Sheets monthly CI
cost report with an automated pipeline, and give the community/stakeholders
a way to review it without a human manually running six exports and pasting
them into sheet tabs every month.

A second, previously separate spreadsheet — **"PyTorch Foundation - Trend"**
— also needs to be replicated by this tool; see "Trend report" below for
its scope and decided behavior.

## v1 scope: finalized past months only

The primary use case is full-month spend, not up-to-the-minute tracking.
**v1 covers only finalized, closed calendar months** — no month-to-date/live
data path. This simplifies both the pipeline (finalize-and-archive only, no
live query serving) and hosting (a static site over pre-computed snapshots,
no request-time compute against ClickHouse or FOCUS). Live MTD is a
possible **future addition**, not part of this spec's initial build — see
"Future addition: real-time MTD" under Hosting.

**Build order**: get the finalize job + static site generation working and
proven first — plain generated static output, not yet served through
Cloudflare. Provisioning R2 and the Cloudflare Worker (and wiring
Access/Auth0 in front of it) is a deliberately separate, later effort once
the data pipeline and rendering are trusted. Don't block on hosting
decisions to start building the pipeline.

## Data trust and caveats

Not all data in the report carries the same trust level, and the site must
say so, not just imply it structurally. Every report page (monthly and
Trend) must carry a visible caveat along these lines:

> Data from the PyTorch Foundation AWS account is billing-grade and
> trusted. Estimates from PyTorch HUD (Meta) and AMD-provided pricing are
> **estimates**, not confirmed real costs. This report does not include
> self-hosted runner costs provided directly by member companies.

Concretely: LF/FOCUS figures are trusted (real AWS billing data). Meta
figures (via ClickHouse/HUD) and AMD figures (derived from AMD-supplied
multipliers) are both estimates — we don't have ground truth for either.
This drives the "Estimated Cost" naming in the schema below, and the
caveat must appear on every page, not just a README/footnote once.

## Data sources

| Section | Source | Fidelity |
|---|---|---|
| LF Financials + LF Runtime | FOCUS Parquet export (S3) via `ci_reports/focus_extract.py` | Exact match to the sheet, verified to the cent for a full month |
| Meta cost/duration by runner type | ClickHouse `misc.runner_cost`, `owning_account='meta'` | Recomputed, see below — will not byte-match old published sheets |
| AMD cost/duration by runner type | Same table, `owning_account='amd'`; cost is always 0 upstream, derived from duration × GPU count × a per-model multiplier | Recomputed, methodology-verified, not byte-matched |
| Intel-jobs breakout (LF / non-LF) | Same table, filtered by job-name regex | Recomputed |

### Meta/AMD figures are intentionally recomputed, not imported from the sheet

This is a deliberate design decision, not a caveat to work around later.
Two independent, verified causes mean a fresh recompute will not byte-match
an old published figure:

1. **HUD's own dashboard drops the first day of the month for some
   viewers.** `cost_analysis.tsx` builds its date-range query with a plain,
   viewer-timezone-dependent `dayjs(dateString)` call, which silently drops
   the first day of the range for any viewer west of UTC — confirmed by
   comparing a fresh full-month recompute (minus day 1's total) against a
   previously published month's figure and finding an exact, to-the-cent
   match (see `TODO.md`'s "Upstream bugs" section for the upstream fix to
   report). Every previously published Meta/AMD figure inherits this to the
   extent it was exported by a viewer in such a timezone.
2. **The published sheet is a point-in-time export, not a stable monthly
   total.** Manual exports were taken while the final day(s) of the month
   were still landing in ClickHouse (ingestion settles to a stable state
   roughly a day after the fact). Some runner types' published values fall
   strictly between the recomputed totals for two adjacent day-cutoffs,
   which no date-boundary choice can reproduce — the sheet simply predates
   full ingestion for that month.

Recomputing Meta/AMD from ClickHouse using a UTC half-open month interval
(the same convention AWS's own CUR/FOCUS billing data uses), run well after
the month closes so ingestion has settled, addresses both causes at once —
this is the fix, not a divergence to reconcile. Consequently:

- A freshly recomputed month **will not equal** the historical sheet's
  published figure for the same month, and that's expected and correct.
- LF stays exactly reproducible because FOCUS was never subject to this bug.
- Pre-2026-07 history should be backfilled by **recomputing from ClickHouse**
  for Meta/AMD (the table holds data back to 2024-06-01, with ingestion
  settling to a stable state after about a day), not by importing the old
  sheet's numbers, to avoid baking a silent undercount into the new archive.
  LF's own pre-2026-07 history can instead be imported from the sheet, since
  FOCUS already verifies exact agreement with it for the months it covers.

## Schema: FOCUS-aligned, not FOCUS-conformant

The archive reuses FOCUS 1.0 column names and semantics where they map
naturally (`ChargePeriodStart`/`ChargePeriodEnd`, `ChargeDescription`,
`ConsumedQuantity`, `ListCost`/`BilledCost`), plus:

- A `Provider`/vendor column (LF / Meta / AMD).
- A **provenance** column: `focus_export`, `clickhouse_recompute`, or
  `legacy_sheet_import`, so every row's origin is traceable.
- An **`EstimatedCost`** column, used in place of `BilledCost`/`ListCost`
  for any row whose provenance is `clickhouse_recompute` (Meta and AMD) —
  named to make clear it is not a confirmed real cost, consistent with the
  site-wide caveat above. Only LF/FOCUS rows populate a real `BilledCost`.
- A `CostMethodology` note on AMD rows specifically, since AMD's
  `EstimatedCost` is derived (GPU-hours × a per-model multiplier) rather
  than read from any billing source at all — Meta's `EstimatedCost` is at
  least a real queried duration/cost from ClickHouse, just not verified
  against Meta's own billing.

This is called "FOCUS-aligned" rather than "FOCUS-conformant" on purpose:
several FOCUS mandatory columns won't be populated for Meta/AMD (e.g. no
natural `BillingAccountId` for Meta), and `BilledCost`/`ListCost` themselves
are intentionally left blank for `EstimatedCost` rows rather than repurposed
— that should be documented explicitly per column rather than faked to
force conformance.

### Column-by-column mapping (decided)

`EstimatedCost`, `CostMethodology`, and `Provenance` are not real FOCUS
columns — FOCUS reserves an `x_`-prefix convention for implementer
extensions, so they're written as `x_EstimatedCost`, `x_CostMethodology`,
`x_Provenance`. Everything else uses a standard FOCUS column name.

| FOCUS column | LF (FOCUS export) | Meta (recompute) | AMD (recompute) |
|---|---|---|---|
| `ProviderName` | `AWS` | `Meta` | `AMD` |
| `BillingAccountId` / `SubAccountId` | real AWS account (from FOCUS, never hardcoded — see Public-repo constraints) | left blank — no billing-account concept for a non-billed estimate | left blank |
| `ChargePeriodStart` / `ChargePeriodEnd` | from FOCUS | UTC month/day boundary used for the ClickHouse query | same |
| `ChargeDescription` | FOCUS charge description, as-is | `runner_type` (and `job_name` for the Intel-jobs breakout) | `runner_type` |
| `ServiceCategory` | FOCUS category (e.g. `Compute`) | `Compute` | `Compute` |
| `ResourceId` / `ResourceName` | FOCUS resource id | `runner_type` — no stable per-resource id exists upstream | `runner_type` |
| `ConsumedQuantity` / `ConsumedUnit` | FOCUS quantity/unit, as-is | duration, normalized to hours | GPU-hours (duration × GPU count) |
| `BilledCost` / `ListCost` / `EffectiveCost` / `ContractedCost` | populated from FOCUS | left blank — no billed/list cost exists | left blank |
| `x_EstimatedCost` | left blank (real `BilledCost` is authoritative) | recomputed dollar total from ClickHouse | GPU-hours × the resolved per-model multiplier |
| `x_CostMethodology` | left blank | `"clickhouse_recompute"` | `"gpu_hours_x_multiplier"` |
| `x_Provenance` | `focus_export` | `clickhouse_recompute` | `clickhouse_recompute` |

`ConsumedUnit` is normalized to hours across all three sources specifically
so Trend's cross-month, cross-vendor aggregation doesn't silently mix units
(e.g. seconds vs. hours) when summing.

## Architecture

- **Finalize job**: once a calendar month is closed, computes that month's
  full report (FOCUS for LF, ClickHouse recompute for Meta/AMD/Intel-jobs)
  and writes a self-contained, immutable snapshot. This is the only data
  path in v1 — there is no live/current-month query path (see "v1 scope"
  above and "Future addition: real-time MTD" under Hosting).
- **Config baked in at finalize time**: the AMD per-runner GPU cost
  multiplier and the runner→vendor/architecture mapping are resolved and
  written directly into that month's snapshot rows (GPU-hours, the
  multiplier actually used, and the resulting cost; the resolved
  architecture/vendor per row) rather than joined live at render time. If
  AMD later revises a multiplier, or the mapping table gets a correction, a
  previously finalized month must never silently change — closed snapshots
  are immutable. This also means new/unmapped runner types get caught and
  fixed via a normal PR to the in-repo mapping file *before* that month's
  finalize job runs, not discovered after the fact. `render_coverage_gaps()`
  in `ci_reports/render.py` already fails loudly on unmapped instance
  families instead of defaulting silently — that behavior must survive this
  move unchanged.

## Storage and sensitivity split

| Data | Location | Why |
|---|---|---|
| Runner → vendor/architecture mapping | In-repo, committed (e.g. `ci_reports/mappings/`) — **not** `data/`, which is gitignored | Not secret; reviewable via normal PRs; see "Mapping coverage checks" below |
| AMD per-runner GPU cost multiplier | In-repo, committed, alongside the vendor/architecture mapping | Revisited: originally scoped as confidential AMD-supplied data requiring restricted R2 storage; simplified to in-repo per explicit decision, since the resulting figures are already labeled `EstimatedCost` (not a real cost) site-wide — worth revisiting again if AMD ever objects to the raw multiplier itself being public, as distinct from the estimated dollar figures it produces |
| Finalized monthly snapshots | R2 (once provisioned — see "Build order"), snapshot prefix, partitioned by `billing_period` and snapshot date | Contains PyTorch Foundation financial data; partitioned so re-finalizing never overwrites a prior point-in-time snapshot in place |

R2 is the working choice for object storage: private-by-default posture, no
egress fees, and an S3-compatible API for the finalize job's own write path
(a direct API token, unrelated to any SSO concern below). Per "Build
order" above, this is deliberately deferred until the pipeline is proven
with plain static output first.

## Mapping coverage checks

The runner→vendor/architecture mapping is hand-maintained and may have
gaps or mistakes. `render_coverage_gaps()` in `ci_reports/render.py`
already fails loudly (rather than defaulting silently) on any instance
family or runner type missing from the mapping — this behavior must remain
a hard failure, not a warning, as the pipeline evolves. In addition, the
finalize job should run this same check as a **CI gate**, not only at
render time: given a month's raw extracted data, assert every distinct
runner_type/instance family it contains is present in the mapping before
finalizing, so a newly-introduced unmapped runner type is caught in review
(via the normal in-repo PR to the mapping file) rather than silently
producing an incomplete or miscategorized snapshot.

The source spreadsheet itself is not always internally consistent — the
same runner_type's implied GPU model can differ between its "AMD
Financials" tab and its "Instance to Vendor Translations" tab. When
hand-maintaining `ci_reports/mappings/*.json` against the sheet, treat such
disagreements as expected slop in a hand-maintained workbook, not a bug in
this pipeline to chase down. Resolve them by tab authority for the purpose
at hand (the Financials-derived GPU multiplier for cost, the Translations
tab for architecture/vendor display) rather than trying to force the two
tabs to agree.

## Hosting

**Cloudflare Workers + R2** is the working direction, and fits the v1
finalized-only scope well: the Worker serves pre-computed snapshot files
(JSON/HTML) straight out of R2 via a native binding — no request-time
compute, free egress, and the finalize job (which needs Python/duckdb for
FOCUS Parquet, not runnable inside a V8 isolate) stays an external job
(e.g. GitHub Actions, tying into the OIDC-role follow-up in `TODO.md`) that
writes finished snapshots into R2 for the Worker to read.

**Finalize trigger**: a monthly-scheduled **GitHub Actions workflow**
(`cron`), not a Cloudflare Worker/Cron Trigger — Cron Triggers only invoke
Workers, which can't run the Python/duckdb finalize job either, so that
would just add indirection without solving the runtime problem. The
workflow authenticates to AWS via an OIDC role to read ClickHouse/FOCUS
(the same OIDC-role need already flagged in `TODO.md`'s follow-ups), runs
the finalize job, then writes the resulting snapshot into R2 via a separate
R2 API token over its S3-compatible API — unrelated to the AWS OIDC auth,
and unrelated to any SSO concern (same non-SSO write path already noted
under Access control below).

Cloudflare Access can gate the Worker/Pages route behind Auth0 (see Access
control below), matching the `lf-fyi-shortener` pattern already in use
elsewhere.

### Future addition: real-time MTD

Deferred, not part of v1. If added later, the two data sources need
different mechanisms rather than one uniform live path:

- **Meta/AMD MTD** (ClickHouse) can be queried live from inside the Worker
  itself — a plain HTTPS+SQL call, same shape as `clickhouse_extract.py`,
  ported to JS `fetch()`, with a short cache (Workers Cache API or KV) to
  avoid hitting ClickHouse on every page view.
- **LF MTD** (FOCUS Parquet in S3) is a poor fit for in-Worker computation —
  aggregating Parquet needs duckdb-style processing, which exceeds what a
  V8 isolate should do at request time. Better handled by running the
  existing Python extraction on a frequent schedule (e.g. hourly) and
  writing a small "MTD-so-far" cache object into R2 for the Worker to read,
  rather than true request-time computation.

## Access control

Two separate controls, not to be conflated:

1. **Bucket privacy**: the R2 bucket (once provisioned, per "Build order")
   must not allow public access. Risk to guard against is *accidental*
   exposure — e.g. leaving R2's `r2.dev` dev URL enabled, or a custom
   domain with no Access policy in front of it — not a targeted attack.
2. **Website/report access**: gated by LFID SSO, via **Auth0**, following
   the same pattern already in use for `linuxfoundation/lf-fyi-shortener`.
   This is unrelated to the finalize job's own write path, which
   authenticates to R2's S3-compatible API via a token and never goes
   through SSO.

**Audience scope — resolved**: this is not LF-staff-only. The intended
audience is the broader PyTorch community/Foundation stakeholders (board,
TAC, member-company representatives), consistent with the original "give
the community a snapshot to review" goal.

**Access model — resolved: binary, no tiers.** Auth0/LFID handles
*authentication* only (who is this?); it should not be relied on for
*authorization* (are they allowed in?) — the shared LFID Auth0 tenant isn't
expected to expose group/org-membership claims to this app. Authorization
is enforced one layer up, as **Cloudflare Access policy rules**:

- LF staff get access for free via an email-domain rule (e.g. `@linuxfoundation.org`) —
  no per-person maintenance.
- Everyone else (board/TAC/member-company representatives) is covered by
  an explicit allowlist of individual emails.

The explicit allowlist must **not** live in this public repo — it's a list
of real people's identities/affiliations, not app config, and doesn't
belong in `ci-reports` even though it isn't cost data. It should live in
the Cloudflare Access policy configuration itself (dashboard or a private
infra-as-code repo), maintained separately from this codebase.

## Trend report

Scoped (via a direct read of the actual "PyTorch Foundation - Trend"
sheet) and now also has decided behavior:

- It is a pure rendering view over the same monthly report data, not a new
  data source: a pivot of Cost and Runtime by architecture/vendor bucket,
  one column per month. Buckets match the monthly report's existing
  architecture breakdown (ARM64/Graviton, x86 by CPU vendor, CUDA, ROCM,
  TPU, XPU, MacOS, Windows) plus a `Total` row/column. It also contains a
  tab marked "(Deprecated)" using an older vendor grouping (by hardware
  vendor rather than architecture), which appears superseded and does not
  need to be replicated.
- Implemented as `ci_reports/site/index.html`, a client-side chart reading
  `data/trend_snapshot.json` (built by `ci_reports/snapshot.py`). Each
  snapshot entry carries `combined_arch` and `combined_runtime_arch` (cost
  and duration per architecture bucket, from
  `render.compute_month_totals()`), so this did end up extending the
  snapshot schema after all, superseding the "does not require extending
  the snapshot schema" note above — the transpose/aggregation still happens
  entirely client-side over pre-computed per-month totals, not a new
  extraction pipeline.
- `combined_runtime_arch` covers instance-hours only, matching the sheet's
  own Architecture pivot: LF's non-"Instance Hour" FOCUS charge lines (S3,
  data transfer, EBS, ...) carry GB/requests/etc, not instance-hours, so
  they're dropped from this bucket rather than lumped into an "Other"
  category that would dwarf every real architecture segment (lumping them
  in was tried and rejected during implementation once the rendered chart
  showed why). This means the stack does not sum to `runtime_total`, which still covers
  every usage type; the trend page shows a caveat note under the Runtime
  view explaining the scope difference.
- Both metrics (Financials and Runtime) render as a stacked bar chart per
  month, broken down by architecture bucket, with a dollar/count-formatted
  Y-axis (rounded to nice gridline steps), a color-coded legend, and a hover
  tooltip on each bar segment. Segments within a bar sit on a 2px surface gap
  so adjacent bands stay visually separable. Clicking a bar opens that
  month's detailed report.
- **Stack order is fixed** (one canonical bottom-to-top order, shared by
  every bar in the current view) — supersedes an earlier decision to sort
  each bar independently by that month's descending value (largest segment
  at the bottom). The 7 real architecture buckets are ranked by descending
  value in the *latest month in view* (biggest at the bottom), recomputed
  each render so the order tracks whatever is actually biggest right now;
  the 4 "not attributable to an architecture" buckets always sit on top of
  those 7 regardless of their value, in a fixed relative order among
  themselves. Still one order for every bar, not a per-bar re-sort, which
  lets a given architecture's band line up across months instead of jumping
  position, and lets the categorical palette below be validated against one
  known adjacency list instead of every possible pairing (an independent
  per-bar sort makes any two colors potentially adjacent). The bar's
  x-position is also a band scale, not a point scale — an earlier
  point-scale implementation centered the first bar directly on the Y axis,
  which visually covered the axis's own labels.
- **Colors are the `dataviz` skill's validated 8-hue categorical palette**,
  assigned to the 7 real architecture buckets that ever appear in
  `combined_arch`/`combined_runtime_arch` (x86_64 Intel/AMD, ARM64 Graviton,
  CUDA, ROCM, Windows, macOS); the remaining "not attributable to an
  architecture" buckets (`x86_64 (Unknown)`, `ARM64 (Unknown)`, `Other`,
  `N/A`) share one neutral gray family distinguished only by lightness, so
  they read as "not a real category." An unrecognized architecture key
  should fold into `Other` rather than get a generated hue — the palette's
  colorblind-safety validation only holds for this fixed set.
- **The legend is split into labeled sections — CPU, GPU, OS, Other** —
  independently of the bars' fixed stack order: x86_64/ARM64 buckets (CPU),
  then CUDA/ROCM (GPU compute platform), then Windows/macOS (the two
  OS-exception buckets — the default OS, Linux, isn't a separate bucket),
  then Other/N/A. Each section renders as its own row with a small caps
  group label and a divider above it, rather than a single flat re-ordered
  list, so the categories read as visually distinct groups, not just a sort
  order. A group with no members present in the current view (e.g. no macOS
  usage that month) is omitted entirely rather than shown empty.
- **Default view is a 12-month sliding window**, not all-time — supersedes
  an earlier decision to default to all-time, revised after the rendered
  chart got hard to read once enough months accumulated. A "Show all
  history" toggle switches to the full archive; there is no separate
  query-string-driven range picker for screenshots beyond this toggle.

## Open questions

None blocking right now. Everything previously listed here (AMD/Meta FOCUS
column mapping, Auth0/LFID audience scope and permission model) has been
resolved above. Revisit this section as implementation surfaces new
questions.

## Public-repo constraints

This file is committed and this repo is public. Per `AGENTS.md`: no real
cost figures, usage quantities, account IDs, bucket names, hostnames, or
credentials in this document, ever — including in future edits. Use
structural descriptions instead (as done above for the AMD multiplier
formula and the recomputation causes above). If a verification claim needs
a concrete number to be checkable, use a synthetic fixture (see
`tests/test_focus_extract.py`) rather than a real one — don't reach for an
uncommitted scratch file as a place to park real figures instead.
