"""The parser's contract: every line becomes exactly one row or one reject.

That invariant is what lets the conservation gate assert
`rows_read = rows_loaded + rows_rejected`. If a line could be silently
skipped, the gate would pass while data went missing.
"""

from __future__ import annotations

import pytest

from staging.delimited import MAX_RAW_LINE_CHARS, ParsedRow, Reject, parse_lines, split_header


def _parse(lines: list[str], delimiter: str = "|", header: list[str] | None = None) -> list:
    return list(parse_lines(lines, delimiter=delimiter, header=header))


def test_first_line_becomes_the_header() -> None:
    out = _parse(["id|name", "1|ada"])
    assert len(out) == 1
    assert isinstance(out[0], ParsedRow)
    assert out[0].values == {"id": "1", "name": "ada"}


def test_header_is_lowercased_and_trimmed() -> None:
    assert split_header(" ID | Name ", "|") == ["id", "name"]


def test_empty_fields_become_none() -> None:
    """Consecutive delimiters mean NULL in both formats, not an empty string."""
    out = _parse(["a|b|c", "1||3"])
    assert out[0].values == {"a": "1", "b": None, "c": "3"}


def test_whitespace_only_field_is_also_none() -> None:
    out = _parse(["a|b", "1|   "])
    assert out[0].values["b"] is None


def test_extra_delimiter_rejects_the_row_with_its_line_number() -> None:
    """AACT's unquoted export: a `|` inside an official title widens the row."""
    out = _parse(["nct_id|title", "NCT01|a title", "NCT02|a | split title"])
    assert isinstance(out[0], ParsedRow)
    reject = out[1]
    assert isinstance(reject, Reject)
    assert reject.line_no == 3
    assert reject.reason == "expected 2 fields, got 3"
    assert reject.raw_line == "NCT02|a | split title"


def test_too_few_fields_also_rejects() -> None:
    out = _parse(["a|b|c", "1|2"])
    assert isinstance(out[0], Reject)
    assert "got 2" in out[0].reason


def test_blank_lines_are_skipped_not_rejected() -> None:
    """Both exports end with a newline; a trailing blank is not a defect."""
    out = _parse(["a|b", "1|2", "", "   ", "3|4"])
    assert [type(o) for o in out] == [ParsedRow, ParsedRow]


def test_explicit_header_keeps_the_first_line_as_data() -> None:
    """FAERS' DELETE file is headerless — line 1 is already a case id."""
    out = _parse(["10243933", "10243934"], delimiter="$", header=["caseid"])
    assert [o.values["caseid"] for o in out] == ["10243933", "10243934"]


def test_long_reject_lines_are_truncated() -> None:
    out = _parse(["a|b", "x" * 50_000 + "|y|z"])
    assert isinstance(out[0], Reject)
    assert len(out[0].raw_line) == MAX_RAW_LINE_CHARS


def test_dollar_delimiter_handles_faers_rows() -> None:
    header = "primaryid$caseid$caseversion"
    out = _parse([header, "100294532$10029453$2"], delimiter="$")
    assert out[0].values == {"primaryid": "100294532", "caseid": "10029453", "caseversion": "2"}


def test_every_line_is_accounted_for() -> None:
    """The invariant the conservation gate depends on."""
    lines = ["a|b", "1|2", "bad|row|here", "3|4", "", "5|6|7|8"]
    out = _parse(lines)
    data_lines = len([line for line in lines[1:] if line.strip()])
    assert len(out) == data_lines
    assert sum(isinstance(o, ParsedRow) for o in out) == 2
    assert sum(isinstance(o, Reject) for o in out) == 2


def test_empty_explicit_header_is_refused() -> None:
    """It would reject every line instead of failing loudly."""
    with pytest.raises(ValueError, match="non-empty"):
        _parse(["1|2"], header=[])
