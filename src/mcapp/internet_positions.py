"""RAM cache of node positions fed by meshmap's box feed (internet feed, wave B1a).

Design: doc/2026-10-11_1005-internet-feed-concept.md 4.5. Contract:
mcmap docs/proposals/data-mcapp-box-feed.md 4.2 (`positions` event with
`reset: true` and the full set; `batch` with `positions[]` upserts and
`removed[]` calls).

Pure CPU: no I/O, no asyncio. `station_positions` is never touched from here.

Blocklist: `blocked` is a provider of the LIVE blocked set (callsigns, upper
case; pass a `set` for O(1) lookups). It is read ONCE per apply / sweep, never
per row. A position is rejected when the set contains its full callsign
(`DL1ABC-9`) OR its base call without SSID (`DL1ABC`): a base-call entry blocks
every SSID, an exact-SSID entry blocks only that SSID and leaves siblings alone.
The sweep runs on every apply and on every snapshot, so a call blocked later is
evicted (and reported in `removed` by apply) on the next event.

Returned payloads never alias the cache: every upsert row is a fresh dict and
every list is new (they are json-serialised off-loop while `apply` may run).

TTL: expired rows are swept on apply AND on `snapshot()`, so the replay path
for a newly connected client never serves a row older than `ttl_ms`.

`lastSeenUnix` is in SECONDS (meshmap contract); the clock is milliseconds.
"""

import math
from collections.abc import Callable, Collection, Mapping
from typing import Any

from .logging_setup import get_logger
from .util import now_ms

logger = get_logger(__name__)

DEFAULT_MAX_ENTRIES = 5000
DEFAULT_TTL_MS = 3 * 3600 * 1000

_MAX_LAT = 90.0
_MAX_LON = 180.0
_STR_KEYS = ("aprsSymGrp", "aprsSym", "hardware", "firmware")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clean_coords(lat: Any, lon: Any) -> tuple[float, float] | None:
    if not _is_num(lat) or not _is_num(lon):
        return None
    flat, flon = float(lat), float(lon)
    if abs(flat) > _MAX_LAT or abs(flon) > _MAX_LON or (flat == 0.0 and flon == 0.0):
        return None
    return flat, flon


def _clean_position(raw: Any) -> dict[str, Any] | None:
    """Validate one Position and keep only whitelisted keys; None if invalid."""
    if not isinstance(raw, Mapping):
        return None
    call = raw.get("call")
    if not isinstance(call, str) or not call.strip():
        return None
    coords = _clean_coords(raw.get("lat"), raw.get("lon"))
    seen = raw.get("lastSeenUnix")
    if coords is None or isinstance(seen, bool) or not isinstance(seen, int):
        return None
    flat, flon = coords
    out: dict[str, Any] = {
        "call": call.strip().upper(),
        "lat": flat,
        "lon": flon,
        "lastSeenUnix": seen,
    }
    alt = raw.get("altitudeM")
    if _is_num(alt):
        out["altitudeM"] = alt
    for key in _STR_KEYS:
        val = raw.get(key)
        if isinstance(val, str):
            out[key] = val
    bat = raw.get("batteryPercent")
    if _is_num(bat):
        out["batteryPercent"] = bat
    gw = raw.get("isGateway")
    if isinstance(gw, bool):
        out["isGateway"] = gw
    return out


def _is_blocked(call: str, blocked: Collection[str]) -> bool:
    """Exact callsign or its base call (SSID stripped) is in the blocked set."""
    return call in blocked or call.split("-", 1)[0] in blocked


class InternetPositionCache:
    def __init__(
        self,
        *,
        blocked: Callable[[], Collection[str]],
        clock_ms: Callable[[], int] = now_ms,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_ms: int = DEFAULT_TTL_MS,
    ) -> None:
        self._blocked_provider = blocked
        self._clock_ms = clock_ms
        self._max = max_entries
        self._ttl_ms = ttl_ms
        self._cache: dict[str, dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self._cache)

    def _rows(self) -> list[dict[str, Any]]:
        """Fresh copies of every row: returned payloads are serialised in a worker thread
        while the loop may `apply()` again, so they must never alias the live cache."""
        return [dict(p) for p in self._cache.values()]

    def _expired(self, pos: dict[str, Any]) -> bool:
        return bool(pos["lastSeenUnix"] * 1000 < self._clock_ms() - self._ttl_ms)

    def _ingest(self, raw_positions: Any, blocked: Collection[str]) -> None:
        dropped = 0
        if not isinstance(raw_positions, list):
            return
        for raw in raw_positions:
            pos = _clean_position(raw)
            if pos is None:
                dropped += 1
                continue
            if _is_blocked(pos["call"], blocked) or self._expired(pos):
                continue
            cur = self._cache.get(pos["call"])
            if cur is not None and cur["lastSeenUnix"] > pos["lastSeenUnix"]:
                continue  # stale replay never overwrites a newer entry
            self._cache[pos["call"]] = pos
        if dropped:
            logger.debug("internet positions: dropped %d invalid rows", dropped)

    def _sweep(self, blocked: Collection[str]) -> None:
        for call in [
            c for c, p in self._cache.items() if _is_blocked(c, blocked) or self._expired(p)
        ]:
            del self._cache[call]
        over = len(self._cache) - self._max
        if over > 0:
            oldest = sorted(self._cache, key=lambda c: self._cache[c]["lastSeenUnix"])
            for call in oldest[:over]:
                del self._cache[call]

    def apply(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        reset = bool(event.get("reset"))
        before = dict(self._cache)
        if reset:
            self._cache = {}
        else:
            removed_in = event.get("removed")
            if isinstance(removed_in, list):
                for c in removed_in:
                    if isinstance(c, str):
                        self._cache.pop(c.strip().upper(), None)
        blocked = self._blocked_provider()  # once per apply: ingest and sweep share it
        self._ingest(event.get("positions"), blocked)
        self._sweep(blocked)
        if reset:
            return {"reset": True, "upserts": self._rows(), "removed": []}
        upserts = [dict(p) for c, p in self._cache.items() if before.get(c) != p]
        removed = [c for c in before if c not in self._cache]
        if not upserts and not removed:
            return None
        return {"upserts": upserts, "removed": removed}

    def snapshot(self) -> dict[str, Any]:
        self._sweep(self._blocked_provider())  # TTL (and blocklist) hold on the replay path too
        return {"reset": True, "upserts": self._rows(), "removed": []}

    def clear(self) -> dict[str, Any]:
        self._cache = {}
        return {"reset": True, "upserts": [], "removed": []}
