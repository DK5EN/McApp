"""Built-in regression suite for msg_status ACK reporting (ingest.py).

Pins the fix for the "Delivered on nothing" bug: the firmware's 7-byte BINARY
BLE ack (`_handle_ack`, ack_type 0x00 "Node ACK" / 0x01 "Gateway ACK",
`ble_protocol.py:84-108`) means "my own node / a gateway took the frame off
the air" — a TRANSPORT fact — not "the addressee answered". Only the inline
`:ackNNN` text-frame path (a real reply FROM the addressee, matched against an
outbound message's `echo_id`) means peer delivery, and only that path may
publish `acked: True`.

Three SSE `msg_status` shapes are pinned here:

  * binary ack (0x00/0x01) -> {"msg_id": <ack_for_msg_id>, "sent": True,
                    "ack_kind": "node" | "gateway" | "unknown(...)"}
                   NEVER contains "acked".
  * binary Peer ACK (0x02, L1 decision) -> {"msg_id": <ack_for_msg_id>,
                    "acked": True, "ack_kind": "peer"}
                   the addressee's own matched :ack/:rej reply
                   (`lora_functions.cpp:857-896`) — mirrors the inline shape
                   below EXACTLY, and is published ONLY on an actual row match.
                   `ack_for_msg_id` IS already the original message's own
                   msg_id here (decoded straight off the wire), unlike the
                   inline path which resolves it via echo_id.
  * inline ack  -> {"msg_id": <ORIGINAL outbound row's own msg_id>,
                     "acked": True, "ack_kind": "peer"}
                   published ONLY when the UPDATE actually matched a row.

Real production shape that triggered the report: a message with
`send_success = 1` and `acked = 0` (three unanswered !ctcping probes to
DL3NCU-1) must never produce an `acked: True` event — case 5 pins exactly
that pair.

Ephemeral tempfile SQLite DB per run (never the live DB), mirroring
`linkcheck_ingest_tests.py` and this package's other `*_tests.py` modules.
Drives the REAL production entry point (`storage.store_message`), not a
reimplementation of the ACK-handling logic. A minimal recording router
(mirrors `MessageRouter.publish`'s signature, `push_tests.py`'s
`_StubMessageRouter`) captures every `msg_status` publish so each case can
assert on the exact payload.

All timestamps are milliseconds (project-wide DB convention).
"""

import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import ACK_MSG_ID_WINDOW_MS, DEDUP_WINDOW_MS, HELD_ACK_WINDOW_MS

logger = get_logger(__name__)

_BASE_TS = 1_786_688_000_000  # fixed ms timestamp so the suite is deterministic


class _RecordingRouter:
    """Minimal MessageRouter stand-in: only `.publish()`, recording every call.

    Mirrors the `(source, message_type, data)` signature of
    `MessageRouter.publish` (main.py) / `_message_router.publish` calls in
    ingest.py — no pubsub dispatch needed since these tests only assert on
    what was published, not on delivery to a subscriber.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, source: str, message_type: str, data: dict[str, Any]) -> None:
        self.published.append((source, message_type, data))


async def run_ack_status_tests() -> bool:  # noqa: PLR0915 - seven independent ACK-shape cases, each needs its own assertions
    """Run the msg_status ACK-reporting regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "ack_status_test.db"
        storage = await create_sqlite_storage(db_path)
        router = _RecordingRouter()
        storage.set_message_router(router)
        try:

            async def _row(msg_id: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM messages WHERE msg_id = ? AND type = 'msg'"
                    " ORDER BY timestamp DESC LIMIT 1",
                    (msg_id,),
                )
                return rows[0] if rows else None

            def _msg_status_events() -> list[dict[str, Any]]:
                return [data for _, topic, data in router.published if topic == "msg_status"]

            # 1. Binary Node ACK (ack_type=0x00): publishes sent/ack_kind="node",
            #    NEVER "acked" — and still sets send_success=1 on the original row.
            #    Pre-fix, this asserted {"msg_id": ..., "acked": True} and failed on
            #    the "sent" key (absent) and the "acked" key (present).
            router.published.clear()
            outbound_node = {
                "msg_id": "N0DE0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 1,
            }
            await storage.store_message(outbound_node, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "N0DE0001",
                    "ack_type": 0x00,
                    "ack_type_text": "Node ACK",
                    "timestamp": _BASE_TS + 2,
                },
                "{}",
            )
            node_row = await _row("N0DE0001")
            node_events = _msg_status_events()
            results.append(
                (
                    "binary Node ACK: send_success=1 on original row",
                    node_row is not None and node_row.get("send_success") == 1,
                )
            )
            results.append(
                (
                    'binary Node ACK: publishes {"sent": True, "ack_kind": "node"}, no "acked"',
                    node_events == [{"msg_id": "N0DE0001", "sent": True, "ack_kind": "node"}],
                )
            )

            # 2. Binary Gateway ACK (ack_type=0x01): ack_kind="gateway".
            router.published.clear()
            outbound_gw = {
                "msg_id": "GATE0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 3,
            }
            await storage.store_message(outbound_gw, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "GATE0001",
                    "ack_type": 0x01,
                    "ack_type_text": "Gateway ACK",
                    "timestamp": _BASE_TS + 4,
                },
                "{}",
            )
            gw_events = _msg_status_events()
            results.append(
                (
                    'binary Gateway ACK: publishes {"sent": True, "ack_kind": "gateway"}',
                    gw_events == [{"msg_id": "GATE0001", "sent": True, "ack_kind": "gateway"}],
                )
            )

            # 3. Inline :ackNNN matching an outbound echo_id: sets acked=1 on the
            #    ORIGINAL row AND publishes acked=True with the original message's
            #    own msg_id (not the echo suffix, not the replying frame's msg_id).
            #    Pre-fix, this path never published anything at all — the assertion
            #    on `inline_events` failed outright.
            router.published.clear()
            outbound_peer = {
                "msg_id": "PEER0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping{087",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 5,
            }
            await storage.store_message(outbound_peer, "{}")
            reply = {
                "msg_id": "REPLY001",
                "src": "DL3NCU-1",
                "dst": "DK5EN-98",
                # Real firmware layout is `%-9.9s:ack%03i` where the padded
                # field is the ORIGINAL SENDER, not the acking station — see
                # live capture 'DL2JA-73 :ack746' answering DL2JA-73's DM.
                # Nothing parses that field (the match keys on the FRAME's
                # src/dst, which carry no 9-char truncation), but a fixture
                # that misstates the wire is bad documentation.
                "msg": "DK5EN-98 :ack087",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 6,
            }
            await storage.store_message(reply, "{}")
            peer_row = await _row("PEER0001")
            inline_events = _msg_status_events()
            results.append(
                (
                    "inline :ack087: acked=1 on the original PEER0001 row",
                    peer_row is not None and peer_row.get("acked") == 1,
                )
            )
            results.append(
                (
                    (
                        'inline :ack087: publishes {"msg_id": "PEER0001", "acked": True,'
                        ' "ack_kind": "peer"}'
                    ),
                    inline_events == [{"msg_id": "PEER0001", "acked": True, "ack_kind": "peer"}],
                )
            )

            # 4. Inline :ackNNN matching nothing (no prior outbound with that
            #    echo_id): publishes nothing, marks nothing.
            router.published.clear()
            orphan_reply = {
                "msg_id": "REPLY002",
                "src": "DB0XYZ-1",
                "dst": "DK5EN-98",
                "msg": "DB0XYZ-1 :ack999",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 7,
            }
            await storage.store_message(orphan_reply, "{}")
            results.append(
                (
                    "inline :ack999 with no matching echo_id: publishes nothing",
                    _msg_status_events() == [],
                )
            )

            # 5. Production shape that triggered the report: three !ctcping probes
            #    DK5EN-98 -> DL3NCU-1, real callsigns, each firmware-Node-ACKed (the
            #    node took the frame off the air) but NEVER answered by the peer —
            #    send_success=1, acked=0 forever. None of the three may ever produce
            #    an acked:True event. Self-contained (own clear + own msg_ids) so it
            #    discriminates independently of case ordering/clears elsewhere.
            router.published.clear()
            probe_ids = ["PING0001", "PING0002", "PING0003"]
            for i, probe_id in enumerate(probe_ids):
                await storage.store_message(
                    {
                        "msg_id": probe_id,
                        "src": "DK5EN-98",
                        "dst": "DL3NCU-1",
                        "msg": "!ctcping",
                        "type": "msg",
                        "src_type": "ble",
                        "timestamp": _BASE_TS + 100 + 2 * i,
                    },
                    "{}",
                )
                await storage.store_message(
                    {
                        "type": "ack",
                        "msg_id": probe_id,
                        "ack_type": 0x00,
                        "ack_type_text": "Node ACK",
                        "timestamp": _BASE_TS + 101 + 2 * i,
                    },
                    "{}",
                )
            probe_rows = [await _row(probe_id) for probe_id in probe_ids]
            results.append(
                (
                    (
                        "production shape: 3 unanswered !ctcping probes"
                        " (DK5EN-98 -> DL3NCU-1) all carry send_success=1, acked=0,"
                        " and never publish an acked:True event"
                    ),
                    all(
                        row is not None and row.get("send_success") == 1 and row.get("acked") != 1
                        for row in probe_rows
                    )
                    and not any(data.get("acked") for data in _msg_status_events()),
                )
            )

            # 6. Binary Peer ACK (ack_type=0x02, L1 decision): the addressee's own
            #    matched :ack/:rej reply, mirrored to the EXACT same
            #    {msg_id, acked, ack_kind: "peer"} shape as the inline :ackNNN
            #    path (case 3 above) — never combined with the sent/ack_kind
            #    shape — and still sets send_success=1 on the original row (0x02
            #    implies the frame was heard by our own node too).
            router.published.clear()
            outbound_peer_ble = {
                "msg_id": "P2ER0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 10,
            }
            await storage.store_message(outbound_peer_ble, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "P2ER0001",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "timestamp": _BASE_TS + 11,
                },
                "{}",
            )
            peer_ble_row = await _row("P2ER0001")
            peer_ble_events = _msg_status_events()
            results.append(
                (
                    "binary Peer ACK (0x02): send_success=1 AND acked=1 on the original row",
                    peer_ble_row is not None
                    and peer_ble_row.get("send_success") == 1
                    and peer_ble_row.get("acked") == 1,
                )
            )
            results.append(
                (
                    (
                        'binary Peer ACK (0x02): publishes ONLY {"msg_id": "P2ER0001",'
                        ' "acked": True, "ack_kind": "peer"} — never "sent"'
                    ),
                    peer_ble_events == [{"msg_id": "P2ER0001", "acked": True, "ack_kind": "peer"}],
                )
            )

            # 7. Unknown ack_type: reported explicitly, not silently folded into
            #    "node". (0x02 must no longer land here — case 6 above pins that.)
            router.published.clear()
            outbound_unk = {
                "msg_id": "UNKN0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "!ctcping",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 8,
            }
            await storage.store_message(outbound_unk, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "UNKN0001",
                    "ack_type": 0x07,
                    "ack_type_text": "Unknown (7)",
                    "timestamp": _BASE_TS + 9,
                },
                "{}",
            )
            unk_events = _msg_status_events()
            results.append(
                (
                    (
                        "binary ACK with unrecognised ack_type: ack_kind names it, not"
                        ' silently "node"'
                    ),
                    len(unk_events) == 1
                    and unk_events[0].get("msg_id") == "UNKN0001"
                    and unk_events[0].get("sent") is True
                    and unk_events[0].get("ack_kind") not in ("node", "gateway"),
                )
            )

            # 7. Attribution (firmware proposal docs/ack-wer-hat-quittiert.md):
            #    an attributed Gateway ACK carries from/via on the event AND lands
            #    in message_acks; the SAME station's repeat is a no-op row-wise
            #    (the firmware will stop gating "first ACK only"); an unattributed
            #    repeat of the same kind collapses into one '' row; the peer ack is
            #    recorded under kind "peer"; get_message_acks maps '' -> None.
            router.published.clear()
            outbound_attr = {
                "msg_id": "ATTR0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "who acked this",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 10,
            }
            await storage.store_message(outbound_attr, "{}")
            gw_ack = {
                "type": "ack",
                "msg_id": "ATTR0001",
                "ack_type": 0x01,
                "ack_type_text": "Gateway ACK",
                "ack_from": "OE1XYZ-12",
                "ack_via": "lora",
                "timestamp": _BASE_TS + 11,
            }
            await storage.store_message(gw_ack, "{}")
            await storage.store_message({**gw_ack, "timestamp": _BASE_TS + 12}, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "ATTR0001",
                    "ack_type": 0x00,
                    "ack_type_text": "Node ACK",
                    "timestamp": _BASE_TS + 13,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "ATTR0001",
                    "ack_type": 0x00,
                    "ack_type_text": "Node ACK",
                    "timestamp": _BASE_TS + 14,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "ATTR0001",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "ack_from": "DL3NCU-1",
                    "ack_via": "udp",
                    "timestamp": _BASE_TS + 15,
                },
                "{}",
            )
            attr_events = _msg_status_events()
            results.append(
                (
                    'attributed Gateway ACK: event carries "from"/"via" beside sent/ack_kind',
                    len(attr_events) >= 1
                    and attr_events[0]
                    == {
                        "msg_id": "ATTR0001",
                        "sent": True,
                        "ack_kind": "gateway",
                        "from": "OE1XYZ-12",
                        "via": "lora",
                    },
                )
            )
            results.append(
                (
                    'unattributed Node ACK: event has NO "from"/"via" keys (legacy shape intact)',
                    len(attr_events) >= 3
                    and attr_events[2] == {"msg_id": "ATTR0001", "sent": True, "ack_kind": "node"},
                )
            )
            results.append(
                (
                    'attributed Peer ACK: event is the acked/peer shape plus "from"/"via"',
                    attr_events[-1]
                    == {
                        "msg_id": "ATTR0001",
                        "acked": True,
                        "ack_kind": "peer",
                        "from": "DL3NCU-1",
                        "via": "udp",
                    },
                )
            )
            acks = await storage.get_message_acks("ATTR0001")
            results.append(
                (
                    "message_acks: one row per (kind, station) — repeats collapse, '' -> None",
                    [(a["kind"], a["from"], a["via"]) for a in acks]
                    == [
                        ("gateway", "OE1XYZ-12", "lora"),
                        ("node", None, None),
                        ("peer", "DL3NCU-1", "udp"),
                    ],
                )
            )
            results.append(
                (
                    "message_acks: timestamps are the FIRST observation, oldest first",
                    [a["timestamp"] for a in acks] == [_BASE_TS + 11, _BASE_TS + 13, _BASE_TS + 15],
                )
            )
            # An ack for a msg_id we never sent must not populate the ledger.
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "NEVER001",
                    "ack_type": 0x01,
                    "ack_type_text": "Gateway ACK",
                    "ack_from": "OE1XYZ-12",
                    "timestamp": _BASE_TS + 16,
                },
                "{}",
            )
            results.append(
                (
                    "message_acks: an ack for an unknown msg_id records nothing",
                    await storage.get_message_acks("NEVER001") == [],
                )
            )

            # --- Store-and-forward DM status (0x03 failed / 0x04 held),
            # doc/2026-09-14_1153-store-forward-dm-status-plan.md §4/§5;
            # firmware spec MeshCom-Firmware-DEV-Main/docs/client-integration-
            # store-forward.md §2/§6. ---

            # 8. Binary Failed ACK (ack_type=0x03), attributed to the
            #    destination: send_success is NEVER set (defect 1 in the
            #    plan — a failed frame is the mesh giving up, not transport
            #    confirmation), delivery_status/holder persist, the
            #    msg_status payload carries acked=False/failed=True (the
            #    compatibility-floor key, plan §5) and NO "sent" key, and the
            #    ledger gets a kind="failed" row.
            router.published.clear()
            outbound_failed = {
                "msg_id": "FAIL0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 20,
            }
            await storage.store_message(outbound_failed, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "FAIL0001",
                    "ack_type": 0x03,
                    "ack_type_text": "Send Failed",
                    "ack_from": "DL3NCU-1",
                    "ack_via": "lora",
                    "timestamp": _BASE_TS + 21,
                },
                "{}",
            )
            failed_row = await _row("FAIL0001")
            failed_events = _msg_status_events()
            results.append(
                (
                    "0x03 failed: send_success is NEVER set (defect 1)",
                    failed_row is not None and failed_row.get("send_success") != 1,
                )
            )
            results.append(
                (
                    "0x03 failed: delivery_status='failed', holder=destination",
                    failed_row is not None
                    and failed_row.get("delivery_status") == "failed"
                    and failed_row.get("holder") == "DL3NCU-1",
                )
            )
            results.append(
                (
                    (
                        '0x03 failed: publishes {"acked": False, "failed": True,'
                        ' "ack_kind": "failed", "from"/"via"}, no "sent"'
                    ),
                    failed_events
                    == [
                        {
                            "msg_id": "FAIL0001",
                            "acked": False,
                            "failed": True,
                            "ack_kind": "failed",
                            "from": "DL3NCU-1",
                            "via": "lora",
                        }
                    ],
                )
            )
            failed_acks = await storage.get_message_acks("FAIL0001")
            results.append(
                (
                    "0x03 failed: message_acks gets a kind='failed' row",
                    [(a["kind"], a["from"]) for a in failed_acks] == [("failed", "DL3NCU-1")],
                )
            )

            # 9. Binary Held ACK (ack_type=0x04), attributed to the store
            #    node holding the DM: send_success IS set (the store node
            #    demonstrably took the frame off the air), delivery_status/
            #    holder persist, the payload carries sent=True plus BOTH
            #    "from" and "holder" (deliberate alias, plan §5), and the
            #    ledger gets a kind="held" row.
            router.published.clear()
            outbound_held = {
                "msg_id": "HELD0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 22,
            }
            await storage.store_message(outbound_held, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "HELD0001",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "OE1STO-1",
                    "ack_via": "lora",
                    "timestamp": _BASE_TS + 23,
                },
                "{}",
            )
            held_row = await _row("HELD0001")
            held_events = _msg_status_events()
            results.append(
                (
                    "0x04 held: send_success=1 (store node took the frame off the air)",
                    held_row is not None and held_row.get("send_success") == 1,
                )
            )
            results.append(
                (
                    "0x04 held: delivery_status='held', holder=store node",
                    held_row is not None
                    and held_row.get("delivery_status") == "held"
                    and held_row.get("holder") == "OE1STO-1",
                )
            )
            results.append(
                (
                    (
                        '0x04 held: publishes {"sent": True, "ack_kind": "held", "from" AND'
                        ' "holder" both set to the store node}'
                    ),
                    held_events
                    == [
                        {
                            "msg_id": "HELD0001",
                            "sent": True,
                            "ack_kind": "held",
                            "from": "OE1STO-1",
                            "via": "lora",
                            "holder": "OE1STO-1",
                        }
                    ],
                )
            )
            held_acks = await storage.get_message_acks("HELD0001")
            results.append(
                (
                    "0x04 held: message_acks gets a kind='held' row",
                    [(a["kind"], a["from"]) for a in held_acks] == [("held", "OE1STO-1")],
                )
            )
            # `_build_message_dict` surfaces both new columns so they survive
            # a reload (plan §2 surface table) — assert directly on it rather
            # than only through the row, since that's the function the
            # client-facing message JSON is actually built from.
            assert held_row is not None  # narrows for mypy; S101 is ignored for *_tests.py
            held_dict = storage._build_message_dict(held_row)
            results.append(
                (
                    "_build_message_dict surfaces delivery_status/holder for a held message",
                    held_dict.get("delivery_status") == "held"
                    and held_dict.get("holder") == "OE1STO-1",
                )
            )

            # 10. Spec §6 sequence 1: held -> acked ends acked. The second
            #     frame (peer ack, unattributed) must NOT blank the holder
            #     the held frame stored — COALESCE(NULL, holder) leaves it.
            router.published.clear()
            outbound_seq1 = {
                "msg_id": "SEQ10001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 24,
            }
            await storage.store_message(outbound_seq1, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "SEQ10001",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "OE1STO-1",
                    "timestamp": _BASE_TS + 25,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "SEQ10001",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "timestamp": _BASE_TS + 26,
                },
                "{}",
            )
            seq1_row = await _row("SEQ10001")
            results.append(
                (
                    "spec §6 sequence held -> acked: ends acked",
                    seq1_row is not None and seq1_row.get("delivery_status") == "acked",
                )
            )
            results.append(
                (
                    (
                        "spec §6 sequence held -> acked: an unattributed acked does"
                        " NOT blank the holder the held frame stored"
                    ),
                    seq1_row is not None and seq1_row.get("holder") == "OE1STO-1",
                )
            )

            # 11. Spec §6 sequence 2: acked -> held stays acked. The later
            #     held frame's rank (2) does not exceed the stored acked
            #     rank (4), so delivery_status/holder are left untouched —
            #     but the held frame still gets its own msg_status event and
            #     its own message_acks ledger row (publish/ledger are gated
            #     on "row exists", not on the rank test winning).
            router.published.clear()
            outbound_seq2 = {
                "msg_id": "SEQ20001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 27,
            }
            await storage.store_message(outbound_seq2, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "SEQ20001",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "ack_from": "DL3NCU-1",
                    "timestamp": _BASE_TS + 28,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "SEQ20001",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "OE1STO-2",
                    "timestamp": _BASE_TS + 29,
                },
                "{}",
            )
            seq2_row = await _row("SEQ20001")
            seq2_events = _msg_status_events()
            results.append(
                (
                    (
                        "spec §6 sequence acked -> held: stays acked (rank 2 does"
                        " not exceed the stored rank 4)"
                    ),
                    seq2_row is not None
                    and seq2_row.get("delivery_status") == "acked"
                    and seq2_row.get("holder") == "DL3NCU-1",
                )
            )
            results.append(
                (
                    (
                        "spec §6 sequence acked -> held: the held frame still"
                        " publishes its own msg_status event"
                    ),
                    len(seq2_events) == 2  # two acks sent, two events expected
                    and seq2_events[1].get("ack_kind") == "held",
                )
            )
            seq2_acks = await storage.get_message_acks("SEQ20001")
            results.append(
                (
                    (
                        "spec §6 sequence acked -> held: message_acks still gets the"
                        " held row even though delivery_status didn't move"
                    ),
                    [(a["kind"], a["from"]) for a in seq2_acks]
                    == [("peer", "DL3NCU-1"), ("held", "OE1STO-2")],
                )
            )

            # 12. Spec §6 sequence 3: failed -> acked ends acked.
            router.published.clear()
            outbound_seq3 = {
                "msg_id": "SEQ30001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 30,
            }
            await storage.store_message(outbound_seq3, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "SEQ30001",
                    "ack_type": 0x03,
                    "ack_type_text": "Send Failed",
                    "ack_from": "DL3NCU-1",
                    "timestamp": _BASE_TS + 31,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "SEQ30001",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "ack_from": "DL3NCU-1",
                    "timestamp": _BASE_TS + 32,
                },
                "{}",
            )
            seq3_row = await _row("SEQ30001")
            results.append(
                (
                    (
                        "spec §6 sequence failed -> acked: ends acked, and send_success"
                        " is set by the peer ack that followed"
                    ),
                    seq3_row is not None
                    and seq3_row.get("delivery_status") == "acked"
                    and seq3_row.get("send_success") == 1,
                )
            )

            # 13. held(A) -> held(B): equal rank, so the stored holder does
            #     NOT change (stays A) — deliberate trade-off for a
            #     race-proof precedence scheme, see _write_delivery_status.
            #     Both holders are preserved as two message_acks rows, and
            #     both frames publish their own msg_status event.
            router.published.clear()
            outbound_ab = {
                "msg_id": "HELDAB01",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 33,
            }
            await storage.store_message(outbound_ab, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "HELDAB01",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "OE1STO-A",
                    "timestamp": _BASE_TS + 34,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "HELDAB01",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "OE1STO-B",
                    "timestamp": _BASE_TS + 35,
                },
                "{}",
            )
            ab_row = await _row("HELDAB01")
            ab_events = _msg_status_events()
            results.append(
                (
                    "held(A) -> held(B): stored holder stays A (equal rank does not overwrite)",
                    ab_row is not None
                    and ab_row.get("delivery_status") == "held"
                    and ab_row.get("holder") == "OE1STO-A",
                )
            )
            results.append(
                (
                    "held(A) -> held(B): both frames still publish their own msg_status event",
                    len(ab_events) == 2  # two held frames, two events expected
                    and [e.get("holder") for e in ab_events] == ["OE1STO-A", "OE1STO-B"],
                )
            )
            ab_acks = await storage.get_message_acks("HELDAB01")
            results.append(
                (
                    "held(A) -> held(B): message_acks preserves BOTH holders as two rows",
                    [(a["kind"], a["from"]) for a in ab_acks]
                    == [("held", "OE1STO-A"), ("held", "OE1STO-B")],
                )
            )

            # 14. Unattributed (n=0) failed and held frames: no "from"/"holder"
            #     keys on the event, and the ledger row's from_call is '' ->
            #     surfaced as None by get_message_acks.
            router.published.clear()
            outbound_unattr_failed = {
                "msg_id": "UFAIL001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 36,
            }
            await storage.store_message(outbound_unattr_failed, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "UFAIL001",
                    "ack_type": 0x03,
                    "ack_type_text": "Send Failed",
                    "timestamp": _BASE_TS + 37,
                },
                "{}",
            )
            outbound_unattr_held = {
                "msg_id": "UHELD001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "hello",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 38,
            }
            await storage.store_message(outbound_unattr_held, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "UHELD001",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "timestamp": _BASE_TS + 39,
                },
                "{}",
            )
            unattr_events = _msg_status_events()
            results.append(
                (
                    'unattributed 0x03/0x04: no "from"/"holder" keys on either event',
                    unattr_events
                    == [
                        {
                            "msg_id": "UFAIL001",
                            "acked": False,
                            "failed": True,
                            "ack_kind": "failed",
                        },
                        {"msg_id": "UHELD001", "sent": True, "ack_kind": "held"},
                    ],
                )
            )
            unattr_failed_row = await _row("UFAIL001")
            unattr_held_row = await _row("UHELD001")
            results.append(
                (
                    "unattributed 0x03/0x04: delivery_status still persists, holder stays NULL",
                    unattr_failed_row is not None
                    and unattr_failed_row.get("delivery_status") == "failed"
                    and unattr_failed_row.get("holder") is None
                    and unattr_held_row is not None
                    and unattr_held_row.get("delivery_status") == "held"
                    and unattr_held_row.get("holder") is None,
                )
            )
            unattr_failed_acks = await storage.get_message_acks("UFAIL001")
            unattr_held_acks = await storage.get_message_acks("UHELD001")
            results.append(
                (
                    "unattributed 0x03/0x04: message_acks from_call is '' -> surfaced as None",
                    [(a["kind"], a["from"]) for a in unattr_failed_acks] == [("failed", None)]
                    and [(a["kind"], a["from"]) for a in unattr_held_acks] == [("held", None)],
                )
            )

            # 15. Regression: a `held` message later acked by an INLINE
            #     `:ackNNN` TEXT frame (not a binary 0x02) must end at
            #     delivery_status='acked'. The inline path sets `acked` on the
            #     row it found by echo_id; before this was wired it wrote no
            #     delivery_status at all, so the row read `held` AND `acked` at
            #     once and history contradicted the live event. The outbound
            #     text carries the firmware ack-request suffix, which is what
            #     store_message turns into the echo_id the inline path joins on.
            router.published.clear()
            outbound_inline = {
                "msg_id": "INLN0001",
                "src": "DK5EN-98",
                "dst": "DL3NCU-1",
                "msg": "are you there {042",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 40,
            }
            await storage.store_message(outbound_inline, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "INLN0001",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "DK5EN-90",
                    "timestamp": _BASE_TS + 41,
                },
                "{}",
            )
            held_then_row = await _row("INLN0001")
            results.append(
                (
                    "inline-ack regression: 0x04 first leaves the row held/DK5EN-90",
                    held_then_row is not None
                    and held_then_row.get("delivery_status") == "held"
                    and held_then_row.get("holder") == "DK5EN-90",
                )
            )
            await storage.store_message(
                {
                    "msg_id": "PEER0042",
                    "src": "DL3NCU-1",
                    "dst": "DK5EN-98",
                    "msg": "DK5EN-98 :ack042",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 42,
                },
                "{}",
            )
            inline_row = await _row("INLN0001")
            results.append(
                (
                    (
                        "inline :ackNNN after a hold writes delivery_status='acked'"
                        " (not just acked=1) and keeps the holder"
                    ),
                    inline_row is not None
                    and inline_row.get("acked") == 1
                    and inline_row.get("delivery_status") == "acked"
                    and inline_row.get("holder") == "DK5EN-90",
                )
            )
            results.append(
                (
                    "inline :ackNNN still publishes the unchanged peer payload",
                    {
                        "msg_id": "INLN0001",
                        "acked": True,
                        "ack_kind": "peer",
                    }
                    in _msg_status_events(),
                )
            )

            # 16. Inline-ack SCOPING (the echo_id collision, fixed 2026-09-14).
            #     `echo_id` is a 3-digit per-sender counter, unique only within
            #     that sender and ~1 hour, so the counter alone identifies
            #     nothing. Before the fix the lookup was `WHERE echo_id = ?
            #     ORDER BY timestamp DESC LIMIT 1` and a stranger's ack marked
            #     whichever message last used that number — on mcapp.local an
            #     own DM to DK1TCP-77 rendered ✓✓ Delivered because an
            #     unrelated DH6MAV pair reused 201. Each case below fails if
            #     either half of the guard (addressing, or the time window) is
            #     removed.
            router.published.clear()
            scoped_outbound = {
                "msg_id": "SCOPE001",
                "src": "DK5EN-98",
                "dst": "DK1TCP-77",
                "msg": "are you there {201",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 50,
            }
            await storage.store_message(scoped_outbound, "{}")
            # (a) An ack with the SAME counter from an unrelated pair: the
            #     mcapp.local case verbatim. Must not touch SCOPE001.
            await storage.store_message(
                {
                    "msg_id": "STRNGR01",
                    "src": "DH6MAV-10,DB4UW-1",
                    "dst": "DH6MAV-1",
                    "msg": "DH6MAV-1 :ack201",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 51,
                },
                "{}",
            )
            stranger_row = await _row("SCOPE001")
            results.append(
                (
                    "inline ack scoping: an unrelated pair's :ack201 does NOT ack our DM",
                    stranger_row is not None
                    and stranger_row.get("acked") != 1
                    and stranger_row.get("delivery_status") is None
                    and _msg_status_events() == [],
                )
            )
            # (b) Right counter, right sender, but addressed to someone else —
            #     the ack is not for us.
            await storage.store_message(
                {
                    "msg_id": "WRONGDST",
                    "src": "DK1TCP-77",
                    "dst": "DL9XYZ-3",
                    "msg": "DL9XYZ-3 :ack201",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 52,
                },
                "{}",
            )
            results.append(
                (
                    "inline ack scoping: right sender but addressed elsewhere does NOT ack our DM",
                    (r16b := await _row("SCOPE001")) is not None and r16b.get("acked") != 1,
                )
            )
            # (c) The genuine ack — same counter, correct pair — still matches.
            await storage.store_message(
                {
                    "msg_id": "GENUINE1",
                    "src": "DK1TCP-77,DB0ED-99",
                    "dst": "DK5EN-98",
                    "msg": "DK5EN-98 :ack201",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 53,
                },
                "{}",
            )
            genuine_row = await _row("SCOPE001")
            results.append(
                (
                    "inline ack scoping: the genuine via-routed ack DOES ack our DM",
                    genuine_row is not None
                    and genuine_row.get("acked") == 1
                    and genuine_row.get("delivery_status") == "acked"
                    and {"msg_id": "SCOPE001", "acked": True, "ack_kind": "peer"}
                    in _msg_status_events(),
                )
            )
            # (d) Time window: the counter's uniqueness horizon is ~1 hour
            #     (DEDUP_WINDOW_MS). A correctly-paired ack arriving after it
            #     is not evidence — the counter has had time to be reused.
            router.published.clear()
            await storage.store_message(
                {
                    "msg_id": "SCOPE002",
                    "src": "DK5EN-98",
                    "dst": "DK1TCP-77",
                    "msg": "much earlier {202",
                    "type": "msg",
                    "src_type": "ble",
                    "timestamp": _BASE_TS + 60,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "msg_id": "LATEACK1",
                    "src": "DK1TCP-77",
                    "dst": "DK5EN-98",
                    "msg": "DK5EN-98 :ack202",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 60 + DEDUP_WINDOW_MS + 1000,
                },
                "{}",
            )
            late_row = await _row("SCOPE002")
            results.append(
                (
                    (
                        "inline ack scoping: a correctly-paired ack past the 1h"
                        " counter horizon does NOT ack"
                    ),
                    late_row is not None
                    and late_row.get("acked") != 1
                    and _msg_status_events() == [],
                )
            )

            # 17. The store-and-forward exception to the 1h counter horizon.
            #     A held DM sits in a store node's mailbox until the
            #     destination reappears — up to --storetime (168h max, 24h
            #     default) — and is acked only then. A flat DEDUP_WINDOW_MS
            #     would refuse that ack and strand the message at `held`
            #     forever, breaking the feature the window was meant to
            #     protect. Relying on the binary 0x02 frame instead is not
            #     enough: the extUDP path has no binary ack, and neither does
            #     mc-chat, so for them this text is the ONLY signal.
            router.published.clear()
            held_late = {
                "msg_id": "HELDLATE",
                "src": "DK5EN-98",
                "dst": "DL3NCU-9",
                "msg": "see you tomorrow {301",
                "type": "msg",
                "src_type": "ble",
                "timestamp": _BASE_TS + 70,
            }
            await storage.store_message(held_late, "{}")
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "HELDLATE",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "DK5EN-90",
                    "timestamp": _BASE_TS + 71,
                },
                "{}",
            )
            # 20 hours later — far past the 1h counter horizon, well inside the
            # hold — the destination reappears and answers by TEXT.
            twenty_hours = 20 * 3600 * 1000
            await storage.store_message(
                {
                    "msg_id": "LATEPEER",
                    "src": "DL3NCU-9",
                    "dst": "DK5EN-98",
                    "msg": "DK5EN-98 :ack301",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 70 + twenty_hours,
                },
                "{}",
            )
            held_late_row = await _row("HELDLATE")
            results.append(
                (
                    "held DM acked by TEXT 20h later (past the 1h horizon) still resolves to acked",
                    held_late_row is not None
                    and held_late_row.get("acked") == 1
                    and held_late_row.get("delivery_status") == "acked"
                    and {"msg_id": "HELDLATE", "acked": True, "ack_kind": "peer"}
                    in _msg_status_events(),
                )
            )
            # The exception is scoped to HELD rows only: an ordinary message
            # past the horizon still gets no late match (case 16d), and even a
            # held one is bounded by HELD_ACK_WINDOW_MS.
            router.published.clear()
            await storage.store_message(
                {
                    "msg_id": "HELDGONE",
                    "src": "DK5EN-98",
                    "dst": "DL3NCU-9",
                    "msg": "ancient {302",
                    "type": "msg",
                    "src_type": "ble",
                    "timestamp": _BASE_TS + 80,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "HELDGONE",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "DK5EN-90",
                    "timestamp": _BASE_TS + 81,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "msg_id": "TOOLATE1",
                    "src": "DL3NCU-9",
                    "dst": "DK5EN-98",
                    "msg": "DK5EN-98 :ack302",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 80 + HELD_ACK_WINDOW_MS + 60_000,
                },
                "{}",
            )
            held_gone_row = await _row("HELDGONE")
            results.append(
                (
                    "a held DM's ack past HELD_ACK_WINDOW_MS is still refused (bounded, not open)",
                    held_gone_row is not None
                    and held_gone_row.get("acked") != 1
                    and held_gone_row.get("delivery_status") == "held",
                )
            )

            # 17. msg_id REUSE (the 2026-09-21 group-20 report). A firmware
            #     msg_id is ((_GW_ID & 0x3FFFFF) << 10) | node_msgid with
            #     node_msgid wrapping at 999, so one node's ids repeat every
            #     ~1000 originated frames — a median 24.8 h on DK5EN-98. Every
            #     ack binding used `WHERE msg_id = ? ORDER BY timestamp DESC
            #     LIMIT 1` with no time bound, and the ledger key
            #     (msg_id, kind, from_call) carries no message identity at all,
            #     so a group broadcast rendered "Acknowledged by OE5HWN-12" from
            #     the peer ack of an unrelated DM sent a day earlier under the
            #     same msg_id, while losing its own three node acks to
            #     INSERT OR IGNORE. Both halves are pinned here.
            router.published.clear()
            reuse_gap = 24 * 3600 * 1000 + 45 * 60 * 1000  # 24.75 h, the observed gap
            await storage.store_message(
                {
                    "msg_id": "REUSE001",
                    "src": "DK5EN-98",
                    "dst": "OE5HWN-12",
                    "msg": "the DM that owned this counter first",
                    "type": "msg",
                    "src_type": "ble",
                    "timestamp": _BASE_TS + 100,
                },
                "{}",
            )
            for kind_from, ack_type_ in (("DB0ED-99", 0x00), ("OE5HWN-12", 0x02)):
                await storage.store_message(
                    {
                        "type": "ack",
                        "msg_id": "REUSE001",
                        "ack_type": ack_type_,
                        "ack_type_text": "ACK",
                        "ack_from": kind_from,
                        "timestamp": _BASE_TS + 101,
                    },
                    "{}",
                )
            old_dm_row = await _row("REUSE001")
            old_dm_id = old_dm_row["id"] if old_dm_row is not None else None
            # A day later the counter comes back around on a GROUP message.
            await storage.store_message(
                {
                    "msg_id": "REUSE001",
                    "src": "DK5EN-98",
                    "dst": "20",
                    "msg": "GA aus Freising",
                    "type": "msg",
                    "src_type": "ble",
                    "timestamp": _BASE_TS + 100 + reuse_gap,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "REUSE001",
                    "ack_type": 0x00,
                    "ack_type_text": "Node ACK",
                    "ack_from": "DB0ED-99",
                    "timestamp": _BASE_TS + 101 + reuse_gap,
                },
                "{}",
            )
            reuse_acks = await storage.get_message_acks("REUSE001")
            results.append(
                (
                    (
                        "msg_id reuse: the new message shows ONLY its own ack,"
                        " never the previous owner's peer ack"
                    ),
                    [(a["kind"], a["from"]) for a in reuse_acks] == [("node", "DB0ED-99")],
                )
            )
            results.append(
                (
                    (
                        "msg_id reuse: the new ack is NOT swallowed by the old row's"
                        " identical (msg_id, kind, from_call) key"
                    ),
                    len(reuse_acks) == 1
                    and reuse_acks[0]["timestamp"] == _BASE_TS + 101 + reuse_gap,
                )
            )
            old_dm_after = await storage._query(
                "SELECT acked, send_success, delivery_status FROM messages WHERE id = ?",
                (old_dm_id,),
            )
            results.append(
                (
                    "msg_id reuse: the day-old DM row is untouched by the new message's ack",
                    len(old_dm_after) == 1
                    and old_dm_after[0]["acked"] == 1
                    and old_dm_after[0]["delivery_status"] == "acked",
                )
            )
            new_group_row = await _row("REUSE001")
            results.append(
                (
                    "msg_id reuse: the new group message gets send_success, never acked",
                    new_group_row is not None
                    and new_group_row.get("dst") == "20"
                    and new_group_row.get("send_success") == 1
                    and new_group_row.get("acked") != 1,
                )
            )

            # 17b. The read clamp alone, on rows written BEFORE the prune
            #      existed: a stale ledger row under a reused msg_id is hidden
            #      without any migration or backfill.
            await storage._mutate(
                "INSERT OR REPLACE INTO message_acks"
                " (msg_id, kind, from_call, via, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("REUSE001", "peer", "OE5HWN-12", None, _BASE_TS + 101),
            )
            results.append(
                (
                    "read clamp: a pre-fix ledger row older than ACK_MSG_ID_WINDOW_MS is hidden",
                    [(a["kind"], a["from"]) for a in await storage.get_message_acks("REUSE001")]
                    == [("node", "DB0ED-99")],
                )
            )

            # 17c. The `held` carve-out survives the clamp: a store-and-forward
            #      DM is legitimately acked days later, so neither the binding
            #      nor the ledger may be clamped to 4 h for it.
            await storage.store_message(
                {
                    "msg_id": "HELDWIN1",
                    "src": "DK5EN-98",
                    "dst": "DL3NCU-7",
                    "msg": "held for a long time",
                    "type": "msg",
                    "src_type": "ble",
                    "timestamp": _BASE_TS + 200,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "HELDWIN1",
                    "ack_type": 0x04,
                    "ack_type_text": "Store Held",
                    "ack_from": "DK5EN-90",
                    "timestamp": _BASE_TS + 201,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "type": "ack",
                    "msg_id": "HELDWIN1",
                    "ack_type": 0x02,
                    "ack_type_text": "Peer ACK",
                    "ack_from": "DL3NCU-7",
                    "timestamp": _BASE_TS + 200 + 72 * 3600 * 1000,
                },
                "{}",
            )
            held_win_row = await _row("HELDWIN1")
            results.append(
                (
                    "held carve-out: a binary 0x02 at +72 h still binds past the 4 h clamp",
                    held_win_row is not None
                    and held_win_row.get("acked") == 1
                    and held_win_row.get("delivery_status") == "acked",
                )
            )
            results.append(
                (
                    "held carve-out: the ledger keeps BOTH the held row and the late peer ack",
                    [(a["kind"], a["from"]) for a in await storage.get_message_acks("HELDWIN1")]
                    == [("held", "DK5EN-90"), ("peer", "DL3NCU-7")],
                )
            )

            # 17d. The window boundary itself, both sides.
            for msg_id_, offset, expect_bound in (
                ("EDGEIN01", ACK_MSG_ID_WINDOW_MS - 6 * 60 * 1000, True),
                ("EDGEOUT1", ACK_MSG_ID_WINDOW_MS + 6 * 60 * 1000, False),
            ):
                await storage.store_message(
                    {
                        "msg_id": msg_id_,
                        "src": "DK5EN-98",
                        "dst": "DL3NCU-8",
                        "msg": "boundary probe",
                        "type": "msg",
                        "src_type": "ble",
                        "timestamp": _BASE_TS + 300,
                    },
                    "{}",
                )
                await storage.store_message(
                    {
                        "type": "ack",
                        "msg_id": msg_id_,
                        "ack_type": 0x00,
                        "ack_type_text": "Node ACK",
                        "ack_from": "DB0ED-99",
                        "timestamp": _BASE_TS + 300 + offset,
                    },
                    "{}",
                )
                edge_row = await _row(msg_id_)
                bound = (
                    edge_row is not None
                    and edge_row.get("send_success") == 1
                    and len(await storage.get_message_acks(msg_id_)) == 1
                )
                results.append(
                    (
                        (
                            f"window boundary: an ack at {offset / 3600000:.1f} h"
                            f" {'binds' if expect_bound else 'is refused'}"
                        ),
                        bound is expect_bound,
                    )
                )

            # 18. The inline `:ackNNN` path writes the ledger too. It was the
            #     one "the addressee answered" route that recorded nothing, so
            #     a text peer ack rendered ✓✓ Delivered with an empty
            #     "Acknowledged by" — and text is the ONLY form of a peer ack
            #     on the extUDP path and in mc-chat, neither of which has a
            #     binary 0x02 frame.
            await storage.store_message(
                {
                    "msg_id": "INLLED01",
                    "src": "DK5EN-98",
                    "dst": "DL3NCU-6",
                    "msg": "did you get this {077",
                    "type": "msg",
                    "src_type": "ble",
                    "timestamp": _BASE_TS + 400,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "msg_id": "PEERTXT1",
                    "src": "DL3NCU-6",
                    "dst": "DK5EN-98",
                    "msg": "DK5EN-98 :ack077",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 401,
                },
                "{}",
            )
            inline_ledger = await storage.get_message_acks("INLLED01")
            results.append(
                (
                    (
                        "inline :ackNNN records a peer ledger row attributed to the"
                        " answering station, with the ack's transport as via"
                    ),
                    [(a["kind"], a["from"], a["via"]) for a in inline_ledger]
                    == [("peer", "DL3NCU-6", "lora")],
                )
            )
            # The scoping rules still gate it: an unmatched :ackNNN records
            # nothing, exactly like the publish it sits beside.
            await storage.store_message(
                {
                    "msg_id": "INLLED02",
                    "src": "DK5EN-98",
                    "dst": "DL3NCU-5",
                    "msg": "unanswered {078",
                    "type": "msg",
                    "src_type": "ble",
                    "timestamp": _BASE_TS + 402,
                },
                "{}",
            )
            await storage.store_message(
                {
                    "msg_id": "PEERTXT2",
                    "src": "DL9ZZZ-1",
                    "dst": "DK5EN-98",
                    "msg": "DK5EN-98 :ack078",
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": _BASE_TS + 403,
                },
                "{}",
            )
            results.append(
                (
                    "inline :ackNNN from an unrelated station records NO ledger row",
                    await storage.get_message_acks("INLLED02") == [],
                )
            )

        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    ack_status: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
