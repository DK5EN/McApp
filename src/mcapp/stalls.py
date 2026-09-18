#!/usr/bin/env python3
"""Stall tracking (W1-A core): records every stall between the webapp and the
API — server- and client-observed — with the parameters needed to reproduce
it, keyed by one correlation id.

See doc/2026-09-15_1530-stall-tracking-plan.md for the full design (§1 kinds
and thresholds, §2 the record, §6 the interface contract this module
implements verbatim — `src/mcapp/stall_middleware.py` and
`src/mcapp/sse_routes/stalls.py` are written against these exact signatures
by a sibling wave, so they must not drift).

The recorder never blocks a caller: `record()` and `ingest_client()` push
onto a bounded in-process queue and return; a single dedicated writer thread
drains it with its own SQLite connection (NOT the shared default executor —
instrumenting the pool the recorder itself burdens would misreport pool
starvation it doesn't cause). `install_executor()` swaps in the counting
`ThreadPoolExecutor` that reports genuine `to_thread` pool waits.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import queue
import re
import resource
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .config_loader import StallsConfig
from .logging_setup import get_logger
from .storage.constants import db_read, db_write
from .util import now_ms

logger = get_logger(__name__)

# Set by StallMiddleware (sibling wave, stall_middleware.py) for the lifetime
# of one request; read by anyone downstream (e.g. MessageRouter.publish via
# time_handler) who wants to correlate a handler-side stall with the HTTP
# request that triggered it.
current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)

# ── DDL — identical text to storage/migrations.py's migration 32 ──────────

STALL_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS stall_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    origin TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    request_id TEXT,
    session_id TEXT,
    method TEXT,
    path TEXT,
    query TEXT,
    body TEXT,
    status INTEGER,
    duration_ms REAL,
    context TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_stall_events_ts ON stall_events(ts_ms);
CREATE INDEX IF NOT EXISTS idx_stall_events_kind_ts ON stall_events(kind, ts_ms);
"""

_INSERT_SQL = """
INSERT INTO stall_events
    (ts_ms, origin, kind, severity, request_id, session_id, method, path, query,
     body, status, duration_ms, context, detail)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# §1's client-observed kinds — server-observed kinds (http, loop_lag,
# pool_wait, handler) never arrive through ingest_client.
_CLIENT_KINDS = frozenset(
    {
        "client_http",
        "client_timeout",
        "client_error",
        "sse_answer",
        "sse_answer_missing",
        "sse_heartbeat",
    }
)
_SEVERITIES = frozenset({"sample", "stall", "critical"})

# `_loop_lag_loop`'s own sleep granularity (F4: also the baseline the
# lag-sampler thread subtracts before comparing against config.loop_lag_ms —
# a normal 0.1s sleep must never itself read as lag). Shared so the two loops
# can't drift apart.
_LAG_LOOP_INTERVAL_S = 0.1
# How often the sampler thread wakes to check whether the loop is overdue.
# Cheap when healthy: one float subtraction and compare per tick.
_LAG_SAMPLER_PERIOD_S = 0.05
# Innermost frames kept per stack sample — enough to name the blocking call
# without storing an unbounded traceback.
_LAG_SAMPLE_FRAME_LIMIT = 12

# Trims a stack frame's absolute path down to its "src/mcapp/..." (or
# "ble_service/src/...") relative form — never a full file body, just the
# `File "..."` header traceback.format_stack already produces per frame.
_STACK_PATH_RE = re.compile(r'File "[^"]*?[\\/]((?:src[\\/]mcapp|ble_service[\\/]src)[\\/][^"]*)"')


def _trim_stack_paths(text: str) -> str:
    return _STACK_PATH_RE.sub(lambda m: f'File "{m.group(1)}"', text)


# Redaction: exact key names (case-insensitive) plus a substring rule for the
# many api-key spellings. "auth" and "authorization" are both listed because
# real payloads use both ("Authorization" headers, push subscriptions'
# "auth" secret) and neither is a substring of the other.
_REDACT_EXACT_KEYS = frozenset(
    {"endpoint", "keys", "p256dh", "auth", "authorization", "api_key", "x-api-key"}
)
_REDACTED = "[redacted]"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _REDACT_EXACT_KEYS or "api_key" in lowered or "apikey" in lowered


def redact(obj: Any) -> Any:
    """Recursively replace the VALUES of sensitive keys in a dict/list
    structure with "[redacted]". Pure — never mutates `obj`. Non-dict/list
    leaves (including anything that can't be JSON-serialised) pass through
    unchanged; serialisation, if any, is the caller's job.
    """
    if isinstance(obj, dict):
        return {
            key: (_REDACTED if isinstance(key, str) and _is_sensitive_key(key) else redact(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    return obj


def _str_or_none(value: Any, max_len: int = 4096) -> str | None:
    """Client-supplied string field, clipped: `/api/stalls/client` has no auth and
    `max_rows` caps the row COUNT, not bytes, so an uncapped field is a disk-fill."""
    if not isinstance(value, str):
        return None
    return value[:max_len]


def _qualified_name(fn: Callable[..., Any]) -> str:
    """Best-effort human-readable name for a submitted job, for `pool_wait`'s
    `detail.fn`. `asyncio.to_thread` wraps the real callable in
    `functools.partial(ctx.run, func, *args)` before handing it to the
    executor, so the interesting name is one level down in `.args[0]`, not on
    the partial itself (which has no `__qualname__`) or on `ctx.run` (which
    would report every job as "Context.run").
    """
    if isinstance(fn, functools.partial):
        if fn.args:
            return _qualified_name(fn.args[0])
        return _qualified_name(fn.func)
    qualname = getattr(fn, "__qualname__", None)
    if qualname:
        module = getattr(fn, "__module__", "")
        return f"{module}.{qualname}" if module else str(qualname)
    name = getattr(fn, "__name__", None)
    if name:
        return str(name)
    return repr(fn)


def _percentile(sorted_values: list[float], pct: int) -> float:
    """Nearest-rank percentile over an already-sorted ascending list."""
    if not sorted_values:
        return 0.0
    rank = max(1, -(-pct * len(sorted_values) // 100))  # ceil(pct/100 * n) via integer math
    rank = min(rank, len(sorted_values))
    return sorted_values[rank - 1]


class StallRecorder:
    """Owns the `stall_events` table: a bounded queue, a dedicated writer
    thread, a loop-lag watchdog task, and the query/summary/ingest surface
    the HTTP layer (sibling wave) exposes at `/api/stalls*`.
    """

    def __init__(
        self, db_path: Path | str, config: StallsConfig, *, version: str, slot: str
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config
        self.version = version
        self.slot = slot

        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1000)
        self._writer_thread: threading.Thread | None = None
        self._lag_task: asyncio.Task[None] | None = None

        # F4 (loop-lag attribution): heartbeat published by `_loop_lag_loop`
        # on every iteration, read by `_lag_sampler_loop` (a separate thread)
        # to notice the loop is overdue and capture its stack — the lag loop
        # itself runs ON the loop, so by the time IT wakes the blocker is
        # already gone.
        self._last_tick: float = 0.0
        self._loop_thread_ident: int | None = None
        self._lag_sampler_thread: threading.Thread | None = None
        self._lag_sampler_stop = threading.Event()
        self._lag_sample_lock = threading.Lock()
        self._lag_sample_text: str | None = None
        self._lag_sample_count = 0
        self._lag_sample_age_ms: float = 0.0

        self._dropped = 0
        self._dropped_lock = threading.Lock()

        self._sample_counter = 0
        self._sample_lock = threading.Lock()

        # Separate from the http counter above on purpose: a burst of mesh
        # handler traffic must not starve http samples (or vice versa) by
        # racing them onto one shared modulus.
        self._handler_sample_counter = 0
        self._handler_sample_lock = threading.Lock()

        self._last_lag_ms: float = 0.0

        self._pool_queued = 0
        self._pool_running = 0
        self._pool_max = 0
        self._pool_lock = threading.Lock()

        self._gauges: dict[str, Callable[[], Any]] = {}

        self._stat_cache: dict[str, tuple[float, int]] = {}
        self._stat_cache_lock = threading.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Ensure the table exists, start the writer thread and the
        loop-lag watchdog task.
        """
        await asyncio.to_thread(self._ensure_schema)
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="mcapp-stalls-writer", daemon=True
        )
        self._writer_thread.start()

        # This coroutine is started from the event loop thread, so its ident
        # here IS the loop thread's ident — the one the sampler thread later
        # looks up a frame for.
        self._loop_thread_ident = threading.get_ident()
        self._last_tick = time.perf_counter()
        self._lag_sampler_stop.clear()
        self._lag_sampler_thread = threading.Thread(
            target=self._lag_sampler_loop, name="mcapp-stalls-lagsampler", daemon=True
        )
        self._lag_sampler_thread.start()

        self._lag_task = asyncio.create_task(self._loop_lag_loop())

    async def stop(self) -> None:
        if self._lag_task is not None:
            self._lag_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._lag_task
            self._lag_task = None
        if self._lag_sampler_thread is not None:
            self._lag_sampler_stop.set()
            await asyncio.to_thread(self._lag_sampler_thread.join, 5.0)
            self._lag_sampler_thread = None
        if self._writer_thread is not None:
            try:
                self._queue.put_nowait(None)  # sentinel
            except queue.Full:
                # Never block the loop: hand the (blocking) put to a thread.
                await asyncio.to_thread(self._queue.put, None)
            await asyncio.to_thread(self._writer_thread.join, 5.0)
            self._writer_thread = None

    def _ensure_schema(self) -> None:
        with db_write(self.db_path) as conn:
            conn.executescript(STALL_EVENTS_DDL)

    # ── writer thread ───────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        insert_count = 0
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    break
                try:
                    self._insert_row(item)
                    insert_count += 1
                except Exception:
                    logger.exception("stall recorder: failed to write row")
                    continue
                if insert_count % 200 == 0:
                    try:
                        self._prune()
                    except Exception:
                        logger.exception("stall recorder: failed to prune")
            finally:
                self._queue.task_done()

    def _insert_row(self, row: dict[str, Any]) -> None:
        with db_write(self.db_path) as conn:
            conn.execute(
                _INSERT_SQL,
                (
                    row["ts_ms"],
                    row["origin"],
                    row["kind"],
                    row["severity"],
                    row["request_id"],
                    row["session_id"],
                    row["method"],
                    row["path"],
                    row["query"],
                    row["body"],
                    row["status"],
                    row["duration_ms"],
                    row["context"],
                    row["detail"],
                ),
            )

    def _prune(self) -> None:
        with db_write(self.db_path) as conn:
            conn.execute(
                "DELETE FROM stall_events WHERE id NOT IN "
                "(SELECT id FROM stall_events ORDER BY id DESC LIMIT ?)",
                (self.config.max_rows,),
            )

    # ── recording ───────────────────────────────────────────────────────

    def _cap_json(self, value: Any, cap_bytes: int) -> tuple[str | None, bool]:
        """Redact + JSON-serialise `value`, capping at `cap_bytes` UTF-8
        bytes. Returns (text_or_None, truncated).
        """
        if value is None:
            return None, False
        redacted = redact(value)
        try:
            text = json.dumps(redacted, default=str)
        except (TypeError, ValueError):
            text = json.dumps(str(redacted), default=str)
        encoded = text.encode("utf-8")
        if len(encoded) <= cap_bytes:
            return text, False
        truncated_text = encoded[:cap_bytes].decode("utf-8", errors="ignore")
        return truncated_text, True

    def _enqueue(self, row: dict[str, Any]) -> bool:
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1
            return False
        return True

    def record(  # noqa: PLR0913 - signature fixed by contract (plan §6)
        self,
        *,
        kind: str,
        severity: str,
        origin: str = "server",
        request_id: str | None = None,
        session_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
        query: str | None = None,
        body: Any = None,
        status: int | None = None,
        duration_ms: float | None = None,
        detail: Any = None,
    ) -> None:
        """Non-blocking: fills ts_ms + context=snapshot() itself, redacts and
        caps body/detail, and drops (counting it) on a full queue.
        """
        context = self.snapshot()
        body_json, body_truncated = self._cap_json(body, self.config.body_cap_bytes)
        detail_json, detail_truncated = self._cap_json(detail, self.config.body_cap_bytes * 4)
        if body_truncated:
            context["body_truncated"] = True
        if detail_truncated:
            context["detail_truncated"] = True
        row = {
            "ts_ms": now_ms(),
            "origin": origin,
            "kind": kind,
            "severity": severity,
            "request_id": request_id,
            "session_id": session_id,
            "method": method,
            "path": path,
            "query": query,
            "body": body_json,
            "status": status,
            "duration_ms": duration_ms,
            "context": json.dumps(context, default=str),
            "detail": detail_json,
        }
        self._enqueue(row)

    def severity_for(self, duration_ms: float) -> str | None:
        """ "critical" >= config.critical_ms; "stall" >= config.stall_ms;
        otherwise every `sample_every`-th SUB-THRESHOLD call → "sample" (the
        1-in-N sampling of normal requests from plan §1); else None.
        `sample_every <= 0` disables sampling entirely.
        """
        return self._severity_for(
            duration_ms,
            stall_ms=self.config.stall_ms,
            sample_every=self.config.sample_every,
            lock=self._sample_lock,
            counter_attr="_sample_counter",
        )

    def severity_for_handler(self, duration_ms: float) -> str | None:
        """Same rule as `severity_for`, but the stall threshold is
        `config.handler_ms` instead of `config.stall_ms`, and sampling uses
        its own rate (`config.handler_sample_every`, 1-in-500 by default) and
        its own counter (`_handler_sample_counter`) rather than the http
        ones — a burst of mesh handler traffic must not starve http samples
        (or the reverse), and at the http rate the samples would evict the
        stall rows from the `max_rows` ring (see StallsConfig). Only
        `critical_ms` is shared.
        """
        return self._severity_for(
            duration_ms,
            stall_ms=self.config.handler_ms,
            sample_every=self.config.handler_sample_every,
            lock=self._handler_sample_lock,
            counter_attr="_handler_sample_counter",
        )

    def _severity_for(
        self,
        duration_ms: float,
        *,
        stall_ms: float,
        sample_every: int,
        lock: threading.Lock,
        counter_attr: str,
    ) -> str | None:
        if duration_ms >= self.config.critical_ms:
            return "critical"
        if duration_ms >= stall_ms:
            return "stall"
        if sample_every <= 0:
            return None
        with lock:
            count = int(getattr(self, counter_attr)) + 1
            setattr(self, counter_attr, count)
        return "sample" if count % sample_every == 0 else None

    # ── context snapshot ────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Cheap, sync server context: loop lag, pool occupancy, memory, DB
        file sizes, identity, drop count, plus every registered gauge.
        """
        result: dict[str, Any] = {
            "loop_lag_ms": self._last_lag_ms,
            "pool_queued": self._pool_queued,
            "pool_running": self._pool_running,
            "pool_max": self._pool_max,
            "rss_kb": self._read_rss_kb(),
            "db_bytes": self._stat_size(self.db_path),
            "wal_bytes": self._stat_size(f"{self.db_path}-wal"),
            "version": self.version,
            "slot": self.slot,
            "dropped": self.dropped,
        }
        for name, fn in self._gauges.items():
            try:
                result[name] = fn()
            except Exception:
                result[name] = None
        return result

    def _read_rss_kb(self) -> int | None:
        try:
            parts = Path("/proc/self/statm").read_text(encoding="ascii").split()
            resident_pages = int(parts[1])
            page_size = os.sysconf("SC_PAGE_SIZE")
            return resident_pages * page_size // 1024
        except (OSError, IndexError, ValueError):
            pass
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss is KB on Linux, bytes on macOS.
            return int(usage.ru_maxrss / 1024) if sys.platform == "darwin" else int(usage.ru_maxrss)
        except Exception:
            return None

    def _stat_size(self, path: Path | str) -> int:
        path_str = str(path)
        now = time.monotonic()
        with self._stat_cache_lock:
            cached = self._stat_cache.get(path_str)
            if cached is not None and now - cached[0] < 1.0:
                return cached[1]
        try:
            size = Path(path_str).stat().st_size
        except OSError:
            size = 0
        with self._stat_cache_lock:
            self._stat_cache[path_str] = (now, size)
        return size

    def register_gauge(self, name: str, fn: Callable[[], Any]) -> None:
        self._gauges[name] = fn

    @property
    def dropped(self) -> int:
        with self._dropped_lock:
            return self._dropped

    # ── loop-lag watchdog ───────────────────────────────────────────────

    async def _loop_lag_loop(self) -> None:
        interval_s = _LAG_LOOP_INTERVAL_S
        while True:
            t0 = time.perf_counter()
            self._last_tick = t0
            self._reset_lag_sample()  # any sample from here on belongs to this cycle only
            await asyncio.sleep(interval_s)
            lag_ms = (time.perf_counter() - t0 - interval_s) * 1000
            self._last_lag_ms = lag_ms
            if lag_ms >= self.config.loop_lag_ms:
                severity = "critical" if lag_ms >= 5 * self.config.loop_lag_ms else "stall"
                self.record(
                    kind="loop_lag",
                    severity=severity,
                    duration_ms=lag_ms,
                    detail=self._consume_lag_sample(),
                )

    # ── loop-lag attribution (F4): sampler thread + episode state ──────

    def _reset_lag_sample(self) -> None:
        with self._lag_sample_lock:
            self._lag_sample_text = None
            self._lag_sample_count = 0
            self._lag_sample_age_ms = 0.0

    def _consume_lag_sample(self) -> dict[str, Any] | None:
        """Return the current episode's sample (if any) and clear it. `None`
        means `record()`'s `detail` stays whatever the caller passed — never
        `{"stack": None, ...}` in the stored row.
        """
        with self._lag_sample_lock:
            text = self._lag_sample_text
            count = self._lag_sample_count
            age_ms = self._lag_sample_age_ms
            self._lag_sample_text = None
            self._lag_sample_count = 0
            self._lag_sample_age_ms = 0.0
        if text is None:
            return None
        return {"stack": text, "samples": count, "sampled_at_ms": age_ms}

    def _lag_sampler_loop(self) -> None:
        """Daemon thread: wakes every `_LAG_SAMPLER_PERIOD_S` and, only once
        the loop is overdue past `config.loop_lag_ms`, captures the loop
        thread's stack — cheap (a float compare) on every other tick.
        """
        while not self._lag_sampler_stop.wait(_LAG_SAMPLER_PERIOD_S):
            last_tick = self._last_tick
            if last_tick <= 0.0:
                continue
            overrun_s = time.perf_counter() - last_tick - _LAG_LOOP_INTERVAL_S
            if overrun_s * 1000 < self.config.loop_lag_ms:
                continue
            ident = self._loop_thread_ident
            if ident is None:
                continue
            try:
                frame = sys._current_frames().get(ident)  # noqa: SLF001 - only way to sample another thread's stack for lag attribution
            except Exception:
                frame = None
            if frame is None:
                continue  # the frame can vanish between the check and the read
            try:
                stack_text = _trim_stack_paths(
                    "".join(traceback.format_stack(frame, limit=_LAG_SAMPLE_FRAME_LIMIT))
                )
            except Exception:
                stack_text = None
            if stack_text is None:
                continue
            with self._lag_sample_lock:
                if self._last_tick != last_tick:
                    # The loop woke, recorded and started a new cycle while we
                    # were formatting: this stack belongs to the cycle that is
                    # already filed, never to the next one.
                    continue
                self._lag_sample_text = stack_text
                self._lag_sample_count += 1
                self._lag_sample_age_ms = overrun_s * 1000

    # ── instrumented default executor (pool_wait) ──────────────────────

    def _pool_job_enqueued(self) -> None:
        with self._pool_lock:
            self._pool_queued += 1

    def _pool_job_started(self, qualified_name: str, wait_ms: float) -> None:
        with self._pool_lock:
            self._pool_queued = max(0, self._pool_queued - 1)
            self._pool_running += 1
            queued_snapshot = self._pool_queued
            running_snapshot = self._pool_running
        if wait_ms >= self.config.pool_wait_ms:
            severity = "critical" if wait_ms >= 5 * self.config.pool_wait_ms else "stall"
            self.record(
                kind="pool_wait",
                severity=severity,
                duration_ms=wait_ms,
                detail={
                    "fn": qualified_name,
                    "queued": queued_snapshot,
                    "running": running_snapshot,
                },
            )

    def _pool_job_finished(self) -> None:
        with self._pool_lock:
            self._pool_running = max(0, self._pool_running - 1)

    def _pool_job_cancelled(self) -> None:
        """A job cancelled before it ever started still counted as queued."""
        with self._pool_lock:
            self._pool_queued = max(0, self._pool_queued - 1)

    def install_executor(self, loop: asyncio.AbstractEventLoop) -> None:
        """Install a counting `ThreadPoolExecutor` as `loop`'s default
        executor (same `max_workers` default asyncio itself would pick), so
        every `asyncio.to_thread` job's queue-wait is timed and a `pool_wait`
        stall is recorded when it crosses `config.pool_wait_ms`. Must be
        installed before anything calls `asyncio.to_thread`.
        """
        max_workers = min(32, (os.cpu_count() or 1) + 4)
        self._pool_max = max_workers

        on_enqueue = self._pool_job_enqueued
        on_start = self._pool_job_started
        on_finish = self._pool_job_finished
        on_cancel = self._pool_job_cancelled

        class _StallExecutor(ThreadPoolExecutor):
            def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
                enqueued = time.monotonic()
                qualified_name = _qualified_name(fn)
                on_enqueue()

                started = False

                def _wrapped(*a: Any, **kw: Any) -> Any:
                    nonlocal started
                    started = True
                    wait_ms = (time.monotonic() - enqueued) * 1000
                    on_start(qualified_name, wait_ms)
                    try:
                        return fn(*a, **kw)
                    finally:
                        on_finish()

                future = super().submit(_wrapped, *args, **kwargs)

                def _on_done(f: Future[Any]) -> None:
                    if f.cancelled() and not started:
                        on_cancel()

                future.add_done_callback(_on_done)
                return future

        executor = _StallExecutor(max_workers=max_workers, thread_name_prefix="mcapp-pool")
        loop.set_default_executor(executor)

    # ── handler timing ──────────────────────────────────────────────────

    @contextmanager
    def time_handler(
        self, message_type: str, handler_name: str, routed_message: dict[str, Any]
    ) -> Iterator[None]:
        """Sync context manager around one `MessageRouter.publish` subscriber
        call — usable around an `await` since it only measures wall time.
        """
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            severity = self.severity_for_handler(elapsed_ms)
            if severity is not None:
                self.record(
                    kind="handler",
                    severity=severity,
                    duration_ms=elapsed_ms,
                    request_id=current_request_id.get(),
                    detail={
                        "message_type": message_type,
                        "handler": handler_name,
                        "message": routed_message,
                    },
                )

    # ── query surface (/api/stalls, /api/stalls/summary) ───────────────

    async def query(
        self,
        *,
        since_ms: int | None = None,
        kind: str | None = None,
        severity: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 2000))
        return await asyncio.to_thread(self._query_sync, since_ms, kind, severity, limit)

    def _query_sync(
        self, since_ms: int | None, kind: str | None, severity: str | None, limit: int
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(since_ms)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM stall_events {where} ORDER BY id DESC LIMIT ?"  # noqa: S608 - fixed table, params bound
        params.append(limit)
        with db_read(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(dict(row)) for row in rows]

    def _row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        for field_name in ("context", "detail", "body"):
            raw_value = row.get(field_name)
            if isinstance(raw_value, str):
                # body in particular may not be JSON (e.g. a truncated cut) —
                # keep it as the raw string in that case.
                with suppress(json.JSONDecodeError, ValueError):
                    row[field_name] = json.loads(raw_value)
        return row

    async def summary(self, *, since_ms: int | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._summary_sync, since_ms)

    def _summary_sync(self, since_ms: int | None) -> list[dict[str, Any]]:
        clauses = ["kind = 'http'"]
        params: list[Any] = []
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(since_ms)
        where = " AND ".join(clauses)
        sql = f"SELECT path, duration_ms, severity FROM stall_events WHERE {where}"  # noqa: S608 - fixed table, params bound
        with db_read(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        by_path: dict[str, list[tuple[float, str]]] = {}
        for path, duration_ms, severity in rows:
            by_path.setdefault(path or "", []).append((float(duration_ms or 0.0), severity))

        results: list[dict[str, Any]] = []
        for path, entries in by_path.items():
            durations = sorted(d for d, _ in entries)
            stalls = sum(1 for _, sev in entries if sev in ("stall", "critical"))
            results.append(
                {
                    "path": path,
                    "count": len(durations),
                    "p50": _percentile(durations, 50),
                    "p95": _percentile(durations, 95),
                    "p99": _percentile(durations, 99),
                    "max": durations[-1] if durations else 0.0,
                    "stalls": stalls,
                }
            )
        results.sort(key=lambda r: r["p95"], reverse=True)
        return results

    # ── client ingest (POST /api/stalls/client) ─────────────────────────

    async def ingest_client(self, records: list[dict[str, Any]]) -> int:
        """Validate, redact and enqueue up to 100 client-submitted records.
        Invalid entries are dropped silently; returns the accepted count.
        """
        accepted = 0
        for raw in records[:100]:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            severity = raw.get("severity")
            if kind not in _CLIENT_KINDS or severity not in _SEVERITIES:
                continue

            ts_value = raw.get("ts_ms")
            ts_ms = (
                ts_value
                if isinstance(ts_value, int) and not isinstance(ts_value, bool)
                else now_ms()
            )

            duration_value = raw.get("duration_ms")
            duration_ms = (
                float(duration_value)
                if isinstance(duration_value, (int, float)) and not isinstance(duration_value, bool)
                else None
            )

            status_value = raw.get("status")
            status = (
                status_value
                if isinstance(status_value, int) and not isinstance(status_value, bool)
                else None
            )

            # The client context goes through the same cap as a body: a hostile or
            # buggy client must not be able to store megabytes per row.
            raw_context = raw.get("context")
            context: dict[str, Any] = {}
            if isinstance(raw_context, dict):
                ctx_text, ctx_truncated = self._cap_json(raw_context, self.config.body_cap_bytes)
                if ctx_truncated or ctx_text is None:
                    context = {"client_context_truncated": True}
                else:
                    context = json.loads(ctx_text)
            context["server"] = self.snapshot()

            body_json, body_truncated = self._cap_json(raw.get("body"), self.config.body_cap_bytes)
            detail_json, detail_truncated = self._cap_json(
                raw.get("detail"), self.config.body_cap_bytes * 4
            )
            if body_truncated:
                context["body_truncated"] = True
            if detail_truncated:
                context["detail_truncated"] = True

            row = {
                "ts_ms": ts_ms,
                "origin": "client",
                "kind": kind,
                "severity": severity,
                "request_id": _str_or_none(raw.get("request_id"), 128),
                "session_id": _str_or_none(raw.get("session_id"), 128),
                "method": _str_or_none(raw.get("method"), 16),
                "path": _str_or_none(raw.get("path"), 1024),
                "query": _str_or_none(raw.get("query"), 4096),
                "body": body_json,
                "status": status,
                "duration_ms": duration_ms,
                "context": json.dumps(redact(context), default=str),
                "detail": detail_json,
            }
            if self._enqueue(row):
                accepted += 1
        return accepted


__all__ = [
    "STALL_EVENTS_DDL",
    "StallRecorder",
    "current_request_id",
    "redact",
]
