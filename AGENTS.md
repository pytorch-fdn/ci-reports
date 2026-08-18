<!--
SPDX-FileCopyrightText: 2026 The Linux Foundation

SPDX-License-Identifier: Apache-2.0
-->

# Agent instructions for ci-reports

This repo is **public**. Treat that as a hard constraint on everything you
write here, not just on what you're asked to write.

## Never leak real data

Never let real AWS cost figures, usage quantities, account IDs, bucket
names, or other org-identifying values end up in this repo, in any form:

- Source code and docstrings/comments — describe behavior generically
  (e.g. "matches the sheet's total to the cent"), never with the actual
  number that was checked.
- Committed files — no real Parquet/CSV/JSON exports, screenshots, or logs
  containing real figures. Everything under `data/` is gitignored for this
  reason; don't add exceptions to that.
- Commit messages and PR descriptions — same rule. Describe what changed
  and why, not the real numbers you verified it against.
- Anywhere else — issue comments, README prose, test fixtures. If a
  verification needs a concrete number to be checkable, use a synthetic
  fixture (see `tests/test_focus_extract.py`) instead of real data.

Bucket names and AWS account IDs are not classified secrets, but don't
publish them either — they're unnecessary recon surface for a public repo.
Keep them out of code and docs; pull them from environment variables or
1Password at runtime instead (see `README.md`'s Running section for the
existing pattern).

If you're about to write a real figure you obtained while testing or
verifying something, stop and generalize the statement instead. When in
doubt, leave it out.

## AWS access

This org uses **AWS SSO**, not long-lived IAM credentials. Recommend
[`aws-sso-cli`](https://github.com/synfinatic/aws-sso-cli) to set up local
SSO profiles for the PyTorch AWS account.

Don't hardcode a specific `AWS_PROFILE` value in code, docs, or examples —
profile names are per-user (`aws-sso-cli` lets each person name theirs
however they like). Have the user supply their own `AWS_PROFILE` via the
environment when running or testing anything that touches AWS.

## Everything else

Follow the conventions already documented in `README.md` (how the
extractor works, the `FOCUS_BUCKET`/1Password setup, gitignored `data/`,
tests). Don't duplicate that content here — update `README.md` itself when
those conventions change.
