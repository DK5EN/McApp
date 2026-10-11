"""Suite for `InternetPositionCache` (pure CPU, no I/O).

P1  reset replaces the whole cache and always returns a reset payload.
P2  batch upserts + removed calls.
P3  an unchanged upsert is not re-emitted; nothing changed -> None.
P4  validation drops bad rows silently.
P5  unknown keys are stripped.
P6  blocklist: blocked never enters; newly blocked removed on next apply.
P7  TTL expiry is reported in removed.
P8  bound evicts the oldest lastSeenUnix.
P9  snapshot / clear shapes.
P10 a stale replay (older lastSeenUnix) never overwrites a newer cached row.
P11 snapshot() sweeps expired rows (TTL on the replay path, rows leave RAM).
P12 the blocked provider is read once per apply / sweep, not per row.
P13 returned payloads never alias the live cache (apply / snapshot / reset).
"""

from collections.abc import Callable
from typing import Any

from .internet_positions import InternetPositionCache
from .logging_setup import get_logger

logger = get_logger(__name__)

NOW_S = 1_791_500_000


def _pos(call: str, seen: int = NOW_S, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"call": call, "lat": 48.1, "lon": 11.5, "lastSeenUnix": seen}
    d.update(kw)
    return d


def _cache(
    blocked: set[str] | None = None, **kw: Any
) -> tuple[InternetPositionCache, dict[str, int], set[str]]:
    clock = {"ms": NOW_S * 1000}
    block: set[str] = blocked if blocked is not None else set()
    c = InternetPositionCache(blocked=lambda: block, clock_ms=lambda: clock["ms"], **kw)
    return c, clock, block


def _calls(payload: dict[str, Any] | None, key: str) -> list[str]:
    if payload is None:
        return []
    return sorted(u["call"] if isinstance(u, dict) else u for u in payload[key])


def _p1() -> bool:
    c, _, _ = _cache()
    c.apply({"reset": True, "positions": [_pos("A1"), _pos("B2")]})
    out = c.apply({"reset": True, "positions": [_pos("C3")]})
    return (
        out is not None
        and out["reset"] is True
        and _calls(out, "upserts") == ["C3"]
        and out["removed"] == []
        and len(c) == 1
    )


def _p2() -> bool:
    c, _, _ = _cache()
    c.apply({"reset": True, "positions": [_pos("A1"), _pos("B2")]})
    out = c.apply({"positions": [_pos("c3"), _pos("A1", NOW_S + 5)], "removed": ["b2"]})
    return (
        out is not None
        and "reset" not in out
        and _calls(out, "upserts") == ["A1", "C3"]
        and out["removed"] == ["B2"]
        and len(c) == 2
    )


def _p3() -> bool:
    c, _, _ = _cache()
    c.apply({"positions": [_pos("A1")]})
    same = c.apply({"positions": [_pos("A1")]})
    changed = c.apply({"positions": [_pos("A1", hardware="X")]})
    return (
        same is None
        and changed is not None
        and _calls(changed, "upserts") == ["A1"]
        and c.apply({"positions": [], "removed": ["NOPE"]}) is None
    )


def _p4() -> bool:
    c, _, _ = _cache()
    bad: list[Any] = [
        _pos("A1", lat=91),
        _pos("A2", lon=-181),
        _pos("A3", lat=0, lon=0),
        _pos("A4", lat=float("nan")),
        _pos("A5", lon=float("inf")),
        _pos(""),
        {"lat": 1.0, "lon": 1.0, "lastSeenUnix": NOW_S},
        _pos("A6", lastSeenUnix=1.5),
        _pos("A7", lastSeenUnix="1791500000"),
        _pos("A8", lat="48"),
        "junk",
    ]
    out = c.apply({"positions": [*bad, _pos("OK")]})
    return out is not None and _calls(out, "upserts") == ["OK"] and len(c) == 1


def _p5() -> bool:
    c, _, _ = _cache()
    out = c.apply({"positions": [_pos("A1", evil="x", altitudeM=300, isGateway=True)]})
    if out is None:
        return False
    keys = set(out["upserts"][0])
    return keys == {"call", "lat", "lon", "lastSeenUnix", "altitudeM", "isGateway"}


def _p6() -> bool:
    c, _, block = _cache({"BAD"})
    out = c.apply({"positions": [_pos("BAD"), _pos("bad-7"), _pos("GOOD")]})
    ok = out is not None and _calls(out, "upserts") == ["GOOD"]
    c.apply({"positions": [_pos("LATER-1")]})
    block.add("LATER-1")  # exact-SSID block
    out2 = c.apply({"positions": []})
    ok = ok and out2 is not None and out2["removed"] == ["LATER-1"] and len(c) == 1
    c.apply({"positions": [_pos("BASE-3")]})
    block.add("BASE")  # base-call block covers the SSID
    out3 = c.apply({"positions": []})
    return ok and out3 is not None and out3["removed"] == ["BASE-3"]


def _p7() -> bool:
    c, clock, _ = _cache(ttl_ms=3600 * 1000)
    c.apply({"positions": [_pos("OLD", NOW_S - 100), _pos("NEW", NOW_S)]})
    clock["ms"] += 3600 * 1000 - 50_000  # OLD now 3650 s... boundary check below
    out = c.apply({"positions": []})
    ok = out is not None and out["removed"] == ["OLD"] and len(c) == 1
    # an already-expired incoming row never enters
    out2 = c.apply({"positions": [_pos("ANCIENT", NOW_S - 99999)]})
    return ok and out2 is None and len(c) == 1


def _p8() -> bool:
    c, _, _ = _cache(max_entries=2)
    c.apply({"positions": [_pos("A1", NOW_S - 30), _pos("B2", NOW_S - 20)]})
    out = c.apply({"positions": [_pos("C3", NOW_S - 10)]})
    ok = out is not None and out["removed"] == ["A1"] and _calls(out, "upserts") == ["C3"]
    # a batch entry that is itself the oldest is neither upserted nor "removed"
    out2 = c.apply({"positions": [_pos("D4", NOW_S - 99)]})
    return ok and out2 is None and len(c) == 2


def _p9() -> bool:
    c, _, _ = _cache()
    c.apply({"positions": [_pos("A1")]})
    snap = c.snapshot()
    ok = snap["reset"] is True and _calls(snap, "upserts") == ["A1"] and snap["removed"] == []
    cl = c.clear()
    return ok and cl == {"reset": True, "upserts": [], "removed": []} and len(c) == 0


def _p10() -> bool:
    c, _, _ = _cache()
    c.apply({"positions": [_pos("A1", NOW_S, hardware="NEW")]})
    out = c.apply({"positions": [_pos("A1", NOW_S - 60, hardware="OLD")]})
    snap = c.snapshot()
    ok = out is None and snap["upserts"][0]["hardware"] == "NEW"
    # an equal timestamp is not stale: it replaces
    out2 = c.apply({"positions": [_pos("A1", NOW_S, hardware="SAME_TS")]})
    return ok and out2 is not None and c.snapshot()["upserts"][0]["hardware"] == "SAME_TS"


def _p11() -> bool:
    c, clock, _ = _cache(ttl_ms=3600 * 1000)
    c.apply({"positions": [_pos("OLD", NOW_S - 100), _pos("NEW", NOW_S)]})
    clock["ms"] += 3600 * 1000 - 50_000  # OLD is now past the ttl, NEW is not; no apply runs
    snap = c.snapshot()
    ok = _calls(snap, "upserts") == ["NEW"]
    return ok and len(c) == 1  # swept from RAM, not just filtered from the payload


def _p12() -> bool:
    calls: list[int] = []
    block = {"BAD"}

    def provider() -> set[str]:
        calls.append(1)
        return block

    clock = {"ms": NOW_S * 1000}
    c = InternetPositionCache(blocked=provider, clock_ms=lambda: clock["ms"])
    c.apply({"positions": [_pos(f"S{i}") for i in range(50)]})
    ok = len(calls) == 1
    c.apply({"reset": True, "positions": [_pos(f"T{i}") for i in range(50)], "removed": ["X"]})
    ok = ok and len(calls) == 2
    c.snapshot()
    ok = ok and len(calls) == 3
    # base-call rule through the provider: a base entry blocks every SSID,
    # an exact-SSID entry blocks only that SSID
    block.update({"BASE", "EXACT-1"})
    out = c.apply({"positions": [_pos("BASE-1"), _pos("BASE-2"), _pos("EXACT-1"), _pos("EXACT-2")]})
    return ok and out is not None and _calls(out, "upserts") == ["EXACT-2"]


def _p13() -> bool:
    c, _, _ = _cache()
    first = c.apply({"positions": [_pos("A1", hardware="V1"), _pos("B2")]})
    snap = c.snapshot()
    snap_rows = snap["upserts"]
    frozen = [dict(r) for r in snap_rows]
    ok = first is not None and all(r is not live for r in snap_rows for live in c._cache.values())
    # mutate the cache afterwards; earlier payloads must not move
    c.apply({"positions": [_pos("A1", NOW_S + 5, hardware="V2")], "removed": ["B2"]})
    c.apply({"reset": True, "positions": [_pos("C3")]})
    ok = ok and snap_rows == frozen and len(snap_rows) == 2 and snap["removed"] == []
    # mutating a returned payload must not reach the cache either
    out = c.snapshot()
    out["upserts"][0]["lat"] = 1.0
    out["upserts"].append({"call": "EVIL"})
    reset_out = c.apply({"reset": True, "positions": [_pos("D4")]})
    batch_out = c.apply({"positions": [_pos("E5")]})
    if reset_out is None or batch_out is None:
        return False
    reset_out["upserts"][0]["lat"] = 2.0
    batch_out["upserts"][0]["lat"] = 3.0
    lats = {r["call"]: r["lat"] for r in c.snapshot()["upserts"]}
    return ok and lats == {"D4": 48.1, "E5": 48.1}


def run_internet_positions_tests() -> bool:
    cases: list[tuple[str, Callable[[], bool]]] = [
        ("p1 reset replaces", _p1),
        ("p2 batch upsert + removed", _p2),
        ("p3 unchanged not re-emitted", _p3),
        ("p4 validation drops", _p4),
        ("p5 unknown keys stripped", _p5),
        ("p6 blocklist", _p6),
        ("p7 ttl expiry", _p7),
        ("p8 bound evicts oldest", _p8),
        ("p9 snapshot/clear", _p9),
        ("p10 stale replay guard", _p10),
        ("p11 snapshot sweeps ttl", _p11),
        ("p12 blocked provider read once", _p12),
        ("p13 payloads never alias cache", _p13),
    ]
    results: list[tuple[str, bool]] = []
    for label, fn in cases:
        try:
            ok = fn()
        except Exception:
            logger.exception("case %s raised", label)
            ok = False
        results.append((label, ok))
    for label, ok in results:
        logger.info("%s: %s", "PASS" if ok else "FAIL", label)
    failed = [label for label, ok in results if not ok]
    if failed:
        logger.error("internet positions tests FAILED: %s", failed)
    return not failed
