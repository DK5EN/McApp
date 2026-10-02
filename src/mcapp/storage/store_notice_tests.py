"""Regression suite for the inline `:stoNNN` store-and-forward NOTICE text.

A store node tells the original sender "I am holding your DM" with a plain DM
`'%-9.9s:sto%03i[ <held destination>]'` (src = the holder, dst = the original
sender, e.g. `DB0AAT-3 -> DK5EN-98 "DK5EN-98 :sto071 DJ8MEH-81"`). It is the TEXT
twin of the binary 0x04 held frame and the only held signal on the extUDP path
(and behind a node without the 0x41 status frame). `store_message` must absorb it
like the inline `:ackNNN` text: set `delivery_status = 'held'` on the original,
write the `message_acks` ledger row, publish ONE `msg_status` in exactly the
0x04 held shape, and hide the notice row from history and unread.

Drives the REAL entry point (`storage.store_message`) against an ephemeral
tempfile SQLite DB, with a recording router, like `ack_status_tests.py`. All
timestamps are milliseconds. Registered in `scripts/run_startup_tests.py`.
"""

import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import DEDUP_WINDOW_MS

logger = get_logger(__name__)

ME = "DK5EN-98"
DEST = "DJ8MEH-81"
HOLDER = "DB0AAT-3"


class _RecordingRouter:
    """Minimal MessageRouter stand-in: only `.publish()`, recording every call."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, source: str, message_type: str, data: dict[str, Any]) -> None:
        self.published.append((source, message_type, data))


async def run_store_notice_tests() -> bool:  # noqa: PLR0915 - independent cases, each needs its own fixtures
    """Run the `:stoNNN` notice suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []
    base = now_ms() - 20_000_000  # inside every retention window, well before "now"

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(Path(tmp_dir) / "store_notice_test.db")
        router = _RecordingRouter()
        storage.set_message_router(router)
        try:
            clock = [base]

            def _tick(step: int = 10) -> int:
                clock[0] += step
                return clock[0]

            async def _send_dm(
                msg_id: str, echo: str, *, src: str = ME, dst: str = DEST, ts: int | None = None
            ) -> int:
                stamp = ts if ts is not None else _tick()
                await storage.store_message(
                    {
                        "msg_id": msg_id,
                        "src": src,
                        "dst": dst,
                        "msg": f"hello{{{echo}",
                        "type": "msg",
                        "src_type": "ble",
                        "timestamp": stamp,
                    },
                    "{}",
                )
                return stamp

            async def _notice(
                text: str,
                *,
                msg_id: str = "AB000001",
                dst: str = ME,
                src_type: str = "lora",
                ts: int | None = None,
            ) -> None:
                await storage.store_message(
                    {
                        "msg_id": msg_id,
                        "src": HOLDER,
                        "dst": dst,
                        "msg": text,
                        "type": "msg",
                        "src_type": src_type,
                        "timestamp": ts if ts is not None else _tick(),
                    },
                    "{}",
                )

            async def _row(msg_id: str) -> dict[str, Any]:
                rows = await storage._query(
                    "SELECT * FROM messages WHERE msg_id = ? AND type = 'msg'"
                    " ORDER BY timestamp ASC LIMIT 1",
                    (msg_id,),
                )
                return dict(rows[0])

            def _events() -> list[dict[str, Any]]:
                return [d for _, topic, d in router.published if topic == "msg_status"]

            def _no_effect(row: dict[str, Any]) -> bool:
                return row["delivery_status"] is None and row["send_success"] in (None, 0)

            # a) Real frame: sent DM + inbound notice from the holder.
            router.published.clear()
            await _send_dm("DM000071", "071")
            await _notice(f"{ME} :sto071 {DEST}", msg_id="AB000071")
            row = await _row("DM000071")
            acks = await storage.get_message_acks("DM000071")
            results.append(
                (
                    "a) :sto notice: original delivery_status='held', holder=store node",
                    row["delivery_status"] == "held" and row["holder"] == HOLDER,
                )
            )
            results.append(
                (
                    "a) :sto notice: send_success=1 (mirrors 0x04) and acked untouched",
                    row["send_success"] == 1 and not row["acked"],
                )
            )
            results.append(
                (
                    "a) :sto notice: one kind='held' ledger row from the store node",
                    [(a["kind"], a["from"]) for a in acks] == [("held", HOLDER)],
                )
            )
            results.append(
                (
                    "a) :sto notice: exactly one msg_status in the 0x04 held shape (via=lora)",
                    _events()
                    == [
                        {
                            "msg_id": "DM000071",
                            "sent": True,
                            "ack_kind": "held",
                            "from": HOLDER,
                            "via": "lora",
                            "holder": HOLDER,
                        }
                    ],
                )
            )

            # b) 0x04 binary then the same text, and the reverse: one ledger row.
            for order in ("0x04 then :sto", ":sto then 0x04"):
                tag = "B1" if order.startswith("0x04") else "B2"
                mid = f"DM0000{tag}"
                echo = "181" if tag == "B1" else "182"
                await _send_dm(mid, echo)
                frame = {
                    "type": "ack",
                    "msg_id": mid,
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": HOLDER,
                    "timestamp": _tick(),
                }
                if order.startswith("0x04"):
                    await storage.store_message(frame, "{}")
                    await _notice(f"{ME} :sto{echo} {DEST}", msg_id=f"AB0000{tag}")
                else:
                    await _notice(f"{ME} :sto{echo} {DEST}", msg_id=f"AB0000{tag}")
                    frame["timestamp"] = _tick()
                    await storage.store_message(frame, "{}")
                row = await _row(mid)
                acks = await storage.get_message_acks(mid)
                results.append(
                    (
                        f"b) {order}: still ONE ledger row, status held, holder unchanged",
                        [(a["kind"], a["from"]) for a in acks] == [("held", HOLDER)]
                        and row["delivery_status"] == "held"
                        and row["holder"] == HOLDER,
                    )
                )

            # c) Mismatches: no write, no publish.
            router.published.clear()
            await _send_dm("DM0000C1", "301")
            await _notice(f"{ME} :sto301 {DEST}", msg_id="AB0000C1", dst="DL9OTH-5")
            results.append(
                (
                    "c) wrong original sender (notice addressed to someone else): no write",
                    _no_effect(await _row("DM0000C1")),
                )
            )
            await _send_dm("DM0000C2", "302")
            await _notice(f"{ME} :sto302 DL9OTH-5", msg_id="AB0000C2")
            results.append(
                (
                    "c) wrong held destination: no write",
                    _no_effect(await _row("DM0000C2")),
                )
            )
            t_old = await _send_dm("DM0000C3", "303")
            await _notice(
                f"{ME} :sto303 {DEST}", msg_id="AB0000C3", ts=t_old + DEDUP_WINDOW_MS + 1000
            )
            results.append(
                (
                    "c) original older than the 1 h window: no write",
                    _no_effect(await _row("DM0000C3")),
                )
            )
            await _send_dm("DM0000C4", "304", src="DL9OTH-5")  # someone else's DM, same counter
            await _notice(f"{ME} :sto304 {DEST}", msg_id="AB0000C4")
            results.append(
                (
                    "c) counter match on ANOTHER station's DM: no write",
                    _no_effect(await _row("DM0000C4")),
                )
            )
            results.append(("c) no msg_status published by any mismatch", _events() == []))

            # d) acked / failed beat a later notice (rank enforced by the write).
            await _send_dm("DM0000D1", "401")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "DM0000D1",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "ack_from": DEST,
                    "timestamp": _tick(),
                },
                "{}",
            )
            await _notice(f"{ME} :sto401 {DEST}", msg_id="AB0000D1")
            row = await _row("DM0000D1")
            results.append(
                (
                    "d) acked message + later :sto notice stays acked",
                    row["delivery_status"] == "acked" and row["acked"] == 1,
                )
            )
            await _send_dm("DM0000D2", "402")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "DM0000D2",
                    "ack_type": 0x03,
                    "ack_type_text": "Store Failed",
                    "ack_from": DEST,
                    "timestamp": _tick(),
                },
                "{}",
            )
            await _notice(f"{ME} :sto402 {DEST}", msg_id="AB0000D2")
            results.append(
                (
                    "d) failed message + later :sto notice stays failed",
                    (await _row("DM0000D2"))["delivery_status"] == "failed",
                )
            )

            # e) No held destination: matches on sender + echo counter alone.
            await _send_dm("DM0000E1", "501")
            await _notice(f"{ME} :sto501", msg_id="AB0000E1")
            results.append(
                (
                    "e) :sto without destination matches on sender + echo_id",
                    (await _row("DM0000E1"))["delivery_status"] == "held",
                )
            )
            await _send_dm("DM0000E2", "502")
            await _notice(f"{ME}:sto502 {DEST}", msg_id="AB0000E2")  # no pad separator
            results.append(
                (
                    "e) :sto with no padding separator ('DK5EN-98:sto502 ...') still matches",
                    (await _row("DM0000E2"))["delivery_status"] == "held",
                )
            )
            await _send_dm("DM0000E3", "503")
            await _notice(f"{ME} :sto503 {DEST} extra words", msg_id="AB0000E3")
            results.append(
                (
                    "e) strict grammar: trailing words are not a notice (no write)",
                    _no_effect(await _row("DM0000E3")),
                )
            )

            # f) UDP copy + BLE text copy of one notice: same end state, one ledger row.
            await _send_dm("DM0000F1", "601")
            await _notice(f"{ME} :sto601 {DEST}", msg_id="AB0000F1", src_type="udp")
            await _notice(f"{ME} :sto601 {DEST}", msg_id="AB0000F1", src_type="ble_remote")
            row = await _row("DM0000F1")
            acks = await storage.get_message_acks("DM0000F1")
            results.append(
                (
                    "f) UDP + BLE copies of one notice: held once, one ledger row",
                    row["delivery_status"] == "held"
                    and row["holder"] == HOLDER
                    and [(a["kind"], a["from"]) for a in acks] == [("held", HOLDER)],
                )
            )
            # BLE-only copy: src_type "ble_remote" is outside the closed `via`
            # vocabulary, so it is recorded without `via`, like every BLE ack.
            router.published.clear()
            await _send_dm("DM0000F2", "602")
            await _notice(f"{ME} :sto602 {DEST}", msg_id="AB0000F2", src_type="ble_remote")
            results.append(
                (
                    "f) BLE-only copy: same held shape, no `via` key",
                    _events()
                    == [
                        {
                            "msg_id": "DM0000F2",
                            "sent": True,
                            "ack_kind": "held",
                            "from": HOLDER,
                            "holder": HOLDER,
                        }
                    ],
                )
            )

            # g) The notice rows are hidden from every read path.
            page = await storage.get_messages_page(HOLDER, src=ME, before_timestamp=now_ms())
            results.append(
                (
                    "g) get_messages_page (DM arm): the :sto notice row is excluded",
                    not any(":sto" in m for m in page["messages"]),
                )
            )
            page_own = await storage.get_messages_page(DEST, src=ME, before_timestamp=now_ms())
            results.append(
                (
                    "g) get_messages_page: the original DM still pages normally",
                    sum("hello{071" in m for m in page_own["messages"]) == 1,
                )
            )
            initial, legacy_summary = await storage.get_smart_initial_with_summary()
            blobs = list(initial["messages"]) + list(initial["acks"])
            results.append(
                (
                    "g) smart_initial messages AND acks: the original is there, no :sto notice",
                    any("hello{071" in m for m in blobs) and not any(":sto" in m for m in blobs),
                )
            )
            results.append(
                (
                    "g) legacy summary: no count for the notice-only conversation",
                    "DB0AAT<>DK5EN" not in legacy_summary,
                )
            )
            summary = await storage.get_conversation_summary(ME)
            holder_key = "DB0AAT<>DK5EN"
            results.append(
                (
                    "g) conversation summary: the notice-only conversation does not exist",
                    holder_key not in summary,
                )
            )
            results.append(
                (
                    "g) conversation summary: unread counts the originals, not the notices",
                    summary.get("DJ8MEH<>DK5EN", {}).get("unread") == 0,
                )
            )
            # The notice is hidden but a NORMAL message from the same holder is not.
            await _notice("hallo, Stefan hier", msg_id="AB00FFFF")
            summary2 = await storage.get_conversation_summary(ME)
            results.append(
                (
                    "g) a plain message from the holder still counts (count=1, unread=1)",
                    summary2.get(holder_key, {}).get("count") == 1
                    and summary2.get(holder_key, {}).get("unread") == 1,
                )
            )
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    store_notice: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
