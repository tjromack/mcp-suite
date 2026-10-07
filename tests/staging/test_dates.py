"""Partial dates are the easiest place to invent data without noticing.

`200906` is a month. Pad it to 2009-06-01 and it becomes indistinguishable
from a date someone actually reported. These tests pin the rule: width decides
precision, an exact DATE appears only at day precision, and anything that is
the right width but not a real date is `invalid` rather than quietly demoted.
"""

from __future__ import annotations

import datetime as dt

import pytest

from staging.dates import (
    parse_bool,
    parse_float,
    parse_int,
    parse_iso_date,
    parse_partial_date,
    parse_timestamp,
)


@pytest.mark.parametrize(
    ("raw", "precision", "exact"),
    [
        ("20250326", "day", dt.date(2025, 3, 26)),
        ("200906", "month", None),
        ("2009", "year", None),
    ],
)
def test_width_decides_precision(raw: str, precision: str, exact: dt.date | None) -> None:
    parsed = parse_partial_date(raw)
    assert parsed.precision == precision
    assert parsed.exact == exact
    assert parsed.raw == raw


def test_a_month_never_becomes_a_date() -> None:
    """The whole point: no padded day reaches the DATE column."""
    assert parse_partial_date("200906").exact is None


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_absent_values_carry_no_precision(raw: str | None) -> None:
    parsed = parse_partial_date(raw)
    assert (parsed.raw, parsed.precision, parsed.exact) == (None, None, None)


@pytest.mark.parametrize(
    "raw",
    [
        "00000000",  # zero-filled, common in FAERS
        "20250231",  # right width, impossible calendar date
        "20251301",  # month 13
        "209913",  # six digits, month 13
        "18990101",  # before the supported window
        "202",  # not a recognised width
        "2025-03-26",  # ISO, not the FAERS form
        "notadate",
    ],
)
def test_unusable_values_are_invalid_and_kept_raw(raw: str) -> None:
    parsed = parse_partial_date(raw)
    assert parsed.precision == "invalid"
    assert parsed.exact is None
    assert parsed.raw == raw, "the raw value must survive for audit"


def test_as_columns_matches_the_declared_column_order() -> None:
    raw, precision, exact = parse_partial_date("20250326").as_columns()
    assert (raw, precision, exact) == ("20250326", "day", dt.date(2025, 3, 26))


# --- AACT-side helpers -----------------------------------------------------


def test_iso_date_parses_and_tolerates_a_time_suffix() -> None:
    assert parse_iso_date("2025-11-15") == dt.date(2025, 11, 15)
    assert parse_iso_date("2025-11-15 00:00:00") == dt.date(2025, 11, 15)
    assert parse_iso_date("not a date") is None
    assert parse_iso_date("") is None


def test_timestamps_without_a_zone_are_read_as_utc() -> None:
    parsed = parse_timestamp("2026-09-01 04:12:33.918241")
    assert parsed is not None
    assert parsed.tzinfo is dt.UTC
    assert parse_timestamp("garbage") is None


def test_numeric_helpers_degrade_to_none_rather_than_raising() -> None:
    assert parse_int("42") == 42
    assert parse_int("42.0") == 42  # both feeds emit decimal integers
    assert parse_int("") is None
    assert parse_int("n/a") is None
    assert parse_float("-112.074040") == pytest.approx(-112.07404)
    assert parse_float("") is None


def test_aact_booleans() -> None:
    assert parse_bool("t") is True
    assert parse_bool("f") is False
    assert parse_bool("") is None
    assert parse_bool("maybe") is None
