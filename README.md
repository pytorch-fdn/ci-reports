<!--
SPDX-FileCopyrightText: 2026 The Linux Foundation

SPDX-License-Identifier: Apache-2.0
-->

# ci-reports

Replaces the Ternary feed for the LF-AWS portion of the PyTorch Foundation
Monthly CI Reports (Financials and Runtime workbooks) with data pulled
directly from AWS. See the repo's issue tracker for open follow-ups.

## What this produces, and what it does not

These numbers are the **pre-credit billed cost of CI usage** — what the
resources actually cost, before any account-level credit is applied. This
was a deliberate decision, not an oversight:

- On this AWS account (a linked member account under LF Strategic), for the
  one month checked (2026-07), FOCUS's `ListCost`,
  `BilledCost`, `EffectiveCost`, and `ContractedCost` are identical for
  every `Usage` line item — consistent with an on-demand-only account with
  no negotiated/contracted discount, though the export tooling could in
  principle be duplicating one column into the others rather than this
  being a general guarantee. Don't assume it holds for other accounts or
  months without checking.
- A separate `ChargeCategory = 'Credit'` line (AWS Open Source Promotional
  Credits) sums to almost exactly the negative of the Usage total, so the
  account's *net* invoiced amount is ~$0 — this is why every Cost Explorer
  cost metric (`UnblendedCost`, `BlendedCost`, `AmortizedCost`, `Net*`)
  returns ~$0 too. The credit is what LF pays out of pocket (nothing); the
  Usage total is what the CI infrastructure actually cost.
- `usage_hours x on-demand list price` reproduces the Ternary-era sheet
  numbers exactly (to the cent, verified during development), so Ternary
  itself was reporting this same pre-credit figure, not the post-credit net.
- Reproducing the same metric keeps the new numbers continuous with every
  previously published month rather than creating a discontinuity.

## How it works

### `ci_reports/focus_extract.py`

An AWS FOCUS 1.0 (FinOps Open Cost and Usage Spec) Parquet export already
lands in an S3 bucket the org has read access to (bucket name kept out of
this public repo — see `FOCUS_BUCKET` in Running below), partitioned by
`billing_period=YYYY-MM`. FOCUS's `ListCost` column is AWS's
own precomputed usage × public-on-demand-list-price per line item, so reading
it directly needs no region-prefix table, no OS/license-model mapping, and no
per-service-code guessing, and it prices every usage type AWS billed (the
long tail: EBS, data transfer, CloudWatch, Lambda, ... included).

1. `aws s3 cp --recursive` the month's Parquet files locally (idempotent).
2. Query with `duckdb` (`read_parquet()`), grouped by `ChargeDescription`:
   - Financials: `SUM(ListCost) WHERE ChargeCategory = 'Usage'`
   - Runtime: `SUM(ConsumedQuantity) WHERE ChargeCategory = 'Usage'` — the
     **same underlying query** as Financials (all Usage lines, any
     `ResourceType`), confirmed against the real "LF Raw Ternary Import" tab,
     which lists Lambda/API-Gateway/NAT/EBS quantities alongside EC2
     instance-hours under `Consumed Quantity` too. The sheet's own
     Runner/Instance-Family lookup table — not this extractor — is what
     filters non-instance rows out of the Architecture pivot. The two CSVs
     end up with different row counts because each output drops rows whose
     own measure is zero (e.g. free-tier lines have `ListCost = 0` but
     nonzero `ConsumedQuantity`), not because the row population differs.
   - `ChargeCategory = 'Credit'` rows (the two AWS Open Source Promotional
     Credits) are excluded — `ListCost` is list price, not post-credit cost,
     and the sheet's own totals are built from the positive Usage lines only.
3. Emit the same two-column shape Ternary always supplied, and also write a
   small `data/<month>/focus_totals.json` snapshot (grouped totals only, not
   the raw Parquet) so the CSVs stay reproducible if the bucket's retention
   ever ages the raw files out.

Requires `duckdb` (+ its `pytz` dependency), managed via
[`uv`](https://docs.astral.sh/uv/) (see Running below) — not system Python,
which is externally managed (PEP 668). Everything under
`data/` (raw Parquet, snapshots, and CSVs alike) is gitignored — nothing in
`data/` is committed; see "Running" below for what to keep locally.

Verified against the real July 2026 sheets during development: Financials
matched every charge-description line exactly to the cent
(`SUM(ListCost)` = the sheet's "LF Spend from Ternary" total); Runtime
matched essentially every raw-import line (a couple of differences turned out
to be markdown-escaping artifacts in the manual comparison, not real gaps).
Note the Runtime sum is **not a dollar figure** — `ConsumedQuantity` mixes
instance-hours, GB, requests, etc. in one column (the sheet's raw import does
the same), so it's a same-units-only sanity check, not a spendable total; a
sub-cent-equivalent gap across that many mixed-unit rows is rounding noise,
not a real discrepancy. (Actual dollar/hour figures are deliberately not
reproduced in this public repo.)

The extractor emits the same two-column shape Ternary always supplied:
`Charge Description | Measure | MM/YYYY | Totals`. The sheets' existing
pivots and the hand-maintained Runner/Instance-Family/Vendor/Model/
Architecture lookup table need no changes.

## Coverage

Complete for any month the FOCUS export covers (2026-07 onward) — every
usage type AWS billed is priced, verified line-by-line against the real July
2026 sheets (see above). No `unpriced.json` equivalent needed; nothing is
dropped.

Older months (before 2026-07, e.g. 2026-03) have no FOCUS Parquet data and
are out of scope for this extractor.

Known pre-existing quirk in the source sheets (inherited from Ternary, not
introduced here — confirmed intentional, not a bug, while validating the
FOCUS Runtime path): the Runtime workbook's raw import mixes Lambda
provisioned-concurrency GB-seconds, API Gateway requests, NAT bytes, EBS
IOPS, etc. into the same `Consumed Quantity` column as EC2 instance-hours.
The sheet's own Runner/Instance-Family lookup table filters these out of the
Architecture pivot; both extractors now reproduce this same raw-import row
population rather than pre-filtering it.

Known quirk: `c6a.large` usage falls outside the sheet's `x86_64 (AMD)`
architecture bucket per its existing lookup table. This is not a defect in
either extractor — they emit the raw two-column feed and leave all
classification to the sheet's existing lookup table, so `c6a.large` is
classified exactly as it always was.

## Running

```
uv sync
FOCUS_BUCKET=$(op read "op://Engineering/ci-reports-config/FOCUS_BUCKET" \
  --account pytorch.1password.com) \
  uv run python -m ci_reports.focus_extract "$AWS_PROFILE" 2026-07
```

`uv sync` installs `duckdb`/`pytz` into `.venv/` from `pyproject.toml`/`uv.lock`
— run it once, and again after either file changes.

`AWS_PROFILE` above should target the **`ci-reports-read-only`** permission
set on the PyTorch AWS account — a dedicated role scoped to just
`s3:ListBucket`/`s3:GetObject` on the FOCUS export bucket, nothing else. This
org uses AWS SSO, not long-lived IAM credentials. Set one up with
[`aws-sso-cli`](https://github.com/synfinatic/aws-sso-cli); with its default
profile naming (`<AccountName>:<RoleName>`) this looks like
`PyTorchFoundation:ci-reports-read-only`, though the account-name half depends
on your own `aws-sso-cli` config — export `AWS_PROFILE` to whatever that
resolves to locally.

`FOCUS_BUCKET` is required and deliberately not committed anywhere in this
public repo. The real value is stored in the `pytorch.1password.com`
"Engineering" vault, item `ci-reports-config` (a general config item for this
repo — add more custom fields to it as this extractor grows rather than
creating new items) — requires the
[1Password CLI](https://developer.1password.com/docs/cli/) (`op`) signed in
to that account. Without `op`, ask whoever set up the FOCUS export for the
value and export it directly.

Note that the account's blanket `AWSReadOnlyAccess` permission set does
**not** work for this — it denies `s3:GetObject` on the FOCUS bucket, which
is why `ci-reports-read-only` exists as a separate, narrowly scoped role
rather than reusing it.

Output lands in `data/<YYYY-MM>/` (gitignored — nothing under `data/` is
committed):

- `financials.csv`, `runtime.csv` — sheet-ready, Ternary-shaped output.
- `focus_totals.json` — grouped-totals snapshot. Not committed; regenerate by
  re-running the extractor. If the PointFive bucket's retention ever ages a
  past month's raw Parquet out before this snapshot is captured, that month
  cannot be regenerated — re-run promptly after each month closes.
- `focus_raw/` — the raw Parquet download (~230MB/month), re-downloadable
  from the bucket at any time.

## Tests

```
uv run python tests/test_focus_extract.py  # synthetic duckdb fixture
```

(`pytest` is not currently a dependency; the test file also runs directly.)

## Linting

[`prek`](https://github.com/j178/prek) (a drop-in, dependency-free
`pre-commit` replacement) runs whitespace/line-ending checks and
[`aislop`](https://github.com/scanaislop/aislop) (a static scanner for
AI-generated code issues — dead code, swallowed errors, unsafe casts, etc.)
on every commit:

```
prek install   # one-time, installs the git hook
prek run --all-files   # run manually against everything
```

See `.pre-commit-config.yaml` for the hook list and `.editorconfig` for the
whitespace/line-ending rules editors should follow automatically.

## A note on Drive

Any evaluation sheet published from this data to the Monthly CI Reports Drive
folder is prefixed `DRAFT -` and is never used to overwrite the existing
Financials/Runtime/Trend workbooks. This tooling only reads from AWS and
writes local files; it has no write access to the existing sheets.
