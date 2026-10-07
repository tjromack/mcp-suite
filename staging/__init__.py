"""Relational staging beside the document store.

`core.store` keeps one `documents` row per citable thing, with the source's
detail in a JSONB payload — the right shape for retrieval, the wrong shape for
analysis. This package loads the same upstreams' *relational* files into real
tables with declared keys, partitions and defects intact, so the questions a
document store cannot answer ("how many distinct cases, not reports?", "which
sponsors run trials at how many sites?") become SQL.

Two sources:

* **FAERS** (`staging.faers`) — four quarterly `$`-delimited ASCII extracts,
  all seven tables plus the withdrawn-case list, with case-version dedup.
* **AACT** (`staging.aact`) — five tables from CTTI's pinned monthly
  pipe-delimited archive of ClinicalTrials.gov.

Entry point is the CLI: `python -m staging --help`.
"""

from __future__ import annotations

__all__ = ["aact", "dates", "delimited", "faers", "gates", "loader", "remote_zip", "report"]
