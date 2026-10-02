# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

YEAR_MONTH ?= 2026-07
OP_ACCOUNT ?= pytorch.1password.com

.PHONY: help sync focus clickhouse ci-metrics ci-metrics-snapshot render render-all report sheet-import snapshot publish serve deploy test lint clean

help:
	@echo "Targets (override with e.g. make report YEAR_MONTH=2026-08):"
	@echo "  sync         - uv sync (install deps into .venv/)"
	@echo "  focus        - extract LF data from the AWS FOCUS export (needs AWS_PROFILE)"
	@echo "  clickhouse   - extract Meta/AMD/Intel-jobs slices from ClickHouse"
	@echo "  ci-metrics   - extract CI Metrics slices (PRs, jobs, runtime by workflow) for YEAR_MONTH"
	@echo "  ci-metrics-snapshot - build/update data/ci_metrics_snapshot.json"
	@echo "  render       - render data/<YEAR_MONTH>/report.html from already-extracted data"
	@echo "  render-all   - build both snapshots, then re-render every month (publish runs this)"
	@echo "  report       - focus + clickhouse + render, the full pipeline"
	@echo "  sheet-import - backfill pre-FOCUS months from data/ternary/*.csv"
	@echo "  snapshot     - build/update data/trend_snapshot.json across all extracted months"
	@echo "  publish      - assemble the Workers-Static-Assets-ready site into data/site/"
	@echo "  serve        - serve data/site/ locally at http://localhost:8123"
	@echo "  deploy       - publish + wrangler deploy (Cloudflare Workers Static Assets)"
	@echo "  test         - run the offline synthetic-fixture test scripts"
	@echo "  lint         - run prek (pre-commit) against all files"
	@echo "  clean        - remove data/<YEAR_MONTH> (does not touch other months)"

sync:
	uv sync

focus:
	@if [ -z "$(AWS_PROFILE)" ]; then \
		echo "AWS_PROFILE is not set -- export it or pass AWS_PROFILE=... on the make command line" >&2; \
		exit 1; \
	fi
	FOCUS_BUCKET=$$(op read "op://Engineering/ci-reports-config/FOCUS_BUCKET" --account $(OP_ACCOUNT)) \
	  uv run python -m ci_reports.focus_extract "$(AWS_PROFILE)" $(YEAR_MONTH)

clickhouse:
	CH_HOST=$$(op read "op://Engineering/ci-reports-config/CH_HOST" --account $(OP_ACCOUNT)) \
	CH_USER=$$(op read "op://Engineering/ci-reports-config/CH_USER" --account $(OP_ACCOUNT)) \
	CH_PASS=$$(op read "op://Engineering/ci-reports-config/CH_PASS" --account $(OP_ACCOUNT)) \
	  uv run python -m ci_reports.clickhouse_extract $(YEAR_MONTH)

ci-metrics:
	CH_HOST=$$(op read "op://Engineering/ci-reports-config/CH_HOST" --account $(OP_ACCOUNT)) \
	CH_USER=$$(op read "op://Engineering/ci-reports-config/CH_USER" --account $(OP_ACCOUNT)) \
	CH_PASS=$$(op read "op://Engineering/ci-reports-config/CH_PASS" --account $(OP_ACCOUNT)) \
	  uv run python -m ci_reports.ci_metrics_extract $(YEAR_MONTH)

ci-metrics-snapshot:
	uv run python -m ci_reports.ci_metrics

render:
	uv run python -m ci_reports.render $(YEAR_MONTH)
	@echo "Wrote data/$(YEAR_MONTH)/report.html"

render-all: snapshot ci-metrics-snapshot
	uv run python -m ci_reports.render --all

report: focus clickhouse render

sheet-import:
	uv run python -m ci_reports.sheet_import \
	  data/ternary/ternary-cost-2024-04-to-2026-07.csv \
	  data/ternary/ternary-runtime-2024-04-to-2026-07.csv

snapshot:
	uv run python -m ci_reports.snapshot

publish: render-all
	uv run python -m ci_reports.publish

serve:
	cd data/site && python3 -m http.server 8123

deploy: publish
	@if [ -z "$(CLOUDFLARE_ACCOUNT_ID)" ] || [ -z "$(CLOUDFLARE_API_TOKEN)" ]; then \
		echo "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must both be exported first" >&2; \
		exit 1; \
	fi
	npx --yes wrangler@4 deploy

test:
	uv run python tests/test_focus_extract.py
	uv run python tests/test_amd_cost.py
	uv run python tests/test_sheet_import.py
	uv run python tests/test_architecture_sources.py
	uv run python tests/test_ci_metrics.py

lint:
	prek run --all-files

clean:
	rm -rf data/$(YEAR_MONTH)
