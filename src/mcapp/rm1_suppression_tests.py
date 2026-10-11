"""Regression suite: RM1 remote-admin frames are hidden from chat (wave 1A).

  R1  `is_rm1_frame` truth table: commands and ok/err replies, with and without
      the `{NNN` ack suffix, match; upper-hex or 15/17-hex tag, leading-zero ctr,
      a human "RM1 is cool", and non-str input do not.
  R2  `RM1_HIDDEN_GLOBS` is a superset of the predicate (every accepted frame
      matches a glob) and does not hide a human message starting with "RM1".
  R3  Stored RM1 rows exist in `messages` but are absent from the connect
      snapshot, the DM page and the broadcast page; a normal DM still shows.
  R4  RM1 rows do not count in get_conversation_summary unread; the normal DM does.
  R6  Live SSE / RF Monitor: `broadcast_verdict` drops RM1 with reason `rm1`.
  R5  Push: `_on_mesh_message` does not dispatch an RM1 reply addressed to our own
      callsign, but does dispatch a normal DM.

Fully offline: real SQLite on a temp DB, a fake push dispatcher.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .main import MessageRouter
from .rm1_shape import RM1_HIDDEN_GLOBS, is_rm1_frame
from .sqlite_storage import create_sqlite_storage
from .util import now_ms

logger = get_logger(__name__)

OWN = "DK5EN-98"
PEER = "DK1ABC-1"
TAG = "a16efe171e22e144"

_ACCEPT = [
    f"RM1 1791705931 radio {TAG}",
    f"RM1 1791717840 atxt - {TAG}",
    f"RM1 1791705931 ok f=433.175 sf=11 cr=6 bw=250 p=2/2/22 {TAG}",
    f"RM1 7 err bad arg {TAG}",
    f"RM1 0 sync {TAG}",
    f"RM1 1791705931 ok status {TAG}{{123",
    f"RM1 1791717840 atxt - {TAG}{{7",
]
_REJECT: list[object] = [
    f"RM1 1791705931 radio {TAG.upper()}",
    f"RM1 1791705931 radio {TAG[:15]}",
    f"RM1 1791705931 radio {TAG}0",
    f"RM1 01791705931 radio {TAG}",
    "RM1 is cool",
    "RM1 5 radio",
    f"RM1 5 radio {TAG}{{12}}",
    f"RM1 5 radio {TAG} trailing",
    f"hi RM1 5 radio {TAG}",
    f"RM1  5 radio {TAG}",
    "",
    None,
    123,
    b"RM1 5 radio " + TAG.encode(),
]


def _glob_matches(text: str) -> bool:
    con = sqlite3.connect(":memory:")
    try:
        return any(
            con.execute("SELECT ? GLOB ?", (text, g)).fetchone()[0] for g in RM1_HIDDEN_GLOBS
        )
    finally:
        con.close()


class _FakeDispatcher:
    def __init__(self) -> None:
        self.pushed: list[str] = []

    def start(self) -> None:
        return None

    async def handle_mesh_message(self, data: dict[str, Any], _own: str, _blocked: Any) -> None:
        self.pushed.append(str(data.get("msg_id")))


def _frame(msg_id: str, msg: str, src: str, dst: str, ts: int) -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "msg": msg,
        "msg_id": msg_id,
        "timestamp": ts,
        "type": "msg",
        "src_type": "lora",
    }


async def run_rm1_suppression_tests() -> bool:
    from .sse_handler import SSEManager, broadcast_verdict
    from .sse_routes.push import build_push_router

    results: list[tuple[str, bool]] = []

    rm1_reply = {"src": "DK5EN-90", "dst": "DK5EN-1", "msg": f"RM1 1791705931 ok t=4 p=2 {TAG}"}
    chat = {"src": "DK5EN-90", "dst": "DK5EN-1", "msg": "RM1 is cool"}
    results.append(
        (
            "R6 live verdict drops RM1 (reason rm1, monitor shows it dimmed), not chat",
            broadcast_verdict(None, rm1_reply) == ("dropped", "rm1", None)
            and broadcast_verdict(None, chat)[0] == "shown",
        )
    )

    results.append(("R1a accepted shapes", all(is_rm1_frame(t) for t in _ACCEPT)))
    results.append(("R1b rejected shapes", not any(is_rm1_frame(t) for t in _REJECT)))
    results.append(
        ("R2a glob is a superset of the predicate", all(_glob_matches(t) for t in _ACCEPT))
    )
    results.append(
        (
            "R2b glob leaves human RM1 text alone",
            not any(_glob_matches(t) for t in ("RM1 is cool", "RM1 5 radio", f"hi RM1 5 {TAG}")),
        )
    )

    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "rm1.db")
        try:
            now = now_ms()
            frames = [
                _frame("B0000001", "hello there", PEER, OWN, now - 5_000),
                _frame(
                    "B0000002", f"RM1 1791705931 ok f=433.175 sf=11 {TAG}", PEER, OWN, now - 4_000
                ),
                _frame("B0000003", f"RM1 1791705932 ok status {TAG}{{044", PEER, OWN, now - 3_000),
                _frame("B0000004", f"RM1 1791705933 radio {TAG}", OWN, PEER, now - 2_000),
                _frame("B0000005", f"RM1 1791705934 ok x {TAG}", PEER, "*", now - 1_000),
            ]
            for f in frames:
                await storage.store_message(f, raw="")

            rows = await storage._query(
                "SELECT COUNT(*) AS n FROM messages WHERE msg LIKE 'RM1 %'", ()
            )
            stored_rm1 = rows[0]["n"]

            payload, _summary = await storage.get_smart_initial_with_summary()
            snap = "".join(payload["messages"])
            page = await storage.get_messages_page(OWN, src=PEER)
            dm_page = "".join(page["messages"])
            bpage = await storage.get_messages_page("*")
            b_page = "".join(bpage["messages"])
            results.append(
                (
                    "R3a RM1 rows stored in messages",
                    stored_rm1 == 4,
                )
            )
            results.append(
                (
                    "R3b connect snapshot hides RM1, keeps the normal DM",
                    "RM1 " not in snap and "hello there" in snap,
                )
            )
            results.append(
                (
                    "R3c DM page hides RM1, keeps the normal DM",
                    "RM1 " not in dm_page and "hello there" in dm_page,
                )
            )
            results.append(("R3d broadcast page hides RM1", "RM1 " not in b_page))

            summary = await storage.get_conversation_summary(OWN)
            dm = summary.get("DK1ABC<>DK5EN", {})
            results.append(
                (
                    "R4 unread counts only the normal DM",
                    dm.get("unread") == 1 and "*" not in summary,
                )
            )

            router = MessageRouter(storage)
            router.set_callsign(OWN)
            manager = SSEManager("127.0.0.1", 0, message_router=router)
            dispatcher = _FakeDispatcher()
            build_push_router(
                manager,
                vapid={"public_key": "x", "private_key": "x", "sub": "mailto:a@example.com"},
                dispatcher=dispatcher,  # type: ignore[arg-type]  # duck-typed fake dispatcher
            )
            await router.publish(
                "udp",
                "mesh_message",
                _frame("C0000001", f"RM1 1791705940 ok status {TAG}{{012", PEER, OWN, now),
            )
            pushed_after_rm1 = list(dispatcher.pushed)
            await router.publish(
                "udp", "mesh_message", _frame("C0000002", "plain dm", PEER, OWN, now + 10)
            )
            results.append(
                (
                    "R5 push: RM1 reply to us not dispatched, normal DM dispatched",
                    pushed_after_rm1 == [] and dispatcher.pushed == ["C0000002"],
                )
            )
        except Exception as exc:
            logger.exception("rm1 suppression suite raised")
            results.append((f"rm1 suppression suite raised {exc!r}", False))
        finally:
            await storage.close()

    failed = [label for label, ok in results if not ok]
    for label, ok in results:
        logger.info("%s: %s", "PASS" if ok else "FAIL", label)
    if failed:
        logger.error("rm1 suppression tests FAILED: %s", failed)
    return not failed
