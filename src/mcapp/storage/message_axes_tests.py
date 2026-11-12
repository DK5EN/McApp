"""Regression suite for the live message axes (timing / echo stored at ingest,
format derived at read time) and their startup backfill.

Backend half of the shared spec with mc-chat (axes-backend-spec): the engine and its
contract are subtree-vendored (`classifier/axes.py`, `contract/axes_contract.json`);
this suite pins the host wiring in `storage/ingest.py`:

  1. First message of a sender is `one-shot`; a sender posting every 600 s becomes
     `scheduled`.
  2. Echo: another sender repeating a >=4-word text within 300 s is an echo whose
     `echo_of` is the ORIGINAL's msg_id; the same sender repeating, or a repeat
     outside the window, is not.
  3. The live SSE dict (annotated in place by `store_message`) and the history dict
     (`_build_message_dict`) carry timing / format / echo / echo_of with the right types.
  4. No engine attached: NULL columns, `format` null. Engine raising: the row is
     stored with NULL axes and ingest continues.
  5. Backfill reproduces ingest exactly (~200 mixed messages), is gated by a marker
     keyed on the contract commit, and re-runs when that commit changes.
  6. All three axes queries are index-backed (EXPLAIN QUERY PLAN).
  7. Backfill cost on ~30k synthetic rows (printed, not asserted beyond a loose bound).
  8. Concurrent store_message calls serialise compute + INSERT (ingest == backfill).
  9. The engine's format memo survives concurrent thread + loop use (tiny memo cap).
 10. Only real chat frames are annotated (BLE TYP/config frames are not); a
     blocklist-redirected frame still carries the four keys; the memo cap is pinned.
 11. Backfill == ingest for a late row inside a deep history, and for a second whose
     earlier-millisecond row has the higher id across a page boundary.

All timestamps are milliseconds (project-wide DB convention).
"""

import asyncio
import json
import random
import sys
import tempfile
import threading
import time
import tracemalloc
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..axes_contract import DEFAULT_AXES_CONTRACT_PATH, MEMO_CAP, AxesRuntime, load_axes
from ..ble_protocol import transform_ble
from ..sqlite_storage import create_sqlite_storage
from .constants import _MSG_SELECT
from .ingest import (
    _AXES_BACKFILL_SQL,
    _AXES_ECHO_SQL,
    _AXES_PRIOR_BEFORE_SQL,
    _AXES_PRIOR_SQL,
)

_T0 = 1_800_000_000_000
_SEC = 1000
_TEXT_A = "alpha bravo charlie delta echo foxtrot"

Results = list[tuple[str, bool]]


def _chat(src: str, msg_id: str, ts: int, msg: str, dst: str = "*") -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "msg": msg,
        "type": "msg",
        "msg_id": msg_id,
        "timestamp": ts,
        "src_type": "udp",
    }


async def _new_storage(tmp: str, name: str, *, axes: AxesRuntime | None) -> Any:
    storage = await create_sqlite_storage(Path(tmp) / f"{name}.db")
    storage.set_axes(axes)
    return storage


async def _ingest(storage: Any, message: dict[str, Any]) -> dict[str, Any]:
    await storage.store_message(message, json.dumps(message))
    return message


async def _row(storage: Any, msg_id: str) -> dict[str, Any]:
    rows = await storage._query(
        f"SELECT id, {_MSG_SELECT} FROM messages WHERE msg_id = ?",  # noqa: S608 - fixed constant
        (msg_id,),
    )
    assert len(rows) == 1, f"expected one row for {msg_id}, got {len(rows)}"
    return dict(rows[0])


async def _snapshot(storage: Any) -> list[tuple[Any, ...]]:
    rows = await storage._query("SELECT id, timing, echo, echo_of FROM messages ORDER BY id")
    return [(r["id"], r["timing"], r["echo"], r["echo_of"]) for r in rows]


async def _test_timing_and_echo(results: Results, tmp: str) -> None:
    storage = await _new_storage(tmp, "ingest", axes=load_axes())
    try:
        # periodic sender: 6 posts, 600 s apart, distinct texts
        for i in range(6):
            await _ingest(
                storage,
                _chat(
                    "DL1AAA-1",
                    f"P{i:07d}",
                    _T0 + i * 600 * _SEC,
                    f"periodic beacon number {i} hello",
                ),
            )
        first = await _row(storage, "P0000000")
        last = await _row(storage, "P0000005")
        results.append(
            ("timing: first message of a sender is one-shot", first["timing"] == "one-shot")
        )
        results.append(
            ("timing: 600 s sender with >=5 messages is scheduled", last["timing"] == "scheduled")
        )

        base = _T0 + 10_000 * _SEC
        await _ingest(storage, _chat("DL1BBB-1", "C0FFEE17", base, _TEXT_A))
        await _ingest(storage, _chat("DL1CCC-1", "4D2A9B03", base + 60 * _SEC, _TEXT_A.upper()))
        await _ingest(storage, _chat("DL1DDD-1", "91E6B7F5", base + 1000 * _SEC, _TEXT_A))
        # same sender repeating its own text, well apart from the other scenarios
        own = "kilo lima mike november oscar papa"
        await _ingest(storage, _chat("DL1FFF-1", "5EED1234", base + 3000 * _SEC, own))
        await _ingest(storage, _chat("DL1FFF-1", "0BADF00D", base + 3060 * _SEC, own))
        orig = await _row(storage, "C0FFEE17")
        echo = await _row(storage, "4D2A9B03")
        selfrep = await _row(storage, "0BADF00D")
        late = await _row(storage, "91E6B7F5")
        results.append(
            ("echo: original is not an echo", orig["echo"] == 0 and orig["echo_of"] is None)
        )
        results.append(
            (
                "echo: other sender within 300 s -> echo of the original's msg_id",
                echo["echo"] == 1 and echo["echo_of"] == "C0FFEE17",
            )
        )
        results.append(
            (
                "echo: same-sender repeat is not an echo",
                selfrep["echo"] == 0 and selfrep["echo_of"] is None,
            )
        )
        # LATE is 1000 s after ORIG and 940 s after ECHO: outside the window for both
        results.append(
            (
                "echo: repeat outside the 300 s window is not an echo",
                late["echo"] == 0 and late["echo_of"] is None,
            )
        )
        short = await _ingest(
            storage, _chat("DL1EEE-1", "A1B2C3D4", base + 5000 * _SEC, "too short")
        )
        results.append(("echo: <4-word text is never an echo", short["echo"] is False))
    finally:
        await storage.close()


async def _test_wire_shape(results: Results, tmp: str) -> None:
    storage = await _new_storage(tmp, "wire", axes=load_axes())
    try:
        await _ingest(storage, _chat("DL1BBB-1", "C0FFEE17", _T0, _TEXT_A))
        live = await _ingest(storage, _chat("DL1CCC-1", "4D2A9B03", _T0 + 5 * _SEC, _TEXT_A))
        hist = storage._build_message_dict(await _row(storage, "4D2A9B03"))
        fmt = live["format"]
        results.append(
            (
                "live dict: timing str, echo bool, echo_of str, format {dominant, signals[]}",
                isinstance(live["timing"], str)
                and live["echo"] is True
                and live["echo_of"] == "C0FFEE17"
                and isinstance(fmt, dict)
                and set(fmt) == {"dominant", "signals"}
                and isinstance(fmt["signals"], list),
            )
        )
        for key in ("timing", "echo", "echo_of", "format"):
            results.append((f"history dict equals live dict for {key!r}", hist[key] == live[key]))
        results.append(("history dict: echo is a real bool, not 1", hist["echo"] is True))

        # pos frames are not chat rows: no axes keys
        pos = await _ingest(storage, {**_chat("DL1ZZZ-1", "POS00001", _T0, ""), "type": "pos"})
        results.append(
            ("non-chat frame is not annotated", "timing" not in pos and "format" not in pos)
        )
    finally:
        await storage.close()


async def _test_no_engine_and_failing_engine(results: Results, tmp: str) -> None:
    storage = await _new_storage(tmp, "noeng", axes=None)
    try:
        live = await _ingest(storage, _chat("DL1BBB-1", "NOENG001", _T0, _TEXT_A))
        row = await _row(storage, "NOENG001")
        hist = storage._build_message_dict(row)
        results.append(
            (
                "no engine: NULL columns, live dict carries the four keys as None",
                row["timing"] is None
                and row["echo"] is None
                and all(live[k] is None for k in ("timing", "echo", "echo_of", "format")),
            )
        )
        results.append(
            (
                "no engine: history carries null timing/echo/echo_of/format",
                hist["timing"] is None
                and hist["echo"] is None
                and hist["echo_of"] is None
                and hist["format"] is None,
            )
        )
    finally:
        await storage.close()

    real = load_axes()

    class _Raising:
        def __getattr__(self, name: str) -> Any:
            return getattr(real.engine, name)

        def live_timing(self, *_a: Any, **_k: Any) -> str:
            raise RuntimeError("boom")

    broken = replace(real, engine=_Raising())  # type: ignore[arg-type]
    storage = await _new_storage(tmp, "broken", axes=broken)
    try:
        live = await _ingest(storage, _chat("DL1BBB-1", "BRK00001", _T0, _TEXT_A))
        await _ingest(
            storage, _chat("DL1BBB-1", "BRK00002", _T0 + _SEC, "second message after failure")
        )
        row = await _row(storage, "BRK00001")
        results.append(
            (
                "engine raising: row stored with NULL axes",
                row["timing"] is None and row["echo"] is None and row["echo_of"] is None,
            )
        )
        results.append(
            (
                "engine raising: live dict carries nulls, format still derived",
                live["timing"] is None
                and live["echo"] is None
                and isinstance(live["format"], dict),
            )
        )
        rows = await storage._query("SELECT COUNT(*) AS n FROM messages")
        results.append(("engine raising: ingest continues", rows[0]["n"] == 2))
    finally:
        await storage.close()


def _mixed_stream(
    n: int, seed: int = 7, *, out_of_order: bool = False
) -> list[tuple[str, str, int, str]]:
    """(src, msg_id, ts_ms, text), strictly increasing ts: periodic senders, chatty
    senders, echoes, same-second ties (ts differs by ms only) and short texts."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture, not crypto
    senders = [f"DL{i}AAA-{i % 4}" for i in range(12)]
    texts = [f"weather report station {i} temperature wind rain humidity" for i in range(8)]
    out: list[tuple[str, str, int, str]] = []
    ts = _T0
    for i in range(n):
        kind = rng.random()
        if kind < 0.35:  # periodic beacon
            src = senders[i % 3]
            ts += 600 * _SEC // 3
        elif kind < 0.60:  # echo of a recent text by a different sender
            src = rng.choice(senders)
            ts += rng.randint(1, 90) * _SEC
        elif kind < 0.75:  # same-second tie
            src = rng.choice(senders)
            ts += rng.randint(1, 400)
        else:
            src = rng.choice(senders)
            ts += rng.randint(30, 700) * _SEC
        text = (
            rng.choice(texts)
            if rng.random() < 0.5
            else f"unique chatter {i} about the mesh network today"
        )
        if rng.random() < 0.1:
            text = "ok"
        # msg_ids deliberately unrelated to the row ids, so a wrong echo_of mapping fails.
        mid = f"{(i * 2654435761 + 12345) % 2**32:08X}"
        emit = ts - rng.randint(0, 400) * _SEC if out_of_order and rng.random() < 0.2 else ts
        out.append((src, mid, emit, text))
    return out


async def _test_backfill_equivalence(results: Results, tmp: str) -> None:
    storage = await _new_storage(tmp, "backfill", axes=load_axes())
    try:
        for src, mid, ts, text in _mixed_stream(200):
            await _ingest(storage, _chat(src, mid, ts, text))
        live = await _snapshot(storage)
        kinds = {r[1] for r in live}
        results.append(
            (
                (
                    f"backfill fixture is mixed (timings {sorted(k for k in kinds if k)}, "
                    f"{sum(1 for r in live if r[2])} echoes)"
                ),
                len(kinds) >= 3 and any(r[2] for r in live),
            )
        )

        await storage._mutate("UPDATE messages SET timing = NULL, echo = NULL, echo_of = NULL")
        done = await storage.backfill_message_axes(batch_size=37)
        results.append(("backfill: updates every row", done == len(live)))
        results.append(
            (
                "backfill: reproduces ingest exactly (timing, echo, echo_of)",
                await _snapshot(storage) == live,
            )
        )

        await storage._mutate("UPDATE messages SET timing = NULL, echo = NULL, echo_of = NULL")
        again = await storage.backfill_message_axes()
        rows = await storage._query("SELECT COUNT(*) AS n FROM messages WHERE timing IS NULL")
        results.append(
            ("backfill: marker blocks a second run", again is None and rows[0]["n"] == len(live))
        )

        # a new contract commit (model refit) re-runs once
        data = json.loads(DEFAULT_AXES_CONTRACT_PATH.read_text(encoding="utf-8"))
        data["source"]["commit"] = "feedface" + data["source"]["commit"][8:]
        alt = Path(tmp) / "axes_alt.json"
        alt.write_text(json.dumps(data), encoding="utf-8")
        storage.set_axes(load_axes(alt))
        rerun = await storage.backfill_message_axes()
        results.append(
            (
                "backfill: changed contract commit re-runs",
                rerun == len(live) and await _snapshot(storage) == live,
            )
        )
    finally:
        await storage.close()


async def _reset_and_backfill(storage: Any, **kw: Any) -> int | None:
    await storage._mutate("UPDATE messages SET timing = NULL, echo = NULL, echo_of = NULL")
    await storage._mutate("DELETE FROM classifier_meta WHERE key LIKE 'axes_backfill_done:%'")
    return await storage.backfill_message_axes(**kw)  # type: ignore[no-any-return]


async def _test_out_of_order(results: Results, tmp: str) -> None:
    """Backfill reproduces ingest whatever the arrival order (lower row id = earlier)."""
    storage = await _new_storage(tmp, "ooo", axes=load_axes())
    try:
        # Explicit case: the original arrives FIRST but carries a LATER time; the older-time
        # row arrives second. Ingest: neither is an echo. A backfill that sees all rows would
        # call the first one an echo of the second.
        await _ingest(storage, _chat("DL1BBB-1", "7C1D0A55", _T0 + 100 * _SEC, _TEXT_A))
        await _ingest(storage, _chat("DL1DDD-1", "E2B4F816", _T0 + 60 * _SEC, _TEXT_A))
        await _ingest(storage, _chat("DL1CCC-1", "39A8D6C0", _T0 + 130 * _SEC, _TEXT_A))
        before = await _snapshot(storage)
        results.append(
            (
                "out-of-order: ingest flags only the later-id/later-time row as echo",
                [r[2] for r in before] == [0, 0, 1] and before[2][3] == "E2B4F816",
            )
        )
        await _reset_and_backfill(storage)
        results.append(
            (
                "out-of-order: backfill matches ingest on the explicit case",
                await _snapshot(storage) == before,
            )
        )

        # Random out-of-order stream (20% of rows carry an older time than their arrival).
        stream = _mixed_stream(400, seed=11, out_of_order=True)
        for src, mid, ts, text in stream:
            await _ingest(storage, _chat(src, mid, ts, text))
        live = await _snapshot(storage)
        times = [
            r["timestamp"]
            for r in await storage._query("SELECT timestamp FROM messages ORDER BY id")
        ]
        results.append(
            (
                "out-of-order: fixture really has older-time/higher-id rows",
                any(b < a for a, b in pairwise(times)),
            )
        )
        await _reset_and_backfill(storage, batch_size=37)
        results.append(
            (
                "out-of-order: backfill reproduces ingest on a shuffled stream",
                await _snapshot(storage) == live,
            )
        )
        await _reset_and_backfill(storage, batch_size=5)
        results.append(
            (
                "out-of-order: ... also with tiny pages (second hold-back)",
                await _snapshot(storage) == live,
            )
        )
    finally:
        await storage.close()


async def _test_backfill_failure_isolation(results: Results, tmp: str) -> None:
    storage = await _new_storage(tmp, "isolate", axes=load_axes())
    try:
        for i in range(8):
            await _ingest(
                storage,
                _chat("DL1BBB-1", f"{0xA0 + i:08X}", _T0 + i * 10 * _SEC, f"msg {i} number"),
            )
        live = await _snapshot(storage)
        real = load_axes()
        bad_sec = _T0 // _SEC + 30

        class _Flaky:
            def __getattr__(self, name: str) -> Any:
                return getattr(real.engine, name)

            def live_timing(self, prior: Any, t: float) -> str:
                if t == bad_sec:
                    raise RuntimeError("row failure")
                return real.engine.live_timing(prior, t)

        storage.set_axes(replace(real, engine=_Flaky()))  # type: ignore[arg-type]
        done = await _reset_and_backfill(storage)
        snap = await _snapshot(storage)
        marker = await storage.get_meta("axes_backfill_done:" + real.source_commit)
        results.append(
            (
                "backfill failure: only the failing row is NULL, others unchanged, run completes",
                done == 8
                and [r for r in snap if r[1] is None] == [(live[3][0], None, None, None)]
                and [r for r in snap if r[1] is not None]
                == [r for i, r in enumerate(live) if i != 3],
            )
        )
        results.append(("backfill failure: marker still set", bool(marker)))
    finally:
        await storage.close()


async def _test_live_keys_on_every_path(results: Results, tmp: str) -> None:
    storage = await _new_storage(tmp, "paths", axes=load_axes())
    try:
        keys = ("timing", "echo", "echo_of", "format")
        first = _chat("DL1BBB-1", "1A2B3C4D", _T0, _TEXT_A)
        await _ingest(storage, first)
        dup = await _ingest(storage, _chat("DL1BBB-1", "1A2B3C4D", _T0 + 100, _TEXT_A))
        beacon = await _ingest(storage, _chat("DL1BBB-1", "5E6F7081", _T0 + _SEC, "{CET}12:00:00"))
        ping = await _ingest(storage, _chat("DL1BBB-1", "92A3B4C5", _T0 + 2 * _SEC, "{ping}"))
        results.append(
            (
                "live keys: duplicate / not-stored beacon / linkcheck dicts carry all four keys",
                all(k in d for d in (dup, beacon, ping) for k in keys),
            )
        )
        results.append(
            (
                "live keys: nothing computed -> timing/echo/echo_of are None",
                all(d[k] is None for d in (dup, beacon, ping) for k in keys[:3]),
            )
        )
    finally:
        await storage.close()


async def _test_query_plans(results: Results, tmp: str) -> None:
    storage = await _new_storage(tmp, "plans", axes=load_axes())
    try:

        async def plan(sql: str, params: tuple[Any, ...]) -> str:
            rows = await storage._query("EXPLAIN QUERY PLAN " + sql, params)
            return " | ".join(r["detail"] for r in rows)

        prior = await plan(_AXES_PRIOR_SQL, ("X", _T0, 64))
        echo = await plan(_AXES_ECHO_SQL, (_T0, _T0 + 1, "X"))
        back = await plan(_AXES_BACKFILL_SQL, (10, 0, 0, 0, 500))
        prior_b = await plan(_AXES_PRIOR_BEFORE_SQL, ("X", _T0, 5, 64))
        results.append(
            (
                f"plan: prior times use idx_messages_type_src_timestamp ({prior})",
                "idx_messages_type_src_timestamp" in prior
                and "SCAN" not in prior.replace("SEARCH", ""),
            )
        )
        results.append(
            (
                f"plan: echo window uses an index range ({echo})",
                "SEARCH" in echo and "USING" in echo and "INDEX" in echo,
            )
        )
        results.append(
            (
                f"plan: id-bounded prior fallback uses idx_messages_type_src_timestamp ({prior_b})",
                "idx_messages_type_src_timestamp" in prior_b,
            )
        )
        results.append(
            (
                f"plan: backfill walks idx_messages_timestamp without a sorter ({back})",
                "SEARCH" in back and "idx_messages_timestamp" in back and "TEMP B-TREE" not in back,
            )
        )
    finally:
        await storage.close()


async def _test_backfill_perf(results: Results, tmp: str, n: int = 30_000) -> None:
    storage = await _new_storage(tmp, "perf", axes=load_axes())
    try:
        rng = random.Random(3)  # noqa: S311 - deterministic fixture, not crypto
        senders = [f"DL{i % 10}XX{i // 10}-{i % 5}" for i in range(400)]
        rows: list[tuple[Any, ...]] = []
        ts = _T0
        for i in range(n):
            ts += rng.randint(500, 4000)
            src = senders[rng.randrange(len(senders))]
            text = f"synthetic traffic message {rng.randrange(2000)} from the mesh hello there"
            rows.append((f"S{i:07d}", src, "*", text, "msg", ts))
        await storage._execute_many(
            "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        t0 = time.monotonic()
        done = await storage.backfill_message_axes()
        secs = time.monotonic() - t0
        # Second pass under tracemalloc (which slows the run ~3x) for the memory figure only.
        await storage._mutate("DELETE FROM classifier_meta WHERE key LIKE 'axes_backfill_done:%'")
        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        await storage.backfill_message_axes()
        peak = tracemalloc.get_traced_memory()[1] - base
        tracemalloc.stop()
        print(
            f"    axes backfill perf: {done} rows in {secs:.1f} s, "
            f"peak extra {peak / 1024 / 1024:.1f} MiB"
        )
        results.append(
            (
                (
                    f"perf: {n} rows backfilled in {secs:.1f} s, "
                    f"peak extra {peak / 1024 / 1024:.1f} MiB"
                ),
                done == n and peak < 64 * 1024 * 1024,
            )
        )
    finally:
        await storage.close()


async def _test_concurrent_ingest(results: Results, tmp: str) -> None:
    """Compute and INSERT are one critical section: two concurrent stores of an echo pair
    (different senders, same second) must leave the second-inserted row seeing the first,
    exactly as the backfill (lower id = earlier) computes it."""
    storage = await _new_storage(tmp, "concurrent", axes=load_axes())
    try:
        orig = storage._compute_axes

        async def _slow(*args: Any, **kw: Any) -> Any:
            out = await orig(*args, **kw)  # read the table ...
            await asyncio.sleep(0.05)  # ... then let the sibling task interleave
            return out

        storage._compute_axes = _slow
        a = _chat("DL1AAA-1", "AAAA0001", _T0, _TEXT_A)
        b = _chat("DL1BBB-1", "BBBB0002", _T0, _TEXT_A)
        await asyncio.gather(_ingest(storage, a), _ingest(storage, b))
        storage._compute_axes = orig
        rows = await storage._query("SELECT id, msg_id, echo, echo_of FROM messages ORDER BY id")
        first, second = rows[0], rows[1]
        results.append(
            (
                "concurrent ingest: the second-inserted row is an echo of the first",
                len(rows) == 2
                and first["echo"] == 0
                and second["echo"] == 1
                and second["echo_of"] == first["msg_id"],
            )
        )
        live = await _snapshot(storage)
        await _reset_and_backfill(storage)
        results.append(
            ("concurrent ingest: backfill agrees with ingest", await _snapshot(storage) == live)
        )
    finally:
        await storage.close()


async def _test_format_thread_safety(results: Results, tmp: str) -> None:
    """`_message_format` runs on the loop AND in to_thread workers; the engine memo is an
    unsynchronised LRU. With a tiny cap every call evicts, so an unguarded hit racing an
    eviction raises KeyError (swallowed into format=None)."""
    storage = await _new_storage(tmp, "threads", axes=load_axes(memo_cap=2))
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        texts = [f"station {i} temperature {i} wind rain humidity report" for i in range(40)]
        nones = 0
        errors: list[BaseException] = []
        guard = threading.Lock()

        def _hammer(offset: int) -> None:
            nonlocal nones
            bad = 0
            try:
                for n in range(4000):
                    if storage._message_format(texts[(n * 7 + offset) % len(texts)]) is None:
                        bad += 1
            except BaseException as exc:
                errors.append(exc)
            with guard:
                nones += bad

        threads = [threading.Thread(target=_hammer, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            await asyncio.to_thread(t.join)
        results.append(
            (
                f"format thread-safety: 8 threads x 4000 calls, memo cap 2, {nones} nulls",
                nones == 0 and not errors,
            )
        )
    finally:
        sys.setswitchinterval(old_interval)
        await storage.close()


async def _test_gate_and_blocklist(results: Results, tmp: str) -> None:
    from .. import main as main_module

    keys = ("timing", "echo", "echo_of", "format")
    storage = await _new_storage(tmp, "gate", axes=load_axes())
    try:
        no_type = {"src": "DL1BBB-1", "dst": "20", "msg": _TEXT_A}
        ble_cfg = transform_ble({"TYP": "SN", "GW": False})
        typ_only = {"TYP": "I", "src_type": "ble"}
        ble_src = {"src_type": "BLE", "msg": "x", "src": "DK5EN-1"}
        chat = _chat("DL1BBB-1", "0A0B0C0D", _T0, _TEXT_A)
        for d in (no_type, ble_cfg, typ_only, ble_src, chat):
            storage.annotate_axes_defaults(d)
        results.append(
            (
                "annotation gate: chat frames (with or without 'type') get the four keys",
                all(all(k in d for k in keys) for d in (no_type, chat)),
            )
        )
        results.append(
            (
                "annotation gate: BLE TYP / generic_ble / src_type BLE frames get none",
                not any(k in d for d in (ble_cfg, typ_only, ble_src) for k in keys),
            )
        )

        # Through the router: the cached register is the very dict storage saw.
        router = main_module.MessageRouter(storage)
        main_module._wire_ble_caches(router)
        await router.publish("ble", "ble_notification", {"TYP": "SN", "GW": False})
        results.append(
            (
                "annotation gate: cached BLE register keeps exactly its own keys",
                set(router.cached_ble_registers.get("SN", {})) == {"TYP", "GW"},
            )
        )

        # Blocklist redirect: quarantined group traffic is broadcast without store_message.
        class _Commands:
            blocked_callsigns = frozenset({"DL1BAD-1"})

        router.register_protocol("commands", _Commands())
        redirected = _chat("DL1BAD-1", "BAD00001", _T0, _TEXT_A, dst="20")
        dropped = _chat("DL1BAD-1", "BAD00002", _T0, _TEXT_A, dst="DK5EN-1")
        passed = _chat("DL1BBB-1", "0E0F1011", _T0, _TEXT_A, dst="20")
        for d in (redirected, dropped, passed):
            await router.publish("udp", "mesh_message", d)
        n_bad = await storage._query("SELECT COUNT(*) AS n FROM messages WHERE src = 'DL1BAD-1'")
        results.append(
            (
                "blocklist redirect: live dict carries the four keys, nothing stored",
                all(k in redirected for k in keys)
                and redirected["timing"] is None
                and isinstance(redirected["format"], dict)
                and n_bad[0]["n"] == 0,
            )
        )
        results.append(
            (
                "blocklist drop: untouched; normal frame: stored and annotated",
                not any(k in dropped for k in keys)
                and passed["timing"] == "one-shot"
                and isinstance(passed["format"], dict),
            )
        )
    finally:
        await storage.close()


async def _test_memo_cap(results: Results, tmp: str) -> None:
    results.append(
        (
            "memo cap: load_axes() pins the engine to MEMO_CAP (5000)",
            MEMO_CAP == 5000 and load_axes().engine.MEMO_CAP == 5000,
        )
    )
    results.append(
        ("memo cap: explicit override is honoured", load_axes(memo_cap=7).engine.MEMO_CAP == 7)
    )
    rt = load_axes()
    results.append(
        (
            "engine constants come from public accessors",
            rt.prior_limit == rt.engine.prior_limit
            and rt.echo_window_sec == rt.engine.echo_window_sec,
        )
    )


async def _test_backfill_mutation_guards(results: Results, tmp: str) -> None:
    """Two shapes the other backfill tests do not reach (each pinned by a mutant)."""
    # (a) a row whose same-sender history is mostly LATER-ARRIVED (higher id, earlier time):
    # the streaming deque fills up with those, the id filter leaves too few eligible times,
    # and the exact answer (older, lower-id rows) must come from the database.
    storage = await _new_storage(tmp, "deep", axes=load_axes())
    try:
        limit = storage._axes.prior_limit
        for i in range(10):
            await _ingest(
                storage, _chat("DL1DEP-1", f"{0xD000 + i:08X}", _T0 + i * 600 * _SEC, f"tick {i}")
            )
        await _ingest(
            storage,
            _chat("DL1DEP-1", "1A7E0001", _T0 + 200 * 600 * _SEC, "the row with a late time"),
        )
        for j in range(limit + 8):  # earlier time than the row above, but arrived after it
            await _ingest(
                storage,
                _chat("DL1DEP-1", f"{0xE000 + j:08X}", _T0 + (20 + j) * 600 * _SEC, f"tock {j}"),
            )
        live = await _snapshot(storage)
        await _reset_and_backfill(storage)
        results.append(
            (
                "backfill: > prior_limit later-arrived rows of the same sender == ingest",
                await _snapshot(storage) == live,
            )
        )
    finally:
        await storage.close()

    # (b) a second whose earlier-millisecond row has the higher id, split by a page boundary.
    storage = await _new_storage(tmp, "split", axes=load_axes())
    try:
        for i in range(3):
            await _ingest(
                storage, _chat("DL1FIL-1", f"F1110{i:03X}", _T0 + i * 10 * _SEC, f"filler {i}")
            )
        sec = _T0 + 100 * _SEC
        await _ingest(storage, _chat("DL1AAA-1", "5A5A0001", sec + 500, _TEXT_A))
        await _ingest(storage, _chat("DL1BBB-1", "5B5B0002", sec + 100, _TEXT_A))
        live = await _snapshot(storage)
        results.append(
            ("page split fixture: the lower-ms, higher-id row is an echo", live[-1][2] == 1)
        )
        # keyset order is [fillers, B, A]; batch_size 4 ends the first page right after B.
        await _reset_and_backfill(storage, batch_size=4)
        results.append(
            (
                "backfill: same-second rows split across a page boundary == ingest",
                await _snapshot(storage) == live,
            )
        )
    finally:
        await storage.close()


async def run_message_axes_tests() -> bool:
    """Run the message-axes regression suite. Returns True iff every case passes."""
    results: Results = []
    with tempfile.TemporaryDirectory() as tmp:
        await _test_timing_and_echo(results, tmp)
        await _test_wire_shape(results, tmp)
        await _test_no_engine_and_failing_engine(results, tmp)
        await _test_backfill_equivalence(results, tmp)
        await _test_out_of_order(results, tmp)
        await _test_backfill_failure_isolation(results, tmp)
        await _test_live_keys_on_every_path(results, tmp)
        await _test_concurrent_ingest(results, tmp)
        await _test_format_thread_safety(results, tmp)
        await _test_gate_and_blocklist(results, tmp)
        await _test_memo_cap(results, tmp)
        await _test_backfill_mutation_guards(results, tmp)
        await _test_query_plans(results, tmp)
        await _test_backfill_perf(results, tmp)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  message_axes: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import asyncio

    raise SystemExit(0 if asyncio.run(run_message_axes_tests()) else 1)
