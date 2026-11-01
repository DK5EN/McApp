"""Regression suite for `wire_log.py` (Wire record v1 raw-frame persistence).

Drives the real `WireLog` writer thread (no sleeps: `flush()` joins the queue)
on a fake clock, plus the real UDP/BLE hooks and the WireMonitor ring.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ble_client_remote import BLEClientRemote, set_wire_log
from .commands.constants import has_console
from .config_loader import Config
from .main import MessageRouter
from .udp_handler import UDPHandler
from .wire_log import WireLog, mcproxy_node
from .wire_monitor import WireMonitor

RecordFn = Callable[[str, bool], None]

_NODE = "mcproxy-DK5EN-98"
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _new(directory: Path, clock: _Clock, **kw: Any) -> WireLog:
    return WireLog(directory, node_provider=lambda: _NODE, clock=clock, **kw)


def _test_records(record: RecordFn, tmp: Path) -> None:
    clock = _Clock(datetime(2026, 11, 2, 10, 15, 3, 412000, tzinfo=UTC))
    wl = _new(tmp / "rec", clock)
    keep = b"KEEP\x00\x01"
    wl.log("rx", "udp1799", b'{"type":"msg"}', "192.168.1.5:1799")
    wl.log("tx", "udp1799", keep, "192.168.1.5:1799")
    wl.log("rx", "ble", b"\x40\x01\x02")
    wl.flush()
    wl.close()
    rows = _lines(tmp / "rec" / "2026-11-02.jsonl")
    rx, tx, ble = rows
    record(
        "rx record matches Wire record v1",
        rx
        == {
            "v": 1,
            "ts": "2026-11-02T10:15:03.412Z",
            "ts_real": "2026-11-02T10:15:03.412Z",
            "src": "mcproxy",
            "node": _NODE,
            "dir": "rx",
            "chan": "udp1799",
            "peer": "192.168.1.5:1799",
            "len": 14,
            "hex": b'{"type":"msg"}'.hex(),
        }
        and bool(_TS_RE.match(rx["ts"])),
    )
    record(
        "tx record has dir=tx, peer, ind and full hex",
        tx["dir"] == "tx"
        and tx["peer"] == "192.168.1.5:1799"
        and tx["ind"] == "KEEP"
        and tx["len"] == 6
        and tx["hex"] == keep.hex(),
    )
    record(
        "ble record has chan=ble and no peer",
        ble["chan"] == "ble"
        and "peer" not in ble
        and ble["hex"] == "400102"
        and ble["dir"] == "rx",
    )


def _test_rotation(record: RecordFn, tmp: Path) -> None:
    d = tmp / "rot"
    clock = _Clock(datetime(2026, 11, 2, 23, 59, 59, 900000, tzinfo=UTC))
    wl = _new(d, clock)
    wl.log("rx", "ble", b"day1")
    wl.flush()
    clock.now += timedelta(seconds=1)
    wl.log("rx", "ble", b"day2")
    wl.flush()
    wl.close()
    gz = d / "2026-11-02.jsonl.gz"
    ok = gz.exists() and not (d / "2026-11-02.jsonl").exists() and (d / "2026-11-03.jsonl").exists()
    day1 = (
        [json.loads(x) for x in gzip.decompress(gz.read_bytes()).decode().splitlines()]
        if ok
        else []
    )
    record("rotation gzips day 1 and starts day 2", ok and day1[0]["hex"] == b"day1".hex())

    # A stale plain file from a crashed run is compressed at startup, never pruned.
    d2 = tmp / "stale"
    d2.mkdir()
    (d2 / "2026-01-01.jsonl").write_text('{"v":1}\n', encoding="utf-8")
    wl2 = _new(d2, _Clock(datetime(2026, 11, 2, tzinfo=UTC)))
    wl2.log("rx", "ble", b"x")
    wl2.flush()
    wl2.close()
    record("stale previous-day file is gzipped at start", (d2 / "2026-01-01.jsonl.gz").exists())


def _test_overflow(record: RecordFn, tmp: Path) -> None:
    wl = _new(
        tmp / "ovf", _Clock(datetime(2026, 11, 2, tzinfo=UTC)), queue_size=3, start_thread=False
    )
    t0 = time.monotonic()
    for _ in range(10):
        wl.log("rx", "ble", b"x")
    elapsed = time.monotonic() - t0
    record("queue overflow drops and counts without blocking", wl.dropped == 7 and elapsed < 1.0)


def _test_unwritable(record: RecordFn, tmp: Path) -> None:
    blocker = tmp / "file"
    blocker.write_text("x", encoding="utf-8")
    wl = _new(blocker / "sub", _Clock(datetime(2026, 11, 2, tzinfo=UTC)))
    raised = False
    try:
        wl.log("rx", "ble", b"x")
        wl.log("tx", "ble", b"y")
        wl.flush()
        wl.close()
    except Exception:
        raised = True
    record(
        "unwritable dir raises nothing and drops",
        not raised and wl.written == 0 and wl.dropped == 2,
    )


def _test_disabled(record: RecordFn, tmp: Path) -> None:
    d = tmp / "off"
    wl = WireLog(d, enabled=False, node_provider=lambda: _NODE)
    wl.log("rx", "ble", b"x")
    wl.flush()
    wl.close()
    record("disabled flag writes nothing", not d.exists() and wl.written == 0)


def _ble_client() -> BLEClientRemote:
    return BLEClientRemote("http://127.0.0.1:9", message_router=MessageRouter(None))


async def _test_hooks_and_ring(record: RecordFn, tmp: Path) -> None:
    clock = _Clock(datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC))
    wl = _new(tmp / "hooks", clock)
    router = MessageRouter(None)
    monitor = WireMonitor()
    monitor.router = router
    router.subscribe("mesh_message", monitor.on_mesh_message)
    handler = UDPHandler(
        listen_port=0,
        target_host="127.0.0.1",
        target_port=9,
        message_router=router,
        wire_log=wl,
    )
    # Stub send socket: records the bytes so nothing leaves the host.
    sent: list[bytes] = []

    class _Sock:
        def sendto(self, data: bytes, _addr: tuple[str, int]) -> int:
            sent.append(data)
            return len(data)

        def close(self) -> None:
            return None

    sock = _Sock()
    handler._ensure_send_socket = lambda: sock  # type: ignore[assignment,return-value,method-assign]
    raw = json.dumps(
        {
            "src_type": "node",
            "type": "msg",
            "src": "DL1ABC-1",
            "dst": "*",
            "msg": "hello",
            "msg_id": "AABBCCDD",
        }
    ).encode()
    before = len(monitor.page()["frames"])
    await handler._process_received_message(raw, ("127.0.0.1", 1799))
    await handler.send_message({"type": "msg", "dst": "*", "msg": "out"})
    garbage = b"\xff\xfe\x00\x80not-json\xc3\x28"
    with contextlib.suppress(Exception):  # the listener loop swallows decode trouble
        await handler._process_received_message(garbage, ("203.0.113.9", 4242))
    set_wire_log(wl)
    try:
        await _ble_client()._dispatch_sse_event(
            "notification",
            json.dumps({"format": "binary", "raw_base64": base64.b64encode(b"@abc").decode()}),
        )
    finally:
        set_wire_log(None)
    wl.flush()
    wl.close()
    rows = _lines(tmp / "hooks" / "2026-11-02.jsonl")
    record(
        "UDP rx hook logs the raw bytes before decode",
        rows[0]["dir"] == "rx"
        and rows[0]["chan"] == "udp1799"
        and rows[0]["peer"] == "127.0.0.1:1799"
        and rows[0]["hex"] == raw.hex(),
    )
    record(
        "UDP tx hook logs the sent bytes to the target",
        len(sent) == 1
        and rows[1]["dir"] == "tx"
        and rows[1]["peer"] == "127.0.0.1:9"
        and rows[1]["hex"] == sent[0].hex(),
    )
    record(
        "non-UTF8 datagram from an untrusted IP is logged (hook is before decode and trust filter)",
        rows[2]["dir"] == "rx"
        and rows[2]["peer"] == "203.0.113.9:4242"
        and rows[2]["hex"] == garbage.hex(),
    )
    record(
        "BLE hook logs decoded raw_base64",
        rows[3]["chan"] == "ble" and rows[3]["hex"] == b"@abc".hex(),
    )
    record(
        "the 2000 ring still receives the UDP frame",
        len(monitor.page()["frames"]) == before + 1,
    )


def _gz_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in gzip.decompress(path.read_bytes()).decode().splitlines()]


def _run_start(d: Path, today: datetime) -> None:
    wl = _new(d, _Clock(today))
    wl.log("rx", "ble", b"x")
    wl.flush()
    wl.close()


def _test_crash_leftovers(record: RecordFn, tmp: Path) -> None:
    today = datetime(2026, 11, 2, tzinfo=UTC)
    # (a) power cut mid-gzip: truncated .gz.tmp next to the intact plain file.
    d = tmp / "crash_tmp"
    d.mkdir()
    (d / "2026-01-01.jsonl").write_text('{"v":1,"n":1}\n', encoding="utf-8")
    (d / "2026-01-01.jsonl.gz.tmp").write_bytes(b"\x1f\x8b\x08\x00truncated")
    _run_start(d, today)
    ok_a = (
        not (d / "2026-01-01.jsonl.gz.tmp").exists()
        and not (d / "2026-01-01.jsonl").exists()
        and (d / "2026-01-01.jsonl.gz").exists()
        and _gz_lines(d / "2026-01-01.jsonl.gz") == [{"v": 1, "n": 1}]
    )
    record("leftover .gz.tmp is removed and the plain day re-compressed", ok_a)

    # (b) day already has a .gz: new content goes to <day>.<n>.jsonl.gz, never appended.
    d = tmp / "crash_exists"
    d.mkdir()
    first = d / "2026-01-01.jsonl.gz"
    first.write_bytes(gzip.compress(b'{"v":1,"n":1}\n'))
    before = first.read_bytes()
    (d / "2026-01-01.jsonl").write_text('{"v":1,"n":2}\n', encoding="utf-8")
    _run_start(d, today)
    second = d / "2026-01-01.1.jsonl.gz"
    ok_b = (
        first.read_bytes() == before
        and second.exists()
        and _gz_lines(second) == [{"v": 1, "n": 2}]
        and not (d / "2026-01-01.jsonl").exists()
    )
    (d / "2026-01-01.jsonl").write_text('{"v":1,"n":3}\n', encoding="utf-8")
    _run_start(d, today)
    third = d / "2026-01-01.2.jsonl.gz"
    ok_b = ok_b and third.exists() and _gz_lines(third) == [{"v": 1, "n": 3}]
    record("existing .gz is never appended: next free <day>.<n>.jsonl.gz", ok_b)

    # (c) power cut after the rename but before the unlink: no duplicate member.
    d = tmp / "crash_renamed"
    d.mkdir()
    body = b'{"v":1,"n":9}\n'
    (d / "2026-01-01.jsonl.gz").write_bytes(gzip.compress(body))
    (d / "2026-01-01.jsonl").write_bytes(body)
    _run_start(d, today)
    record(
        "plain file already contained in the .gz is dropped without a duplicate member",
        not (d / "2026-01-01.jsonl").exists()
        and not (d / "2026-01-01.1.jsonl.gz").exists()
        and gzip.decompress((d / "2026-01-01.jsonl.gz").read_bytes()) == body,
    )


def _test_node_id(record: RecordFn, tmp: Path) -> None:
    clock = _Clock(datetime(2026, 11, 2, 10, 0, 0, tzinfo=UTC))
    wl = WireLog(tmp / "node", node_provider=lambda: mcproxy_node("DK5EN-98"), clock=clock)
    wl.log("rx", "udp1799", b"DATA", "10.0.0.1:1799")
    wl.flush()
    wl.close()
    rows = _lines(tmp / "node" / "2026-11-02.jsonl")
    record(
        "node is the writer: mcproxy-<callsign>; rx = datagram arriving from the node",
        mcproxy_node("DK5EN-98") == "mcproxy-DK5EN-98"
        and mcproxy_node(None) == "mcproxy-unknown"
        and rows[0]["node"] == "mcproxy-DK5EN-98"
        and rows[0]["dir"] == "rx",
    )


async def _test_ble_replay(record: RecordFn, tmp: Path) -> None:
    wl = _new(tmp / "blereplay", _Clock(datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)))
    client = _ble_client()

    async def _fake_request(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"registers": {"I": {"TYP": "I", "CALL": "DK5EN-98"}, "SN": {"TYP": "SN"}}}

    client._request = _fake_request  # type: ignore[method-assign]
    set_wire_log(wl)
    try:
        await client._replay_cached_registers()
        wl.flush()
        replay_written = wl.written
        await client._dispatch_sse_event(
            "notification",
            json.dumps({"format": "binary", "raw_base64": base64.b64encode(b"@live").decode()}),
        )
        wl.flush()
    finally:
        set_wire_log(None)
        wl.close()
    rows = _lines(tmp / "blereplay" / "2026-11-02.jsonl")
    record(
        "cached-register replay writes no wire record; a live notification writes one",
        replay_written == 0 and len(rows) == 1 and rows[0]["hex"] == b"@live".hex(),
    )


def _test_config_bool(record: RecordFn) -> None:
    saved = os.environ.pop("MCAPP_WIRE_LOG_ENABLED", None)

    def _file(value: object) -> bool:
        kw: dict[str, Any] = {}
        Config._apply_wire_log_overrides(kw, {"WIRE_LOG_ENABLED": value})
        return bool(kw["wire_log_enabled"])

    def _env(value: str, file_value: object = True) -> bool:
        os.environ["MCAPP_WIRE_LOG_ENABLED"] = value
        try:
            kw: dict[str, Any] = {}
            Config._apply_wire_log_overrides(kw, {"WIRE_LOG_ENABLED": file_value})
            return bool(kw["wire_log_enabled"])
        finally:
            del os.environ["MCAPP_WIRE_LOG_ENABLED"]

    try:
        record(
            "config file WIRE_LOG_ENABLED parses false/'false'/'0'/'off' as off, true/'1' as on",
            not _file(False)
            and not _file("false")
            and not _file("0")
            and not _file("off")
            and _file(True)
            and _file("true")
            and _file("1"),
        )
        record(
            "env MCAPP_WIRE_LOG_ENABLED overrides the config file in both directions",
            not _env("false", True) and _env("1", False) and _env("true", "false"),
        )
    finally:
        if saved is not None:
            os.environ["MCAPP_WIRE_LOG_ENABLED"] = saved


async def run_wire_log_tests() -> bool:
    """Return True iff every WireLog regression case passes."""
    if has_console:
        print("\nTesting WireLog (Wire record v1):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'PASS' if ok else 'FAIL'} | {label}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _test_records(_record, tmp)
        _test_rotation(_record, tmp)
        _test_overflow(_record, tmp)
        _test_unwritable(_record, tmp)
        _test_disabled(_record, tmp)
        _test_crash_leftovers(_record, tmp)
        _test_node_id(_record, tmp)
        _test_config_bool(_record)
        await _test_ble_replay(_record, tmp)
        await _test_hooks_and_ring(_record, tmp)

    return all(ok for _, ok in results)
