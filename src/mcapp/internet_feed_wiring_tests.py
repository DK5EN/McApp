"""Regression suite for the internet feed glue in main.py / sse_handler.py / push.py.

Design: doc/2026-10-11_1005-internet-feed-concept.md §2, §4.3. The service, the
routes and the www ingest guards have their own suites; this one covers the
`www_message` topic wiring none of them can see.

  I1  `www_message` has exactly three subscribers, in order: storage, SSE
      broadcast, push. No command handler, no wire monitor.
  I2  A new, recent www message is stored, broadcast once and pushed once.
  I3  meshmap replaying the same message (reconnect lookback) is neither
      broadcast nor pushed again.
  I4  A backfilled message older than WWW_PUSH_MAX_AGE_MS is stored and
      broadcast, never pushed.
  I5  A replay storage never flagged (blocked / filtered, so never stored) is
      still broadcast only once (SSEManager seen-set).

Fully offline: real SQLite on a temp DB, a fake push dispatcher, no network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .internet_feed_types import WWW_MESSAGE_TOPIC
from .logging_setup import get_logger
from .main import MessageRouter
from .sqlite_storage import create_sqlite_storage
from .storage.www import WWW_PUSH_MAX_AGE_MS
from .util import now_ms

logger = get_logger(__name__)

OWN = "DK5EN-98"
PEER = "OE1ABC-12"


class _FakeDispatcher:
    def __init__(self) -> None:
        self.pushed: list[str] = []

    def start(self) -> None:
        return None

    async def handle_mesh_message(self, data: dict[str, Any], _own: str, _blocked: Any) -> None:
        self.pushed.append(str(data.get("msg_id")))


def _frame(msg_id: str, timestamp: int) -> dict[str, Any]:
    return {
        "src": PEER,
        "dst": "20",
        "msg": "hello from the internet",
        "msg_id": msg_id,
        "timestamp": timestamp,
        "type": "msg",
        "src_type": "www",
    }


async def run_internet_feed_wiring_tests() -> bool:
    from .sse_handler import SSEManager
    from .sse_routes.push import build_push_router

    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "wiring.db")
        try:
            router = MessageRouter(storage)
            router.set_callsign(OWN)
            manager = SSEManager("127.0.0.1", 0, message_router=router)
            dispatcher = _FakeDispatcher()
            build_push_router(
                manager,
                vapid={"public_key": "x", "private_key": "x", "sub": "mailto:a@example.com"},
                dispatcher=dispatcher,  # type: ignore[arg-type]  # duck-typed fake dispatcher
            )

            names = [
                getattr(h, "__name__", repr(h)) for h in router._subscribers[WWW_MESSAGE_TOPIC]
            ]
            results.append(
                (
                    "I1 www_message subscribers are storage, broadcast, push in order",
                    names == ["_storage_handler", "_www_broadcast_handler", "_on_www_message"],
                )
            )

            broadcast: list[str] = []

            async def _capture(routed: dict[str, Any]) -> None:
                broadcast.append(str(routed["data"].get("msg_id")))

            manager._broadcast_handler = _capture  # type: ignore[method-assign, assignment]  # capture the delegate

            now = now_ms()
            await router.publish("www", WWW_MESSAGE_TOPIC, _frame("D0000001", now - 5_000))
            rows = await storage._query("SELECT COUNT(*) AS n FROM messages", ())
            results.append(
                (
                    "I2 new recent www message: stored, broadcast once, pushed once",
                    rows[0]["n"] == 1
                    and broadcast == ["D0000001"]
                    and dispatcher.pushed == ["D0000001"],
                )
            )

            await router.publish("www", WWW_MESSAGE_TOPIC, _frame("D0000001", now - 5_000))
            results.append(
                (
                    "I3 replayed www message: not broadcast, not pushed again",
                    broadcast == ["D0000001"] and dispatcher.pushed == ["D0000001"],
                )
            )

            old_ts = now - WWW_PUSH_MAX_AGE_MS - 60_000
            await router.publish("www", WWW_MESSAGE_TOPIC, _frame("D0000002", old_ts))
            rows = await storage._query("SELECT COUNT(*) AS n FROM messages", ())
            results.append(
                (
                    "I4 backfilled old www message: stored and broadcast, not pushed",
                    rows[0]["n"] == 2
                    and broadcast == ["D0000001", "D0000002"]
                    and dispatcher.pushed == ["D0000001"],
                )
            )

            # A frame storage never reached (blocked sender, filtered text) has no
            # duplicate flag; the broadcast seen-set must still drop its replay.
            unstored = {"type": "www_message", "data": _frame("D0000003", now)}
            await manager._www_broadcast_handler(unstored)
            await manager._www_broadcast_handler(
                {"type": "www_message", "data": _frame("D0000003", now)}
            )
            results.append(
                (
                    "I5 unflagged www replay (never stored) is broadcast only once",
                    broadcast.count("D0000003") == 1,
                )
            )
        except Exception as exc:
            logger.exception("internet feed wiring suite raised")
            results.append((f"wiring suite raised {exc!r}", False))
        finally:
            await storage.close()

    failed = [label for label, ok in results if not ok]
    for label, ok in results:
        logger.info("%s: %s", "PASS" if ok else "FAIL", label)
    if failed:
        logger.error("internet feed wiring tests FAILED: %s", failed)
    return not failed
