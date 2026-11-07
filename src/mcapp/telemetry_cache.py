"""Stale-while-revalidate response cache for the bucketed telemetry endpoints.

A 744 h or yearly read costs seconds on a Pi Zero 2W; the payload changes slowly.
Fresh entries are served as is, stale ones are served at once while one background
task recomputes, and only a cold or expired key makes the caller wait (single-flight).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable

from .logging_setup import get_logger
from .util import now_ms

logger = get_logger(__name__)


class TelemetryResponseCache:
    """Per-key bytes cache: ttl fresh, max_stale revalidated in background, LRU-bounded.

    Every compute, cold or refresh, runs as its own task held in ``_inflight``; callers
    await it through ``asyncio.shield``. A cancelled caller therefore cannot take the
    shared result (or the other waiters) down, and a refresh and a cold request for the
    same key can never compute twice at once.
    """

    def __init__(
        self,
        ttl_ms: int = 5 * 60_000,
        max_stale_ms: int = 60 * 60_000,
        max_entries: int = 4,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._ttl_ms = ttl_ms
        self._max_stale_ms = max_stale_ms
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[Hashable, tuple[int, bytes]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Task[bytes]] = {}
        self._tasks: set[asyncio.Task[bytes]] = set()

    def _store(self, key: Hashable, value: bytes) -> None:
        self._entries[key] = (self._clock(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def _compute_store(self, key: Hashable, compute: Callable[[], Awaitable[bytes]]) -> bytes:
        value = await compute()
        self._store(key, value)
        return value

    def _done(self, key: Hashable, task: asyncio.Task[bytes]) -> None:
        self._tasks.discard(task)
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            exc = task.exception()  # marks it retrieved when nobody awaits
            if exc is not None:
                logger.warning("Telemetry cache compute failed for %r: %s", key, exc)

    def _start(self, key: Hashable, compute: Callable[[], Awaitable[bytes]]) -> asyncio.Task[bytes]:
        task = asyncio.get_running_loop().create_task(self._compute_store(key, compute))
        self._inflight[key] = task
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._done(key, t))
        return task

    async def get(self, key: Hashable, compute: Callable[[], Awaitable[bytes]]) -> bytes:
        entry = self._entries.get(key)
        if entry is not None:
            age = self._clock() - entry[0]
            if age < self._max_stale_ms:
                self._entries.move_to_end(key)
                if age >= self._ttl_ms and key not in self._inflight:
                    self._start(key, compute)
                return entry[1]
        task = self._inflight.get(key) or self._start(key, compute)
        return await asyncio.shield(task)
