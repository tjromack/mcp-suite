"""FAERS partial dates: keep the raw value, record its precision, never guess.

FAERS date columns are free-width numeric strings. `20250326` is a full date,
`200906` is a month with no day, `2009` is a year with no month, and plenty of
rows carry `00000000`, a future date, or something that is not a date at all.

The common shortcut is to coerce everything to a DATE by padding the missing
parts — `200906` becomes 2009-06-01 — which invents a day the reporter never
supplied and then lets it be averaged, bucketed and plotted as though it were
observed. Downstream nobody can tell the padded days from the real ones.

So each date column lands as three columns instead of one:

    event_dt_raw   the upstream string, untouched
    event_dt_prec  'day' | 'month' | 'year' | 'invalid'
    event_dt       a real DATE, and only when precision is 'day'

An analysis that needs day resolution filters on precision and knows exactly
how much data that cost it. One that works monthly can use `event_dt_raw`'s
first six characters. Nothing has to trust a padded value.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

# FAERS began in 2004 (the legacy AERS extracts reach back to 1968, and some
# cases carry historical event dates). Anything outside this window is a data
# error rather than a date, most often a transposition or a zero-filled field.
MIN_YEAR = 1900
MAX_YEAR = 2100

PRECISION_DAY = "day"
PRECISION_MONTH = "month"
PRECISION_YEAR = "year"
PRECISION_INVALID = "invalid"


@dataclass(frozen=True)
class PartialDate:
    """One upstream date value, decomposed but not completed."""

    raw: str | None
    precision: str | None
    exact: _dt.date | None

    def as_columns(self) -> tuple[str | None, str | None, _dt.date | None]:
        """The triple in the order the staging tables declare them."""
        return self.raw, self.precision, self.exact


EMPTY = PartialDate(raw=None, precision=None, exact=None)


def parse_partial_date(value: str | None) -> PartialDate:
    """Classify a FAERS date string without completing it.

    Width decides intent: 8 digits is a day, 6 a month, 4 a year. A value that
    is the right width but not a real date — month 13, 31 February, a
    zero-filled field — is `invalid`, kept raw so it stays auditable.
    """
    if value is None:
        return EMPTY
    raw = value.strip()
    if not raw:
        return EMPTY

    if not raw.isdigit():
        return PartialDate(raw=raw, precision=PRECISION_INVALID, exact=None)

    def _year_ok(text: str) -> bool:
        return MIN_YEAR <= int(text) <= MAX_YEAR

    if len(raw) == 8:
        if not _year_ok(raw[:4]):
            return PartialDate(raw=raw, precision=PRECISION_INVALID, exact=None)
        try:
            exact = _dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            # Right width, impossible calendar date (e.g. 20250231). Record the
            # intended precision as invalid rather than silently demoting it to
            # a month — the row is wrong, not coarse.
            return PartialDate(raw=raw, precision=PRECISION_INVALID, exact=None)
        return PartialDate(raw=raw, precision=PRECISION_DAY, exact=exact)

    if len(raw) == 6:
        if not _year_ok(raw[:4]) or not 1 <= int(raw[4:6]) <= 12:
            return PartialDate(raw=raw, precision=PRECISION_INVALID, exact=None)
        return PartialDate(raw=raw, precision=PRECISION_MONTH, exact=None)

    if len(raw) == 4:
        if not _year_ok(raw):
            return PartialDate(raw=raw, precision=PRECISION_INVALID, exact=None)
        return PartialDate(raw=raw, precision=PRECISION_YEAR, exact=None)

    return PartialDate(raw=raw, precision=PRECISION_INVALID, exact=None)


def parse_iso_date(value: str | None) -> _dt.date | None:
    """Parse AACT's `YYYY-MM-DD`, returning None for anything else.

    AACT does its own date normalisation upstream and exports ISO dates, so
    unlike FAERS there is no partial-date problem to preserve here — a value
    that will not parse is a defect, and the row's reject reason says so.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return _dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_timestamp(value: str | None) -> _dt.datetime | None:
    """Parse AACT's `updated_at`, returning None for anything unrecognised.

    AACT exports Postgres timestamps (`2026-09-01 04:12:33.918241`), sometimes
    with a zone suffix. Values without a zone are read as UTC, which is what
    AACT's own database stores them in — stated here rather than left to the
    driver's locale.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def parse_int(value: str | None) -> int | None:
    """Parse an integer column, tolerating the decimal forms both feeds emit."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))
        except ValueError:
            return None


def parse_float(value: str | None) -> float | None:
    """Parse a numeric column (AACT latitude/longitude), None when unusable."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_bool(value: str | None) -> bool | None:
    """Parse AACT's `t`/`f` booleans."""
    if value is None:
        return None
    raw = value.strip().lower()
    if raw in ("t", "true", "y", "yes", "1"):
        return True
    if raw in ("f", "false", "n", "no", "0"):
        return False
    return None
