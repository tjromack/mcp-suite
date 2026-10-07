# `staging/` — the relational layer beside the document store

`documents` holds one embedded row per thing the MCP tools can cite, with each
source's detail in a JSONB payload. That is the right shape for retrieval and
the wrong shape for analysis: you cannot join it, it has no declared grain, and
it has no per-partition integrity. Questions like *how many distinct adverse-event
cases, as opposed to reports?* or *which sponsors run trials at how many sites,
in how many countries?* are not retrieval questions, and the document store
cannot answer them.

This package is the other half. It loads the same upstreams' **relational**
files into real tables, with their keys, their partitions and their defects
intact — **beside** `documents`, not instead of it.

| | `documents` | `staging.*` |
|---|---|---|
| Shape | one row per citable record, detail in JSONB | upstream tables, as tables |
| Access | vector + full-text retrieval | SQL joins and aggregates |
| Grain | one row per `(source_id, doc_id)` | declared and proved per table |
| Freshness | watermark-driven incremental refresh | pinned archives you re-load |
| Serves | the MCP tools | analysis and SQL practice |

## Sources

| | FAERS | AACT |
|---|---|---|
| What | FDA adverse-event reports | ClinicalTrials.gov, relationally |
| Format | `$`-delimited ASCII, one zip per quarter | `\|`-delimited text, one zip per month |
| Loaded | 7 tables + the withdrawn-case list, 4 quarters | 5 of 49 tables, one pinned snapshot |
| Partition | `quarter` (`2026Q2`) | `snapshot` (`2026-10-01`) |
| Archive | ~65 MB per quarter | 2.5 GB, of which ~330 MB is read |

The FDA renamed the programme to the Adverse Event Monitoring System (AEMS) in
2026. The extracts, the file layout and the download URL are unchanged, so the
table names keep the name the data itself still uses.

## Running it

```bash
# create the schema (every command does this anyway; idempotent)
uv run python -m staging schema

# load four consecutive FAERS quarters, then resolve case versions
uv run python -m staging load-faers --quarters 2025Q3,2025Q4,2026Q1,2026Q2

# load one pinned AACT monthly archive
uv run python -m staging load-aact --snapshot 2026-10-01

# run every gate and regenerate docs/STAGING.md
uv run python -m staging gates
```

Useful flags:

- `--limit N` — load the first N lines of each table. A whole-pipeline smoke
  test in about a minute. It is a smoke test, not a preview: a load replaces
  its partition either way, so a limited run leaves that quarter holding only
  the rows it read. Use a throwaway partition, or re-load afterwards. The
  integrity gate catches it if you forget — the first N child rows reference
  parents the first N parent rows do not contain.
- `--from-dir` / `--from-zip` — read an archive you already have on disk
  instead of fetching it. Same code path; only the byte source changes.

Re-running a partition replaces it. The delete and the reload happen in one
transaction, so an interrupted load leaves the previous good partition in place
rather than a half-written one.

## Three decisions worth knowing about

**Only the members that are needed are transferred.** A zip's directory lives
at the end of the file and every member is an addressable byte range, so
[`remote_zip.py`](remote_zip.py) reads the central directory over HTTP `Range`
requests and then fetches only the five AACT tables this layer uses. The
archive is 2.5 GB; the load moves about an eighth of it. The same class reads a
local file, so `--from-zip` and the tests share one code path.

**Partial dates are kept, not completed.** FAERS date columns are
variable-width: `20250326` is a day, `200906` a month with no day, `2009` a
year. Padding the missing parts to make a `DATE` invents values that are then
indistinguishable from observed ones. Each date lands as three columns instead
— `*_raw`, `*_prec` and a real `DATE` populated only at day precision — so an
analysis that needs day resolution can filter on precision and know exactly
what that cost. In the loaded corpus that is not a rounding error:
`docs/STAGING.md` publishes the share.

**Nothing is dropped.** Both upstreams ship unquoted delimited text, so a
delimiter inside a free-text field widens the row and there is no escaping to
undo it. Those lines go to `staging.load_reject` verbatim, with their line
numbers, and are counted in `staging.load_run`. The conservation gate then
re-checks `rows_read = rows_loaded + rows_rejected` from the database.

## Gates

`python -m staging gates` runs five checks, each written so that a clean
database returns no rows, and writes the numbers to
[`docs/STAGING.md`](../docs/STAGING.md). It exits non-zero if any gate fails.

| Gate | What it proves |
|---|---|
| `grain` | the declared key of every table is actually unique |
| `conservation` | nothing was dropped, and each table holds what its runs claim |
| `dedup` | kept cases + superseded versions = demo rows with a case id |
| `integrity` | no child row points at a missing parent, per partition |
| `rejects` | every rejected line is counted, stored and attributable |

A grain failure means the *declaration* is wrong. The fix is to correct the
declaration and say so in the report — not to quietly widen the key until the
gate goes green.

## Layout

```
staging/
├── schema.sql      DDL: 14 tables + load_run + load_reject
├── remote_zip.py   ranged-HTTP zip member reader
├── delimited.py    line → row or reject, never neither
├── dates.py        FAERS partial dates; AACT ISO dates
├── loader.py       COPY + reject capture + run bookkeeping
├── faers.py        FAERS specs, row builders, case-version dedup
├── aact.py         AACT specs and row builders
├── gates.py        the five gates
├── report.py       renders docs/STAGING.md
└── __main__.py     the CLI
```

## What this is not

It is not a pharmacovigilance pipeline. FAERS reports are voluntary,
unverified and duplicated across reporters; a case being present says someone
reported it, not that a drug caused anything. Nothing here computes a
disproportionality statistic, and the counts are not incidence rates. The same
disclaimer the MCP tools append in code applies to anything built on these
tables.

It is also not a replacement for AACT itself. If you want the full
ClinicalTrials.gov relational database, CTTI publishes it — this is five of its
49 tables, pinned to one month, for the joins this repo actually makes.

## Sources and terms

- FAERS/AEMS quarterly extracts —
  <https://fis.fda.gov/extensions/FPD-QDE-FAERS/FPD-QDE-FAERS.html>.
  US government work, public domain. The FDA's own caveats about duplication
  and causality apply and are restated above.
- AACT — <https://aact.ctti-clinicaltrials.org/download>, published by CTTI
  from ClinicalTrials.gov. Cite CTTI when you publish anything derived from it.

Neither archive is committed to this repo; both are downloaded on demand.
