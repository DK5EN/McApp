"""Built-in suite for the stale-while-revalidate telemetry response cache.

Fully offline, injected clock, no sleeps beyond event-loop yields. Every case names
the rule whose removal it catches.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx

from .sse_routes.weather import build_weather_router
from .telemetry_cache import TelemetryResponseCache
from .weblite import FastAPI

_TTL = 1000
_STALE = 10_000


class _Clock:
    def __init__(self) -> None:
        self.t = 0

    def __call__(self) -> int:
        return self.t


class _Compute:
    """Counting compute; returns b'v<N>' and can be told to fail."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def __call__(self) -> bytes:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return b"v%d" % self.calls


async def _yield(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _new(clock: _Clock, max_entries: int = 8) -> TelemetryResponseCache:
    return TelemetryResponseCache(_TTL, _STALE, max_entries, clock)


class _Storage:
    def __init__(self) -> None:
        self.raw = 0
        self.bucketed = 0

    async def get_telemetry_chart_data(
        self, hours: int = 48, stats_hours: int | None = None
    ) -> bytes:
        if hours <= 192:
            self.raw += 1
        else:
            self.bucketed += 1
        return b"{}"

    async def get_telemetry_chart_data_bucketed(self) -> bytes:
        self.bucketed += 1
        return b"{}"


class _Manager:
    def __init__(self) -> None:
        self.storage = _Storage()

    def require_storage(self) -> _Storage:
        return self.storage


async def _unit_cases(results: list[tuple[str, bool]]) -> None:  # noqa: PLR0915
    clock = _Clock()

    c = _new(clock)
    f = _Compute()
    a = await c.get("k", f)
    clock.t = _TTL - 1
    b = await c.get("k", f)
    await _yield()
    results.append(("fresh hit: no compute inside ttl", a == b == b"v1" and f.calls == 1))

    c = _new(clock := _Clock())
    f = _Compute()
    await c.get("k", f)
    clock.t = _TTL
    s1 = await c.get("k", f)
    s2 = await c.get("k", f)
    await _yield()
    results.append(
        (
            "stale: old bytes at once, exactly one refresh (no second while running)",
            s1 == s2 == b"v1" and f.calls == 2,
        )
    )
    new = await c.get("k", f)
    results.append(
        (
            "stale: refreshed bytes served afterwards, no further compute",
            new == b"v2" and f.calls == 2,
        )
    )

    c = _new(clock := _Clock())
    f = _Compute()
    await c.get("k", f)
    clock.t = _TTL
    f.fail = True
    r = await c.get("k", f)
    await _yield()
    r2 = await c.get("k", f)
    await _yield()
    results.append(
        (
            "refresh failure keeps old entry (and retries later)",
            r == r2 == b"v1" and f.calls == 3,
        )
    )

    c = _new(_Clock())
    gate = asyncio.Event()
    calls = 0

    async def slow() -> bytes:
        nonlocal calls
        calls += 1
        await gate.wait()
        return b"x"

    tasks = [asyncio.ensure_future(c.get("k", slow)) for _ in range(3)]
    await _yield()
    gate.set()
    out = await asyncio.gather(*tasks)
    results.append(
        ("cold single-flight: compute once for 3 callers", calls == 1 and out == [b"x"] * 3)
    )

    c = _new(_Clock())
    gate = asyncio.Event()
    fcalls = 0

    async def bad() -> bytes:
        nonlocal fcalls
        fcalls += 1
        await gate.wait()
        raise ValueError("cold")

    tasks = [asyncio.ensure_future(c.get("k", bad)) for _ in range(3)]
    await _yield()
    gate.set()
    errs = await asyncio.gather(*tasks, return_exceptions=True)
    ok_fn = _Compute()
    after = await c.get("k", ok_fn)
    results.append(
        (
            "cold failure: propagates to all waiters, caches nothing",
            fcalls == 1
            and all(isinstance(e, ValueError) for e in errs)
            and after == b"v1"
            and ok_fn.calls == 1,
        )
    )

    c = _new(clock := _Clock())
    f = _Compute()
    await c.get("k", f)
    clock.t = _STALE
    got = await c.get("k", f)
    results.append(
        ("age >= max_stale: computes synchronously, no stale serve", got == b"v2" and f.calls == 2)
    )

    # Cancelled first caller must not take the shared compute (and the waiters) down.
    c = _new(_Clock())
    gate = asyncio.Event()
    ccalls = 0

    async def gated() -> bytes:
        nonlocal ccalls
        ccalls += 1
        await gate.wait()
        return b"x"

    first = asyncio.ensure_future(c.get("k", gated))
    await _yield()
    second = asyncio.ensure_future(c.get("k", gated))
    await _yield()
    first.cancel()
    await _yield()
    gate.set()
    got2 = await second
    ok_fn = _Compute()
    cached = await c.get("k", ok_fn)
    results.append(
        (
            "cancelled first cold caller: waiter still gets bytes, compute once, entry cached",
            first.cancelled()
            and got2 == b"x"
            and ccalls == 1
            and cached == b"x"
            and ok_fn.calls == 0,
        )
    )

    # A background refresh and a cold request for the same key share one compute.
    c = _new(clock := _Clock())
    rgate = asyncio.Event()
    rcalls = 0

    async def prime_then_gated() -> bytes:
        nonlocal rcalls
        rcalls += 1
        if rcalls > 1:
            await rgate.wait()
        return b"r%d" % rcalls

    await c.get("k", prime_then_gated)
    clock.t = _TTL
    stale = await c.get("k", prime_then_gated)  # starts the refresh, blocked on rgate
    await _yield()
    clock.t = _STALE  # entry now too old to serve: cold path must join the refresh
    cold = asyncio.ensure_future(c.get("k", prime_then_gated))
    await _yield()
    rgate.set()
    cold_val = await cold
    results.append(
        (
            "refresh in flight + cold request: one compute, cold caller joins it",
            stale == b"r1" and cold_val == b"r2" and rcalls == 2,
        )
    )

    c = _new(_Clock(), max_entries=2)
    f = _Compute()
    await c.get("a", f)
    await c.get("b", f)
    await c.get("a", f)  # a is now most recent
    await c.get("c", f)  # evicts b
    n = f.calls
    await c.get("a", f)
    kept = f.calls == n
    await c.get("b", f)
    results.append(
        ("LRU: least recently used key evicted, recent one kept", kept and f.calls == n + 1)
    )


async def _route_cases(results: list[tuple[str, bool]]) -> None:
    mgr = _Manager()
    app = FastAPI()
    app.include_router(build_weather_router(cast(Any, mgr)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/api/telemetry?hours=48")
        await client.get("/api/telemetry?hours=48")
        results.append(("route: raw hours=48 is not cached", mgr.storage.raw == 2))
        r1 = await client.get("/api/telemetry?hours=744")
        r2 = await client.get("/api/telemetry?hours=744")
        results.append(
            (
                "route: hours=744 cached (storage called once)",
                mgr.storage.bucketed == 1 and r1.content == r2.content == b"{}",
            )
        )
        await client.get("/api/telemetry?hours=99999")
        results.append(("route: hours=99999 shares the clamped 744 key", mgr.storage.bucketed == 1))
        await client.get("/api/telemetry?hours=744&stats_hours=24")
        results.append(("route: stats_hours is part of the key", mgr.storage.bucketed == 2))
        await client.get("/api/telemetry/yearly")
        await client.get("/api/telemetry/yearly")
        results.append(("route: yearly cached (storage called once)", mgr.storage.bucketed == 3))


async def run_telemetry_cache_tests() -> bool:
    """Run the telemetry cache suite. Returns True iff all cases pass."""
    results: list[tuple[str, bool]] = []
    await _unit_cases(results)
    await _route_cases(results)
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    telemetry_cache: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
