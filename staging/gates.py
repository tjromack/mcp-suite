"""Gates: every one returns rows only when something is wrong.

A staging layer that nobody checks is a staging layer nobody should trust, and
"we loaded it" is not a claim about correctness. These five gates are the
claim, and `python -m staging gates` runs them all and writes the numbers to
docs/STAGING.md.

  grain         each table's declared key behaves as declared — unique where
                uniqueness is claimed, and with its fan-out measured where it
                is only a join key
  conservation  rows_read = rows_loaded + rows_rejected, and the table holds
                exactly the rows the load run says it loaded
  dedup         kept cases + superseded versions = demo rows carrying a caseid
  integrity     no child row points at a parent that is not there
  rejects       how many lines did not parse, and why

The pattern is the one a pipeline gate uses: the query is written so that a
clean database returns nothing. That way a gate cannot pass by returning an
empty result for the wrong reason — an empty result *is* the pass condition,
and the row count that accompanies it proves the gate had data to look at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from staging.aact import CHILD_TABLES as AACT_CHILDREN
from staging.faers import CHILD_TABLES as FAERS_CHILDREN

logger = logging.getLogger("staging.gates")


@dataclass(frozen=True)
class Grain:
    """What a table's key is, and whether it identifies a row.

    The distinction matters more than it looks. A *unique* key identifies one
    row, so joining on it is safe. A *join* key does not: joining on it
    multiplies rows by however many share it, and the multiplier is invisible
    until a sum comes out too big. Declaring which one a table has — and
    measuring the multiplier where it is only a join key — is the difference
    between a table you can use and a table you can be wrong with.
    """

    key: tuple[str, ...]
    unique: bool
    note: str = ""


# Declared grain per table. Proved, not assumed: the gate measures each one and
# the report publishes the result. The `unique=False` entries below are not
# laziness — they are what the data turned out to be, and the notes say how.
GRAIN: dict[str, Grain] = {
    "faers_demo": Grain(("quarter", "primaryid"), unique=True),
    "faers_deleted_case": Grain(("quarter", "caseid"), unique=True),
    "faers_case_current": Grain(("caseid",), unique=True),
    # FAERS child tables have no unique key as shipped. `(primaryid, drug_seq)`
    # is how you JOIN a drug to its therapy dates and indications; it is not
    # what identifies a row. One case's drug_seq 1 can arrive 90 times, one row
    # per reported manufacturing lot, varying in lot_num and dose detail. Count
    # those rows and you count lot reports, not drugs.
    "faers_drug": Grain(
        ("quarter", "primaryid", "drug_seq"),
        unique=False,
        note="one row per reported lot/dose of a drug — join key, not identity",
    ),
    "faers_reac": Grain(
        ("quarter", "primaryid", "pt"),
        unique=False,
        note="the same reaction term can be reported more than once per case",
    ),
    "faers_outc": Grain(("quarter", "primaryid", "outc_cod"), unique=False),
    "faers_rpsr": Grain(("quarter", "primaryid", "rpsr_cod"), unique=False),
    "faers_ther": Grain(
        ("quarter", "primaryid", "dsg_drug_seq"),
        unique=False,
        note="one row per therapy interval; a drug can have several",
    ),
    "faers_indi": Grain(
        ("quarter", "primaryid", "indi_drug_seq", "indi_pt"),
        unique=False,
        note="duplicate indication rows occur upstream",
    ),
    # AACT exports its own surrogate keys, which are unique by construction.
    "aact_studies": Grain(("snapshot", "nct_id"), unique=True),
    "aact_sponsors": Grain(("snapshot", "id"), unique=True),
    "aact_conditions": Grain(("snapshot", "id"), unique=True),
    "aact_interventions": Grain(("snapshot", "id"), unique=True),
    "aact_facilities": Grain(("snapshot", "id"), unique=True),
}

# Child → (parent, join columns). The partition column joins too: a FAERS
# child row belongs to the parent in its own quarter, not to any quarter.
PARENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    **{child: ("faers_demo", ("quarter", "primaryid")) for child in FAERS_CHILDREN},
    **{child: ("aact_studies", ("snapshot", "nct_id")) for child in AACT_CHILDREN},
}


@dataclass
class GateResult:
    """One gate's verdict. `failures` is empty when the gate passes."""

    name: str
    description: str
    passed: bool
    rows_checked: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    detail: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "pass" if self.passed else "FAIL"


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"staging.{table}"))


async def grain_gate(pool: asyncpg.Pool) -> GateResult:
    """Measure every table's key and check it behaves as declared.

    For a table declared unique, any repeated key is a failure: the
    declaration is wrong and the fix is to correct it, not to widen the key
    until the gate goes quiet.

    For a table declared as carrying only a join key, repetition is expected
    and the useful output is the **fan-out multiplier** — rows divided by
    distinct keys, and the worst single key. That is the number that says how
    far wrong a naive `count(*)` after a join would be, and it is published
    rather than asserted.
    """
    failures: list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []
    checked = 0

    async with pool.acquire() as conn:
        for table, grain in GRAIN.items():
            if not await _table_exists(conn, table):
                continue
            columns = ", ".join(grain.key)
            row = await conn.fetchrow(
                f"""
                WITH grouped AS (
                    SELECT {columns}, count(*) AS n, count(DISTINCT t) AS distinct_rows
                      FROM staging.{table} t
                     GROUP BY {columns}
                )
                SELECT
                    (SELECT count(*) FROM staging.{table})                     AS rows_total,
                    (SELECT count(*) FROM grouped)                             AS distinct_keys,
                    (SELECT count(*) FROM grouped WHERE n > 1)                 AS repeated_keys,
                    (SELECT coalesce(sum(n - 1), 0) FROM grouped WHERE n > 1)  AS excess_rows,
                    (SELECT coalesce(max(n), 0) FROM grouped)                  AS worst,
                    -- Rows that repeat a key AND differ in content, versus rows
                    -- that are byte-identical. A duplicate nothing can tell
                    -- apart is an upstream artefact; a collision that carries
                    -- different data means the key is simply not the key.
                    (SELECT count(*) FROM grouped WHERE distinct_rows > 1)     AS conflicting_keys,
                    (SELECT coalesce(sum(n - distinct_rows), 0) FROM grouped)  AS identical_rows
                """
            )
            rows_total = int(row["rows_total"])
            distinct_keys = int(row["distinct_keys"])
            checked += rows_total
            entry = {
                "table": table,
                "key": " + ".join(grain.key),
                "unique": grain.unique,
                "note": grain.note,
                "rows": rows_total,
                "distinct_keys": distinct_keys,
                "repeated_keys": int(row["repeated_keys"]),
                "excess_rows": int(row["excess_rows"]),
                "conflicting_keys": int(row["conflicting_keys"]),
                "identical_rows": int(row["identical_rows"]),
                "worst_group": int(row["worst"]),
                "fan_out": (rows_total / distinct_keys) if distinct_keys else 1.0,
            }
            detail.append(entry)
            if grain.unique and entry["repeated_keys"]:
                failures.append(entry)

    return GateResult(
        name="grain",
        description="keys declared unique are unique; join keys have their fan-out measured",
        passed=not failures,
        rows_checked=checked,
        failures=failures,
        detail=detail,
    )


async def conservation_gate(pool: asyncpg.Pool) -> GateResult:
    """Prove nothing was dropped.

    Two checks in one. First, every load run must satisfy
    `rows_read = rows_loaded + rows_rejected` — arithmetic the loader keeps by
    construction, re-read from the database rather than trusted. Second, each
    table must physically hold the number of rows its successful load runs
    claim, which catches a partition deleted by one run and not rewritten.
    """
    failures: list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []

    async with pool.acquire() as conn:
        unbalanced = await conn.fetch(
            """
            SELECT id, source, partition, table_name, rows_read, rows_loaded, rows_rejected
              FROM staging.load_run
             WHERE status = 'success'
               AND rows_read <> rows_loaded + rows_rejected
             ORDER BY id
            """
        )
        for row in unbalanced:
            failures.append({"check": "run arithmetic", **dict(row)})

        claimed = await conn.fetch(
            """
            SELECT table_name, sum(rows_loaded) AS claimed
              FROM (
                    SELECT DISTINCT ON (source, partition, table_name)
                           table_name, rows_loaded
                      FROM staging.load_run
                     WHERE status = 'success'
                     ORDER BY source, partition, table_name, id DESC
                   ) latest
             GROUP BY table_name
             ORDER BY table_name
            """
        )
        for row in claimed:
            table = row["table_name"]
            if not await _table_exists(conn, table):
                continue
            actual = int(await conn.fetchval(f"SELECT count(*) FROM staging.{table}"))
            entry = {
                "table": table,
                "claimed_by_load_runs": int(row["claimed"]),
                "rows_in_table": actual,
            }
            detail.append(entry)
            if entry["claimed_by_load_runs"] != actual:
                failures.append({"check": "table vs load runs", **entry})

    return GateResult(
        name="conservation",
        description="rows_read = loaded + rejected, and tables hold what the runs claim",
        passed=not failures,
        rows_checked=sum(d["rows_in_table"] for d in detail),
        failures=failures,
        detail=detail,
    )


async def dedup_gate(pool: asyncpg.Pool) -> GateResult:
    """Prove the case-version dedup partitions the demo rows exactly.

    Every FAERS demo row carrying a caseid is either the surviving version of
    that case or one it superseded. Kept + superseded must therefore equal the
    demo rows with a caseid — no case lost to the dedup, none counted twice.
    """
    async with pool.acquire() as conn:
        if not await _table_exists(conn, "faers_case_current"):
            return GateResult("dedup", "case-version dedup conserves every row", True)
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM staging.faers_demo)                  AS demo_rows,
                (SELECT count(*) FROM staging.faers_demo
                  WHERE caseid IS NOT NULL)                                AS with_caseid,
                (SELECT count(*) FROM staging.faers_case_current)           AS kept,
                (SELECT coalesce(sum(superseded), 0)
                   FROM staging.faers_case_current)                         AS superseded,
                (SELECT count(*) FROM staging.faers_case_current
                  WHERE is_deleted)                                         AS deleted_cases
            """
        )

    numbers = {key: int(value) for key, value in dict(row).items()}
    balanced = numbers["kept"] + numbers["superseded"] == numbers["with_caseid"]
    failures = []
    if not balanced:
        failures.append(
            {
                "check": "kept + superseded = demo rows with a caseid",
                **numbers,
                "difference": numbers["with_caseid"] - (numbers["kept"] + numbers["superseded"]),
            }
        )

    return GateResult(
        name="dedup",
        description="kept cases + superseded versions = demo rows with a caseid",
        passed=balanced,
        rows_checked=numbers["demo_rows"],
        failures=failures,
        detail=[numbers],
    )


async def integrity_gate(pool: asyncpg.Pool) -> GateResult:
    """Child rows with no parent, per partition, as a single gate.

    One query per child table, each scoped to its own partition column, so an
    orphan is attributed to the quarter or snapshot that produced it rather
    than to the load as a whole. The anti-join is `NOT EXISTS`: a `LEFT JOIN`
    filtered in `WHERE` quietly becomes an inner join and reports zero orphans
    whether or not there are any.

    Orphans come in two kinds and the difference matters. Some are **explained**
    — the parent row was rejected at load time, so its children were always
    going to point at nothing. That is the reject policy working as designed
    and visible end to end: a pipe inside a study title rejects the study, and
    its sponsors and sites are orphaned by exactly that. The rest are
    **unexplained**, which means the upstream itself is referentially
    inconsistent. Only the unexplained ones fail the gate; the explained ones
    are reconciled against `load_reject.key_hint` and reported.
    """
    failures: list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []
    checked = 0

    async with pool.acquire() as conn:
        for child, (parent, keys) in PARENTS.items():
            if not await _table_exists(conn, child) or not await _table_exists(conn, parent):
                continue
            partition, join_key = keys[0], keys[1]
            on = " AND ".join(f"p.{k} = c.{k}" for k in keys)
            rows = await conn.fetch(
                f"""
                SELECT
                    c.{partition}::text                                   AS partition,
                    count(*)                                              AS orphan_rows,
                    count(DISTINCT c.{join_key})                          AS orphan_keys,
                    count(*) FILTER (WHERE r.key_hint IS NOT NULL)        AS explained_rows,
                    count(DISTINCT c.{join_key})
                        FILTER (WHERE r.key_hint IS NOT NULL)             AS explained_keys
                  FROM staging.{child} c
                  -- DISTINCT so a parent rejected twice cannot multiply its
                  -- children; this join classifies, it must not fan out.
                  LEFT JOIN (
                        SELECT DISTINCT partition, key_hint
                          FROM staging.load_reject
                         WHERE table_name = '{parent}' AND key_hint IS NOT NULL
                       ) r
                    ON r.key_hint = c.{join_key}::text
                   AND r.partition = c.{partition}::text
                 WHERE NOT EXISTS (
                           SELECT 1 FROM staging.{parent} p WHERE {on}
                       )
                 GROUP BY 1
                 ORDER BY 1
                """
            )
            total = int(await conn.fetchval(f"SELECT count(*) FROM staging.{child}"))
            checked += total
            orphan_rows = sum(int(r["orphan_rows"]) for r in rows)
            explained_rows = sum(int(r["explained_rows"]) for r in rows)
            entry = {
                "child": child,
                "parent": parent,
                "join": " + ".join(keys),
                "rows": total,
                "orphan_rows": orphan_rows,
                "explained_rows": explained_rows,
                "unexplained_rows": orphan_rows - explained_rows,
            }
            detail.append(entry)
            for row in rows:
                unexplained = int(row["orphan_rows"]) - int(row["explained_rows"])
                if unexplained:
                    failures.append(
                        {
                            "child": child,
                            "parent": parent,
                            **dict(row),
                            "unexplained_rows": unexplained,
                        }
                    )

    return GateResult(
        name="integrity",
        description="every orphan is explained by a rejected parent, per partition",
        passed=not failures,
        rows_checked=checked,
        failures=failures,
        detail=detail,
    )


async def reject_report(pool: asyncpg.Pool) -> GateResult:
    """Publish the reject counts.

    Not a pass/fail gate: unquoted upstream text guarantees some rejects, and a
    zero here would more likely mean the parser is too permissive than that the
    data is clean. It fails only if a reject was counted but never stored
    below the storage cap, which would mean the audit trail is incomplete.
    """
    detail: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.source, r.table_name,
                   sum(r.rows_read)     AS rows_read,
                   sum(r.rows_rejected) AS rejected,
                   count(*) FILTER (WHERE r.notes IS NOT NULL) AS capped_runs
              FROM (
                    SELECT DISTINCT ON (source, partition, table_name) *
                      FROM staging.load_run
                     WHERE status = 'success'
                     ORDER BY source, partition, table_name, id DESC
                   ) r
             GROUP BY r.source, r.table_name
            HAVING sum(r.rows_rejected) > 0
             ORDER BY sum(r.rows_rejected) DESC
            """
        )
        for row in rows:
            entry = dict(row)
            entry["rejected"] = int(entry["rejected"])
            entry["rows_read"] = int(entry["rows_read"])
            entry["reject_rate"] = (
                entry["rejected"] / entry["rows_read"] if entry["rows_read"] else 0.0
            )
            examples = await conn.fetch(
                """
                SELECT reason, count(*) AS n
                  FROM staging.load_reject
                 WHERE source = $1 AND table_name = $2
                   AND load_id IN (
                       SELECT DISTINCT ON (source, partition, table_name) id
                         FROM staging.load_run
                        WHERE status = 'success'
                        ORDER BY source, partition, table_name, id DESC
                   )
                 GROUP BY reason ORDER BY count(*) DESC LIMIT 3
                """,
                row["source"],
                row["table_name"],
            )
            entry["reasons"] = [f"{r['reason']} ×{r['n']}" for r in examples]
            detail.append(entry)

        # Scope every number to the newest successful run per (source,
        # partition, table). `load_run` and `load_reject` keep superseded runs
        # as history, so summing all of them double-counts a re-load — a
        # partition loaded twice would report twice its rejects and look like
        # the feed had got worse.
        latest = """
            SELECT DISTINCT ON (source, partition, table_name) id
              FROM staging.load_run
             WHERE status = 'success'
             ORDER BY source, partition, table_name, id DESC
        """
        stored = int(
            await conn.fetchval(
                f"SELECT count(*) FROM staging.load_reject WHERE load_id IN ({latest})"
            )
            or 0
        )
        counted = int(
            await conn.fetchval(
                f"SELECT coalesce(sum(rows_rejected), 0) FROM staging.load_run "
                f"WHERE id IN ({latest})"
            )
            or 0
        )
        uncapped = int(
            await conn.fetchval(
                f"SELECT count(*) FROM staging.load_run "
                f"WHERE id IN ({latest}) AND notes IS NOT NULL"
            )
            or 0
        )
        if counted > stored and uncapped == 0:
            failures.append(
                {
                    "check": "every rejected line is stored unless the run says it capped",
                    "counted": counted,
                    "stored": stored,
                }
            )

    return GateResult(
        name="rejects",
        description="rejected lines are counted, stored and attributable",
        passed=not failures,
        rows_checked=counted,
        failures=failures,
        detail=detail,
    )


async def date_precision_report(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """How many FAERS dates are NOT day-precision — the cost of not guessing.

    Reported rather than gated. The number is the point: it says how much of
    the corpus a day-resolution analysis would have had to invent if this
    layer had padded partial dates instead of keeping them.
    """
    columns = [
        ("faers_demo", "event_dt"),
        ("faers_demo", "fda_dt"),
        ("faers_ther", "start_dt"),
        ("faers_ther", "end_dt"),
        ("faers_drug", "exp_dt"),
    ]
    out: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        for table, column in columns:
            if not await _table_exists(conn, table):
                continue
            row = await conn.fetchrow(
                f"""
                SELECT count(*)                                        AS rows,
                       count(*) FILTER (WHERE {column}_prec = 'day')   AS day,
                       count(*) FILTER (WHERE {column}_prec = 'month') AS month,
                       count(*) FILTER (WHERE {column}_prec = 'year')  AS year,
                       count(*) FILTER (WHERE {column}_prec = 'invalid') AS invalid,
                       count(*) FILTER (WHERE {column}_prec IS NULL)   AS absent
                  FROM staging.{table}
                """
            )
            counts: dict[str, int] = {k: int(v) for k, v in dict(row).items()}
            present = counts["rows"] - counts["absent"]
            partial = counts["month"] + counts["year"] + counts["invalid"]
            entry: dict[str, Any] = {
                "table": table,
                "column": column,
                **counts,
                "present": present,
                "partial_or_invalid": partial,
                "partial_share": (partial / present) if present else 0.0,
            }
            out.append(entry)
    return out


GATES = (grain_gate, conservation_gate, dedup_gate, integrity_gate, reject_report)


async def run_all(pool: asyncpg.Pool) -> list[GateResult]:
    """Run every gate in order and log a one-line verdict for each."""
    results = []
    for gate in GATES:
        result = await gate(pool)
        results.append(result)
        logger.info(
            "gate %-13s %-4s  (%s rows checked, %d failure rows)",
            result.name,
            result.status,
            f"{result.rows_checked:,}",
            len(result.failures),
        )
    return results
