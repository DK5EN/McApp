#!/usr/bin/env python3
"""Isolated, offline test suite for `stalls.py` (W1-A core) — the stall
recorder's queue/writer-thread mechanics, thresholds, instrumented executor,
handler timing, query/summary surface, and client ingest.

Run standalone: `uv run python -c "import asyncio; from mcapp.stall_tests
import run_stall_tests; raise SystemExit(0 if asyncio.run(run_stall_tests())
else 1)"`. Registered into scripts/run_startup_tests.py by the orchestrator
(plan §7, wave 2) — not wired into this repo's gated runner yet by this file
alone.

Every case uses a fresh temp-directory SQLite file (never `/etc/mcapp` or a
real `messages.db`) and every recorder is started and stopped explicitly, so
no writer thread or loop-lag task survives a test case.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .commands.constants import has_console
from .config_loader import Config, StallsConfig
from .stalls import StallRecorder, redact

RecordFn = Callable[[str, bool], None]


_DRAIN_TIMEOUT_S = 2.0


async def _drain(recorder: StallRecorder) -> bool:
    """Wait for the writer thread to finish every currently-queued row,
    WITHOUT going through `asyncio.to_thread` (whose own job would itself be
    submitted to a `StallRecorder`-instrumented default executor once
    `install_executor` has run in a test, recording an unrelated `pool_wait`
    row and shadowing the one the test is trying to observe). Polls
    `queue.Queue.unfinished_tasks` on the event loop instead — see
    `queue.Queue.join`'s own implementation for why that counter is exactly
    "put but not yet task_done()".
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT_S
    while time.monotonic() < deadline:
        if recorder._queue.unfinished_tasks == 0:
            return True
        await asyncio.sleep(0.01)
    return False


def _make_row(ts_ms: int, **overrides: Any) -> dict[str, Any]:
    """A minimally-valid `stall_events` row dict, for driving the writer's
    internal `_insert_row`/`_prune` directly in the prune test.
    """
    row: dict[str, Any] = {
        "ts_ms": ts_ms,
        "origin": "server",
        "kind": "http",
        "severity": "stall",
        "request_id": None,
        "session_id": None,
        "method": "GET",
        "path": "/api/x",
        "query": None,
        "body": None,
        "status": 200,
        "duration_ms": 10.0,
        "context": "{}",
        "detail": None,
    }
    row.update(overrides)
    return row


def _test_redact(_record: RecordFn) -> None:
    data = {
        "endpoint": "https://push.example.com/abc123",
        "keys": {"p256dh": "pubkey-material", "auth": "auth-secret"},
        "msg": "73 de DK5EN",
        "nested": {
            "Authorization": "Bearer xyz",  # case-insensitive match
            "X-API-Key": "topsecret",  # substring rule (api_key)
            "note": "keep me",
        },
    }
    out = redact(data)
    _record(
        "redact: endpoint masked",
        out["endpoint"] == "[redacted]",
    )
    _record(
        "redact: keys sub-object masked wholesale",
        out["keys"] == "[redacted]",
    )
    _record(
        "redact: message text untouched",
        out["msg"] == "73 de DK5EN",
    )
    _record(
        "redact: nested Authorization masked case-insensitively",
        out["nested"]["Authorization"] == "[redacted]",
    )
    _record(
        "redact: nested X-API-Key masked via substring rule",
        out["nested"]["X-API-Key"] == "[redacted]",
    )
    _record(
        "redact: unrelated nested field untouched",
        out["nested"]["note"] == "keep me",
    )
    _record(
        "redact: original object left unmutated (pure function)",
        data["endpoint"] != "[redacted]",
    )

    # p256dh/auth as top-level (not just nested under "keys") — each exact
    # key has its own rule, not just a blanket wipe of "keys".
    flat = redact({"p256dh": "pub", "auth": "sec", "msg": "keep"})
    _record(
        "redact: top-level p256dh masked",
        flat["p256dh"] == "[redacted]",
    )
    _record(
        "redact: top-level auth masked",
        flat["auth"] == "[redacted]",
    )
    _record(
        "redact: top-level msg untouched",
        flat["msg"] == "keep",
    )


def _test_severity_for(_record: RecordFn) -> None:
    # severity_for is pure state on the recorder — never start()ed, so a
    # throwaway path (never opened) is fine here.
    with tempfile.TemporaryDirectory() as tmp:
        config = StallsConfig(stall_ms=500, critical_ms=2000, sample_every=3)
        recorder = StallRecorder(Path(tmp) / "unused.db", config, version="v", slot="s")

        _record(
            "severity_for: >= critical_ms is critical", recorder.severity_for(2000) == "critical"
        )
        _record(
            "severity_for: above critical_ms is critical",
            recorder.severity_for(9999) == "critical",
        )
        _record("severity_for: >= stall_ms is stall", recorder.severity_for(500) == "stall")
        _record(
            "severity_for: between stall_ms and critical_ms is stall",
            recorder.severity_for(1999) == "stall",
        )

        # Sub-threshold calls: sample_every=3 → exactly every 3rd such call.
        results = [recorder.severity_for(10.0) for _ in range(6)]
        _record(
            "severity_for: sample_every=3 samples exactly every 3rd sub-threshold call",
            results == [None, None, "sample", None, None, "sample"],
        )


async def _test_record_and_truncation(_record: RecordFn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stalls.db"
        config = StallsConfig(body_cap_bytes=10)
        recorder = StallRecorder(db_path, config, version="v3", slot="s3")
        await recorder.start()
        try:
            recorder.record(
                kind="http",
                severity="stall",
                method="GET",
                path="/api/messages",
                status=200,
                duration_ms=750.0,
                body={"large": "x" * 200},
            )
            await _drain(recorder)

            rows = await recorder.query(kind="http", limit=10)
            _record("record: row lands in stall_events", len(rows) == 1)
            if rows:
                row = rows[0]
                _record("record: context parsed back to a dict", isinstance(row["context"], dict))
                _record(
                    "record: body over cap sets context.body_truncated",
                    row["context"].get("body_truncated") is True,
                )
                _record(
                    "record: stored duration_ms round-trips",
                    row["duration_ms"] == 750.0,
                )
        finally:
            await recorder.stop()


def _test_queue_full_drops(_record: RecordFn) -> None:
    # Deliberately never start()ed — no writer thread drains the queue, so
    # its fixed maxsize=1000 is reachable directly and deterministically.
    with tempfile.TemporaryDirectory() as tmp:
        config = StallsConfig()
        recorder = StallRecorder(Path(tmp) / "unused.db", config, version="v4", slot="s4")

        for _ in range(1000):
            recorder.record(kind="loop_lag", severity="stall", duration_ms=1.0)
        _record(
            "queue full: exactly 1000 buffered records nothing dropped yet",
            recorder.dropped == 0,
        )

        recorder.record(kind="loop_lag", severity="stall", duration_ms=1.0)  # 1001st: queue.Full
        _record("queue full: overflow increments dropped, raises nothing", recorder.dropped == 1)

        recorder.record(kind="loop_lag", severity="stall", duration_ms=1.0)  # 1002nd
        _record("queue full: dropped keeps counting on repeated overflow", recorder.dropped == 2)


async def _test_prune_cadence(_record: RecordFn) -> None:
    """Prune must fire from the writer thread itself (every 200 inserts), not
    only when called by hand — a never-prune mutation must fail here."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cadence.db"
        recorder = StallRecorder(db_path, StallsConfig(max_rows=3), version="v5c", slot="s5c")
        await recorder.start()
        try:
            for i in range(200):
                recorder.record(kind="http", severity="sample", path=f"/p{i}", duration_ms=1.0)
            for _ in range(100):
                rows = await recorder.query(limit=2000)
                if len(rows) <= 3 and any(r["path"] == "/p199" for r in rows):
                    break
                await asyncio.sleep(0.05)
            rows = await recorder.query(limit=2000)
            _record(
                "prune cadence: writer thread pruned to max_rows after 200 inserts",
                len(rows) <= 3 and any(r["path"] == "/p199" for r in rows),
            )
        finally:
            await recorder.stop()


async def _test_prune(_record: RecordFn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stalls.db"
        recorder = StallRecorder(db_path, StallsConfig(max_rows=5), version="v5", slot="s5")
        await recorder.start()
        try:
            for i in range(10):
                recorder._insert_row(_make_row(1000 + i))
            recorder._prune()

            rows = await recorder.query(limit=100)
            ts_values = sorted(int(row["ts_ms"]) for row in rows)
            _record(
                "prune: keeps exactly max_rows rows",
                len(rows) == 5,
            )
            _record(
                "prune: keeps the newest rows (highest ts_ms), not the oldest",
                ts_values == [1005, 1006, 1007, 1008, 1009],
            )
        finally:
            await recorder.stop()


async def _test_install_executor(_record: RecordFn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stalls.db"
        config = StallsConfig(pool_wait_ms=0)
        recorder = StallRecorder(db_path, config, version="v6", slot="s6")
        await recorder.start()
        try:
            loop = asyncio.get_running_loop()
            recorder.install_executor(loop)

            def _sample_job() -> int:
                return 42

            result = await asyncio.to_thread(_sample_job)
            _record("install_executor: instrumented job still returns its result", result == 42)

            await _drain(recorder)
            rows = await recorder.query(kind="pool_wait", limit=10)
            _record("install_executor: a pool_wait row was recorded", len(rows) >= 1)
            if rows:
                # query() itself runs through the instrumented pool, so its own
                # pool_wait row can race the SELECT: look at every row, not rows[0].
                _record(
                    "install_executor: pool_wait detail carries the job's qualified name",
                    any(
                        isinstance(r.get("detail"), dict)
                        and "_sample_job" in str(r["detail"].get("fn", ""))
                        for r in rows
                    ),
                )
        finally:
            await recorder.stop()


async def _test_loop_lag(_record: RecordFn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stalls.db"
        config = StallsConfig(loop_lag_ms=1)
        recorder = StallRecorder(db_path, config, version="v7", slot="s7")
        await recorder.start()
        try:
            # Block the event loop's OWN thread directly (on purpose) so the
            # loop-lag watchdog's asyncio.sleep(0.1) measurably overruns.
            time.sleep(0.05)  # noqa: ASYNC251 - deliberately blocks the loop to make the watchdog measure real lag

            found = False
            for _ in range(20):  # poll, don't count ticks — up to ~1s total
                await asyncio.sleep(0.05)
                await _drain(recorder)
                rows = await recorder.query(kind="loop_lag", limit=10)
                if rows:
                    found = True
                    break
            _record("loop_lag: blocking the loop past loop_lag_ms records a loop_lag row", found)
        finally:
            await recorder.stop()


def _block_the_loop_for_attribution_test() -> None:
    time.sleep(0.3)  # deliberately blocks the loop so the sampler thread catches it


async def _test_loop_lag_stack_attributed(_record: RecordFn) -> None:
    """F4: a lag long enough for the sampler thread to catch mid-block must
    attribute a stack naming the blocker; a lag the sampler never got a
    chance to sample (thread frozen) must record no `stack` key at all — the
    existing fields stay exactly as `_test_loop_lag` already pins.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stalls.db"
        config = StallsConfig(loop_lag_ms=50)
        recorder = StallRecorder(db_path, config, version="v7b", slot="s7b")
        await recorder.start()
        try:
            # Let the lag-loop task actually begin its first sleep cycle
            # before blocking — otherwise the block happens entirely before
            # that task has run even once, and its first post-block tick
            # measures a fresh (unlagged) cycle instead of the blocked one.
            await asyncio.sleep(0.02)
            # Block the loop's OWN thread synchronously (on purpose), from
            # within a coroutine, long enough (300ms) for the ~50ms-period
            # sampler thread to sample the loop thread's stack mid-block.
            await _blocking_coro_stub()

            row = None
            for _ in range(40):  # poll, don't count ticks — up to ~2s total
                await asyncio.sleep(0.05)
                await _drain(recorder)
                rows = await recorder.query(kind="loop_lag", limit=10)
                row = next(
                    (
                        r
                        for r in rows
                        if isinstance(r.get("detail"), dict) and "stack" in r["detail"]
                    ),
                    None,
                )
                if row is not None:
                    break
            _record(
                "loop_lag stack: a lag row with an attributed stack was recorded", row is not None
            )
            if row is not None:
                detail = row["detail"]
                stack = detail.get("stack")
                _record(
                    "loop_lag stack: stack names the blocking test function",
                    isinstance(stack, str) and "_block_the_loop_for_attribution_test" in stack,
                )
                _record(
                    "loop_lag stack: samples count is at least 1",
                    isinstance(detail.get("samples"), int) and detail["samples"] >= 1,
                )
        finally:
            await recorder.stop()

    # Negative half: the sampler thread is frozen (stop event set right after
    # start, before it ever wakes), so a real lag from the same blocking call
    # is still recorded — but with no sample ever taken, `detail` must carry
    # no "stack" key at all, never `{"stack": None, ...}`.
    with tempfile.TemporaryDirectory() as tmp2:
        db_path2 = Path(tmp2) / "stalls.db"
        config2 = StallsConfig(loop_lag_ms=1)
        recorder2 = StallRecorder(db_path2, config2, version="v7c", slot="s7c")
        await recorder2.start()
        try:
            recorder2._lag_sampler_stop.set()  # freeze the sampler before it can ever sample
            await asyncio.sleep(0.02)  # let the lag-loop task begin its first cycle
            await _blocking_coro_stub()

            row2 = None
            for _ in range(40):
                await asyncio.sleep(0.05)
                await _drain(recorder2)
                rows2 = await recorder2.query(kind="loop_lag", limit=10)
                if rows2:
                    row2 = rows2[0]
                    break
            _record("loop_lag stack: a lag row was recorded (baseline)", row2 is not None)
            if row2 is not None:
                detail2 = row2.get("detail")
                no_stack = detail2 is None or (isinstance(detail2, dict) and "stack" not in detail2)
                _record(
                    "loop_lag stack: a lag the frozen sampler never sampled carries no stack key",
                    no_stack,
                )
        finally:
            await recorder2.stop()


async def _blocking_coro_stub() -> None:
    _block_the_loop_for_attribution_test()


async def _test_time_handler(_record: RecordFn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stalls.db"
        config = StallsConfig(handler_ms=10, critical_ms=2000)
        recorder = StallRecorder(db_path, config, version="v8", slot="s8")
        await recorder.start()
        try:
            routed_message = {"src": "DK5EN", "dst": "20", "msg": "hi"}
            with recorder.time_handler("chat", "test_handler", routed_message):
                time.sleep(0.02)  # noqa: ASYNC251 - 20ms > handler_ms=10ms, deliberately slow "handler"

            await _drain(recorder)
            rows = await recorder.query(kind="handler", limit=10)
            _record("time_handler: over-threshold call records a handler row", len(rows) == 1)
            if rows:
                detail = rows[0].get("detail")
                _record(
                    "time_handler: detail carries handler name and the routed message",
                    isinstance(detail, dict)
                    and detail.get("handler") == "test_handler"
                    and detail.get("message_type") == "chat"
                    and detail.get("message") == routed_message,
                )
        finally:
            await recorder.stop()

        # A call that stays under handler_ms must record nothing when
        # sampling is disabled (sample_every=0).
        with tempfile.TemporaryDirectory() as tmp2:
            db_path2 = Path(tmp2) / "stalls.db"
            recorder2 = StallRecorder(
                db_path2,
                StallsConfig(handler_ms=5000, handler_sample_every=0),
                version="v8b",
                slot="s8b",
            )
            await recorder2.start()
            try:
                with recorder2.time_handler("chat", "fast_handler", {}):
                    pass
                await _drain(recorder2)
                rows2 = await recorder2.query(kind="handler", limit=10)
                _record("time_handler: under-threshold call records nothing", len(rows2) == 0)
            finally:
                await recorder2.stop()


async def _test_time_handler_sampling(_record: RecordFn) -> None:
    """Sibling of `_test_time_handler` (split out to stay under ruff's
    PLR0915 statement cap): the 1-in-N healthy-baseline sample rows and the
    "critical" severity for `time_handler`, both wired via
    `severity_for_handler`.
    """
    # A fast (sub-threshold) call with handler_sample_every=1 must still produce a
    # "handler" row, with severity "sample" — the healthy baseline
    # `severity_for` already gives the http kind. This is the case that
    # fails on unpatched code (time_handler used to only ever record
    # "stall"/"critical", never "sample").
    with tempfile.TemporaryDirectory() as tmp3:
        db_path3 = Path(tmp3) / "stalls.db"
        recorder3 = StallRecorder(
            db_path3,
            StallsConfig(handler_ms=5000, critical_ms=10000, handler_sample_every=1),
            version="v8c",
            slot="s8c",
        )
        await recorder3.start()
        try:
            with recorder3.time_handler("chat", "fast_handler", {}):
                pass
            await _drain(recorder3)
            rows3 = await recorder3.query(kind="handler", limit=10)
            _record(
                "time_handler: handler_sample_every=1 records a sub-threshold call as a row",
                len(rows3) == 1,
            )
            if rows3:
                _record(
                    "time_handler: sub-threshold sample row carries severity 'sample'",
                    rows3[0].get("severity") == "sample",
                )
        finally:
            await recorder3.stop()

    # An over-critical-threshold call must record severity "critical", not
    # just "stall".
    with tempfile.TemporaryDirectory() as tmp4:
        db_path4 = Path(tmp4) / "stalls.db"
        recorder4 = StallRecorder(
            db_path4,
            StallsConfig(handler_ms=5, critical_ms=15),
            version="v8d",
            slot="s8d",
        )
        await recorder4.start()
        try:
            with recorder4.time_handler("chat", "critical_handler", {}):
                time.sleep(0.02)  # noqa: ASYNC251 - 20ms > critical_ms=15ms, deliberately slow
            await _drain(recorder4)
            rows4 = await recorder4.query(kind="handler", limit=10)
            _record(
                "time_handler: over-critical_ms call records severity 'critical'",
                len(rows4) == 1 and rows4[0].get("severity") == "critical",
            )
        finally:
            await recorder4.stop()


async def _test_ingest_client(_record: RecordFn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stalls.db"
        recorder = StallRecorder(db_path, StallsConfig(), version="v9", slot="s9")
        await recorder.start()
        try:
            # Deliberately `list[Any]`, not `list[dict[str, Any]]`: one entry
            # below is not even a dict, to exercise ingest_client's own
            # shape-validation rather than relying on a type checker to keep
            # it out.
            records: list[Any] = [
                {
                    "kind": "client_http",
                    "severity": "stall",
                    "duration_ms": 600,
                    "path": "/api/messages",
                    "context": {"app_version": "1.0", "ua": "test-agent"},
                },
                {"kind": "not_a_real_kind", "severity": "stall"},  # invalid kind
                {"kind": "client_error", "severity": "critical", "status": 500},
                {"kind": "sse_heartbeat", "severity": "not_a_real_severity"},  # invalid severity
                "not even a dict",  # wrong shape entirely
            ]
            accepted = await recorder.ingest_client(records)
            _record("ingest_client: exactly the 2 valid records accepted", accepted == 2)

            await _drain(recorder)
            rows = await recorder.query(kind="client_http", limit=10)
            _record("ingest_client: valid record was stored", len(rows) == 1)
            if rows:
                context = rows[0]["context"]
                _record(
                    "ingest_client: origin is client",
                    rows[0]["origin"] == "client",
                )
                _record(
                    "ingest_client: client's own context preserved",
                    context.get("app_version") == "1.0",
                )
                server_context = context.get("server")
                _record(
                    "ingest_client: server sub-snapshot added alongside client context",
                    isinstance(server_context, dict) and server_context.get("version") == "v9",
                )

            many = [{"kind": "client_error", "severity": "sample"} for _ in range(150)]
            accepted_many = await recorder.ingest_client(many)
            _record("ingest_client: capped at 100 accepted records per call", accepted_many == 100)

            # Regression (advisor F1): the endpoint has no auth and max_rows caps the
            # COUNT, so a client must not be able to store megabytes per row.
            huge = {
                "kind": "client_http",
                "severity": "stall",
                "path": "/x" * 100_000,
                "request_id": "r" * 100_000,
                "context": {"blob": "z" * 1_000_000},
            }
            await recorder.ingest_client([huge])
            huge_row = None
            for _ in range(100):
                rows = await recorder.query(kind="client_http", limit=50)
                huge_row = next(
                    (r for r in rows if str(r.get("path", "")).startswith("/x/x")), None
                )
                if huge_row is not None:
                    break
                await asyncio.sleep(0.05)
            _record(
                "ingest_client: oversized client fields are clipped and context capped",
                huge_row is not None
                and len(huge_row["path"]) == 1024
                and len(huge_row["request_id"]) == 128
                and huge_row["context"].get("client_context_truncated") is True
                and "blob" not in huge_row["context"],
            )
        finally:
            await recorder.stop()


def _test_stalls_config(_record: RecordFn) -> None:
    defaults = StallsConfig()
    _record(
        "StallsConfig: documented defaults",
        (
            defaults.stall_ms == 500
            and defaults.critical_ms == 2000
            and defaults.sample_every == 50
            and defaults.loop_lag_ms == 100
            and defaults.pool_wait_ms == 100
            and defaults.handler_ms == 500
            and defaults.handler_sample_every == 500
            and defaults.body_cap_bytes == 8192
            and defaults.max_rows == 5000
        ),
    )

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        config_path.write_text(
            '{"CALL_SIGN": "DK5EN", "stalls": {"stall_ms": 100, "unknown_key": "ignored"}}',
            encoding="utf-8",
        )
        cfg = Config.load(config_path)
        _record(
            "Config.load: stalls.stall_ms overridden from config file",
            cfg.stalls.stall_ms == 100,
        )
        _record(
            "Config.load: unspecified stalls fields keep their dataclass default",
            cfg.stalls.critical_ms == 2000 and cfg.stalls.max_rows == 5000,
        )
        _record(
            "Config.load: an unknown key inside stalls is silently ignored",
            not hasattr(cfg.stalls, "unknown_key"),
        )

        # Absent "stalls" key entirely → every field is the dataclass default.
        config_path2 = Path(tmp) / "config2.json"
        config_path2.write_text('{"CALL_SIGN": "DK5EN"}', encoding="utf-8")
        cfg2 = Config.load(config_path2)
        _record(
            "Config.load: absent stalls key falls through to defaults",
            cfg2.stalls == StallsConfig(),
        )


async def run_stall_tests() -> bool:
    """Return True iff every stall-tracking core guard passes."""
    if has_console:
        print("\n🧪 Testing stall tracking (stalls.py core):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _test_redact(_record)
    _test_severity_for(_record)
    await _test_record_and_truncation(_record)
    _test_queue_full_drops(_record)
    await _test_prune(_record)
    await _test_prune_cadence(_record)
    await _test_install_executor(_record)
    await _test_loop_lag(_record)
    await _test_loop_lag_stack_attributed(_record)
    await _test_time_handler(_record)
    await _test_time_handler_sampling(_record)
    await _test_ingest_client(_record)
    _test_stalls_config(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Stall Tracking Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total
