"""Unit tests for the shared upstream retry policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.http_retry import (
    MAX_RETRY_AFTER_SECONDS,
    RETRY_DELAYS,
    delay_for,
    is_retryable_status,
    retry_after_seconds,
)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
def test_retryable_statuses(status):
    assert is_retryable_status(status)


@pytest.mark.parametrize("status", [200, 201, 301, 400, 401, 403, 404, 422])
def test_non_retryable_statuses(status):
    # 404 means "no such record" at every source; 4xx means the request was
    # wrong. Retrying either wastes the caller's rate budget.
    assert not is_retryable_status(status)


def test_retry_after_delay_seconds():
    assert retry_after_seconds("30") == 30.0


def test_retry_after_http_date():
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    when = (now + timedelta(seconds=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert retry_after_seconds(when, now=now) == pytest.approx(20.0, abs=1.0)


def test_retry_after_in_the_past_is_zero():
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    when = (now - timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert retry_after_seconds(when, now=now) == 0.0


@pytest.mark.parametrize("value", [None, "", "   ", "soon", "not-a-date"])
def test_unusable_retry_after_values(value):
    assert retry_after_seconds(value) is None


def test_absurd_retry_after_is_refused():
    # An hour-long hold should fail the run, not sleep through it.
    assert retry_after_seconds(str(MAX_RETRY_AFTER_SECONDS + 1)) is None


def test_delay_uses_backoff_when_no_header():
    assert [delay_for(i) for i in range(1, len(RETRY_DELAYS) + 1)] == list(RETRY_DELAYS)


def test_delay_never_shorter_than_the_server_asked():
    assert delay_for(1, retry_after=30.0) == 30.0


def test_delay_ignores_a_shorter_retry_after():
    # The server asking for 1s doesn't license hammering it faster than backoff.
    assert delay_for(2, retry_after=1.0) == RETRY_DELAYS[1]


def test_delay_saturates_past_the_last_step():
    assert delay_for(len(RETRY_DELAYS) + 5) == RETRY_DELAYS[-1]
