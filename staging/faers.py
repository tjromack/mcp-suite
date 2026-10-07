"""FAERS quarterly ASCII extracts → `staging.faers_*`.

The FDA publishes one zip per quarter containing seven `$`-delimited tables
plus a headerless list of withdrawn case ids. The archive is not cumulative:
each quarter holds the cases the FDA processed in that quarter, and a case
amended later reappears in a later quarter as a new version. So loading four
consecutive quarters and counting rows counts *amendments*, not events — which
is why `resolve_case_versions` exists and why the dedup is audited rather than
assumed.

The FDA renamed the programme to the Adverse Event Monitoring System (AEMS) in
2026; the extracts, the file layout and the `faers_ascii_<year>q<n>.zip` URL
are unchanged, so the table names here keep the name the data still uses.

Upstream: https://fis.fda.gov/extensions/FPD-QDE-FAERS/FPD-QDE-FAERS.html
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import asyncpg

from staging.dates import parse_int, parse_partial_date
from staging.loader import RowError, TableSpec

SOURCE = "faers"
DELIMITER = "$"

_QUARTER_RE = re.compile(r"^(20\d{2})[Qq]([1-4])$")

ARCHIVE_URL = "https://fis.fda.gov/content/Exports/faers_ascii_{year}q{quarter}.zip"


@dataclass(frozen=True)
class Quarter:
    """A FAERS quarter, and the several ways it is spelled."""

    year: int
    quarter: int

    @property
    def label(self) -> str:
        """Canonical partition value, e.g. `2025Q1`."""
        return f"{self.year}Q{self.quarter}"

    @property
    def file_suffix(self) -> str:
        """How the quarter appears in member names, e.g. `25Q1`."""
        return f"{self.year % 100:02d}Q{self.quarter}"

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(year=self.year, quarter=self.quarter)


def parse_quarter(text: str) -> Quarter:
    match = _QUARTER_RE.match(text.strip())
    if not match:
        raise ValueError(f"{text!r} is not a FAERS quarter (expected e.g. 2025Q1)")
    return Quarter(year=int(match.group(1)), quarter=int(match.group(2)))


def _members(stem: str, quarter: Quarter) -> tuple[str, ...]:
    """Member-name candidates for one table inside the quarterly archive."""
    suffix = quarter.file_suffix
    return (
        f"ASCII/{stem}{suffix}.txt",
        f"ascii/{stem.lower()}{suffix.lower()}.txt",
        f"{stem}{suffix}.txt",
    )


def _require_id(values: dict[str, str | None], column: str) -> int:
    raw = values.get(column)
    parsed = parse_int(raw)
    if parsed is None:
        raise RowError(f"{column}={raw!r} is not an integer")
    return parsed


# --- row builders ----------------------------------------------------------
#
# Each returns a tuple in the TableSpec's declared column order. `parse_int`
# returns None rather than raising for optional numerics, so a malformed
# sequence number degrades to NULL instead of rejecting a whole row that is
# otherwise fine; the identifying columns use `_require_id` and do reject.


def _demo_row(v: dict[str, str | None], quarter: str) -> tuple:
    event = parse_partial_date(v.get("event_dt"))
    mfr = parse_partial_date(v.get("mfr_dt"))
    init_fda = parse_partial_date(v.get("init_fda_dt"))
    fda = parse_partial_date(v.get("fda_dt"))
    rept = parse_partial_date(v.get("rept_dt"))
    return (
        quarter,
        _require_id(v, "primaryid"),
        parse_int(v.get("caseid")),
        parse_int(v.get("caseversion")),
        v.get("i_f_code"),
        *event.as_columns(),
        *mfr.as_columns(),
        *init_fda.as_columns(),
        *fda.as_columns(),
        *rept.as_columns(),
        v.get("rept_cod"),
        v.get("auth_num"),
        v.get("mfr_num"),
        v.get("mfr_sndr"),
        v.get("lit_ref"),
        v.get("age"),
        v.get("age_cod"),
        v.get("age_grp"),
        v.get("sex"),
        v.get("e_sub"),
        v.get("wt"),
        v.get("wt_cod"),
        v.get("to_mfr"),
        v.get("occp_cod"),
        v.get("reporter_country"),
        v.get("occr_country"),
    )


def _drug_row(v: dict[str, str | None], quarter: str) -> tuple:
    exp = parse_partial_date(v.get("exp_dt"))
    return (
        quarter,
        _require_id(v, "primaryid"),
        parse_int(v.get("caseid")),
        parse_int(v.get("drug_seq")),
        v.get("role_cod"),
        v.get("drugname"),
        v.get("prod_ai"),
        v.get("val_vbm"),
        v.get("route"),
        v.get("dose_vbm"),
        v.get("cum_dose_chr"),
        v.get("cum_dose_unit"),
        v.get("dechal"),
        v.get("rechal"),
        v.get("lot_num"),
        *exp.as_columns(),
        v.get("nda_num"),
        v.get("dose_amt"),
        v.get("dose_unit"),
        v.get("dose_form"),
        v.get("dose_freq"),
    )


def _reac_row(v: dict[str, str | None], quarter: str) -> tuple:
    return (
        quarter,
        _require_id(v, "primaryid"),
        parse_int(v.get("caseid")),
        v.get("pt"),
        v.get("drug_rec_act"),
    )


def _outc_row(v: dict[str, str | None], quarter: str) -> tuple:
    return (quarter, _require_id(v, "primaryid"), parse_int(v.get("caseid")), v.get("outc_cod"))


def _rpsr_row(v: dict[str, str | None], quarter: str) -> tuple:
    return (quarter, _require_id(v, "primaryid"), parse_int(v.get("caseid")), v.get("rpsr_cod"))


def _ther_row(v: dict[str, str | None], quarter: str) -> tuple:
    start = parse_partial_date(v.get("start_dt"))
    end = parse_partial_date(v.get("end_dt"))
    return (
        quarter,
        _require_id(v, "primaryid"),
        parse_int(v.get("caseid")),
        parse_int(v.get("dsg_drug_seq")),
        *start.as_columns(),
        *end.as_columns(),
        v.get("dur"),
        v.get("dur_cod"),
    )


def _indi_row(v: dict[str, str | None], quarter: str) -> tuple:
    return (
        quarter,
        _require_id(v, "primaryid"),
        parse_int(v.get("caseid")),
        parse_int(v.get("indi_drug_seq")),
        v.get("indi_pt"),
    )


def _deleted_row(v: dict[str, str | None], quarter: str) -> tuple:
    return (quarter, _require_id(v, "caseid"))


def table_specs(quarter: Quarter) -> list[TableSpec]:
    """The eight members loaded from one quarterly archive, largest last.

    DEMO is first because the cross-table integrity gate checks every other
    table against it; loading the parent first means a partial load still has
    a consistent parent to report orphans against.
    """
    return [
        TableSpec(
            table="faers_demo",
            members=_members("DEMO", quarter),
            columns=(
                "quarter",
                "primaryid",
                "caseid",
                "caseversion",
                "i_f_code",
                "event_dt_raw",
                "event_dt_prec",
                "event_dt",
                "mfr_dt_raw",
                "mfr_dt_prec",
                "mfr_dt",
                "init_fda_dt_raw",
                "init_fda_dt_prec",
                "init_fda_dt",
                "fda_dt_raw",
                "fda_dt_prec",
                "fda_dt",
                "rept_dt_raw",
                "rept_dt_prec",
                "rept_dt",
                "rept_cod",
                "auth_num",
                "mfr_num",
                "mfr_sndr",
                "lit_ref",
                "age",
                "age_cod",
                "age_grp",
                "sex",
                "e_sub",
                "wt",
                "wt_cod",
                "to_mfr",
                "occp_cod",
                "reporter_country",
                "occr_country",
            ),
            build=_demo_row,
            delimiter=DELIMITER,
            partition_column="quarter",
        ),
        TableSpec(
            table="faers_deleted_case",
            members=(
                f"Deleted/DELETE{quarter.file_suffix}.txt",
                f"Deleted/DELETED{quarter.file_suffix}.txt",
                f"deleted/delete{quarter.file_suffix.lower()}.txt",
            ),
            columns=("quarter", "caseid"),
            build=_deleted_row,
            delimiter=DELIMITER,
            partition_column="quarter",
            # Headerless: the first line is already a case id, so declaring the
            # header here stops `parse_lines` eating that row as column names.
            header=("caseid",),
        ),
        TableSpec(
            table="faers_outc",
            members=_members("OUTC", quarter),
            columns=("quarter", "primaryid", "caseid", "outc_cod"),
            build=_outc_row,
            delimiter=DELIMITER,
            partition_column="quarter",
        ),
        TableSpec(
            table="faers_rpsr",
            members=_members("RPSR", quarter),
            columns=("quarter", "primaryid", "caseid", "rpsr_cod"),
            build=_rpsr_row,
            delimiter=DELIMITER,
            partition_column="quarter",
        ),
        TableSpec(
            table="faers_ther",
            members=_members("THER", quarter),
            columns=(
                "quarter",
                "primaryid",
                "caseid",
                "dsg_drug_seq",
                "start_dt_raw",
                "start_dt_prec",
                "start_dt",
                "end_dt_raw",
                "end_dt_prec",
                "end_dt",
                "dur",
                "dur_cod",
            ),
            build=_ther_row,
            delimiter=DELIMITER,
            partition_column="quarter",
        ),
        TableSpec(
            table="faers_reac",
            members=_members("REAC", quarter),
            columns=("quarter", "primaryid", "caseid", "pt", "drug_rec_act"),
            build=_reac_row,
            delimiter=DELIMITER,
            partition_column="quarter",
        ),
        TableSpec(
            table="faers_indi",
            members=_members("INDI", quarter),
            columns=("quarter", "primaryid", "caseid", "indi_drug_seq", "indi_pt"),
            build=_indi_row,
            delimiter=DELIMITER,
            partition_column="quarter",
        ),
        TableSpec(
            table="faers_drug",
            members=_members("DRUG", quarter),
            columns=(
                "quarter",
                "primaryid",
                "caseid",
                "drug_seq",
                "role_cod",
                "drugname",
                "prod_ai",
                "val_vbm",
                "route",
                "dose_vbm",
                "cum_dose_chr",
                "cum_dose_unit",
                "dechal",
                "rechal",
                "lot_num",
                "exp_dt_raw",
                "exp_dt_prec",
                "exp_dt",
                "nda_num",
                "dose_amt",
                "dose_unit",
                "dose_form",
                "dose_freq",
            ),
            build=_drug_row,
            delimiter=DELIMITER,
            partition_column="quarter",
        ),
    ]


# Child tables, for the cross-table integrity gate.
CHILD_TABLES = (
    "faers_drug",
    "faers_reac",
    "faers_outc",
    "faers_rpsr",
    "faers_ther",
    "faers_indi",
)


# --- case-version resolution ----------------------------------------------

# The FDA's own rule: the surviving version of a case is the one with the
# latest FDA receipt date. Two further keys follow it so the result is a
# TOTAL order and the dedup is reproducible — a tiebreak that leaves ties is
# the classic way a "deterministic" dedup silently changes between runs.
#   fda_dt      parsed date, newest first (NULL = unparseable, sorts last)
#   fda_dt_raw  the raw string, so an unparseable-but-ordered value still ranks
#   primaryid   highest wins, as the brief specifies
#   quarter     final tiebreak: the same primaryid can recur across quarters
_RESOLVE_SQL = """
TRUNCATE staging.faers_case_current;

INSERT INTO staging.faers_case_current
    (caseid, primaryid, quarter, caseversion, fda_dt_raw, fda_dt,
     n_versions, superseded, is_deleted)
WITH ranked AS (
    SELECT
        caseid,
        primaryid,
        quarter,
        caseversion,
        fda_dt_raw,
        fda_dt,
        ROW_NUMBER() OVER (
            PARTITION BY caseid
            ORDER BY fda_dt DESC NULLS LAST,
                     fda_dt_raw DESC NULLS LAST,
                     primaryid DESC,
                     quarter DESC
        ) AS rn,
        COUNT(*) OVER (PARTITION BY caseid) AS n_versions
    FROM staging.faers_demo
    WHERE caseid IS NOT NULL
)
SELECT
    ranked.caseid,
    ranked.primaryid,
    ranked.quarter,
    ranked.caseversion,
    ranked.fda_dt_raw,
    ranked.fda_dt,
    ranked.n_versions,
    ranked.n_versions - 1,
    EXISTS (
        SELECT 1 FROM staging.faers_deleted_case d WHERE d.caseid = ranked.caseid
    )
FROM ranked
WHERE rn = 1;
"""


async def resolve_case_versions(pool: asyncpg.Pool) -> dict[str, int]:
    """Rebuild `faers_case_current` and return the conservation numbers.

    Returns `{demo_rows, cases_kept, superseded, with_caseid, deleted}`. The
    gate asserts `cases_kept + superseded == with_caseid`: every demo row for
    a case is either the surviving version or one it superseded, with none
    lost and none counted twice.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_RESOLVE_SQL)
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM staging.faers_demo)                     AS demo_rows,
                (SELECT count(*) FROM staging.faers_demo WHERE caseid IS NOT NULL)
                                                                              AS with_caseid,
                (SELECT count(*) FROM staging.faers_case_current)              AS cases_kept,
                (SELECT coalesce(sum(superseded), 0) FROM staging.faers_case_current)
                                                                              AS superseded,
                (SELECT count(*) FROM staging.faers_case_current WHERE is_deleted)
                                                                              AS deleted
            """
        )
    return {key: int(value) for key, value in dict(row).items()}
