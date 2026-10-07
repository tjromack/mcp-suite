"""End-to-end against a real Postgres. Skipped when one is not reachable.

The rest of the staging suite runs against fakes, which is fast and catches
most things — but not everything. The `$1::date` cast in the partition delete
passed every mocked test and then failed on the first real load, because
asyncpg infers a bound parameter's type from the cast attached to it and so
demanded a `datetime.date` where the loader passes a string. A fake connection
has no type inference, so no fake could have caught it.

What needs a real database is exactly that: driver type inference, COPY column
binding, the generated/typed columns, and the gate SQL. These tests load a
small fixture through the real loader and run every gate over it.

    docker compose up -d && uv run pytest tests/staging/test_integration.py
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from staging import aact, faers, gates, report
from staging.loader import apply_schema, load_table

asyncpg = pytest.importorskip("asyncpg")

SCHEMA_SQL = (Path(__file__).resolve().parents[2] / "staging" / "schema.sql").read_text(
    encoding="utf-8"
)

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://ctmcp:ctmcp_password@localhost:5432/clinical_trials"
)

QUARTER = "2099Q1"  # a partition no real load will ever write
SNAPSHOT = "2099-01-01"


# Two FAERS cases. Case 10000001 is reported twice — an initial version and a
# later follow-up — so the dedup has something real to resolve. Case 10000002
# is reported once and appears in the FDA's withdrawn list.
DEMO_LINES = [
    "primaryid$caseid$caseversion$i_f_code$event_dt$mfr_dt$init_fda_dt$fda_dt$rept_cod$"
    "auth_num$mfr_num$mfr_sndr$lit_ref$age$age_cod$age_grp$sex$e_sub$wt$wt_cod$rept_dt$"
    "to_mfr$occp_cod$reporter_country$occr_country",
    "100000011$10000001$1$I$202401$20240115$20240120$20240120$EXP$$M1$ACME$$55$YR$$F$Y$$$"
    "20240120$$HP$US$US",
    "100000012$10000001$2$F$202401$20240301$20240120$20240305$EXP$$M1$ACME$$55$YR$$F$Y$$$"
    "20240305$$HP$US$US",
    "100000021$10000002$1$I$2023$20240210$20240215$20240215$DIR$$$$$70$YR$$M$N$$$"
    "20240215$$CN$GB$GB",
    # Unusable primary id: must be rejected, not loaded, not silently dropped.
    "notanid$10000003$1$I$$$$20240101$EXP$$$$$$$$$$$$$$$$",
]

REAC_LINES = [
    "primaryid$caseid$pt$drug_rec_act",
    "100000012$10000001$Headache$",
    "100000021$10000002$Nausea$",
    # Orphan: no demo row with this primaryid in this quarter.
    "199999999$19999999$Rash$",
]

DELETE_LINES = ["10000002"]

# Built rather than written out: the studies header is wide, and a hand-counted
# row that is accidentally one field short would test the reject lane by
# accident instead of testing the loader.
STUDIES_HEADER = [
    "nct_id",
    "study_type",
    "overall_status",
    "phase",
    "enrollment",
    "enrollment_type",
    "source",
    "source_class",
    "acronym",
    "brief_title",
    "official_title",
    "why_stopped",
    "number_of_arms",
    "number_of_groups",
    "has_dmc",
    "is_fda_regulated_drug",
    "is_fda_regulated_device",
    "start_date",
    "start_date_type",
    "completion_date",
    "completion_date_type",
    "primary_completion_date",
    "primary_completion_date_type",
    "study_first_submitted_date",
    "study_first_posted_date",
    "last_update_submitted_date",
    "last_update_posted_date",
    "results_first_posted_date",
    "updated_at",
]

_GOOD_STUDY = {
    "nct_id": "NCT09000001",
    "study_type": "INTERVENTIONAL",
    "overall_status": "COMPLETED",
    "phase": "PHASE2",
    "enrollment": "120",
    "enrollment_type": "ACTUAL",
    "source": "Acme",
    "source_class": "INDUSTRY",
    "brief_title": "A short title",
    "official_title": "An official title",
    "number_of_arms": "2",
    "has_dmc": "t",
    "is_fda_regulated_drug": "t",
    "is_fda_regulated_device": "f",
    "start_date": "2024-01-01",
    "start_date_type": "ACTUAL",
    "completion_date": "2025-01-01",
    "updated_at": "2026-01-01 12:00:00",
}

# Same row, but with an unescaped `|` inside the title — the actual defect in
# AACT's unquoted export. It widens the row by exactly one field.
_SPLIT_STUDY = {**_GOOD_STUDY, "nct_id": "NCT09000002", "brief_title": "A | split title"}


def _study_line(values: dict[str, str]) -> str:
    return "|".join(values.get(column, "") for column in STUDIES_HEADER)


STUDIES_LINES = [
    "|".join(STUDIES_HEADER),
    _study_line(_GOOD_STUDY),
    _study_line(_SPLIT_STUDY),
]

SPONSORS_LINES = [
    "id|nct_id|agency_class|lead_or_collaborator|name",
    "900000001|NCT09000001|INDUSTRY|lead|Acme Pharma",
    # Orphan: points at a study not in this snapshot.
    "900000002|NCT09999999|OTHER|collaborator|Ghost University",
]


async def _connect() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=DSN, min_size=1, max_size=2)


@pytest.fixture
async def pool():  # type: ignore[no-untyped-def]
    try:
        created = await _connect()
    except Exception as exc:  # no database here — that is fine, skip
        pytest.skip(f"no Postgres at DATABASE_URL ({type(exc).__name__}: {exc})")
    # force=True: the normal path skips the DDL when the schema is already
    # there, which would run these tests against a stale shape after a
    # column is added.
    await apply_schema(created, SCHEMA_SQL, force=True)
    yield created
    # Leave no trace: these partitions are reserved for the test.
    async with created.acquire() as conn:
        for table in ("faers_demo", "faers_reac", "faers_deleted_case"):
            await conn.execute(f"DELETE FROM staging.{table} WHERE quarter = $1", QUARTER)
        for table in ("aact_studies", "aact_sponsors"):
            await conn.execute(
                f"DELETE FROM staging.{table} WHERE snapshot = $1::text::date", SNAPSHOT
            )
        await conn.execute(
            "DELETE FROM staging.load_run WHERE partition = ANY($1::text[])", [QUARTER, SNAPSHOT]
        )
        # `resolve_case_versions` TRUNCATEs and rebuilds `faers_case_current`
        # for the whole table, so a test that calls it leaves its fixture cases
        # behind in a shared table. Left there they break the dedup gate's
        # conservation check against a real corpus by exactly the number of
        # fixture rows — which is how this cleanup came to be written.
        await conn.execute(
            "DELETE FROM staging.faers_case_current WHERE caseid = ANY($1::bigint[])",
            [10000001, 10000002, 10000003],
        )
    await created.close()


def _spec(source_specs, table):  # type: ignore[no-untyped-def]
    return next(s for s in source_specs if s.table == table)


async def _load_fixture(pool) -> dict[str, object]:  # type: ignore[no-untyped-def]
    quarter = faers.parse_quarter(QUARTER)
    fspecs = faers.table_specs(quarter)
    aspecs = aact.table_specs()
    results = {}
    for table, lines in (
        ("faers_demo", DEMO_LINES),
        ("faers_reac", REAC_LINES),
        ("faers_deleted_case", DELETE_LINES),
    ):
        results[table] = await load_table(
            pool, source="faers", partition=QUARTER, spec=_spec(fspecs, table), lines=lines
        )
    for table, lines in (("aact_studies", STUDIES_LINES), ("aact_sponsors", SPONSORS_LINES)):
        results[table] = await load_table(
            pool, source="aact", partition=SNAPSHOT, spec=_spec(aspecs, table), lines=lines
        )
    return results


async def test_a_date_partition_delete_binds(pool) -> None:
    """The regression. `$1::date` raised DataError here; `$1::text::date` does not."""
    spec = _spec(aact.table_specs(), "aact_studies")
    result = await load_table(
        pool, source="aact", partition=SNAPSHOT, spec=spec, lines=STUDIES_LINES
    )
    assert result.rows_loaded == 1


async def test_rows_land_with_their_values_intact(pool) -> None:
    await _load_fixture(pool)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM staging.faers_demo WHERE primaryid = 100000012 AND quarter = $1",
            QUARTER,
        )
    assert row["caseid"] == 10000001
    assert row["caseversion"] == 2
    assert row["fda_dt"] == dt.date(2024, 3, 5)
    assert row["fda_dt_prec"] == "day"
    # A year-month event date keeps its raw value and gets no DATE.
    assert row["event_dt_raw"] == "202401"
    assert row["event_dt_prec"] == "month"
    assert row["event_dt"] is None


async def test_bad_rows_are_rejected_and_recorded(pool) -> None:
    results = await _load_fixture(pool)
    assert results["faers_demo"].rows_rejected == 1  # type: ignore[union-attr]
    assert results["aact_studies"].rows_rejected == 1  # type: ignore[union-attr]
    async with pool.acquire() as conn:
        reasons = await conn.fetch(
            "SELECT table_name, reason FROM staging.load_reject WHERE partition = ANY($1::text[])",
            [QUARTER, SNAPSHOT],
        )
    by_table = {r["table_name"]: r["reason"] for r in reasons}
    assert "primaryid" in by_table["faers_demo"]
    width = len(STUDIES_HEADER)
    assert by_table["aact_studies"] == f"expected {width} fields, got {width + 1}"


async def test_reloading_a_partition_replaces_it(pool) -> None:
    await _load_fixture(pool)
    await _load_fixture(pool)
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM staging.faers_demo WHERE quarter = $1", QUARTER
        )
    assert count == 3, "a second load duplicated the partition instead of replacing it"


async def test_case_version_dedup_keeps_the_latest_and_conserves(pool) -> None:
    await _load_fixture(pool)
    await faers.resolve_case_versions(pool)
    async with pool.acquire() as conn:
        kept = await conn.fetchrow(
            "SELECT * FROM staging.faers_case_current WHERE caseid = 10000001"
        )
        deleted = await conn.fetchrow(
            "SELECT is_deleted FROM staging.faers_case_current WHERE caseid = 10000002"
        )
    # Two versions of case 10000001; the later fda_dt wins.
    assert kept["primaryid"] == 100000012
    assert kept["n_versions"] == 2
    assert kept["superseded"] == 1
    # The FDA's withdrawn-case list is applied, not ignored.
    assert deleted["is_deleted"] is True


async def test_every_gate_runs_and_reports_over_real_rows(pool) -> None:
    await _load_fixture(pool)
    await faers.resolve_case_versions(pool)
    results = await gates.run_all(pool)
    by_name = {r.name: r for r in results}
    assert set(by_name) == {"grain", "conservation", "dedup", "integrity", "rejects"}

    # The fixture plants one orphan per source, so the integrity gate must see
    # them. A gate that reports zero here is not checking anything.
    orphans = by_name["integrity"].failures
    assert any(f["child"] == "faers_reac" for f in orphans)
    assert any(f["child"] == "aact_sponsors" for f in orphans)

    assert by_name["grain"].passed, by_name["grain"].failures
    assert by_name["dedup"].passed, by_name["dedup"].failures


async def test_conservation_holds_for_the_fixture_runs(pool) -> None:
    results = await _load_fixture(pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name, rows_read, rows_loaded, rows_rejected FROM staging.load_run "
            "WHERE partition = ANY($1::text[]) AND status = 'success'",
            [QUARTER, SNAPSHOT],
        )
    assert rows
    for row in rows:
        assert row["rows_read"] == row["rows_loaded"] + row["rows_rejected"], dict(row)
    assert all(r.conserved for r in results.values())  # type: ignore[union-attr]


async def test_the_report_renders_from_real_gate_output(pool) -> None:
    await _load_fixture(pool)
    await faers.resolve_case_versions(pool)
    results = await gates.run_all(pool)
    precision = await gates.date_precision_report(pool)
    tables = await report.inventory(pool)
    markdown = report.render(results, precision, tables)
    assert markdown.startswith("# Relational staging")
    assert report.ANALYSIS_MARKER in markdown
    assert "staging.faers_demo" in markdown
