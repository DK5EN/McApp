"""Regression suite for internet-feed (`src_type == "www"`) frames in `store_message`.

A www frame is chat text another box published to meshmap; we never heard it on
RF. Each case below pins ONE guard in `IngestMixin.store_message` (all asking
`storage/www.py::is_www_frame`) and, where it can, runs the identical RF frame
as a positive control so the www case cannot pass vacuously:

  W1  `{CET}` www frame does not touch the gateway-uptime ledger (RF control does).
  W2  `RM1 ...` www frame does not call `reply_hook` (RF control does).
  W3  `:ackNNN` www frame does not mark our DM acked (RF control does).
  W4  `:stoNNN` www frame does not set the DM held (RF control does).
  W5  www `ack` / `tele` / `pos` frames are dropped unstored, no side tables touched.
  W6  www msg is stored as a normal row (src_type www, classified), and writes
      nothing to station_positions / signal_log; an rssi/snr on it is blanked.
  W7  RF first, then www: one row, still the RF src_type, RF signal kept.
  W8  www first, then RF: one row upgraded to the RF src_type, RF signal ingested.
  W8b www first, then a signal-free UDP copy (0/0 sentinel): still upgraded.
  W8c www first, restart (in-memory claim gone), then RF: DB backstop still upgrades.
  W9  Three www copies (re-sent ticks) stay one row.
  W10 A www replay / a www copy of a stored RF row is flagged for the www_message
      broadcast and push subscribers; an RF duplicate is not.
  W11 www_push_allowed: only a new, recent www frame may be pushed.

Ephemeral tempfile SQLite DB per case; drives the REAL `store_message`.
Timestamps are milliseconds (project-wide DB convention).
"""

import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .www import (
    WWW_DUPLICATE_KEY,
    WWW_PUSH_MAX_AGE_MS,
    is_www_duplicate,
    is_www_frame,
    www_push_allowed,
)

logger = get_logger(__name__)

_T0 = 1_791_500_000_000
ME = "DK5EN-98"
PEER = "DL2JA-2"
HOLDER = "DB0AAT-3"


def _frame(src_type: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "src": PEER,
        "dst": "20",
        "msg": "hello group",
        "type": "msg",
        "src_type": src_type,
        "timestamp": _T0,
    }
    base.update(fields)
    return base


async def _count(storage: Any, table: str, where: str = "1=1", params: tuple[Any, ...] = ()) -> int:
    rows = await storage._query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)  # noqa: S608 - fixed literals from this file
    return int(rows[0]["n"])


async def _row(storage: Any, msg_id: str) -> dict[str, Any] | None:
    rows = await storage._query("SELECT * FROM messages WHERE msg_id = ?", (msg_id,))
    return dict(rows[0]) if rows else None


async def _beacon_ms(storage: Any) -> int | None:
    rows = await storage._query("SELECT last_beacon_ms FROM link_uptime_state WHERE id = 1", ())
    return rows[0]["last_beacon_ms"] if rows else None


class _Hook:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, message: dict[str, Any]) -> None:
        self.calls += 1


async def _case(name: str, results: list[tuple[str, bool]], body: Any) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / f"{name}.db")
        try:
            await body(storage, results)
        except Exception as exc:
            logger.exception("www case %s raised", name)
            results.append((f"{name} raised {exc!r}", False))
        finally:
            await storage.close()


async def _w1(storage: Any, results: list[tuple[str, bool]]) -> None:
    beacon = "{CET}2026-10-11 10:00:00"
    await storage.store_message(
        _frame("www", src="OE1XAR-33", dst="*", msg=beacon, msg_id="A0000001"), ""
    )
    www_untouched = await _beacon_ms(storage) is None
    await storage.store_message(
        _frame("udp", src="OE1XAR-33", dst="*", msg=beacon, msg_id="A0000002"), ""
    )
    results.append(
        (
            "W1 www {CET} leaves the uptime ledger untouched (udp control records)",
            www_untouched and await _beacon_ms(storage) is not None,
        )
    )


async def _w2(storage: Any, results: list[tuple[str, bool]]) -> None:
    hook = _Hook()
    storage.reply_hook = hook
    text = "RM1 7 ok batt 4.1 a1b2c3d4e5f60718"
    await storage.store_message(
        _frame("www", src="DK5EN-90", dst=ME, msg=text, msg_id="A0000003"), ""
    )
    www_calls = hook.calls
    await storage.store_message(
        _frame("udp", src="DK5EN-90", dst=ME, msg=text, msg_id="A0000004"), ""
    )
    results.append(
        (
            "W2 www RM1 does not call reply_hook (udp control does)",
            www_calls == 0 and hook.calls == 1,
        )
    )


async def _own_dm(storage: Any) -> None:
    await storage.store_message(
        _frame("ble", src=ME, dst=PEER, msg="hello{123", msg_id="D0000001", timestamp=_T0),
        "",
    )


async def _w3(storage: Any, results: list[tuple[str, bool]]) -> None:
    await _own_dm(storage)
    await storage.store_message(
        _frame("www", src=PEER, dst=ME, msg=f"{ME} :ack123", msg_id="A0000005", timestamp=_T0 + 10),
        "",
    )
    row = await _row(storage, "D0000001")
    www_clean = row is not None and not row["acked"]
    await storage.store_message(
        _frame(
            "lora", src=PEER, dst=ME, msg=f"{ME} :ack123", msg_id="A0000006", timestamp=_T0 + 20
        ),
        "",
    )
    row = await _row(storage, "D0000001")
    results.append(
        (
            "W3 www :ack does not mark our DM acked (lora control does)",
            www_clean and bool(row and row["acked"]),
        )
    )


async def _w4(storage: Any, results: list[tuple[str, bool]]) -> None:
    await _own_dm(storage)
    notice = f"{ME} :sto123 {PEER}"
    await storage.store_message(
        _frame("www", src=HOLDER, dst=ME, msg=notice, msg_id="A0000007", timestamp=_T0 + 10), ""
    )
    row = await _row(storage, "D0000001")
    www_clean = row is not None and row["delivery_status"] is None
    await storage.store_message(
        _frame("lora", src=HOLDER, dst=ME, msg=notice, msg_id="A0000008", timestamp=_T0 + 20), ""
    )
    row = await _row(storage, "D0000001")
    results.append(
        (
            "W4 www :sto does not set held (lora control does)",
            www_clean and bool(row and row["delivery_status"] == "held"),
        )
    )


async def _w5(storage: Any, results: list[tuple[str, bool]]) -> None:
    await _own_dm(storage)
    await storage.store_message(
        _frame("www", type="ack", msg_id="D0000001", ack_type=0, timestamp=_T0 + 5), ""
    )
    row = await _row(storage, "D0000001")
    ack_ok = row is not None and not row["send_success"]
    await storage.store_message(
        _frame("www", type="tele", msg_id="A0000009", temp1=21.5, timestamp=_T0 + 6), ""
    )
    await storage.store_message(
        _frame("www", type="pos", msg_id="A000000A", lat=48.1, lon=11.5, timestamp=_T0 + 7), ""
    )
    results.append(
        (
            "W5 www ack/tele/pos dropped: no send_success, no row, no telemetry, no position",
            ack_ok
            and await _row(storage, "A0000009") is None
            and await _row(storage, "A000000A") is None
            and await _count(storage, "station_positions", "callsign = ?", (PEER,)) == 0
            and await _count(storage, "telemetry") == 0,
        )
    )


async def _w6(storage: Any, results: list[tuple[str, bool]]) -> None:
    await storage.store_message(
        _frame("www", msg_id="B0000001", rssi=-90, snr=5.0, hop_count=2, via="DB0AAT-3"), ""
    )
    row = await _row(storage, "B0000001")
    results.append(
        (
            "W6 www msg stored as a normal row (src_type www, rssi/snr blanked, via kept)",
            row is not None
            and row["src_type"] == "www"
            and row["type"] == "msg"
            and row["rssi"] is None
            and row["snr"] is None
            and row["via"] == "DB0AAT-3",
        )
    )
    results.append(
        (
            "W6 www msg writes no station_positions / signal_log",
            await _count(storage, "station_positions") == 0
            and await _count(storage, "signal_log") == 0,
        )
    )


async def _w7(storage: Any, results: list[tuple[str, bool]]) -> None:
    await storage.store_message(_frame("lora", msg_id="C0000001", rssi=-100, snr=5.0), "")
    await storage.store_message(_frame("www", msg_id="C0000001", timestamp=_T0 + 30_000), "")
    row = await _row(storage, "C0000001")
    results.append(
        (
            "W7 RF then www: one row, still lora, RF rssi kept",
            await _count(storage, "messages", "msg_id = ?", ("C0000001",)) == 1
            and row is not None
            and row["src_type"] == "lora"
            and row["rssi"] == -100,
        )
    )
    # An RF row is never relabelled by a later copy of another RF transport.
    await storage.store_message(_frame("ble_remote", msg_id="C0000001", timestamp=_T0 + 40_000), "")
    row = await _row(storage, "C0000001")
    results.append(
        ("W7 later RF copy never relabels an RF row", bool(row and row["src_type"] == "lora"))
    )


async def _w8(storage: Any, results: list[tuple[str, bool]]) -> None:
    await storage.store_message(_frame("www", msg_id="C0000002"), "")
    await storage.store_message(
        _frame("lora", msg_id="C0000002", rssi=-100, snr=5.0, timestamp=_T0 + 30_000), ""
    )
    row = await _row(storage, "C0000002")
    results.append(
        (
            "W8 www then RF: one row upgraded to lora, RF signal filled and logged",
            await _count(storage, "messages", "msg_id = ?", ("C0000002",)) == 1
            and row is not None
            and row["src_type"] == "lora"
            and row["rssi"] == -100
            and await _count(storage, "signal_log", "callsign = ?", (PEER,)) == 1,
        )
    )


async def _w8b(storage: Any, results: list[tuple[str, bool]]) -> None:
    # The upgrade must not depend on the RF copy carrying a signal: a udp copy's
    # 0/0 sentinel is blanked to None before `_enrich_duplicate_row`.
    await storage.store_message(_frame("www", msg_id="C0000004"), "")
    await storage.store_message(
        _frame("udp", msg_id="C0000004", rssi=0, snr=0, timestamp=_T0 + 30_000), ""
    )
    row = await _row(storage, "C0000004")
    results.append(
        (
            "W8b www then signal-free udp copy: one row upgraded to udp",
            await _count(storage, "messages", "msg_id = ?", ("C0000004",)) == 1
            and row is not None
            and row["src_type"] == "udp",
        )
    )


async def _w8c(storage: Any, results: list[tuple[str, bool]]) -> None:
    # A restart between the two copies empties the in-memory claim; the DB
    # backstop (`_find_duplicate_row_id`) must still reach the upgrade.
    await storage.store_message(_frame("www", msg_id="C0000005"), "")
    storage._recent_ingest.clear()
    await storage.store_message(
        _frame("lora", msg_id="C0000005", rssi=-100, snr=5.0, timestamp=_T0 + 30_000), ""
    )
    row = await _row(storage, "C0000005")
    results.append(
        (
            "W8c www, restart, then RF: one row upgraded to lora via the DB backstop",
            await _count(storage, "messages", "msg_id = ?", ("C0000005",)) == 1
            and row is not None
            and row["src_type"] == "lora",
        )
    )


async def _w9(storage: Any, results: list[tuple[str, bool]]) -> None:
    for tick in range(3):
        await storage.store_message(
            _frame("www", msg_id="C0000003", timestamp=_T0 + tick * 30_000), ""
        )
    row = await _row(storage, "C0000003")
    results.append(
        (
            "W9 three www copies stay one www row",
            await _count(storage, "messages", "msg_id = ?", ("C0000003",)) == 1
            and row is not None
            and row["src_type"] == "www",
        )
    )


async def _w10(storage: Any, results: list[tuple[str, bool]]) -> None:
    first = _frame("www", msg_id="C0000010")
    await storage.store_message(first, "")
    replay = _frame("www", msg_id="C0000010")
    await storage.store_message(replay, "")
    await storage.store_message(_frame("lora", msg_id="C0000011", rssi=-90, snr=4.0), "")
    late_www = _frame("www", msg_id="C0000011", timestamp=_T0 + 20_000)
    await storage.store_message(late_www, "")
    rf_repeat = _frame("lora", msg_id="C0000011", rssi=-91, snr=3.0, timestamp=_T0 + 40_000)
    await storage.store_message(rf_repeat, "")
    results.append(
        (
            "W10 storage flags a www replay and a www copy of a stored RF row, nothing else",
            WWW_DUPLICATE_KEY not in first
            and replay.get(WWW_DUPLICATE_KEY) is True
            and late_www.get(WWW_DUPLICATE_KEY) is True
            and WWW_DUPLICATE_KEY not in rf_repeat
            and is_www_duplicate(replay)
            and not is_www_duplicate(first),
        )
    )


def _push_gate(results: list[tuple[str, bool]]) -> None:
    now = _T0 + WWW_PUSH_MAX_AGE_MS
    fresh = _frame("www")
    results.append(
        (
            "W11 www_push_allowed: fresh new frame yes; replay, stale, untimed no",
            www_push_allowed(fresh, now)
            and not www_push_allowed({**fresh, WWW_DUPLICATE_KEY: True}, now)
            and not www_push_allowed(fresh, now + 1)
            and not www_push_allowed({**fresh, "timestamp": None}, now)
            and not www_push_allowed({**fresh, "timestamp": True}, now),
        )
    )


def _predicate(results: list[tuple[str, bool]]) -> None:
    results.append(
        (
            "W0 is_www_frame tolerates non-dict / non-str src_type",
            is_www_frame({"src_type": "www"})
            and not is_www_frame({"src_type": "lora"})
            and not is_www_frame({"src_type": 5})
            and not is_www_frame({})
            and not is_www_frame(None),
        )
    )


async def run_www_ingest_tests() -> bool:
    """Run the www ingest suite. Returns True iff every case passes."""
    results: list[tuple[str, bool]] = []
    _predicate(results)
    _push_gate(results)
    for name, body in (
        ("w1", _w1),
        ("w2", _w2),
        ("w3", _w3),
        ("w4", _w4),
        ("w5", _w5),
        ("w6", _w6),
        ("w7", _w7),
        ("w8", _w8),
        ("w8b", _w8b),
        ("w8c", _w8c),
        ("w9", _w9),
        ("w10", _w10),
    ):
        await _case(name, results, body)

    failed = [label for label, ok in results if not ok]
    for label, ok in results:
        logger.info("%s: %s", "PASS" if ok else "FAIL", label)
    if failed:
        logger.error("www ingest tests FAILED: %s", failed)
    return not failed
