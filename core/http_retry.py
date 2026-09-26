"""Shared retry policy for the upstream clients.

Three connectors talk to three APIs with three HTTP libraries — httpx for
openFDA and NCBI, curl_cffi for the Akamai-fronted ClinicalTrials.gov — but
they should agree on *when* to retry and *how long* to wait. That decision
lives here so it can be reasoned about, and tested, once.

Retryable:

- **429 Too Many Requests.** The likeliest real failure: openFDA allows 1,000
  requests/day anonymously and NCBI 3/second. Both say so with a 429.
- **5xx.** Free public APIs return them as transient signal rather than as a
  statement about the request.
- **A malformed body.** A truncated or HTML error page decodes as neither JSON
  nor XML; retrying is right, and failing with a clear message beats a
  ``JSONDecodeError`` surfacing from inside a connector.

Not retryable: every other 4xx. A 400 or a 403 means the request was wrong, and
repeating it wastes the caller's rate budget. 404 carries meaning of its own at
each source ("no such record") and is returned to the caller untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

# 3 retries, growing. Deliberately coarse: these are minute-scale rate limits
# and multi-second upstream hiccups, not microsecond contention.
RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 10.0)

# An upstream can ask for a longer wait than we're willing to block an ingest
# run for. Past this, treat the attempt as failed rather than sleeping on it.
MAX_RETRY_AFTER_SECONDS = 60.0


def is_retryable_status(status_code: int) -> bool:
    """True for 429 and 5xx — the statuses worth trying again."""
    return status_code == 429 or 500 <= status_code < 600


def retry_after_seconds(
    header_value: str | None,
    *,
    now: datetime | None = None,
    cap: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse a ``Retry-After`` header into seconds, or None if unusable.

    The header comes in two shapes per RFC 9110: delay-seconds (``120``) and an
    HTTP-date (``Wed, 21 Oct 2026 07:28:00 GMT``). Both are honoured. Negative
    or already-past values become 0; anything above ``cap`` returns None, since
    an ingest run should fail fast rather than sleep for an hour.
    """
    if not header_value:
        return None
    raw = header_value.strip()

    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()

    if seconds > cap:
        return None
    return max(0.0, seconds)


def delay_for(attempt: int, retry_after: float | None = None) -> float:
    """Seconds to wait before attempt number ``attempt`` (1-based).

    Never shorter than the upstream asked for: when a server sends
    ``Retry-After``, ignoring it is how a client earns a longer ban.
    """
    backoff = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1] if attempt >= 1 else 0.0
    return max(backoff, retry_after or 0.0)
