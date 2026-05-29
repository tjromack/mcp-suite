"""Unit tests for the TTLCache (injectable clock — no sleeping)."""

from __future__ import annotations

from core.cache import TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_set_then_get_within_ttl():
    clock = FakeClock()
    c = TTLCache(10.0, clock=clock)
    c.set("k", {"v": 1})
    clock.t = 9.99
    assert c.get("k") == {"v": 1}


def test_entry_expires_and_is_evicted():
    clock = FakeClock()
    c = TTLCache(10.0, clock=clock)
    c.set("k", "val")
    clock.t = 10.0  # exactly at expiry → expired
    assert c.get("k") is None
    # eviction happened
    assert "k" not in c._store


def test_missing_key_returns_none():
    assert TTLCache(10.0).get("nope") is None


def test_ttl_zero_disables_cache():
    c = TTLCache(0)
    assert c.enabled is False
    c.set("k", "v")
    assert c.get("k") is None


def test_negative_ttl_disabled():
    assert TTLCache(-5).enabled is False


def test_clear():
    c = TTLCache(100.0)
    c.set("a", 1)
    c.set("b", 2)
    c.clear()
    assert c.get("a") is None and c.get("b") is None
