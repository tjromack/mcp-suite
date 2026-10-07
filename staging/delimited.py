"""Parse delimited lines into rows, routing anything malformed to a reject lane.

Both upstreams ship unquoted delimited text, so a delimiter inside a free-text
field cannot be escaped or detected — the row simply arrives with too many
fields. In AACT's September 2026 archive that is roughly one row in 9,000 of
`studies.txt`, where an official title contains a literal `|`.

The rule here is the one the add-on brief asks for: never silently drop and
never silently guess. A row whose field count does not match the header is
kept verbatim in `staging.load_reject` with its line number and a reason, and
counted in the load run. Downstream totals can then be reconciled as
`rows_read = rows_loaded + rows_rejected`, which is what the conservation gate
checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Reject:
    """A line that could not be parsed into the declared shape.

    `key` is the row's identifying value when it can still be recovered. A row
    is usually rejected because a free-text field later in the line contains a
    delimiter, so the leading key column is normally intact — and keeping it
    turns an anonymous reject into one that downstream checks can reason about.
    It is what lets the integrity gate say "this child is orphaned *because*
    its parent was rejected on line 41,233" rather than just "orphaned".
    """

    line_no: int
    reason: str
    raw_line: str
    key: str | None = None


@dataclass(frozen=True)
class ParsedRow:
    """A line that matched the header, as `{column: value}` with '' → None."""

    line_no: int
    values: dict[str, str | None]


# A rejected line is stored for inspection, not for replay — truncate so one
# pathological 4 MB row cannot bloat the reject table.
MAX_RAW_LINE_CHARS = 2000


def split_header(line: str, delimiter: str) -> list[str]:
    """Normalise a header line into lowercase column names."""
    return [field.strip().lower() for field in line.split(delimiter)]


def parse_lines(
    lines: Iterable[str],
    *,
    delimiter: str,
    header: list[str] | None = None,
    key_index: int | None = None,
) -> Iterator[ParsedRow | Reject]:
    """Yield a `ParsedRow` or `Reject` per data line, in file order.

    When `header` is None the first non-empty line is taken as the header, the
    way both upstreams ship their files. Pass it explicitly for headerless
    files — FAERS' `Deleted/DELETE*.txt` is a bare list of case ids.

    Blank lines are skipped rather than rejected: both exports end with one,
    and a trailing newline is not a data defect.

    `key_index` names the position of the identifying column, so a rejected
    line can still report which record it was.
    """
    if header is not None and not header:
        # An empty explicit header would silently reject every line — the field
        # count can never be zero — so refuse it at the door instead.
        raise ValueError("header must be non-empty when provided")

    columns = header
    width = len(columns) if columns else 0

    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue

        if columns is None:
            columns = split_header(line, delimiter)
            width = len(columns)
            continue

        fields = line.split(delimiter)
        if len(fields) != width:
            yield Reject(
                line_no=line_no,
                reason=f"expected {width} fields, got {len(fields)}",
                raw_line=line[:MAX_RAW_LINE_CHARS],
                key=(
                    fields[key_index].strip() or None
                    if key_index is not None and key_index < len(fields)
                    else None
                ),
            )
            continue

        yield ParsedRow(
            line_no=line_no,
            # Consecutive delimiters mean NULL in both formats; an all-whitespace
            # field is upstream padding, not a value.
            values={column: (value.strip() or None) for column, value in zip(columns, fields)},
        )
