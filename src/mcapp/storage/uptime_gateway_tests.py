"""Regression suite for gateway-mode awareness in the uptime ledger (schema v40).

Contract: `doc/2026-11-06_0020-gateway-availability-fix.md`. A node with
gateway mode off never receives a hop-0 `{CET}` beacon, so without the `SN`
register's `GW` flag the ledger records one endless outage. Covers:

  1. `GW=false` closes the silence as a `gap` and opens `disabled`; repeats
     are no-ops; a same-millisecond open/close and a repeated start_ms never
     crash.
  2. `GW=true` stores the `disabled` row and resets the beacon anchors, so no
     gap spans the disabled stretch.
  3. A counted beacon while disabled overrides a stale `GW=false`.
  4. Startup `dark` reconcile while disabled.
  5. Reader: open `disabled` tail, `state`, `gateway_enabled`, stats
     (`disabled` is in neither uptime nor coverage), legacy NULL rows.
  6. Buckets: widths, contiguity, agreement with stats, exactness when the
     `segments` list is downsampled, null for short ranges.
  7. The register hook, driven through the REAL `ble_protocol.dispatcher`.

Ephemeral SQLite DB per scenario, explicit clocks (ms).
"""

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..ble_protocol import dispatcher
from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import DARK_THRESHOLD_MS, GAP_TOLERANCE_MS, LATEST_SCHEMA_VERSION

logger = get_logger(__name__)

_T = 1_790_000_000_000
_H = 3_600_000
_D = 24 * _H

Results = list[tuple[str, bool]]


async def _segs(storage: Any) -> list[tuple[int, int, str]]:
    rows = await storage._query(
        "SELECT start_ms, end_ms, kind FROM link_uptime_segments ORDER BY start_ms"
    )
    return [(r["start_ms"], r["end_ms"], r["kind"]) for r in rows]


async def _state(storage: Any) -> dict[str, Any]:
    rows = await storage._query("SELECT * FROM link_uptime_state WHERE id = 1")
    return dict(rows[0])


async def _test_disable_enable(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "a.db")
    try:
        await storage.record_link_beacon(_T)
        t_off = _T + GAP_TOLERANCE_MS + 60_000
        await storage.record_link_gateway_mode(False, t_off)
        st = await _state(storage)
        results.append(
            (
                "GW=false: silence beyond tolerance closed as gap ending now, disabled opens",
                await _segs(storage) == [(_T, t_off, "gap")]
                and st["disabled_since_ms"] == t_off
                and st["gateway_enabled"] == 0
                and st["last_beacon_ms"] == t_off
                and st["open_up_start_ms"] == t_off,
            )
        )
        await storage.record_link_gateway_mode(False, t_off + _H)
        st2 = await _state(storage)
        results.append(
            (
                "repeated GW=false is a no-op",
                await _segs(storage) == [(_T, t_off, "gap")] and st2["disabled_since_ms"] == t_off,
            )
        )
        t_on = t_off + 5 * _H
        await storage.record_link_gateway_mode(True, t_on)
        st3 = await _state(storage)
        results.append(
            (
                "GW=true stores disabled row, clears it, resets anchors",
                await _segs(storage) == [(_T, t_off, "gap"), (t_off, t_on, "disabled")]
                and st3["disabled_since_ms"] is None
                and st3["gateway_enabled"] == 1
                and st3["last_beacon_ms"] == t_on
                and st3["open_up_start_ms"] == t_on,
            )
        )
        await storage.record_link_gateway_mode(True, t_on + _H)
        results.append(
            ("repeated GW=true is a no-op", len(await _segs(storage)) == 2),
        )
    finally:
        await storage.close()


async def _test_within_tolerance_and_edges(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "b.db")
    try:
        await storage.record_link_beacon(_T)
        t_off = _T + 60_000
        await storage.record_link_gateway_mode(False, t_off)
        results.append(
            (
                "GW=false within tolerance writes no gap row",
                await _segs(storage) == []
                and (await _state(storage))["disabled_since_ms"] == t_off,
            )
        )
        # Same-millisecond open and close: no zero-length row, no crash.
        await storage.record_link_gateway_mode(True, t_off)
        results.append(
            ("disabled opened and closed in one ms stores nothing", await _segs(storage) == [])
        )
        # Repeated start_ms: a disabled row already sits at the start.
        await storage._mutate(
            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind) VALUES (?, ?, 'disabled')",
            (_T + 2 * _H, _T + 2 * _H + 10),
        )
        await storage.record_link_gateway_mode(False, _T + 2 * _H)
        ok = True
        try:
            await storage.record_link_gateway_mode(True, _T + 3 * _H)
        except Exception:
            ok = False
        segs = [x for x in await _segs(storage) if x[2] == "disabled"]
        results.append(
            (
                "repeated disabled start_ms extends instead of crashing",
                ok and segs == [(_T + 2 * _H, _T + 3 * _H, "disabled")],
            )
        )
    finally:
        await storage.close()


async def _test_no_gap_spans_disabled(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "c.db")
    try:
        await storage.record_link_beacon(_T)
        await storage.record_link_gateway_mode(False, _T + 60_000)
        await storage.record_link_gateway_mode(True, _T + _D)
        await storage.record_link_beacon(_T + _D + 60_000)
        kinds = [k for _, _, k in await _segs(storage)]
        res = await storage.get_link_uptime(2 * _D, now_ms_=_T + _D + 120_000)
        seg_kinds = [s["kind"] for s in res["segments"]]
        results.append(
            (
                "no gap row or segment spans a disabled stretch",
                "gap" not in kinds and "gap" not in seg_kinds and "disabled" in seg_kinds,
            )
        )
    finally:
        await storage.close()


async def _test_beacon_override(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "d.db")
    try:
        await storage.record_link_beacon(_T)
        await storage.record_link_gateway_mode(False, _T + 60_000)
        t_b = _T + 2 * _H
        await storage.record_link_beacon(t_b)
        st = await _state(storage)
        results.append(
            (
                "counted beacon while disabled closes disabled and sets gateway_enabled",
                await _segs(storage) == [(_T + 60_000, t_b, "disabled")]
                and st["disabled_since_ms"] is None
                and st["gateway_enabled"] == 1
                and st["last_beacon_ms"] == t_b
                and st["open_up_start_ms"] == t_b,
            )
        )
    finally:
        await storage.close()


async def _test_reconcile_disabled(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "e.db")
    try:
        await storage.record_link_beacon(_T)
        await storage.record_link_gateway_mode(False, _T + 60_000)
        t_tick = _T + _H
        await storage.link_uptime_tick(t_tick)
        t_start = t_tick + DARK_THRESHOLD_MS + 5_000
        await storage.reconcile_link_uptime_startup(t_start)
        st = await _state(storage)
        results.append(
            (
                "startup reconcile while disabled: disabled to tick, dark to now, disabled reopens",
                await _segs(storage)
                == [(_T + 60_000, t_tick, "disabled"), (t_tick, t_start, "dark")]
                and st["disabled_since_ms"] == t_start,
            )
        )
    finally:
        await storage.close()


async def _test_reader(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "f.db")
    try:
        await storage.record_link_beacon(_T)
        await storage.record_link_gateway_mode(False, _T + _H)
        # open disabled tail, no gap tail
        res = await storage.get_link_uptime(2 * _H, now_ms_=_T + 2 * _H)
        last = res["segments"][-1]
        results.append(
            (
                "open disabled tail rendered as disabled, state disabled, gateway_enabled false",
                last["kind"] == "disabled"
                and last["end_ms"] == _T + 2 * _H
                and res["state"] == "disabled"
                and res["gateway_enabled"] is False
                and all(s["kind"] != "gap" or s["end_ms"] <= _T + _H for s in res["segments"]),
            )
        )
        await storage.record_link_gateway_mode(True, _T + 4 * _H)
        await storage.record_link_beacon(_T + 4 * _H + 300_000)
        # window 5h ending 10 min after enable: dark 50m, gap 1h, disabled 3h, up 10m
        now = _T + 4 * _H + 600_000
        res = await storage.get_link_uptime(5 * _H, now_ms_=now)
        up, gap, dark, dis = 600_000, _H, 3_000_000, 3 * _H
        obs = up + gap
        segs = res["segments"]

        def total(kind: str) -> int:
            return sum(s["end_ms"] - s["start_ms"] for s in segs if s["kind"] == kind)

        results.append(
            (
                "stats: disabled in neither uptime nor coverage (literal numbers)",
                (total("up"), total("gap"), total("dark"), total("disabled"))
                == (up, gap, dark, dis)
                and abs(res["uptime_pct"] - 100.0 * up / obs) < 1e-9
                and abs(res["coverage_pct"] - 100.0 * obs / (5 * _H)) < 1e-9
                and res["longest_outage_ms"] == _H
                and res["state"] == "active"
                and res["gateway_enabled"] is True,
            )
        )
    finally:
        await storage.close()


async def _test_legacy_row(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "g.db")
    try:
        await storage.record_link_beacon(_T)
        await storage.record_link_beacon(_T + 60_000)
        st = await _state(storage)
        res = await storage.get_link_uptime(_H, now_ms_=_T + 120_000)
        results.append(
            (
                "legacy row (new columns NULL) behaves as before",
                st["gateway_enabled"] is None
                and st["disabled_since_ms"] is None
                and res["state"] == "active"
                and res["gateway_enabled"] is None
                and [s["kind"] for s in res["segments"]] == ["dark", "up"],
            )
        )
        results.append(
            ("LATEST_SCHEMA_VERSION is 40", LATEST_SCHEMA_VERSION == 40),
        )
    finally:
        await storage.close()


async def _test_buckets(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "h.db")
    try:
        d30 = 30 * _D
        now = _T + 40 * _D
        start = now - d30
        await storage.record_link_beacon(start - _D)  # observed before the window
        # one gap inside bucket 0 (1h), one straddling buckets 1/2 (2h, 1h per side)
        gap1 = (start + _H, start + 2 * _H)
        gap2 = (start + 6 * _H * 2 - _H, start + 6 * _H * 2 + _H)
        await storage._mutate(
            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind) VALUES (?, ?, 'gap')",
            gap1,
        )
        await storage._mutate(
            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind) VALUES (?, ?, 'gap')",
            gap2,
        )
        await storage._mutate(
            "UPDATE link_uptime_state SET last_beacon_ms = ? WHERE id = 1", (now - 60_000,)
        )
        res = await storage.get_link_uptime(d30, now_ms_=now)
        b = res["buckets"]
        width_ok = all(
            x["up_ms"] + x["gap_ms"] + x["dark_ms"] + x["disabled_ms"]
            == x["end_ms"] - x["start_ms"]
            for x in b
        )
        contiguous = (
            b[0]["start_ms"] == start
            and b[-1]["end_ms"] == now
            and all(b[i]["end_ms"] == b[i + 1]["start_ms"] for i in range(len(b) - 1))
        )
        up = sum(x["up_ms"] for x in b)
        gap = sum(x["gap_ms"] for x in b)
        stats_ok = (
            abs(res["uptime_pct"] - 100.0 * up / (up + gap)) < 1e-9
            and abs(res["coverage_pct"] - 100.0 * (up + gap) / d30) < 1e-9
        )
        results.append(
            ("30d buckets: 120 x 6h", len(b) == 120 and b[0]["end_ms"] - b[0]["start_ms"] == 6 * _H)
        )
        results.append(("buckets sum to their width", width_ok))
        results.append(("buckets are contiguous over the window", contiguous))
        results.append(("buckets agree with the stats", stats_ok))
        results.append(
            (
                "longest_gap_ms is the full gap in every bucket it touches",
                b[0]["longest_gap_ms"] == _H
                and b[1]["longest_gap_ms"] == 2 * _H
                and b[2]["longest_gap_ms"] == 2 * _H
                and b[3]["longest_gap_ms"] == 0
                and gap == 3 * _H,
            )
        )
        r1y = await storage.get_link_uptime(365 * _D, now_ms_=now)
        results.append(
            (
                "1y buckets are 1 day",
                len(r1y["buckets"]) == 365
                and r1y["buckets"][0]["end_ms"] - r1y["buckets"][0]["start_ms"] == _D,
            )
        )
        r24 = await storage.get_link_uptime(_D, now_ms_=now)
        r7 = await storage.get_link_uptime(7 * _D, now_ms_=now)
        results.append(
            ("buckets null for 24h and 7d", r24["buckets"] is None and r7["buckets"] is None)
        )
    finally:
        await storage.close()


async def _test_longest_gap_is_the_full_outage(results: Results, tmp: Path) -> None:
    """A 3-day outage over 6 h buckets: every bucket reports 3 days, so merging runs
    with max stays exact instead of claiming a 6 h longest outage."""
    storage = await create_sqlite_storage(tmp / "longest.db")
    try:
        await storage.record_link_beacon(_T)
        await storage.record_link_beacon(_T + 3 * _D)
        res = await storage.get_link_uptime(30 * _D, now_ms_=_T + 3 * _D + 60_000)
        touched = [b for b in res["buckets"] if b["gap_ms"] > 0]
        results.append(
            (
                "a bucket reports the full length of the outage it touches",
                len(touched) > 2 and all(b["longest_gap_ms"] == 3 * _D for b in touched),
            )
        )
    finally:
        await storage.close()


async def _test_reconcile_keeps_prior_silence(results: Results, tmp: Path) -> None:
    """A restart resets the beacon anchors, so silence the proxy watched before it went
    down must be stored first, or it reads as `up`."""
    never = await create_sqlite_storage(tmp / "n1.db")
    try:
        await never.reconcile_link_uptime_startup(_T)
        await never.link_uptime_tick(_T + 5 * _H)
        await never.reconcile_link_uptime_startup(_T + 5 * _H + 600_000)
        results.append(
            (
                "restart on a never-beaconed box keeps its silence as a gap",
                await _segs(never)
                == [(_T, _T + 5 * _H, "gap"), (_T + 5 * _H, _T + 5 * _H + 600_000, "dark")],
            )
        )
    finally:
        await never.close()
    beaconed = await create_sqlite_storage(tmp / "n2.db")
    try:
        await beaconed.record_link_beacon(_T)
        await beaconed.link_uptime_tick(_T + 5 * _H)
        await beaconed.reconcile_link_uptime_startup(_T + 5 * _H + 600_000)
        results.append(
            (
                "restart after a watched 5 h outage keeps it as a gap",
                await _segs(beaconed)
                == [(_T, _T + 5 * _H, "gap"), (_T + 5 * _H, _T + 5 * _H + 600_000, "dark")],
            )
        )
    finally:
        await beaconed.close()
    healthy = await create_sqlite_storage(tmp / "n3.db")
    try:
        await healthy.record_link_beacon(_T)
        await healthy.link_uptime_tick(_T + 300_000)
        await healthy.reconcile_link_uptime_startup(_T + 300_000 + 600_000)
        results.append(
            (
                "a deploy restart on a healthy link stores dark only, never a gap",
                await _segs(healthy) == [(_T + 300_000, _T + 900_000, "dark")],
            )
        )
    finally:
        await healthy.close()


async def _test_buckets_exact_when_downsampled(results: Results, tmp: Path) -> None:
    storage = await create_sqlite_storage(tmp / "i.db")
    try:
        d30 = 30 * _D
        now = _T + 40 * _D
        start = now - d30
        await storage.record_link_beacon(start - _D)
        n = 600
        gap_len = 20 * 60_000
        for i in range(n):
            s = start + _H + i * _H
            await storage._mutate(
                "INSERT INTO link_uptime_segments (start_ms, end_ms, kind) VALUES (?, ?, 'gap')",
                (s, s + gap_len),
            )
        await storage._mutate(
            "UPDATE link_uptime_state SET last_beacon_ms = ? WHERE id = 1", (now - 60_000,)
        )
        res = await storage.get_link_uptime(d30, now_ms_=now)
        b = res["buckets"]
        results.append(
            (
                "segments downsampled to <= 500 while buckets stay exact",
                len(res["segments"]) <= 500
                and sum(x["gap_ms"] for x in b) == n * gap_len
                and all(x["longest_gap_ms"] in (0, gap_len) for x in b)
                and b[0]["gap_ms"] == 5 * gap_len,
            )
        )
    finally:
        await storage.close()


async def _test_hook(results: Results, tmp: Path) -> None:
    from ..main import _record_register_gateway_mode

    storage = await create_sqlite_storage(tmp / "j.db")
    try:
        await storage.record_link_beacon(_T)
        off = dispatcher({"TYP": "SN", "GW": False, "CALL": "DK5EN-98"})
        on = dispatcher({"TYP": "SN", "GW": True, "CALL": "DK5EN-98"})
        results.append(
            (
                "real dispatcher keeps GW through to the hook",
                off is not None
                and off.get("GW") is False
                and on is not None
                and on.get("GW") is True,
            )
        )
        assert off is not None
        await _record_register_gateway_mode(storage, off)
        st = await _state(storage)
        results.append(("SN GW=false reaches the ledger", st["gateway_enabled"] == 0))
        await _record_register_gateway_mode(storage, {"TYP": "SN", "GW": 1})
        await _record_register_gateway_mode(storage, {"TYP": "SN", "GW": "true"})
        await _record_register_gateway_mode(storage, {"TYP": "SN"})
        await _record_register_gateway_mode(storage, {"TYP": "SN", "GW": None})
        await _record_register_gateway_mode(storage, {"TYP": "SN1", "GW": True})
        st = await _state(storage)
        results.append(
            (
                "non-bool, absent and wrong-TYP GW are ignored",
                st["gateway_enabled"] == 0 and st["disabled_since_ms"] is not None,
            )
        )
        assert on is not None
        await _record_register_gateway_mode(storage, on)
        st = await _state(storage)
        results.append(("SN GW=true re-enables", st["gateway_enabled"] == 1))

        class _Broken:
            async def record_link_gateway_mode(
                self, enabled: bool, now_ms_: int, *, seed_only: bool = False
            ) -> None:
                raise RuntimeError("db down")

        raised = False
        try:
            await _record_register_gateway_mode(_Broken(), off)  # type: ignore[arg-type]  # deliberately wrong type
        except Exception:
            raised = True
        results.append(("a raising ledger write never propagates", not raised))
    finally:
        await storage.close()


async def _test_replay_seeds_only(results: Results, tmp: Path) -> None:
    """R1: ble_service's age-less cached `SN` is replayed on every SSE attach
    through the REAL `BLEClientRemote` + `MessageRouter` + `_wire_ble_caches`
    hook. It may seed an unknown mode but never change a known one; live frames
    stay authoritative.
    """
    import json

    from .. import main as main_module
    from ..ble_client_remote import BLEClientRemote

    clock = [_T]
    saved_now = main_module.__dict__["now_ms"]
    setattr(main_module, "now_ms", lambda: clock[0])  # noqa: B010 - deliberate monkeypatch
    storage = await create_sqlite_storage(tmp / "k.db")
    try:
        router = main_module.MessageRouter(storage)
        main_module._wire_ble_caches(router)
        client = BLEClientRemote("http://127.0.0.1:9", message_router=router)
        cached: dict[str, Any] = {"registers": {"SN": {"TYP": "SN", "GW": False}}, "count": 1}

        async def _fake_request(*_a: Any, **_k: Any) -> dict[str, Any]:
            return cached

        client._request = _fake_request  # type: ignore[method-assign]  # canned ble_service reply

        async def live(gw: bool) -> None:
            await client._handle_notification(
                json.dumps(
                    {"format": "json", "parsed": {"TYP": "SN", "GW": gw}, "timestamp": clock[0]}
                )
            )

        # Known-enabled ledger (a live GW=true), then a STALE cached GW=false.
        await live(True)
        await storage.record_link_beacon(_T)
        clock[0] = _T + 60_000
        await client._replay_cached_registers()
        st1 = await _state(storage)
        await storage.record_link_beacon(_T + 606_500)
        clock[0] = _T + 700_000
        await client._replay_cached_registers()
        st2 = await _state(storage)
        res = await storage.get_link_uptime(_D, now_ms_=_T + 700_001)
        results.append(
            (
                "replayed stale GW=false on a known-enabled ledger changes nothing (no flap)",
                st1["gateway_enabled"] == 1
                and st1["disabled_since_ms"] is None
                and st2["gateway_enabled"] == 1
                and st2["disabled_since_ms"] is None
                and await _segs(storage) == []
                and res["state"] == "active"
                and res["gateway_enabled"] is True,
            )
        )
        # A live frame is authoritative again right after a replay.
        clock[0] = _T + 800_000
        await live(False)
        st3 = await _state(storage)
        results.append(
            (
                "a live GW=false after a replay still disables (marker does not leak)",
                st3["gateway_enabled"] == 0 and st3["disabled_since_ms"] == _T + 800_000,
            )
        )
        # Known-disabled ledger: a stale replayed GW=true must not re-enable.
        cached["registers"] = {"SN": {"TYP": "SN", "GW": True}}
        clock[0] = _T + 900_000
        await client._replay_cached_registers()
        st4 = await _state(storage)
        results.append(
            (
                "replayed GW=true on a known-disabled ledger changes nothing",
                st4["gateway_enabled"] == 0
                and st4["disabled_since_ms"] == _T + 800_000
                and await _segs(storage) == [],
            )
        )
    finally:
        setattr(main_module, "now_ms", saved_now)  # noqa: B010 - deliberate monkeypatch
        await storage.close()


async def _test_replay_seeds_unknown(results: Results, tmp: Path) -> None:
    """R1: on a NULL ledger the replayed `SN` seeds the mode."""
    from .. import main as main_module
    from ..ble_client_remote import BLEClientRemote

    clock = [_T + 60_000]
    saved_now = main_module.__dict__["now_ms"]
    setattr(main_module, "now_ms", lambda: clock[0])  # noqa: B010 - deliberate monkeypatch
    storage = await create_sqlite_storage(tmp / "k2.db")
    try:
        router = main_module.MessageRouter(storage)
        main_module._wire_ble_caches(router)
        client = BLEClientRemote("http://127.0.0.1:9", message_router=router)

        async def _fake_request(*_a: Any, **_k: Any) -> dict[str, Any]:
            return {"registers": {"SN": {"TYP": "SN", "GW": False}}, "count": 1}

        client._request = _fake_request  # type: ignore[method-assign]  # canned ble_service reply
        await client._replay_cached_registers()
        st = await _state(storage)
        results.append(
            (
                "replayed SN seeds an unknown (NULL) ledger",
                st["gateway_enabled"] == 0 and st["disabled_since_ms"] == _T + 60_000,
            )
        )

        # A fresh beacon outranks a stale cached GW=false on a still-unknown ledger.
        beaconed = await create_sqlite_storage(tmp / "k3.db")
        try:
            router_b = main_module.MessageRouter(beaconed)
            main_module._wire_ble_caches(router_b)
            client_b = BLEClientRemote("http://127.0.0.1:9", message_router=router_b)
            client_b._request = _fake_request  # type: ignore[method-assign]  # canned reply
            await beaconed.record_link_beacon(_T)
            await client_b._replay_cached_registers()
            fresh = await _state(beaconed)
            results.append(
                (
                    "a replayed GW=false cannot disable while beacons are fresh",
                    fresh["disabled_since_ms"] is None,
                )
            )
        finally:
            await beaconed.close()
    finally:
        setattr(main_module, "now_ms", saved_now)  # noqa: B010 - deliberate monkeypatch
        await storage.close()


async def _test_hook_through_router(results: Results, tmp: Path) -> None:
    """R4: the `_wire_ble_caches` subscriber itself (not the helper) feeds the
    ledger when an `SN` frame is published on `ble_notification`."""
    from .. import main as main_module

    storage = await create_sqlite_storage(tmp / "l.db")
    try:
        router = main_module.MessageRouter(storage)
        main_module._wire_ble_caches(router)
        await router.publish("ble", "ble_notification", {"TYP": "SN", "GW": False})
        st = await _state(storage)
        cached = router.cached_ble_registers.get("SN")
        results.append(
            (
                "router subscriber: published SN GW=false reaches the ledger and the cache",
                st["gateway_enabled"] == 0
                and st["disabled_since_ms"] is not None
                and cached is not None
                and "GW" in cached
                and set(cached) == {"TYP", "GW"},
            )
        )
    finally:
        await storage.close()


async def _test_never_beaconed_keeps_silence(results: Results, tmp: Path) -> None:
    """R2: a box that never heard a beacon must keep its pre-disabled silence."""
    storage = await create_sqlite_storage(tmp / "m.db")
    try:
        await storage.reconcile_link_uptime_startup(_T)
        await storage.record_link_gateway_mode(False, _T + 5 * _H)
        await storage.record_link_gateway_mode(True, _T + 6 * _H)
        await storage.record_link_beacon(_T + 6 * _H + 60_000)
        res = await storage.get_link_uptime(_D, now_ms_=_T + 6 * _H + 120_000)
        gap = [s for s in res["segments"] if s["kind"] == "gap"]
        results.append(
            (
                "never-beaconed GW=false stores the 5h silence as a gap, not up",
                await _segs(storage)
                == [(_T, _T + 5 * _H, "gap"), (_T + 5 * _H, _T + 6 * _H, "disabled")]
                and len(gap) == 1
                and gap[0]["end_ms"] - gap[0]["start_ms"] == 5 * _H
                and res["longest_outage_ms"] == 5 * _H
                and res["uptime_pct"] is not None
                and res["uptime_pct"] < 20.0,
            )
        )
        # Within tolerance the silence is stored dark (what the reader showed).
        s2 = await create_sqlite_storage(tmp / "m2.db")
        try:
            await s2.reconcile_link_uptime_startup(_T)
            await s2.record_link_gateway_mode(False, _T + 60_000)
            await s2.record_link_gateway_mode(True, _T + _H)
            results.append(
                (
                    "never-beaconed GW=false within tolerance stores dark",
                    await _segs(s2)
                    == [(_T, _T + 60_000, "dark"), (_T + 60_000, _T + _H, "disabled")],
                )
            )
        finally:
            await s2.close()
        # Beacon override path keeps it too.
        s3 = await create_sqlite_storage(tmp / "m3.db")
        try:
            await s3.reconcile_link_uptime_startup(_T)
            await s3.record_link_gateway_mode(False, _T + 5 * _H)
            await s3.record_link_beacon(_T + 6 * _H)
            results.append(
                (
                    "never-beaconed beacon override keeps the silence",
                    await _segs(s3)
                    == [(_T, _T + 5 * _H, "gap"), (_T + 5 * _H, _T + 6 * _H, "disabled")],
                )
            )
            # Same shape written by an older build: disabled, last_beacon NULL.
            await s3._mutate(
                "DELETE FROM link_uptime_segments WHERE 1 = 1",
            )
            await s3._mutate(
                "UPDATE link_uptime_state SET last_beacon_ms = NULL,"
                " disabled_since_ms = ?, gateway_enabled = 0 WHERE id = 1",
                (_T + 5 * _H,),
            )
            await s3.record_link_beacon(_T + 6 * _H)
            results.append(
                (
                    "beacon override with a NULL anchor stores the pre-disabled silence",
                    await _segs(s3)
                    == [(_T, _T + 5 * _H, "gap"), (_T + 5 * _H, _T + 6 * _H, "disabled")],
                )
            )
        finally:
            await s3.close()
        # First beacon after a long silence on a never-beaconed, enabled box.
        s4 = await create_sqlite_storage(tmp / "m4.db")
        try:
            await s4.reconcile_link_uptime_startup(_T)
            await s4.record_link_beacon(_T + 5 * _H)
            results.append(
                (
                    "first beacon after a long silence stores the gap",
                    await _segs(s4) == [(_T, _T + 5 * _H, "gap")],
                )
            )
        finally:
            await s4.close()
    finally:
        await storage.close()


async def _test_reconcile_after_closed_rows(results: Results, tmp: Path) -> None:
    """R7: a `disabled` or `gap` row closed after the last heartbeat and before
    a crash must not be overlapped by the startup `dark` row (a bucket would
    sum above its width)."""
    shapes: list[tuple[str, Callable[[Any], Awaitable[None]]]] = []

    async def disabled_shape(st: Any) -> None:
        await st.record_link_beacon(_T)
        await st.link_uptime_tick(_T)
        await st.record_link_gateway_mode(False, _T + 1000)
        await st.link_uptime_tick(_T + 30 * _H)
        await st.record_link_gateway_mode(True, _T + 30 * _H + 25_000)

    async def gap_shape(st: Any) -> None:
        await st.record_link_beacon(_T)
        await st.link_uptime_tick(_T + _H)
        await st.record_link_gateway_mode(False, _T + 2 * _H)

    shapes.append(("disabled", disabled_shape))
    shapes.append(("gap", gap_shape))
    for name, build in shapes:
        storage = await create_sqlite_storage(tmp / f"n_{name}.db")
        try:
            await build(storage)
            await storage.reconcile_link_uptime_startup(_T + 40 * _H)
            # a crash loop must stay safe: reconcile again at the same instant
            await storage.reconcile_link_uptime_startup(_T + 41 * _H)
            rows = await _segs(storage)
            no_overlap = all(rows[i][1] <= rows[i + 1][0] for i in range(len(rows) - 1))
            res = await storage.get_link_uptime(30 * _D, now_ms_=_T + 42 * _H)
            bad = [
                b
                for b in res["buckets"]
                if b["up_ms"] + b["gap_ms"] + b["dark_ms"] + b["disabled_ms"]
                != b["end_ms"] - b["start_ms"]
            ]
            results.append(
                (
                    f"reconcile after a closed {name} row: no overlap, buckets sum to width",
                    no_overlap and not bad and any(r[2] == "dark" for r in rows),
                )
            )
        finally:
            await storage.close()


async def _test_reader_never_beaconed_disabled(results: Results, tmp: Path) -> None:
    """R4: the reader's NULL-anchor tail while disabled splits on the DISABLE
    instant, not on `now` (a disable within tolerance reads `dark`)."""
    storage = await create_sqlite_storage(tmp / "o.db")
    try:
        await storage.reconcile_link_uptime_startup(_T)
        await storage._mutate(
            "UPDATE link_uptime_state SET disabled_since_ms = ?, gateway_enabled = 0 WHERE id = 1",
            (_T + 300_000,),
        )
        res = await storage.get_link_uptime(_D, now_ms_=_T + 2 * _H)
        kinds = [(s["start_ms"], s["end_ms"], s["kind"]) for s in res["segments"]]
        results.append(
            (
                "reader: NULL-anchor tail before a disable within tolerance is dark",
                (_T, _T + 300_000, "dark") in kinds
                and (_T + 300_000, _T + 2 * _H, "disabled") in kinds
                and all(k[2] != "gap" for k in kinds),
            )
        )
    finally:
        await storage.close()


async def _test_downsample_keeps_disabled(results: Results, tmp: Path) -> None:
    """R4: a >500-segment window containing a disabled row downsamples without
    losing it (`disabled` ranks between `dark` and `gap`)."""
    storage = await create_sqlite_storage(tmp / "p.db")
    try:
        d30 = 30 * _D
        now = _T + 40 * _D
        start = now - d30
        await storage.record_link_beacon(start - _D)
        for i in range(600):
            s = start + _H + i * _H
            await storage._mutate(
                "INSERT INTO link_uptime_segments (start_ms, end_ms, kind) VALUES (?, ?, 'gap')",
                (s, s + 20 * 60_000),
            )
        dis_start = start + 26 * _D
        await storage._mutate(
            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind) VALUES (?, ?, 'disabled')",
            (dis_start, dis_start + 2 * _H),
        )
        await storage._mutate(
            "UPDATE link_uptime_state SET last_beacon_ms = ? WHERE id = 1", (now - 60_000,)
        )
        res = await storage.get_link_uptime(d30, now_ms_=now)
        segs = res["segments"]
        dis = [s for s in segs if s["kind"] == "disabled"]
        total_dis = sum(x["disabled_ms"] for x in res["buckets"])
        results.append(
            (
                "downsample with a disabled row: <= 500 segments, disabled kept, buckets exact",
                len(segs) <= 500
                and len(dis) >= 1
                and any(s["start_ms"] <= dis_start < s["end_ms"] for s in dis)
                and total_dis == 2 * _H,
            )
        )
    finally:
        await storage.close()


async def run_uptime_gateway_tests() -> bool:
    """Run the gateway-mode uptime suite. Returns True iff all pass."""
    results: Results = []
    scenarios: list[Callable[[Results, Path], Awaitable[None]]] = [
        _test_disable_enable,
        _test_within_tolerance_and_edges,
        _test_no_gap_spans_disabled,
        _test_beacon_override,
        _test_reconcile_disabled,
        _test_reader,
        _test_legacy_row,
        _test_buckets,
        _test_buckets_exact_when_downsampled,
        _test_hook,
        _test_replay_seeds_only,
        _test_replay_seeds_unknown,
        _test_hook_through_router,
        _test_never_beaconed_keeps_silence,
        _test_reconcile_after_closed_rows,
        _test_reader_never_beaconed_disabled,
        _test_downsample_keeps_disabled,
        _test_longest_gap_is_the_full_outage,
        _test_reconcile_keeps_prior_silence,
    ]
    with tempfile.TemporaryDirectory() as tmp_dir:
        for scenario in scenarios:
            try:
                await scenario(results, Path(tmp_dir))
            except Exception as exc:
                results.append((f"{scenario.__name__} raised {type(exc).__name__}: {exc}", False))

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    uptime_gateway: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
