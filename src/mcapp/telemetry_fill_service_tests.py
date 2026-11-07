"""Built-in suite for the telemetry gap-fill service, Wave 2 (plan
doc/2026-11-07_1000-telemetry-gap-fill-plan.md).

Fully offline: httpx.MockTransport stands in for meshmap, the clock and the sleep
are injected. Every case names the rule whose removal it catches.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from . import telemetry_fill_service as fill_mod
from .main import MessageRouter
from .sqlite_storage import create_sqlite_storage
from .sse_handler import SSEManager
from .storage.telemetry_fill import FILL_SETTLE_MS, select_fill_rows
from .telemetry_fill_service import (
    CHUNK_MAX_MS,
    CHUNK_MIN_MS,
    HISTORY_MS,
    MIN_REQUEST_MS,
    REQUEST_SPACING_S,
    RESPONSE_CAP,
    TelemetryFillService,
)

_NOW = 1_800_000_000_000  # ms
_DAY = 24 * 3600 * 1000
_RF_OLD = _NOW - 20 * _DAY  # keeps a station visible, far from every tested window


class _Meshmap:
    """Fake server: per-call sample timestamps (ms); newest-5000 cap like the real one."""

    def __init__(self, data: dict[str, list[int]], fail: set[str] | None = None) -> None:
        self.data = data
        self.fail = fail or set()
        self.requests: list[dict[str, str]] = []
        self.fail_after: int | None = None  # 500 on every request past this many
        self.always_full = False  # answer with RESPONSE_CAP rows whatever the window

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        self.requests.append(q)
        if q["call"] in self.fail or (
            self.fail_after is not None and len(self.requests) > self.fail_after
        ):
            return httpx.Response(500)
        lo, hi = int(q["fromUnix"]) * 1000, int(q["toUnix"]) * 1000
        if self.always_full:
            step = max((hi - lo) // RESPONSE_CAP, 1)
            hits = [lo + i * step for i in range(RESPONSE_CAP)]
        else:
            hits = [t for t in self.data.get(q["call"], []) if lo <= t <= hi]
            hits = hits[-RESPONSE_CAP:]
        entries = [{"timestamp": t / 1000, "temp": 10.0 + (t // 1000) % 7} for t in hits]
        return httpx.Response(200, json={"call": q["call"], "entries": entries})

    def calls(self, call: str) -> list[dict[str, str]]:
        return [r for r in self.requests if r["call"] == call]


def _check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))


def _service(
    storage: Any,
    server: _Meshmap,
    sleeps: list[float],
    is_blocked: Callable[[str], bool] = lambda _c: False,
) -> TelemetryFillService:
    async def _sleep(s: float) -> None:
        sleeps.append(s)

    return TelemetryFillService(
        storage=storage,
        is_blocked=is_blocked,
        base_url="https://meshmap.invalid",
        transport=httpx.MockTransport(server.handler),
        clock=lambda: _NOW,
        sleep=_sleep,
        rng=lambda: 0.5,
    )


async def _rf(storage: Any, call: str, ts_ms: int) -> None:
    await storage.store_telemetry(
        call, {"timestamp": ts_ms, "src_type": "ble_remote", "temp1": 1.0}
    )


async def _filled(storage: Any, call: str) -> list[int]:
    rows = await storage._query(
        "SELECT timestamp FROM telemetry WHERE callsign = ? AND origin = 'meshmap'"
        " ORDER BY timestamp",
        (call,),
    )
    return [int(r["timestamp"]) for r in rows]


async def _enable(storage: Any, on: bool) -> None:
    await storage.update_internet_feed_state(enabled=on)


async def _reset_cursor(storage: Any, call: str) -> None:
    await storage._mutate("DELETE FROM telemetry_fill_state WHERE callsign = ?", (call,))


async def _core(results: list[tuple[str, bool]]) -> None:  # noqa: PLR0915 - one scenario per rule
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "svc.db")
        try:
            await _enable(storage, True)
            end = _NOW - FILL_SETTLE_MS

            # 1. Chunk halving: ~9.6 days at one sample / 100 s = ~8300 rows > cap.
            call = "DL1DENSE-1"
            c0 = _NOW - 10 * _DAY
            dense = list(range(c0 + 1000, end - 1000, 100_000))
            await _rf(storage, call, _RF_OLD)
            await storage.set_fill_cursor(call, c0)
            server = _Meshmap({call: dense})
            sleeps: list[float] = []
            svc = _service(storage, server, sleeps)
            await svc.run_cycle()
            got = await _filled(storage, call)
            _check(
                results,
                "halving: a capped response is re-requested smaller (>1 request)",
                len(dense) > RESPONSE_CAP and len(server.requests) > 1,
            )
            _check(
                results,
                "halving: the OLDEST rows are filled afterwards (nothing lost to the cap)",
                got == dense and got[0] == dense[0],
            )
            _check(
                results,
                "halving: first retry keeps the same fromUnix (does not advance)",
                len(server.requests) > 1
                and server.requests[0]["fromUnix"] == server.requests[1]["fromUnix"],
            )
            _check(
                results,
                "spacing: sleep(10) before every request but the first",
                sleeps.count(REQUEST_SPACING_S) == len(server.requests) - 1,
            )
            _check(
                results,
                "request carries call, unix-second window and the fields list",
                server.requests[0]["call"] == call
                and "press" in server.requests[0]["fields"]
                and int(server.requests[0]["toUnix"]) * 1000 <= end,
            )
            cur = await storage.get_fill_cursor(call)
            _check(results, "cursor ends at now - settle", cur == end // 1000 * 1000 or cur == end)

            # 2. Idempotent second pass over the same window.
            await _reset_cursor(storage, call)
            await storage.set_fill_cursor(call, c0)
            before = len(await _filled(storage, call))
            await svc.run_cycle()
            st = svc.status()["stations"][0]
            _check(
                results,
                "idempotent: a second cycle over the same window inserts 0 rows",
                st["rows_last_cycle"] == 0 and len(await _filled(storage, call)) == before,
            )

            # 3. Cursor only after a successful write; resume after "restart".
            call = "DL2RESUME-1"
            c0 = _NOW - 70 * _DAY
            samples = list(range(c0 + 5000, end - 5000, 3_600_000))
            # half an hour off the hourly sample grid, so no sample is a RF twin
            await _rf(storage, call, _RF_OLD + 1_800_000)
            await storage.set_fill_cursor(call, c0)
            server = _Meshmap({call: samples})
            server.fail_after = 1  # first chunk ok, then HTTP 500
            svc1 = _service(storage, server, [])
            await svc1.run_cycle()
            cur1 = await storage.get_fill_cursor(call)
            _check(
                results,
                "cursor: advanced past chunk 1, held at the failed chunk",
                cur1 == c0 + CHUNK_MAX_MS,
            )
            _check(
                results,
                "cursor: failure is recorded on the station status",
                {x["callsign"]: x["error"] for x in svc1.status()["stations"]}.get(call)
                == "HTTP 500",
            )
            server2 = _Meshmap({call: samples})
            svc2 = _service(storage, server2, [])  # new instance = restart
            await svc2.run_cycle()
            _check(
                results,
                "resume: new instance starts at the stored cursor, not at the floor",
                int(server2.requests[0]["fromUnix"]) * 1000 == cur1,
            )
            _check(
                results,
                "resume: every sample filled exactly once afterwards",
                await _filled(storage, call) == samples,
            )

            # 4. A storage failure costs that station its cycle, not the others.
            bad_w, good_w = "DL3WRITE-1", "DL3XGOOD-1"  # bad sorts first
            c0 = _NOW - 5 * _DAY
            for c in (bad_w, good_w):
                await _rf(storage, c, _RF_OLD)
                await storage.set_fill_cursor(c, c0)

            class _BrokenWrite:
                def __init__(self, inner: Any) -> None:
                    self._inner = inner

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

                async def insert_filled_telemetry(self, callsign: str, rows: Any) -> int:
                    if callsign == bad_w:
                        raise RuntimeError("disk full")
                    return int(await self._inner.insert_filled_telemetry(callsign, rows))

            sample = c0 + 10_000
            svc3 = TelemetryFillService(
                storage=_BrokenWrite(storage),
                is_blocked=lambda c: c not in {bad_w, good_w},
                base_url="https://meshmap.invalid",
                transport=httpx.MockTransport(
                    _Meshmap({bad_w: [sample], good_w: [sample]}).handler
                ),
                clock=lambda: _NOW,
                sleep=_noop,
            )
            raised = False
            try:
                await svc3.run_cycle()
            except RuntimeError:
                raised = True
            by_call = {x["callsign"]: x for x in svc3.status()["stations"]}
            _check(results, "storage error: run_cycle does not raise", not raised)
            _check(
                results,
                "storage error: cursor stays where it was, error recorded on the station",
                await storage.get_fill_cursor(bad_w) == c0
                and by_call[bad_w]["error"] == "storage error: RuntimeError",
            )
            _check(
                results,
                "storage error: the next station still fills and advances",
                await _filled(storage, good_w) == [sample]
                and await storage.get_fill_cursor(good_w) is not None
                and by_call[good_w]["error"] is None,
            )
            _check(results, "run_cycle clears running after an error", not svc3.status()["running"])
            call = bad_w

            # 5. Per-station isolation: HTTP 500 on one, next still fills.
            ok_call, bad_call = "DL5GOOD-1", "DL4BAD-1"  # bad sorts first
            for c in (ok_call, bad_call):
                await _rf(storage, c, _RF_OLD)
                await storage.set_fill_cursor(c, _NOW - 3 * _DAY)
            ts = [_NOW - 2 * _DAY + 7000]
            server = _Meshmap({ok_call: ts, bad_call: ts}, fail={bad_call})
            svc = _service(
                storage,
                server,
                [],
                is_blocked=lambda c: c in {call, good_w, "DL1DENSE-1", "DL2RESUME-1"},
            )
            await svc.run_cycle()
            _check(
                results,
                "isolation: failing station keeps its cursor, error recorded",
                await storage.get_fill_cursor(bad_call) == _NOW - 3 * _DAY
                and len(server.calls(bad_call)) == 1,
            )
            _check(
                results,
                "isolation: the next station still fills",
                await _filled(storage, ok_call) == ts,
            )

            # 6. Hidden and blocked stations get no request.
            hid, blk, vis = "DL6HID-1", "DL7BLK-1", "DL8VIS-1"
            for c in (hid, blk, vis):
                await _rf(storage, c, _RF_OLD)
                await storage.set_fill_cursor(c, _NOW - 3 * _DAY)
            await storage.set_wx_sidebar([], [hid])
            server = _Meshmap({c: [_NOW - 2 * _DAY + 9000] for c in (hid, blk, vis)})
            skip = {blk, "DL1DENSE-1", "DL2RESUME-1", "DL3WRITE-1", good_w, ok_call, bad_call}
            svc = _service(storage, server, [], is_blocked=lambda c: c in skip)
            await svc.run_cycle()
            _check(
                results,
                "hidden: no request for a wx_sidebar-hidden station, rows stay",
                not server.calls(hid) and await _filled(storage, hid) == [],
            )
            _check(results, "blocked: no request for a blocked station", not server.calls(blk))
            _check(results, "visible: the visible station is fetched", len(server.calls(vis)) == 1)

            # 7. Feed disabled: zero requests.
            await _enable(storage, False)
            server = _Meshmap({vis: [_NOW - 1 * _DAY]})
            await storage.set_wx_sidebar([], [])
            await _reset_cursor(storage, vis)
            await storage.set_fill_cursor(vis, _NOW - 2 * _DAY)
            svc = _service(storage, server, [])
            await svc.run_cycle()
            _check(results, "disabled: no HTTP request while the feed is off", not server.requests)
            _check(
                results,
                "disabled: existing filled rows stay",
                len(await _filled(storage, "DL1DENSE-1")) > 0,
            )
            await _enable(storage, True)

            # 8. run(): start delay from rng, then cycle, then pause 30 min; stop ends it.
            stop = asyncio.Event()
            seen: list[float] = []

            async def _sleep_stop(s: float) -> None:
                seen.append(s)
                if len(seen) >= 2:
                    stop.set()

            server = _Meshmap({})
            svc = TelemetryFillService(
                storage=storage,
                is_blocked=lambda _c: True,
                base_url="https://meshmap.invalid",
                transport=httpx.MockTransport(server.handler),
                clock=lambda: _NOW,
                sleep=_sleep_stop,
                rng=lambda: 0.5,
            )
            await svc.run(stop)
            _check(
                results,
                "run: start delay 0-300 s from rng, then the 30 min cycle pause",
                seen == [150.0, 1800.0] and svc.status()["next_cycle_ms"] == _NOW + 1_800_000,
            )

            _check(
                results,
                "constants: chunk min < max and history is 365 d",
                CHUNK_MIN_MS < CHUNK_MAX_MS and HISTORY_MS == 365 * _DAY,
            )
        finally:
            await storage.close()


async def _noop(_s: float) -> None:
    return None


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _Spy:
    """Counts calls to the two table-scanning helpers."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.counts = {"get_fill_row_counts": 0, "get_telemetry_stations": 0}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name in self.counts:

            async def _counted(*a: Any, **k: Any) -> Any:
                self.counts[name] += 1
                return await attr(*a, **k)

            return _counted
        return attr


def _only(*calls: str) -> Callable[[str], bool]:
    keep = set(calls)
    return lambda c: c not in keep


async def _rework(results: list[tuple[str, bool]]) -> None:  # noqa: PLR0915 - one scenario per rule
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "rework.db")
        try:
            await _enable(storage, True)
            end = _NOW - FILL_SETTLE_MS
            gap_off = 10 * 60 * 1000

            # A. Feed switched off after the first request: no second request.
            call = "DL1OFF-1"
            await _rf(storage, call, _RF_OLD)
            await storage.set_fill_cursor(call, _NOW - 70 * _DAY)
            server = _Meshmap({})

            async def _sleep_off(_s: float) -> None:
                await _enable(storage, False)

            svc = TelemetryFillService(
                storage=storage,
                is_blocked=_only(call),
                base_url="https://meshmap.invalid",
                transport=httpx.MockTransport(server.handler),
                clock=lambda: _NOW,
                sleep=_sleep_off,
            )
            await svc.run_cycle()
            _check(
                results,
                "disabled mid-station: the first fill stops after the request in flight",
                len(server.requests) == 1 and svc.status()["stations"][0]["error"] is None,
            )
            await _enable(storage, True)

            # B. Station set: RF row within 30 days only.
            old, new = "DL1STALE-1", "DL1RECENT-1"
            await _rf(storage, old, _NOW - 40 * _DAY)
            await _rf(storage, new, _NOW - 20 * _DAY)
            for c in (old, new):
                await storage.set_fill_cursor(c, _NOW - 3 * _DAY)
            server = _Meshmap({})
            svc = _service(storage, server, [], is_blocked=_only(old, new))
            await svc.run_cycle()
            _check(
                results,
                "stations: an RF row older than 30 days does not make a station",
                not server.calls(old) and len(server.calls(new)) > 0,
            )

            # C. Minimum-chunk guard: a response that is ALWAYS capped must terminate.
            call = "DL2CAP-1"
            await _rf(storage, call, _RF_OLD)
            await storage.set_fill_cursor(call, end - 3 * 3600 * 1000)
            server = _Meshmap({})
            server.always_full = True
            svc = _service(storage, server, [], is_blocked=_only(call))
            cap = _Capture()
            fill_mod.logger.addHandler(cap)
            try:
                await asyncio.wait_for(svc.run_cycle(), 20)
                done = True
            except TimeoutError:
                done = False
            finally:
                fill_mod.logger.removeHandler(cap)
            _check(
                results,
                "min chunk: an always-capped server ends the loop, cursor reaches the end",
                done and await storage.get_fill_cursor(call) == end and len(server.requests) < 10,
            )
            _check(
                results,
                "min chunk: accepting a capped response at the minimum logs a WARNING",
                any("capped response accepted" in m for m in cap.messages),
            )

            # D. +-15 min padding of the existing-rows query.
            call = "DL3PAD-1"
            c0 = end - 2 * _DAY
            await storage.set_fill_cursor(call, c0)
            await _rf(storage, call, end + 9 * 60 * 1000)  # after chunk_end
            await _rf(storage, call, c0 - gap_off)  # before chunk start
            s_hi, s_lo, s_ok = end - 5 * 60 * 1000, c0 + 2 * 60 * 1000, end - 3 * 3600 * 1000
            server = _Meshmap({call: [s_lo, s_ok, s_hi]})
            svc = _service(storage, server, [], is_blocked=_only(call))
            await svc.run_cycle()
            _check(
                results,
                "padding: RF rows just outside the chunk still suppress both of its edges",
                await _filled(storage, call) == [s_ok],
            )
            row = next(x for x in (await svc.route_payload())["stations"] if x["callsign"] == call)
            _check(
                results,
                "cache: the route row count follows an insert without a rescan",
                row["rows"] == 1 and row["filled_until_ms"] == end,
            )

            # E. Chunk boundary: inclusive bounds put the same second in two chunks.
            c0 = end - 40 * _DAY
            bnd = c0 + CHUNK_MAX_MS
            r1, r2 = "DL4BND-1", "DL4TWIN-1"
            for c in (r1, r2):
                await _rf(storage, c, _RF_OLD)
                await storage.set_fill_cursor(c, c0)
            server = _Meshmap({r1: [bnd], r2: [bnd - 20_000, bnd + 20_000]})
            svc = _service(storage, server, [], is_blocked=_only(r1, r2))
            await svc.run_cycle()
            _check(
                results,
                "boundary: a sample on the exact boundary second is inserted once",
                len(server.calls(r1)) == 2 and await _filled(storage, r1) == [bnd],
            )
            _check(
                results,
                "boundary: a sample 20 s after a filled row of the previous chunk is a twin",
                await _filled(storage, r2) == [bnd - 20_000],
            )

            # F. No cursor: the first request starts one history span back.
            call = "DL5NEW-1"
            await _rf(storage, call, _RF_OLD)
            server = _Meshmap({})
            svc = _service(storage, server, [], is_blocked=_only(call))
            await svc.run_cycle()
            _check(
                results,
                "first fill: no cursor starts at now - 365 d (unix seconds)",
                bool(server.requests)
                and int(server.requests[0]["fromUnix"]) == (_NOW - HISTORY_MS) // 1000,
            )

            # G. An empty chunk still advances the cursor.
            call = "DL6EMPTY-1"
            await _rf(storage, call, _RF_OLD)
            await storage.set_fill_cursor(call, end - 3 * _DAY)
            server = _Meshmap({})
            svc = _service(storage, server, [], is_blocked=_only(call))
            await svc.run_cycle()
            _check(
                results,
                "empty chunk: the cursor advances to the chunk end",
                await storage.get_fill_cursor(call) == end and len(server.requests) == 1,
            )

            # H. Growth after a halving: dense head, empty tail.
            call = "DL7GROW-1"
            c0 = _NOW - 40 * _DAY
            await _rf(storage, call, _RF_OLD)
            await storage.set_fill_cursor(call, c0)
            server = _Meshmap({call: list(range(c0 + 1000, c0 + 20 * 3600 * 1000, 10_000))})
            svc = _service(storage, server, [], is_blocked=_only(call))
            await svc.run_cycle()
            spans = [int(q["toUnix"]) - int(q["fromUnix"]) for q in server.requests]
            _check(
                results,
                "growth: the chunk doubles back after a halving (few requests, wide window)",
                len(server.requests) < 25
                and max(spans[-4:]) * 1000 > 5 * _DAY
                and await storage.get_fill_cursor(call) == end,
            )

            # I. A final chunk shorter than 60 s is skipped (fromUnix == toUnix is a 400).
            u, v = "DL8SLIVER-1", "DL8TINY-1"
            for c in (u, v):
                await _rf(storage, c, _RF_OLD)
            c0 = end - CHUNK_MAX_MS - MIN_REQUEST_MS // 2
            await storage.set_fill_cursor(u, c0)
            await storage.set_fill_cursor(v, end - MIN_REQUEST_MS // 2)
            server = _Meshmap({})
            svc = _service(storage, server, [], is_blocked=_only(u, v))
            await svc.run_cycle()
            _check(
                results,
                "sliver: a 30 s remainder is not requested",
                len(server.calls(u)) == 1
                and not server.calls(v)
                and await storage.get_fill_cursor(u) == c0 + CHUNK_MAX_MS,
            )
        finally:
            await storage.close()


class _FlakyService(TelemetryFillService):
    """run_cycle raises on the first call and stops the service on the second."""

    stop: asyncio.Event
    calls = 0

    async def run_cycle(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        self.stop.set()


class _IdleService(TelemetryFillService):
    calls = 0

    async def run_cycle(self) -> None:
        self.calls += 1


async def _run_loop(results: list[tuple[str, bool]]) -> None:
    def make(cls: type[TelemetryFillService]) -> TelemetryFillService:
        return cls(
            storage=None,
            is_blocked=lambda _c: False,
            base_url="https://meshmap.invalid",
            clock=lambda: _NOW,
            rng=lambda: 0.0,
        )

    # run() survives a raising cycle and runs the next one (real _pause, short interval).
    flaky = make(_FlakyService)
    assert isinstance(flaky, _FlakyService)
    stop = asyncio.Event()
    flaky.stop = stop
    survived = True
    with patch.object(fill_mod, "CYCLE_S", 0.01):
        try:
            await asyncio.wait_for(flaky.run(stop), 10)
        except Exception:
            survived = False
    _check(
        results,
        "run: a raising cycle is logged and the loop runs the next cycle",
        survived and flaky.calls == 2 and flaky._next_cycle_ms is not None,
    )

    # run() leaves the 30 min wait promptly when stop is set.
    idle = make(_IdleService)
    assert isinstance(idle, _IdleService)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    t0 = time.monotonic()
    with patch.object(fill_mod, "CYCLE_S", 3600):
        try:
            await asyncio.wait_for(idle.run(stop), 10)
            ended = True
        except TimeoutError:
            ended = False
    _check(
        results,
        "run: stop during the 30 min wait ends the loop promptly",
        ended and time.monotonic() - t0 < 2.0 and idle.calls == 1,
    )


async def _routes(results: list[tuple[str, bool]]) -> None:
    now = int(time.time() * 1000)
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.dict(os.environ, {"MESHCOM_VAPID_PATH": str(Path(tmp) / "vapid.json")}),
    ):
        storage = await create_sqlite_storage(Path(tmp) / "route.db")
        try:
            router = MessageRouter(storage)
            router.set_callsign("DK5EN-98")
            manager = SSEManager("127.0.0.1", 0, message_router=router)
            app = manager._create_app()
            client = TestClient(app, raise_server_exceptions=False)

            empty = client.get("/api/telemetry/fill").json()
            _check(
                results,
                "route: no service -> enabled false, empty stations",
                empty
                == {
                    "enabled": False,
                    "running": False,
                    "last_cycle_ms": None,
                    "next_cycle_ms": None,
                    "rows_total": 0,
                    "stations": [],
                },
            )

            await _enable(storage, True)
            vis, hid = "DL9VIS-1", "DL9HID-1"
            for c in (vis, hid):
                await _rf(storage, c, now - 3_600_000)
            await storage.set_wx_sidebar([], [hid])
            from .storage.telemetry_fill import FillRow

            def fr(ts: int) -> FillRow:
                cols: dict[str, Any] = dict.fromkeys(
                    (
                        "temp1",
                        "temp2",
                        "hum",
                        "qfe",
                        "qnh",
                        "alt",
                        "gas",
                        "co2",
                        "batt",
                        "extras_json",
                    )
                )
                cols["temp1"] = 5.0
                return FillRow(timestamp_ms=ts, **cols)

            await storage.insert_filled_telemetry(vis, [fr(now - 7_200_000), fr(now - 9_000_000)])
            await storage.insert_filled_telemetry(hid, [fr(now - 7_200_000)])
            await storage.set_fill_cursor(vis, now - 5_000_000)
            spy = _Spy(storage)
            svc = TelemetryFillService(
                storage=spy,
                is_blocked=lambda _c: False,
                transport=httpx.MockTransport(lambda _r: httpx.Response(500)),
                clock=lambda: now,
                sleep=_noop,
            )
            manager.telemetry_fill_service = svc
            body = client.get("/api/telemetry/fill").json()
            _check(
                results,
                "route: exact keys",
                set(body)
                == {
                    "enabled",
                    "running",
                    "last_cycle_ms",
                    "next_cycle_ms",
                    "rows_total",
                    "stations",
                },
            )
            _check(
                results,
                "route: visible station only, with cursor, rows count and null error",
                body["stations"]
                == [{"callsign": vis, "filled_until_ms": now - 5_000_000, "rows": 2, "error": None}]
                and body["rows_total"] == 2
                and body["enabled"] is True,
            )

            client.get("/api/telemetry/fill")
            client.get("/api/telemetry/fill")
            _check(
                results,
                "route: three polls scan telemetry once (cached station set and counts)",
                spy.counts == {"get_fill_row_counts": 1, "get_telemetry_stations": 1},
            )

            data = client.get("/api/telemetry?hours=48").json()
            by_origin = {(r["callsign"], r["timestamp"]): r["origin"] for r in data}
            _check(
                results,
                "telemetry: rows carry origin (null for RF, 'meshmap' for filled)",
                by_origin.get((vis, now - 3_600_000)) is None
                and (vis, now - 3_600_000) in by_origin
                and by_origin.get((vis, now - 7_200_000)) == "meshmap",
            )
        finally:
            await storage.close()


async def _off_loop(results: list[tuple[str, bool]]) -> None:  # noqa: PLR0915 - two race scenarios
    """Advisor round 2: CPU work off the event loop, and the cycle refresh under the lock."""
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "offloop.db")
        try:
            await _enable(storage, True)
            end = _NOW - FILL_SETTLE_MS

            # G. JSON parsing and select_fill_rows run in a worker thread: a capped
            #    5000-row chunk costs ~0.5 s of CPU on a Pi Zero, a loop_lag stall.
            call = "DL1THR-1"
            await _rf(storage, call, _RF_OLD)
            await storage.set_fill_cursor(call, end - 2 * 3600 * 1000)
            server = _Meshmap({call: [end - 3600 * 1000]})
            loop_thread = threading.get_ident()
            seen: dict[str, int] = {}
            real_select = select_fill_rows
            real_json = json.loads

            def _select(*a: Any, **kw: Any) -> Any:
                seen["select"] = threading.get_ident()
                return real_select(*a, **kw)

            def _loads(*a: Any, **kw: Any) -> Any:
                seen["json"] = threading.get_ident()
                return real_json(*a, **kw)

            with (
                patch("mcapp.telemetry_fill_service.select_fill_rows", _select),
                patch.object(json, "loads", _loads),
            ):
                await _service(storage, server, [], is_blocked=_only(call)).run_cycle()
            _check(
                results,
                "off-loop: response JSON is parsed in a worker thread",
                seen.get("json", loop_thread) != loop_thread,
            )
            _check(
                results,
                "off-loop: select_fill_rows runs in a worker thread",
                seen.get("select", loop_thread) != loop_thread,
            )

            # H. A cold-start route refresh that read its counts BEFORE the cycle's
            #    insert must not overwrite the cycle's fresh counts afterwards.
            call = "DL1RACE-1"
            await _rf(storage, call, _RF_OLD)
            await storage.set_fill_cursor(call, end - 2 * 3600 * 1000)
            server = _Meshmap({call: [end - 3600 * 1000]})
            svc = _service(storage, server, [], is_blocked=_only(call))
            reached = asyncio.Event()
            release = asyncio.Event()
            real_counts = storage.get_fill_row_counts
            gated = {"n": 0}

            async def _counts() -> dict[str, int]:
                value: dict[str, int] = await real_counts()
                gated["n"] += 1
                if gated["n"] == 1:  # only the route's cold refresh is held
                    reached.set()
                    await release.wait()
                return value

            with patch.object(storage, "get_fill_row_counts", _counts):
                route = asyncio.create_task(svc.route_payload())
                await reached.wait()
                cycle = asyncio.create_task(svc.run_cycle())
                await asyncio.wait({cycle}, timeout=1.0)
                release.set()
                await route
                await cycle
            body = await svc.route_payload()
            rows = {s["callsign"]: s["rows"] for s in body["stations"]}
            db_rows = len(await _filled(storage, call))
            _check(
                results,
                "cache: a cold route refresh racing the cycle keeps the post-insert count",
                db_rows > 0 and rows.get(call) == db_rows,
            )
        finally:
            await storage.close()


async def run_telemetry_fill_service_tests() -> bool:
    """Run the telemetry gap-fill service suite. Returns True iff all cases pass."""
    results: list[tuple[str, bool]] = []
    await _core(results)
    await _rework(results)
    await _run_loop(results)
    await _routes(results)
    await _off_loop(results)
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    telemetry_fill_service: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
