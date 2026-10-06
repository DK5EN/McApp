"""Regression suite for the Node Admin integration code in `main.py` (RM1 remote admin).

Plan: doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md §5 (send path, redaction),
§6.1 "Orchestrator". The service, the routes and the ingest hook have their own suites;
this one covers the glue none of them can see: `MessageRouter.send_node_admin`
(through the real `_handle_outbound`, `_send_via_ble`, `_send_via_udp` and the RF
Monitor TX capture), `_redact_rm1`, the `/api/status` feature flag, and one LOOPBACK
that wires service + router + storage + SecretBox the way `build_app` does and plays
the NODE with a reference implementation built only from `remote_cmd` primitives.

Fully offline, transmits nothing: fake UDP / BLE transports, a real `WireMonitor`
(ring only, no SSE), real SQLite on a temp DB, a real `SecretBox` on a temp key file
and a hand-driven clock.

Cases (each one fails on a named mutation, see the task log):

  W1  send_node_admin over 'udp' and 'ble': the transport receives dst and msg
      byte-identical, the call returns None, exactly one TX capture 'sent'.
  W2  failing transports (BLE not connected / no client / UDP raises / no UDP
      handler): the reason is returned, the capture is 'failed', and neither the
      `msg_status` send_failed event nor the error toast carries the tag.
  W3  a frame that never reaches `send` (own callsign as dst, a suppressed
      command) returns a non-None reason, transmits nothing, captures 'suppressed'.
  W4  `_redact_rm1` truth table, and the `_udp_message_handler` INFO line has no tag.
  W5  loopback, BLE and UDP: sync, command, the node's reply in BOTH shapes (UDP
      copy with `{087`, BLE copy without) through real `store_message`: verified
      exactly once, hwm raised, one verified broadcast, 10 s spacing, next counter;
      a node signing with another password leaves `bad_tag`.
  W6  `/api/status` `features` carries 'node_admin' only when the manager has a service.
  W7  failed monitor frame has no tag, sent one keeps it; the DEBUG 'Processing' line has none.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import tempfile
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import main as main_module
from . import remote_cmd
from .main import MessageRouter
from .node_admin_service import NodeAdminService
from .node_admin_types import NodeAdminBusyError
from .secret_box import SecretBox
from .sqlite_storage import SQLiteStorage, create_sqlite_storage
from .sse_handler import SSEManager
from .sse_routes.stream import build_stream_router
from .wire_monitor import WireMonitor

Record = Callable[[str, bool], None]

T0 = 1_791_000_000_000
TARGET = "DK5EN-90"
ATTACHED = "DK5EN-14"
PASSWD = "secret"
OTHER_PASSWD = "other-pass"
BIG_CTR = 1_791_234_567  # a real unix-time counter: the tag then sits past the 40-char log cut


def _tag_of(text: str) -> str:
    return text.rsplit(" ", 1)[1]


def _command(ctr: int, cmd: str, args: str = "", passwd: str = PASSWD) -> str:
    key = remote_cmd.derive_key(passwd)
    return remote_cmd.build_command_text(key, TARGET, ATTACHED, ctr, cmd, args)


# ── fakes ──────────────────────────────────────────────────────────────────


class FakeUDP:
    """Stand-in UDP protocol: `send_message(dict)`; records what reached the wire."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def send_message(self, message_data: dict[str, Any]) -> None:
        self.calls.append(dict(message_data))
        if self.fail is not None:
            raise self.fail


class FakeBLE:
    """Stand-in BLE client: `send_message(msg, group) -> bool`, `is_connected` property."""

    def __init__(self, *, connected: bool = True, result: bool = True) -> None:
        self.is_connected = connected
        self.result = result
        self.calls: list[tuple[str | None, str | None]] = []

    async def send_message(self, msg: str | None, group: str | None) -> bool:
        self.calls.append((msg, group))
        return self.result


class Bench:
    """A real `MessageRouter` with a real `WireMonitor` and recording subscribers."""

    def __init__(self, *, udp: FakeUDP | None, ble: FakeBLE | None) -> None:
        self.router = MessageRouter(None)
        self.router.set_callsign(ATTACHED)
        self.monitor = WireMonitor()
        self.router.wire_monitor = self.monitor
        self.udp = udp
        self.ble = ble
        if udp is not None:
            self.router.register_protocol("udp", udp)
        if ble is not None:
            self.router.register_protocol("ble_client", ble)
        self.statuses: list[dict[str, Any]] = []
        self.toasts: list[dict[str, Any]] = []

        async def _status(routed: dict[str, Any]) -> None:
            self.statuses.append(dict(routed["data"]))

        async def _toast(routed: dict[str, Any]) -> None:
            self.toasts.append(dict(routed["data"]))

        self.router.subscribe("msg_status", _status)
        self.router.subscribe("websocket_message", _toast)

    def tx_captures(self) -> list[dict[str, Any]]:
        return [e for e in self.monitor._ring if e["dir"] == "tx"]

    def everything_published(self) -> str:
        return json.dumps([self.statuses, self.toasts], default=str)


# ── W1 ─────────────────────────────────────────────────────────────────────


async def case_w1_send(record: Record) -> None:
    for transport in ("udp", "ble"):
        bench = Bench(udp=FakeUDP(), ble=FakeBLE())
        text = _command(BIG_CTR, "display", "off")
        got = await bench.router.send_node_admin(transport, TARGET, text)
        udp_calls = bench.udp.calls if bench.udp else []
        ble_calls = bench.ble.calls if bench.ble else []
        if transport == "udp":
            wire = [(c["dst"], c["msg"]) for c in udp_calls]
            other_quiet = ble_calls == []
        else:
            wire = [(dst, msg) for msg, dst in ble_calls]
            other_quiet = udp_calls == []
        record(
            f"W1 {transport}: the transport receives (dst, msg) byte-identical, once",
            wire == [(TARGET, text)] and other_quiet,
        )
        record(f"W1 {transport}: send_node_admin returns None on success", got is None)
        caps = bench.tx_captures()
        record(
            f"W1 {transport}: exactly one TX capture, verdict 'sent', no reason",
            len(caps) == 1
            and caps[0]["verdict"] == "sent"
            and caps[0]["reason"] is None
            and caps[0]["link"] == "app"
            and caps[0]["frame"]["msg"] == text,
        )
        record(
            f"W1 {transport}: a success publishes no send_failed and no error toast",
            bench.statuses == [] and bench.toasts == [],
        )

    # A frame the normaliser could mangle: an inner-space argument.
    bench = Bench(udp=FakeUDP(), ble=FakeBLE())
    tricky = _command(7, "setout", "a2 on")
    await bench.router.send_node_admin("udp", TARGET, tricky)
    record(
        "W1 a command with a spaced argument leaves unchanged",
        [c["msg"] for c in (bench.udp.calls if bench.udp else [])] == [tricky],
    )


# ── W2 ─────────────────────────────────────────────────────────────────────


async def case_w2_failure(record: Record) -> None:
    text = _command(BIG_CTR, "reboot")
    tag = _tag_of(text)
    stripped = text.rsplit(" ", 1)[0]
    scenarios: list[tuple[str, str, Bench, str]] = [
        (
            "ble not connected",
            "ble",
            Bench(udp=FakeUDP(), ble=FakeBLE(connected=False, result=False)),
            "BLE not connected",
        ),
        ("ble client missing", "ble", Bench(udp=FakeUDP(), ble=None), "BLE client not available"),
        (
            "udp send raises",
            "udp",
            Bench(udp=FakeUDP(fail=OSError("no route")), ble=None),
            "no route",
        ),
        ("udp handler missing", "udp", Bench(udp=None, ble=FakeBLE()), "UDP handler not available"),
    ]
    for label, transport, bench, want_reason in scenarios:
        got = await bench.router.send_node_admin(transport, TARGET, text)
        record(f"W2 {label}: the failure reason is returned, not None", got == want_reason)
        caps = bench.tx_captures()
        record(
            f"W2 {label}: one TX capture, verdict 'failed' with the same reason",
            len(caps) == 1 and caps[0]["verdict"] == "failed" and caps[0]["reason"] == want_reason,
        )
        failed = [s for s in bench.statuses if s.get("send_failed") is True]
        record(
            f"W2 {label}: send_failed carries the RM1 text WITHOUT its tag",
            len(failed) == 1
            and failed[0].get("msg") == stripped
            and failed[0].get("dst") == TARGET
            and failed[0].get("reason") == want_reason,
        )
        record(
            f"W2 {label}: the tag appears in no published event (status, toast)",
            tag not in bench.everything_published(),
        )


# ── W3 ─────────────────────────────────────────────────────────────────────


async def case_w3_not_sent(record: Record) -> None:
    text = _command(BIG_CTR, "status")
    for transport in ("udp", "ble"):
        bench = Bench(udp=FakeUDP(), ble=FakeBLE())
        got = await bench.router.send_node_admin(transport, ATTACHED, text)
        record(
            f"W3 {transport}: a DM to the router's own callsign returns a 'not sent' reason",
            isinstance(got, str) and bool(got),
        )
        record(
            f"W3 {transport}: ...nothing reaches either transport",
            (bench.udp.calls if bench.udp else []) == []
            and (bench.ble.calls if bench.ble else []) == [],
        )
        caps = bench.tx_captures()
        record(
            f"W3 {transport}: ...one TX capture, verdict 'suppressed' (self_message)",
            len(caps) == 1
            and caps[0]["verdict"] == "suppressed"
            and caps[0]["reason"] == "self_message",
        )
        record(
            f"W3 {transport}: ...and no send_failed event (it is not a transport failure)",
            bench.statuses == [],
        )

    # Locally executed command (invalid destination): the suppression branch.
    bench = Bench(udp=FakeUDP(), ble=FakeBLE())
    got = await bench.router.send_node_admin("udp", "*", "!ver")
    caps = bench.tx_captures()
    record(
        "W3 a suppressed command: non-None reason, nothing transmitted, captures 'suppressed'",
        isinstance(got, str)
        and bool(got)
        and bench.udp is not None
        and bench.udp.calls == []
        and len(caps) == 1
        and caps[0]["verdict"] == "suppressed",
    )


# ── W4 ─────────────────────────────────────────────────────────────────────


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


async def case_w4_redact(record: Record) -> None:
    tag = "0123456789abcdef"
    table: list[tuple[str, Any, Any]] = [
        ("trailing tag of a command", f"RM1 5 display off {tag}", "RM1 5 display off"),
        ("trailing tag of a bare command", f"RM1 5 status {tag}", "RM1 5 status"),
        ("trailing tag of a reply", f"RM1 5 ok v=4.35u {tag}", "RM1 5 ok v=4.35u"),
        ("'RM1 x' has no tag token", "RM1 x", "RM1 x"),
        ("'RM1 ' alone", "RM1 ", "RM1 "),
        ("'RM1' without the space", "RM1", "RM1"),
        ("non-RM1 text", "hello there general kenobi", "hello there general kenobi"),
        ("lower-case 'rm1' is not a frame", f"rm1 5 status {tag}", f"rm1 5 status {tag}"),
        ("leading space is not a frame", f" RM1 5 status {tag}", f" RM1 5 status {tag}"),
        ("an ack-suffixed non-RM1 text", "!wx{087", "!wx{087"),
        ("None", None, None),
        ("an int", 42, 42),
        ("bytes", b"RM1 5 status abc", b"RM1 5 status abc"),
        ("a list", ["RM1 5 status abc"], ["RM1 5 status abc"]),
    ]
    bad = [name for name, value, want in table if main_module._redact_rm1(value) != want]
    record("W4 _redact_rm1 truth table (tag stripped only from an 'RM1 ' frame)", not bad)
    if bad:
        print("      ", bad)

    # The INFO log of `_udp_message_handler`: published through the real router.
    text = _command(BIG_CTR, "display", "off")
    full_tag = _tag_of(text)
    bench = Bench(udp=FakeUDP(), ble=None)
    cap = _LogCapture()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(cap)
    try:
        await bench.router.publish(
            "sse",
            "udp_message",
            {"src": ATTACHED, "dst": TARGET, "msg": text, "src_type": "sse"},
        )
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    handler_lines = [ln for ln in cap.lines if "_udp_message_handler" in ln]
    record(
        "W4 the _udp_message_handler INFO line names the frame without its tag",
        len(handler_lines) == 1
        and f"RM1 {BIG_CTR} display off" in handler_lines[0]
        and full_tag[:8] not in handler_lines[0],
    )
    record(
        "W4 the tag is in no INFO-or-higher log line of the whole send",
        full_tag[:8] not in "\n".join(cap.lines),
    )
    record(
        "W4 control: the frame itself went out intact through the same handler",
        bench.udp is not None and [c["msg"] for c in bench.udp.calls] == [text],
    )


# ── W5: loopback ───────────────────────────────────────────────────────────


class FakeClock:
    """Hand-driven clock; `sleep` parks (the loopback never needs the auto-sync gate)."""

    def __init__(self) -> None:
        self.now = T0

    def clock(self) -> int:
        return self.now

    async def sleep(self, seconds: float) -> None:
        await asyncio.Event().wait()


# Mutation switch for the node simulator's reply orientation (W5 must fail when True).
NODE_SWAP_REPLY_ORIENTATION = False


class SimNode:
    """The managed node, from `remote_cmd` primitives only (no service code).

    Verifies a received command exactly like the firmware: tag over
    `RM1|<dst>|<src>|<ctr>|<line>`, counter above its single high-water mark,
    answers with `RM1 <ctr> <result> <tag>` where the reply tag uses the COMMAND's
    orientation (dst = this node, src = the commander). `sign_password` lets a test
    make the node sign with a key McApp does not hold.
    """

    def __init__(
        self, call: str, password: str, *, hwm: int, sign_password: str | None = None
    ) -> None:
        self.call = call
        self.key = remote_cmd.derive_key(password)
        self.sign_key = remote_cmd.derive_key(
            sign_password if sign_password is not None else password
        )
        self.hwm = hwm
        self.executed: list[str] = []

    def handle(self, dst: str, src: str, text: str) -> str | None:
        tokens = text.split(" ")
        if len(tokens) < 4 or tokens[0] != "RM1" or dst != self.call:
            return None
        ctr = int(tokens[1])
        line = " ".join(tokens[2:-1])
        want = remote_cmd.rm_tag(self.key, self.call, src, ctr, line)
        if not hmac.compare_digest(want, tokens[-1]):
            return None  # silent tag reject
        if line == "sync":
            result = f"ok ctr={self.hwm} v=4.35u"
        else:
            if ctr <= self.hwm:
                return None  # silent replay reject
            self.hwm = ctr
            self.executed.append(line)
            result = (
                "ok v=4.35u up=7 bat=100" if line == "status" else f"ok {line.replace(' ', '=')}"
            )
        if NODE_SWAP_REPLY_ORIENTATION:
            tag = remote_cmd.rm_reply_tag(self.sign_key, src, self.call, ctr, result)
        else:
            tag = remote_cmd.rm_reply_tag(self.sign_key, self.call, src, ctr, result)
        return f"RM1 {ctr} {result} {tag}"


class Loop:
    """service + router + storage + SecretBox wired the way `build_app` does."""

    def __init__(self, storage: SQLiteStorage, tmp: Path, *, ble: bool) -> None:
        self.storage = storage
        self.tmp = tmp
        self.ble_connected = ble
        self.time = FakeClock()
        self.udp = FakeUDP()
        self.ble_client = FakeBLE(connected=ble)
        self.router = MessageRouter(storage)
        self.router.set_callsign(ATTACHED)
        self.router.register_protocol("udp", self.udp)
        self.router.register_protocol("ble_client", self.ble_client)
        self.bcasts: list[tuple[str, dict[str, Any]]] = []
        self.box = SecretBox(key_path=tmp / "secret.key", binding=b"board:TEST")
        self.svc = NodeAdminService(
            storage,
            self.box,
            self.router.send_node_admin,
            lambda: self.router.my_callsign,
            lambda: self.ble_connected,
            self._broadcast,
            clock_ms=self.time.clock,
            unix_s=lambda: 0,
            sleep=self.time.sleep,
        )
        storage.reply_hook = self.svc.on_reply
        self._seq = 0

    async def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        self.bcasts.append((event, dict(payload)))

    def last_frame(self) -> tuple[str, str, str]:
        """(transport, src, text) of the newest frame a transport received."""
        if self.ble_connected:
            msg, _dst = self.ble_client.calls[-1]
            return "ble", ATTACHED, str(msg)  # the node knows its own call
        sent = self.udp.calls[-1]
        return "udp", str(sent["src"]), str(sent["msg"])

    def frames_seen(self) -> int:
        return len(self.ble_client.calls) if self.ble_connected else len(self.udp.calls)

    def reply_frames(self, reply: str) -> tuple[dict[str, Any], dict[str, Any]]:
        self._seq += 1
        msg_id = f"1AE1{self._seq:04X}"
        ts = self.time.now + self._seq * 1000
        base = {"src": TARGET, "dst": ATTACHED, "type": "msg", "msg_id": msg_id}
        udp = {**base, "msg": f"{reply}{{087", "src_type": "udp", "timestamp": ts}
        ble = {**base, "msg": reply, "src_type": "ble_remote", "timestamp": ts + 100}
        return udp, ble

    async def deliver(self, reply: str) -> None:
        """Both copies of one reply into the REAL `store_message`, concurrently."""
        udp, ble = self.reply_frames(reply)
        await asyncio.gather(
            self.storage.store_message(udp, ""), self.storage.store_message(ble, "")
        )
        await self.svc.drain()

    async def row(self, log_id: int) -> dict[str, Any]:
        for r in await self.svc.history(TARGET, 100):
            if r["id"] == log_id:
                return r
        raise AssertionError(f"no row {log_id}")

    def verified_broadcasts(self, log_id: int) -> int:
        return sum(
            1
            for event, p in self.bcasts
            if event == "node_admin:reply"
            and p.get("id") == log_id
            and p.get("state") == "verified"
        )

    async def info(self) -> dict[str, Any]:
        for t in await self.svc.list_targets():
            if t["target"] == TARGET:
                return t
        raise AssertionError("no target")


@contextlib.asynccontextmanager
async def make_loop(*, ble: bool) -> AsyncIterator[Loop]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        storage = await create_sqlite_storage(tmp / "node_admin_wiring_test.db")
        loop = Loop(storage, tmp, ble=ble)
        try:
            await loop.svc.set_key(TARGET, PASSWD, 15)
            yield loop
        finally:
            await loop.svc.stop()
            await storage.close()


async def _round(loop: Loop, node: SimNode) -> tuple[str, str, str, str | None]:
    """The node processes the newest transmitted frame; returns (transport, src, text, reply)."""
    transport, src, text = loop.last_frame()
    return transport, src, text, node.handle(TARGET, src, text)


async def _bring_up(loop: Loop, node: SimNode, label: str, record: Record) -> bool:
    """Sync round through the loop: the target becomes 'synced', hwm = the node's."""
    sent = await loop.svc.sync(TARGET)
    await loop.svc.drain()
    transport, _src, text, reply = await _round(loop, node)
    record(
        f"W5 {label}: the sync frame leaves through the router on the right transport",
        loop.frames_seen() == 1
        and transport == ("ble" if loop.ble_connected else "udp")
        and text == sent["text"],
    )
    if reply is None:
        record(f"W5 {label}: the simulated node accepts the sync tag", False)
        return False
    await loop.deliver(reply)
    row = await loop.row(sent["log_id"])
    info = await loop.info()
    synced = (
        row["state"] == "verified"
        and info["last_hwm"] == node.hwm
        and info["last_sync_at"] is not None
    )
    record(f"W5 {label}: the sync reply verifies, raises last_hwm to the node's mark", synced)
    loop.time.now += 10_000  # the node's rate limit
    return synced  # an unsynced target would park the next send behind the auto-sync gate


async def case_w5_loopback(record: Record) -> None:
    for ble in (False, True):
        label = "ble" if ble else "udp"
        async with make_loop(ble=ble) as loop:
            node = SimNode(TARGET, PASSWD, hwm=500)
            if not await _bring_up(loop, node, label, record):
                continue

            sent = await loop.svc.send(TARGET, "status", "", "auto")
            await loop.svc.drain()
            transport, src, text, reply = await _round(loop, node)
            record(
                f"W5 {label}: the command reaches the transport byte-identical to the logged text",
                text == sent["text"] and transport == label and loop.frames_seen() == 2,
            )
            record(
                f"W5 {label}: the counter honours the node's mark (hwm 500 -> ctr 501)",
                sent["ctr"] == 501,
            )
            record(
                f"W5 {label}: the node verifies the tag McApp built (src={src})",
                reply is not None and node.executed == ["status"],
            )
            if reply is None:
                continue
            await loop.deliver(reply)
            row = await loop.row(sent["log_id"])
            info = await loop.info()
            record(
                f"W5 {label}: UDP+BLE reply copies leave the row verified, bare reply stored",
                row["state"] == "verified" and row["verified"] == 1 and row["reply_text"] == reply,
            )
            record(
                f"W5 {label}: exactly one verified broadcast for the two copies",
                loop.verified_broadcasts(sent["log_id"]) == 1,
            )
            record(f"W5 {label}: last_hwm raised to the verified counter", info["last_hwm"] == 501)

            busy = False
            try:
                await loop.svc.send(TARGET, "status", "", "auto")
            except NodeAdminBusyError:
                busy = True
            record(f"W5 {label}: a second command right after the reply is refused (Busy)", busy)
            loop.time.now += 9_999
            busy = False
            try:
                await loop.svc.send(TARGET, "status", "", "auto")
            except NodeAdminBusyError:
                busy = True
            record(f"W5 {label}: ...still Busy at 9 999 ms", busy)
            loop.time.now += 1
            second = await loop.svc.send(TARGET, "display", "off", "auto")
            await loop.svc.drain()
            _t, _s, text2, reply2 = await _round(loop, node)
            record(
                f"W5 {label}: ...accepted at 10 000 ms as counter 502 (refusals burned none)",
                second["ctr"] == 502 and text2 == second["text"] and reply2 is not None,
            )
            if reply2 is not None:
                await loop.deliver(reply2)
                row2 = await loop.row(second["log_id"])
                record(
                    f"W5 {label}: the second command verifies too, one broadcast",
                    row2["state"] == "verified" and loop.verified_broadcasts(second["log_id"]) == 1,
                )


async def case_w5_wrong_key(record: Record) -> None:
    async with make_loop(ble=False) as loop:
        node = SimNode(TARGET, PASSWD, hwm=500)
        if not await _bring_up(loop, node, "wrong key", record):
            return
        # The node executes the command (it holds the right key to verify it) but
        # signs its reply with a password McApp does not have.
        node.sign_key = remote_cmd.derive_key(OTHER_PASSWD)
        sent = await loop.svc.send(TARGET, "status", "", "auto")
        await loop.svc.drain()
        _t, _s, _text, reply = await _round(loop, node)
        if reply is None:
            record("W5 wrong key: the simulated node accepts the command", False)
            return
        hwm_before = int((await loop.info())["last_hwm"])
        await loop.deliver(reply)
        row = await loop.row(sent["log_id"])
        info = await loop.info()
        record(
            "W5 wrong key: a reply McApp cannot verify leaves the row bad_tag, not verified",
            row["state"] == "bad_tag" and row["verified"] == 0 and row["reply_text"] == reply,
        )
        record(
            "W5 wrong key: no verified broadcast, last_hwm unmoved, rejected count raised",
            loop.verified_broadcasts(sent["log_id"]) == 0
            and info["last_hwm"] == hwm_before
            and loop.svc.rejected_replies >= 1,
        )


# ── W7: monitor ring and DEBUG log ─────────────────────────────────────────


class _DebugCapture(_LogCapture):
    def __init__(self) -> None:
        super().__init__()
        self.setLevel(logging.DEBUG)


async def case_w7_monitor_and_debug(record: Record) -> None:
    text = _command(BIG_CTR, "reboot")
    tag = _tag_of(text)
    stripped = text.rsplit(" ", 1)[0]

    # (1) failed: the ring frame is cut to `RM1 <ctr> <cmd>`.
    for label, transport, bench in (
        ("ble not connected", "ble", Bench(udp=None, ble=FakeBLE(connected=False, result=False))),
        ("udp send raises", "udp", Bench(udp=FakeUDP(fail=OSError("no route")), ble=None)),
    ):
        await bench.router.send_node_admin(transport, TARGET, text)
        caps = bench.tx_captures()
        ring_blob = json.dumps(list(bench.monitor._ring), default=str)
        record(
            f"W7 {label}: the failed monitor frame carries 'RM1 <ctr> <cmd>' without the tag",
            len(caps) == 1
            and caps[0]["verdict"] == "failed"
            and caps[0]["frame"]["msg"] == stripped
            and caps[0]["frame"]["dst"] == TARGET
            and tag not in ring_blob,
        )

    # (2) sent: on air anyway, the monitor keeps the full frame.
    bench = Bench(udp=FakeUDP(), ble=FakeBLE())
    await bench.router.send_node_admin("udp", TARGET, text)
    caps = bench.tx_captures()
    record(
        "W7 a sent monitor frame still carries the full signed text",
        len(caps) == 1 and caps[0]["verdict"] == "sent" and caps[0]["frame"]["msg"] == text,
    )

    # (3) the DEBUG "Processing" line (and every other DEBUG line) holds no tag.
    cap = _DebugCapture()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(cap)
    try:
        for transport, bench in (
            ("udp", Bench(udp=FakeUDP(), ble=None)),
            ("ble", Bench(udp=None, ble=FakeBLE(connected=False, result=False))),
        ):
            await bench.router.send_node_admin(transport, TARGET, text)
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    processing = [ln for ln in cap.lines if "Processing" in ln]
    record(
        "W7 the DEBUG 'Processing' line names the frame without its tag (sent and failed)",
        len(processing) == 2
        and all(f"RM1 {BIG_CTR} reboot" in ln and tag not in ln for ln in processing),
    )
    record(
        "W7 the tag is in no DEBUG-or-higher log line of either send",
        tag not in "\n".join(cap.lines),
    )


# ── W6: status feature flag ────────────────────────────────────────────────


class _StubManager:
    """Only what the /api/status handler touches; no `node_admin_service` attribute."""

    def __init__(self, router: MessageRouter) -> None:
        self.clients_lock = asyncio.Lock()
        self.clients: dict[str, Any] = {}
        self.message_router = router


async def _status(manager: Any) -> dict[str, Any]:
    routes: list[Any] = list(build_stream_router(manager, "vTest").routes)
    route = next(r for r in routes if r.path == "/api/status")
    result: dict[str, Any] = await route.endpoint()
    return result


async def case_w6_features(record: Record) -> None:
    router = MessageRouter(None)
    router.set_callsign(ATTACHED)

    stub: Any = _StubManager(router)
    try:
        bare = await _status(stub)
        crashed = False
    except Exception:
        traceback.print_exc()
        bare, crashed = {}, True
    record(
        "W6 a manager without the attribute does not crash and reports no features",
        not crashed and bare.get("features") == [],
    )

    stub.node_admin_service = None
    record("W6 service None: features is empty", (await _status(stub)).get("features") == [])

    stub.node_admin_service = SimpleNamespace(name="service")
    record(
        "W6 service set: features is exactly ['node_admin']",
        (await _status(stub)).get("features") == ["node_admin"],
    )

    real: Any = SSEManager("127.0.0.1", 0, message_router=router)
    record(
        "W6 real SSEManager defaults to no node_admin feature",
        real.node_admin_service is None and (await _status(real)).get("features") == [],
    )
    real.node_admin_service = SimpleNamespace(name="service")
    record(
        "W6 real SSEManager with a service advertises 'node_admin'",
        (await _status(real)).get("features") == ["node_admin"],
    )
    record(
        "W6 the other /api/status fields are untouched by the flag",
        (await _status(real)).get("call_sign") == ATTACHED
        and (await _status(real)).get("status") == "ok",
    )


# ── runner ─────────────────────────────────────────────────────────────────


async def case_w8_always_on(record: Record) -> None:
    """Node Admin has no config switch (operator decision 2026-10-05): the service is
    wired unconditionally and a leftover `enabled` key in config.json changes nothing."""
    import inspect
    import json
    import tempfile
    from pathlib import Path

    from . import main as main_module
    from .config_loader import Config

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "CALL_SIGN": "DK5EN-98",
                    "node_admin": {
                        "enabled": False,
                        "allowed_origins": ["http://localhost:5173", 3, None],
                    },
                }
            )
        )
        cfg = Config.load(path)
    record(
        "w8: a leftover node_admin.enabled key is ignored, allowed_origins keeps only strings",
        not hasattr(cfg.node_admin, "enabled")
        and cfg.node_admin.allowed_origins == ["http://localhost:5173"],
    )
    default_cfg = Config()
    record(
        "w8: no node_admin key at all gives no extra origins and no switch",
        default_cfg.node_admin.allowed_origins == []
        and not hasattr(default_cfg.node_admin, "enabled"),
    )
    source = inspect.getsource(main_module.build_app)
    record(
        "w8: build_app wires the service and the reply hook with no config gate",
        # Four spaces = the function body itself: nesting any of these under an
        # `if` (of whatever shape) would indent them further and fail the check.
        "\n    node_admin_service = NodeAdminService(" in source
        and "\n    storage_handler.reply_hook = node_admin_service.on_reply" in source
        and "\n    await node_admin_service.start()" in source
        and "node_admin.enabled" not in source
        and "if cfg.node_admin" not in source,
    )


async def run_node_admin_wiring_tests() -> bool:
    results: list[tuple[str, bool]] = []

    def record(label: str, ok: bool) -> None:
        results.append((label, ok))
        print(f"{'PASS' if ok else 'FAIL'} | {label}")

    cases: list[Callable[[Record], Awaitable[None]]] = [
        case_w1_send,
        case_w2_failure,
        case_w3_not_sent,
        case_w4_redact,
        case_w5_loopback,
        case_w5_wrong_key,
        case_w6_features,
        case_w7_monitor_and_debug,
        case_w8_always_on,
    ]
    for case in cases:
        try:
            await asyncio.wait_for(case(record), timeout=120)
        except Exception:
            record(f"{case.__name__} raised", False)
            traceback.print_exc()
    passed = sum(1 for _, ok in results if ok)
    ok_all = passed == len(results) and bool(results)
    print(f"\nNode admin wiring: {passed}/{len(results)} passed")
    print(f"node_admin_wiring: {'PASS' if ok_all else 'FAIL'}")
    return ok_all


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run_node_admin_wiring_tests()) else 1)
