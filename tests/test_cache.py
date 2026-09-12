"""Unit tests for the TTL cache, including request coalescing."""

from __future__ import annotations

import asyncio

import pytest

from glm_usage.cache import COALESCED, HIT, MISS, STALE, IntervalGate, TTLCache
from glm_usage.client import InvalidTokenError


async def test_miss_then_hit() -> None:
    cache = TTLCache(60)
    calls = 0

    async def loader() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    assert await cache.get_or_load("k", loader) == ({"call": 1}, MISS)
    assert await cache.get_or_load("k", loader) == ({"call": 1}, HIT)
    assert calls == 1


async def test_concurrent_misses_collapse_into_one_load() -> None:
    cache = TTLCache(60)
    calls = 0

    async def loader() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "value"

    results = await asyncio.gather(*(cache.get_or_load("k", loader) for _ in range(10)))

    states = [state for _, state in results]
    assert calls == 1
    assert states.count(MISS) == 1
    assert states.count(COALESCED) == 9
    assert {value for value, _ in results} == {"value"}


async def test_loader_failure_reaches_every_waiter() -> None:
    cache = TTLCache(60)
    calls = 0

    class Boom(RuntimeError):
        pass

    async def loader() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        raise Boom("upstream down")

    results = await asyncio.gather(
        *(cache.get_or_load("k", loader) for _ in range(3)), return_exceptions=True
    )

    assert calls == 1
    assert all(isinstance(result, Boom) for result in results)
    # A failed load leaves nothing behind, so the next caller retries.
    assert cache.get("k") is None


async def test_stale_value_is_served_when_loader_fails() -> None:
    cache = TTLCache(0.01, stale_ttl=60)

    async def loader() -> str:
        return "fresh"

    assert await cache.get_or_load("k", loader) == ("fresh", MISS)

    await asyncio.sleep(0.02)

    async def failing() -> str:
        raise RuntimeError("upstream down")

    assert await cache.get_or_load("k", failing) == ("fresh", STALE)


async def test_auth_failure_never_falls_back_to_stale() -> None:
    """鉴权错误 opt out 了 stale 兜底（``allow_stale = False``），必须原样抛出去。"""
    cache = TTLCache(0.01, stale_ttl=60)

    async def loader() -> str:
        return "fresh"

    await cache.get_or_load("k", loader)
    await asyncio.sleep(0.02)

    async def rejected() -> str:
        raise InvalidTokenError("token 已过期")

    with pytest.raises(InvalidTokenError):
        await cache.get_or_load("k", rejected)


async def test_stale_window_ends() -> None:
    cache = TTLCache(0.01, stale_ttl=0.01)

    async def loader() -> str:
        return "fresh"

    await cache.get_or_load("k", loader)
    await asyncio.sleep(0.03)

    async def failing() -> str:
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        await cache.get_or_load("k", failing)


async def test_cancelled_leader_does_not_wedge_followers() -> None:
    cache = TTLCache(60)
    started = asyncio.Event()

    async def slow() -> str:
        started.set()
        await asyncio.sleep(5)
        return "never"

    leader = asyncio.create_task(cache.get_or_load("k", slow))
    await started.wait()
    follower = asyncio.create_task(cache.get_or_load("k", slow))
    await asyncio.sleep(0.01)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    with pytest.raises(asyncio.CancelledError):
        await follower

    # The inflight slot is released, so the next caller starts a fresh load.
    assert await cache.get_or_load("k", lambda: asyncio.sleep(0, result="ok")) == ("ok", MISS)


def test_pruning_keeps_the_cache_bounded() -> None:
    cache = TTLCache(30, max_entries=4)
    for index in range(10):
        cache.set(f"key-{index}", index)

    assert len(cache) == 4
    assert cache.get("key-9") == 9
    assert cache.get("key-0") is None


def test_expired_entries_are_pruned_first() -> None:
    now = [1000.0]
    cache = TTLCache(1, max_entries=3, clock=lambda: now[0])
    cache.set("stale", "old")
    now[0] += 10
    cache.set("fresh-1", 1)
    cache.set("fresh-2", 2)
    cache.set("fresh-3", 3)

    assert len(cache) == 3
    assert cache.get("stale") is None
    assert cache.get("fresh-3") == 3


async def test_max_age_forces_a_reload() -> None:
    cache = TTLCache(60)
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_load("k", loader) == (1, MISS)
    assert await cache.get_or_load("k", loader) == (1, HIT)
    # The entry is still fresh by TTL, but the caller refuses anything not
    # fetched in this very moment.
    assert await cache.get_or_load("k", loader, max_age=0) == (2, MISS)


async def test_max_age_disables_the_stale_fallback() -> None:
    clock = [1000.0]
    cache = TTLCache(1, stale_ttl=100, clock=lambda: clock[0])

    async def loader() -> str:
        return "fresh"

    assert await cache.get_or_load("k", loader) == ("fresh", MISS)
    clock[0] += 2  # past the TTL, still inside the stale window

    async def failing() -> str:
        raise RuntimeError("upstream down")

    # Without a cap the old value is still served...
    assert await cache.get_or_load("k", failing) == ("fresh", STALE)
    # ...but a caller that asked for fresh data gets the error instead.
    with pytest.raises(RuntimeError):
        await cache.get_or_load("k", failing, max_age=1)


def test_age_reports_entry_lifetime() -> None:
    clock = [1000.0]
    cache = TTLCache(60, clock=lambda: clock[0])

    assert cache.age("missing") is None
    cache.set("k", "v")
    clock[0] += 12.5
    assert cache.age("k") == 12.5


def test_stats_report_occupancy() -> None:
    cache = TTLCache(30, stale_ttl=5)
    cache.set("a", 1)
    assert cache.stats() == {"entries": 1, "fresh": 1, "inflight": 0, "ttl": 30, "staleTtl": 5}


def test_interval_gate_allows_once_per_interval() -> None:
    clock = [1000.0]
    gate = IntervalGate(30, clock=lambda: clock[0])

    assert gate.allow("k") is True
    assert gate.allow("k") is False

    clock[0] += 29
    assert gate.allow("k") is False

    clock[0] += 1
    assert gate.allow("k") is True
    # Independent keys do not share the budget.
    assert gate.allow("other") is True


def test_interval_gate_can_be_disabled() -> None:
    gate = IntervalGate(0, clock=lambda: 1000.0)
    assert gate.allow("k") is True
    assert gate.allow("k") is True
    assert gate.min_interval == 0


def test_interval_gate_stays_bounded() -> None:
    clock = [1000.0]
    gate = IntervalGate(10, max_entries=4, clock=lambda: clock[0])

    for index in range(10):
        clock[0] += 1
        gate.allow(f"key-{index}")

    assert len(gate._last) <= 4
