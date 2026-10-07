"""Gate behaviour, and the schema/spec consistency the gates assume.

The highest-value test here is not a gate at all: it is that every column a
`TableSpec` writes actually exists in `schema.sql`, in the declared order. A
typo there survives every other unit test and fails at COPY time against a
real database, several hundred megabytes into a load.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from staging import aact, faers
from staging.gates import GRAIN, PARENTS, dedup_gate, integrity_gate, reject_report

SCHEMA = (Path(__file__).resolve().parents[2] / "staging" / "schema.sql").read_text(
    encoding="utf-8"
)


def _schema_columns() -> dict[str, list[str]]:
    """Column names per `staging.<table>`, in declaration order."""
    tables: dict[str, list[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS staging\.(\w+)\s*\((.*?)\n\);", SCHEMA, re.S
    ):
        table, body = match.group(1), match.group(2)
        columns = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            # Table-level constraints, not columns. Matched on the two-word
            # form: `PRIMARY KEY (...)`. A prefix match on "PRIMARY" alone
            # would swallow the `primaryid` column every FAERS table declares.
            if re.match(r"(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE\s*\(|CONSTRAINT)\b", line, re.I):
                continue
            name = line.split()[0]
            if name.isidentifier():
                columns.append(name)
        tables[table] = columns
    return tables


SCHEMA_COLUMNS = _schema_columns()


def _all_specs():
    return [*faers.table_specs(faers.parse_quarter("2025Q1")), *aact.table_specs()]


# --- schema / spec consistency ---------------------------------------------


def test_the_schema_parser_found_the_tables() -> None:
    """Guard the guard: a parser that finds nothing would pass everything."""
    assert len(SCHEMA_COLUMNS) >= 14
    assert "faers_demo" in SCHEMA_COLUMNS


@pytest.mark.parametrize("spec", _all_specs(), ids=lambda s: s.table)
def test_every_spec_column_exists_in_the_schema(spec) -> None:  # type: ignore[no-untyped-def]
    declared = SCHEMA_COLUMNS[spec.table]
    missing = [c for c in spec.columns if c not in declared]
    assert not missing, f"{spec.table} writes columns the schema does not declare: {missing}"


@pytest.mark.parametrize("table,grain", list(GRAIN.items()))
def test_every_grain_key_column_exists(table: str, grain) -> None:  # type: ignore[no-untyped-def]
    declared = SCHEMA_COLUMNS[table]
    missing = [c for c in grain.key if c not in declared]
    assert not missing, f"{table} grain names unknown columns: {missing}"


def test_every_staged_table_has_a_declared_grain() -> None:
    """A table nobody declared a grain for is a table nobody can safely join."""
    assert set(SCHEMA_COLUMNS) - set(GRAIN) == {"load_run", "load_reject"}


def test_every_child_has_a_parent_and_both_are_staged() -> None:
    for child, (parent, keys) in PARENTS.items():
        assert child in SCHEMA_COLUMNS
        assert parent in SCHEMA_COLUMNS
        for column in keys:
            assert column in SCHEMA_COLUMNS[child]
            assert column in SCHEMA_COLUMNS[parent]


def test_partition_columns_lead_every_grain() -> None:
    for table, grain in GRAIN.items():
        if table.startswith("faers_") and table != "faers_case_current":
            assert grain.key[0] == "quarter", table
        elif table.startswith("aact_"):
            assert grain.key[0] == "snapshot", table


# --- gate behaviour --------------------------------------------------------


async def test_dedup_gate_passes_when_kept_plus_superseded_balances(fetch_pool) -> None:
    pool, conn = fetch_pool
    conn.fetchval.return_value = True
    conn.fetchrow.return_value = {
        "demo_rows": 1000,
        "with_caseid": 990,
        "kept": 600,
        "superseded": 390,
        "deleted_cases": 5,
    }
    result = await dedup_gate(pool)
    assert result.passed
    assert result.rows_checked == 1000


async def test_dedup_gate_fails_and_reports_the_difference(fetch_pool) -> None:
    pool, conn = fetch_pool
    conn.fetchval.return_value = True
    conn.fetchrow.return_value = {
        "demo_rows": 1000,
        "with_caseid": 990,
        "kept": 600,
        "superseded": 380,  # ten rows unaccounted for
        "deleted_cases": 0,
    }
    result = await dedup_gate(pool)
    assert not result.passed
    assert result.failures[0]["difference"] == 10


async def test_integrity_gate_is_an_anti_join_not_a_filtered_left_join(fetch_pool) -> None:
    """A LEFT JOIN filtered in WHERE collapses to an inner join and reports zero.

    The gate does use a LEFT JOIN, but only against `load_reject` to classify
    an orphan. The parent must be excluded with NOT EXISTS, and never filtered
    in WHERE.
    """
    pool, conn = fetch_pool
    conn.fetchval.return_value = True
    await integrity_gate(pool)
    queries = [call.args[0] for call in conn.fetch.call_args_list]
    assert queries, "the gate issued no queries"
    for sql in queries:
        assert "NOT EXISTS" in sql
        # The only LEFT JOIN permitted is the reject-classification one.
        for fragment in sql.split("LEFT JOIN")[1:]:
            assert "load_reject" in fragment.split("ON")[0]


async def test_integrity_gate_classifies_orphans_against_the_reject_lane(fetch_pool) -> None:
    pool, conn = fetch_pool
    conn.fetchval.return_value = True
    await integrity_gate(pool)
    sql = conn.fetch.call_args_list[0].args[0]
    assert "key_hint" in sql
    assert "explained_rows" in sql
    # DISTINCT matters: a parent rejected twice must not multiply its children.
    assert "SELECT DISTINCT partition, key_hint" in sql


async def test_integrity_gate_scopes_orphans_to_the_partition(fetch_pool) -> None:
    pool, conn = fetch_pool
    conn.fetchval.return_value = True
    await integrity_gate(pool)
    faers_queries = [q for q in (c.args[0] for c in conn.fetch.call_args_list) if "faers_" in q]
    assert faers_queries
    assert all("p.quarter = c.quarter" in q for q in faers_queries)


async def test_reject_report_flags_counted_but_unstored_lines(fetch_pool) -> None:
    """If rejects were counted and not stored, the audit trail has a hole."""
    pool, conn = fetch_pool
    conn.fetch.return_value = []
    conn.fetchval.side_effect = [10, 99, 0]  # stored, counted, uncapped runs
    result = await reject_report(pool)
    assert not result.passed
    assert result.failures[0]["counted"] == 99


async def test_reject_report_accepts_a_capped_run(fetch_pool) -> None:
    pool, conn = fetch_pool
    conn.fetch.return_value = []
    conn.fetchval.side_effect = [10, 99, 1]  # one run recorded that it capped
    result = await reject_report(pool)
    assert result.passed


def test_uniqueness_is_claimed_only_where_it_holds() -> None:
    """FAERS child tables carry join keys, not identities — see `Grain`.

    Flipping one of these to `unique=True` would make the gate fail against
    real data, which is the point: the declaration has to match the feed.
    """
    assert GRAIN["faers_demo"].unique
    assert GRAIN["aact_studies"].unique
    for table in ("faers_drug", "faers_reac", "faers_ther", "faers_indi"):
        assert not GRAIN[table].unique, table
        assert GRAIN["faers_drug"].note, "a non-unique key needs a note saying why"


async def test_reject_counts_are_scoped_to_the_latest_run_per_partition(fetch_pool) -> None:
    """A partition loaded twice must not report twice its rejects.

    `load_run` and `load_reject` keep superseded runs as history, so every
    count has to pick the newest run per (source, partition, table).
    """
    pool, conn = fetch_pool
    conn.fetch.return_value = []
    conn.fetchval.side_effect = [0, 0, 0]
    await reject_report(pool)
    counting = [c.args[0] for c in conn.fetchval.call_args_list]
    assert counting, "the gate counted nothing"
    for sql in counting:
        assert "DISTINCT ON (source, partition, table_name)" in sql
