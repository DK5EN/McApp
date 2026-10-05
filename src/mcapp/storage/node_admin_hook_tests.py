"""Regression suite for the Node Admin (RM1) reply-hook seam in `store_message`.

`IngestMixin.reply_hook` is called for every inbound `RM1 ...` text BEFORE
`_should_filter_message`, next to the gateway-uptime beacon hook. It only
observes. The same trap bit the link-check guard and the {CET} recorder: a hook
placed after an early return never fires for the frames it exists for (CLAUDE.md
"Link Check", "Gateway Uptime"). Plan: doc/2026-10-05_1000-node-admin-ui-
concept-and-plan.md §5 "Reply hook", §6.1 B3.

Cases:
  H1  Placement: the hook fires for an RM1 frame the filter DROPS (src_type
      'TEST'), and the filter's verdict is unchanged (no row stored). A second
      pin: for a stored frame the hook ran BEFORE the INSERT.
  H2  Both copies: UDP-shaped reply (with the `{087` ack suffix) then the
      `ble_remote` copy ~100 ms later, sequentially and concurrently: exactly
      one `messages` row, the hook invoked for BOTH copies with each copy's raw
      text (the service, not the seam, dedups).
  H3  A raising hook still stores the row and store_message returns normally.
  H4  Non-RM1 frames and non-str `msg` never call the hook.
  H5  Observe only: the stored row equals the row stored with no hook, and the
      hook did not alter the message dict.

Ephemeral tempfile SQLite DB per case; drives the REAL `store_message`.
All timestamps are milliseconds (project-wide DB convention).
"""

import asyncio
import copy
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage

logger = get_logger(__name__)

_T0 = 1_791_000_000_000
_GAP_MS = 107  # measured udp -> ble_remote gap of the production pair
_TAG = "a1b2c3d4e5f60718"
_REPLY = f"RM1 7 ok batt 4.1 {_TAG}"


def _reply(msg_id: str, src_type: str, ts: int, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "src": "DK5EN-90",
        "dst": "DK5EN-98",
        "msg": text,
        "type": "msg",
        "msg_id": msg_id,
        "timestamp": ts,
        "src_type": src_type,
        **extra,
    }


async def _count(storage: Any, msg_id: str) -> int:
    rows = await storage._query("SELECT COUNT(*) AS n FROM messages WHERE msg_id = ?", (msg_id,))
    return int(rows[0]["n"])


async def _total(storage: Any) -> int:
    rows = await storage._query("SELECT COUNT(*) AS n FROM messages", ())
    return int(rows[0]["n"])


async def _with_storage(name: str) -> tuple[Any, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    storage = await create_sqlite_storage(Path(tmp.name) / f"{name}.db")
    return storage, tmp


class _Recorder:
    """Async hook that records a deep copy of every message it is called with."""

    def __init__(self, storage: Any | None = None) -> None:
        self.seen: list[dict[str, Any]] = []
        self.rows_at_call: list[int] = []
        self._storage = storage

    async def __call__(self, message: dict[str, Any]) -> None:
        self.seen.append(copy.deepcopy(message))
        if self._storage is not None:
            self.rows_at_call.append(await _total(self._storage))


async def _raising_hook(message: dict[str, Any]) -> None:
    raise RuntimeError("hook exploded")


async def _test_placement(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("nah_h1")
    try:
        rec = _Recorder(storage)
        storage.reply_hook = rec

        # src_type 'TEST' is a legitimate _should_filter_message drop.
        dropped = _reply("H1000001", "TEST", _T0, _REPLY)
        assert storage._should_filter_message(dropped) is True
        await storage.store_message(dropped, "")
        results.append(
            (
                "H1 hook fires for an RM1 frame _should_filter_message drops",
                len(rec.seen) == 1 and rec.seen[0]["msg"] == _REPLY,
            )
        )
        results.append(
            (
                "H1 filter verdict unchanged: the dropped frame stores no row",
                await _count(storage, "H1000001") == 0,
            )
        )

        # Stored frame: the hook ran before the INSERT (row count 0 at call time).
        stored = _reply("H1000002", "udp", _T0 + 1, _REPLY)
        await storage.store_message(stored, "")
        results.append(
            (
                "H1 for a stored frame the hook ran before the INSERT",
                len(rec.seen) == 2
                and rec.rows_at_call[-1] == 0
                and await _count(storage, "H1000002") == 1,
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_both_copies(results: list[tuple[str, bool]]) -> None:
    udp_text = f"{_REPLY}{{087"
    for mode in ("sequential", "concurrent"):
        storage, tmp = await _with_storage(f"nah_h2_{mode}")
        try:
            rec = _Recorder()
            storage.reply_hook = rec
            udp = _reply("H2000001", "udp", _T0, udp_text, rssi=-97, snr=5.0)
            ble = _reply("H2000001", "ble_remote", _T0 + _GAP_MS, _REPLY)
            if mode == "sequential":
                await storage.store_message(udp, "")
                await asyncio.sleep(0.1)
                await storage.store_message(ble, "")
            else:
                await asyncio.gather(storage.store_message(udp, ""), storage.store_message(ble, ""))
            texts = sorted(m["msg"] for m in rec.seen)
            results.append(
                (
                    f"H2 {mode} udp+ble pair: one messages row (dedup unchanged)",
                    await _count(storage, "H2000001") == 1,
                )
            )
            results.append(
                (
                    f"H2 {mode} udp+ble pair: hook invoked for BOTH copies with each raw text",
                    texts == sorted([udp_text, _REPLY])
                    and sorted(m["src_type"] for m in rec.seen) == ["ble_remote", "udp"],
                )
            )
        finally:
            await storage.close()
            tmp.cleanup()


async def _test_raising_hook(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("nah_h3")
    try:
        storage.reply_hook = _raising_hook
        raised = False
        try:
            await storage.store_message(_reply("H3000001", "udp", _T0, _REPLY), "")
        except Exception:
            logger.exception("raising reply hook escaped store_message")
            raised = True
        results.append(
            (
                "H3 raising hook: store_message returns normally and the row is stored",
                not raised and await _count(storage, "H3000001") == 1,
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_non_rm1(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("nah_h4")
    try:
        rec = _Recorder()
        storage.reply_hook = rec
        cases: list[tuple[str, Any]] = [
            ("H4000001", "hello RM1 7 ok batt"),  # RM1 not at the start
            ("H4000002", "RM1"),  # no trailing space
            ("H4000003", "RM1x 7 ok"),  # prefix is 'RM1 ', not 'RM1'
            ("H4000004", "rm1 7 ok batt"),  # firmware tokens are upper-case RM1
            ("H4000005", " RM1 7 ok batt"),  # leading space
            ("H4000006", "ordinary chat"),
            ("H4000007", 12345),  # non-str msg
            ("H4000008", None),
            ("H4000009", ["RM1 7 ok"]),
        ]
        raised = False
        for i, (msg_id, text) in enumerate(cases):
            try:
                await storage.store_message(_reply(msg_id, "udp", _T0 + i, text), "")
            except Exception:
                logger.exception("non-RM1 frame %r raised out of store_message", text)
                raised = True
        no_msg = _reply("H400000A", "udp", _T0 + 50, "x")
        del no_msg["msg"]
        await storage.store_message(no_msg, "")
        results.append(
            ("H4 non-RM1 / non-str msg never call the hook", not rec.seen and not raised)
        )

        # Positive control, so "never called" cannot pass on a dead hook.
        await storage.store_message(_reply("H400000B", "udp", _T0 + 60, "RM1 1 ok"), "")
        results.append(("H4 control: an RM1 frame does call the hook", len(rec.seen) == 1))
    finally:
        await storage.close()
        tmp.cleanup()


async def _row_without_id(storage: Any, msg_id: str) -> dict[str, Any] | None:
    rows = await storage._query("SELECT * FROM messages WHERE msg_id = ?", (msg_id,))
    if not rows:
        return None
    row = dict(rows[0])
    row.pop("id", None)
    return row


async def _test_observe_only(results: list[tuple[str, bool]]) -> None:
    a, tmp_a = await _with_storage("nah_h5_none")
    b, tmp_b = await _with_storage("nah_h5_hook")
    try:
        rec = _Recorder()
        b.reply_hook = rec
        frame_a = _reply("H5000001", "udp", _T0, f"{_REPLY}{{087", rssi=-97, snr=5.0)
        frame_b = copy.deepcopy(frame_a)
        await a.store_message(frame_a, "")
        await b.store_message(frame_b, "")
        row_a = await _row_without_id(a, "H5000001")
        row_b = await _row_without_id(b, "H5000001")
        results.append(
            (
                "H5 stored row with the hook equals the row without it",
                row_a is not None and row_a == row_b and len(rec.seen) == 1,
            )
        )
        # The hook was handed the pre-store dict, untouched by the seam itself.
        expected = _reply("H5000001", "udp", _T0, f"{_REPLY}{{087", rssi=-97, snr=5.0)
        results.append(("H5 hook saw the unmodified inbound dict", rec.seen[0] == expected))
    finally:
        await a.close()
        await b.close()
        tmp_a.cleanup()
        tmp_b.cleanup()


async def run_node_admin_hook_tests() -> bool:
    """Run the Node Admin reply-hook seam suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []
    await _test_placement(results)
    await _test_both_copies(results)
    await _test_raising_hook(results)
    await _test_non_rm1(results)
    await _test_observe_only(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    node_admin_hook: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
