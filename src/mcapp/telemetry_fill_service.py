"""Telemetry gap fill from meshmap (Wave 2).

Plan: doc/archive/2026-11-07_1000-telemetry-gap-fill-plan.md.

While the Internet feed is enabled, every visible station's WX gaps are filled from
`GET /api/v2/telemetry` and persisted with `origin = 'meshmap'`. The merge rules live in
`storage/telemetry_fill.py`; this module is only the fetch loop around them:

- Chunks of 30 days, halved when a response reaches the server's silent 5000-row cap
  (the cap keeps the NEWEST rows, so an uncut capped response loses its oldest ones).
- The per-station cursor advances only after the chunk's rows were written, so a
  restart or a failed request repeats the chunk and the filled-twin rule absorbs it.
- One request every 10 s for the whole service. Any failure stops that station for
  the cycle, never the service. The feed switch is re-read before EVERY request.
- Which stations exist, their filled-row counts and cursors live in memory, refreshed
  at the start of each cycle and bumped on insert; `/api/telemetry/fill` serves from
  that cache so a 60 s webapp poll never scans `telemetry`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .internet_feed import BASE_URL_ENV, DEFAULT_BASE_URL
from .logging_setup import get_logger
from .storage.telemetry_fill import FILL_MATCH_WINDOW_MS, FILL_SETTLE_MS, select_fill_rows
from .util import now_ms

logger = get_logger(__name__)

CYCLE_S = 30 * 60
START_DELAY_MAX_S = 300.0
REQUEST_SPACING_S = 10.0
HISTORY_MS = 365 * 24 * 3600 * 1000
STATION_WINDOW_MS = 30 * 24 * 3600 * 1000  # same window as the webapp's station picker
MIN_REQUEST_MS = 60 * 1000  # a shorter window can collapse to fromUnix == toUnix (HTTP 400)
CHUNK_MAX_MS = 30 * 24 * 3600 * 1000
CHUNK_MIN_MS = 3600 * 1000
RESPONSE_CAP = 5000
FIELDS = "temp,temp2,hum,press,qnh,alt,batt,gas_res,eco2,wlevel,wtemp"
CONNECT_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 60.0

SleepFn = Callable[[float], Awaitable[None]]


class _FetchError(Exception):
    """A request that cannot be used. Stops the station for this cycle."""


def _parse_entries(content: bytes) -> list[dict[str, Any]]:
    """Decode a meshmap telemetry body into its entry dicts (runs in a worker thread)."""
    try:
        body = json.loads(content)
    except ValueError as exc:
        raise _FetchError("bad JSON") from exc
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise _FetchError("no entries")
    return [e for e in entries if isinstance(e, dict)]


class _Halt(Exception):  # noqa: N818 - control flow, not an error
    """Shutdown or feed switched off: stop the station quietly, no error to record."""


class TelemetryFillService:
    def __init__(  # noqa: PLR0913 - every collaborator is injectable for tests
        self,
        *,
        storage: Any,
        is_blocked: Callable[[str], bool],
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], int] = now_ms,
        sleep: SleepFn | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._storage = storage
        self._is_blocked = is_blocked
        url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        self._base = url.rstrip("/")
        self._transport = transport
        self._clock = clock
        self._sleep_override = sleep
        self._rng = rng or random.Random().random  # noqa: S311 - start jitter, not security
        self._client: httpx.AsyncClient | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._last_cycle_ms: int | None = None
        self._next_cycle_ms: int | None = None
        self._stations: dict[str, dict[str, Any]] = {}
        # In-memory view for the route: RF station set, filled-row counts, cursors.
        self._cache_loaded = False
        self._cache_lock = asyncio.Lock()
        self._station_calls: list[str] = []
        self._row_counts: dict[str, int] = {}
        self._cursors: dict[str, int | None] = {}
        self._requested = False  # a request already went out in this cycle

    # ── status ─────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "last_cycle_ms": self._last_cycle_ms,
            "next_cycle_ms": self._next_cycle_ms,
            "stations": [dict(s) for _, s in sorted(self._stations.items())],
        }

    async def _refresh(self) -> None:
        """Re-read the station set, filled-row counts and cursors (the only scans)."""
        since = self._clock() - STATION_WINDOW_MS
        calls: list[str] = await self._storage.get_telemetry_stations(since)
        counts: dict[str, int] = await self._storage.get_fill_row_counts()
        cursors: dict[str, int | None] = {}
        for call in calls:
            cursors[call] = await self._storage.get_fill_cursor(call)
        self._station_calls = calls
        self._row_counts = counts
        self._cursors = cursors
        self._cache_loaded = True

    async def _ensure_cache(self) -> None:
        if self._cache_loaded:
            return
        async with self._cache_lock:
            if not self._cache_loaded:
                await self._refresh()

    async def visible_stations(self) -> list[str]:
        """Stations with an RF row in the last 30 days, minus hidden and blocked."""
        await self._ensure_cache()
        sidebar = await self._storage.get_wx_sidebar()
        hidden = set((sidebar or {}).get("hidden") or [])
        return [c for c in self._station_calls if c not in hidden and not self._is_blocked(c)]

    async def route_payload(self) -> dict[str, Any]:
        """The `/api/telemetry/fill` body, served from the in-memory cache."""
        state = await self._storage.get_internet_feed_state()
        known = {s["callsign"]: s for s in self.status()["stations"]}
        stations: list[dict[str, Any]] = [
            {
                "callsign": call,
                "filled_until_ms": self._cursors.get(call),
                "rows": self._row_counts.get(call, 0),
                "error": known.get(call, {}).get("error"),
            }
            for call in await self.visible_stations()
        ]
        return {
            "enabled": bool(state.get("enabled")),
            "running": self._running,
            "last_cycle_ms": self._last_cycle_ms,
            "next_cycle_ms": self._next_cycle_ms,
            "rows_total": sum(s["rows"] for s in stations),
            "stations": stations,
        }

    # ── loop ───────────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        self._stop = stop_event
        try:
            await self._pause(self._rng() * START_DELAY_MAX_S)
            while not stop_event.is_set():
                try:
                    await self.run_cycle()
                except Exception:
                    logger.exception("telemetry fill cycle failed")
                self._next_cycle_ms = self._clock() + CYCLE_S * 1000
                await self._pause(CYCLE_S)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _pause(self, seconds: float) -> None:
        if self._sleep_override is not None:
            await self._sleep_override(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), seconds)

    async def run_cycle(self) -> None:
        """One pass over the visible stations. No requests while the feed is off."""
        self._running = True
        self._requested = False
        try:
            # Under the lock: a cold route refresh must not finish after this
            # cycle's first insert and overwrite its counts with older ones.
            async with self._cache_lock:
                await self._refresh()
            if not await self._enabled():
                return
            now = self._clock()
            for call in await self.visible_stations():
                if self._stop.is_set() or not await self._enabled():
                    break
                if self._is_blocked(call):
                    continue
                try:
                    await self._fill_station(call, now)
                except Exception as exc:
                    # A storage failure costs this station its cycle, not the others.
                    logger.exception("telemetry fill %s failed", call)
                    self._state(call)["error"] = f"storage error: {type(exc).__name__}"
        finally:
            self._running = False
            self._last_cycle_ms = self._clock()

    async def _enabled(self) -> bool:
        return bool((await self._storage.get_internet_feed_state()).get("enabled"))

    # ── per station ────────────────────────────────────────────────────

    def _state(self, call: str) -> dict[str, Any]:
        return self._stations.setdefault(
            call, {"callsign": call, "filled_until_ms": None, "rows_last_cycle": 0, "error": None}
        )

    async def _fill_station(self, call: str, now: int) -> None:
        st = self._state(call)
        st["rows_last_cycle"] = 0
        st["error"] = None
        floor = now - HISTORY_MS
        cursor = await self._storage.get_fill_cursor(call)
        start = max(floor if cursor is None else cursor, floor)
        st["filled_until_ms"] = self._cursors[call] = cursor
        end = now - FILL_SETTLE_MS
        span = CHUNK_MAX_MS
        while start < end and not self._stop.is_set():
            chunk_end = min(start + span, end)
            if chunk_end - start < MIN_REQUEST_MS:
                break  # a sliver: the next cycle's later `end` covers it
            try:
                entries = await self._fetch(call, start, chunk_end)
            except _Halt:
                return
            except _FetchError as exc:
                st["error"] = str(exc)
                logger.warning("telemetry fill %s: %s", call, exc)
                return
            capped = len(entries) >= RESPONSE_CAP
            if capped and chunk_end - start > CHUNK_MIN_MS:
                span = max((chunk_end - start) // 2, CHUNK_MIN_MS)
                continue  # same start: the capped response dropped its OLDEST rows
            if capped:
                logger.warning(
                    "telemetry fill %s: capped response accepted at the minimum chunk "
                    "(%d ms from %d), the oldest rows may be missing",
                    call,
                    chunk_end - start,
                    start,
                )
            else:
                span = min(span * 2, CHUNK_MAX_MS)  # grow back after a halving
            rf, filled = await self._storage.get_telemetry_timestamps(
                call, start - FILL_MATCH_WINDOW_MS, chunk_end + FILL_MATCH_WINDOW_MS
            )
            # CPU-bound over up to 5000 entries: off the loop, like the parse.
            rows = await asyncio.to_thread(select_fill_rows, entries, rf, filled, now)
            inserted = await self._storage.insert_filled_telemetry(call, rows)
            st["rows_last_cycle"] += inserted
            self._row_counts[call] = self._row_counts.get(call, 0) + inserted
            await self._storage.set_fill_cursor(call, chunk_end)
            st["filled_until_ms"] = self._cursors[call] = chunk_end
            start = chunk_end

    async def _fetch(self, call: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        if self._requested:
            await self._pause(REQUEST_SPACING_S)
        # Re-read the switch before EVERY request: a first fill is ~13 of them.
        if self._stop.is_set() or not await self._enabled():
            raise _Halt
        self._requested = True
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self._base}/api/v2/telemetry",
                params={
                    "call": call,
                    "fromUnix": str(start_ms // 1000),
                    "toUnix": str(end_ms // 1000),
                    "fields": FIELDS,
                },
            )
        except httpx.HTTPError as exc:
            raise _FetchError(f"request failed: {type(exc).__name__}") from exc
        if resp.status_code != httpx.codes.OK:
            raise _FetchError(f"HTTP {resp.status_code}")
        # A capped 5000-row body is ~0.9 MB: parse it off the loop (loop_lag).
        return await asyncio.to_thread(_parse_entries, resp.content)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)
            if self._transport is not None:
                self._client = httpx.AsyncClient(
                    transport=self._transport, timeout=timeout, follow_redirects=False
                )
            else:
                ctx = await asyncio.to_thread(httpx.create_ssl_context)
                self._client = httpx.AsyncClient(
                    verify=ctx, timeout=timeout, follow_redirects=False
                )
        return self._client
