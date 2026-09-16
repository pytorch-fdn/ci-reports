# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# SPDX-License-Identifier: Apache-2.0

"""Coverage check for ci_reports/mappings/architecture_sources.json -- the
hand-maintained Location/Funded By table -- run as a CI gate, not only at
render time (same rule the spec's "Mapping coverage checks" section already
states for the vendor/architecture lookup). Checks both directions:

- every architecture the vendor/architecture lookup (or the synthesized
  "Other"/"N/A" buckets) can produce has an entry here, so a report table
  never silently shows "--" for a real bucket without that also showing up
  under Coverage gaps; and
- every entry's "architecture" is one of those known values, so a typo'd
  name doesn't sit in the file unnoticed, rendering "--" for the real
  bucket while the file itself looks complete.

The second direction is the one a render-time-only check (or the coverage
gap render.py adds) can't reliably catch on its own: a typo produces an
extra, orphaned entry, not a missing one, so a smoke test that only deletes
rows would still pass.

No real cost/usage figures involved -- this only reads architecture names,
locations, and funder names, all already public in the committed mapping
files. No fixture needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ci_reports.render import known_architectures, load_architecture_sources

SYNTHESIZED_ARCHES = {"Other", "N/A"}


def test_every_known_architecture_has_a_source_entry():
    sources = load_architecture_sources()
    missing = (known_architectures() | SYNTHESIZED_ARCHES) - set(sources)
    assert not missing, f"architecture_sources.json is missing: {sorted(missing)}"


def test_every_source_entry_is_a_known_architecture():
    sources = load_architecture_sources()
    valid = known_architectures() | SYNTHESIZED_ARCHES
    unknown = set(sources) - valid
    assert not unknown, (
        f"architecture_sources.json has entries for unrecognized architectures "
        f"(typo, or a stale entry for a retired one): {sorted(unknown)}"
    )


def test_every_entry_has_non_empty_location_and_funded_by():
    sources = load_architecture_sources()
    for architecture, row in sources.items():
        for field in ("location", "funded_by"):
            values = row.get(field)
            assert isinstance(values, list) and values, (
                f"{architecture!r}: {field!r} must be a non-empty list, got {values!r}"
            )
            assert all(isinstance(v, str) and v for v in values), (
                f"{architecture!r}: {field!r} must contain only non-empty strings"
            )


if __name__ == "__main__":
    test_every_known_architecture_has_a_source_entry()
    test_every_source_entry_is_a_known_architecture()
    test_every_entry_has_non_empty_location_and_funded_by()
    print("OK")
