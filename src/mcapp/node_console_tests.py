"""Built-in regression suite for `node_console.py` — the node debug console
bridge (MeshCom firmware TCP net console, port 2323).

Harness: a real `asyncio.start_server` fake console (`_FakeConsole`) on
`127.0.0.1:0` implementing the NONCE/HMAC (and no-password) handshake, and —
this is the part that matters — reading every command **one byte at a time
with a small per-byte delay, echoing each byte back as it is read**, exactly
like the real firmware's `serial_command_esp32.cpp` loop. A command is only
executed once its terminating `\\n` has actually been read. This is what
lets `_test_rst_after_restore_command...` below reproduce the real bug this
rework fixes: closing our side before the node's echo has been read can get
those bytes discarded, so a `close()`-then-hope restore is not the same
thing as a *confirmed* one. `NodeConsoleSession` instances are driven for
real (`start()`/`stop()`/`shutdown()`), never reimplemented — only the
timing knobs (`info_reply_window_s`/`idle_flush_s`/`flag_command_gap_s`/
`close_drain_s`/`handshake_timeout_s`/`restore_retry_backoffs_s`) are
shortened so this suite stays fast. **No network connection other than
127.0.0.1 is made — never a real node.**

Cases (see module sections below):

  1. Wrong password -> `state == "error"`, password never leaks.
  2. No-password handshake: flag skip-if-on, restore-with-CONFIRMATION on
     stop, asserted against the fake's own flag state (not just the
     command list) — `--txcapture on` is never sent (already on) and
     `--txcapture off` is never sent at restore.
  3. Console lines arrive as `link="console"` envelopes with the contract
     shape, and an unterminated partial line is flushed after ~1 idle
     interval with nothing more arriving.
  3b. No line is silently dropped when a timeout-driven partial-line flush
      races a yielding SSE broadcast — `_stream_lines` keeps exactly one
      pending read task instead of losing a completed-during-the-yield one.
  4. `max_session_s` auto-stops the session on its own, restoring (and
     confirming) the flag it changed.
  5. `shutdown()` blocks until the flag restore is confirmed, unlike
     `stop()`'s prompt return for a live session.
  6. A `stop()` that lands during the `--info` probe window sends no `on`
     command at all and leaves the fake's flag state untouched.
  7. The node RSTing right after the first restore command -> `state ==
     "error"`, `pending_restore` contains exactly that flag, the error
     names it; the NEXT session treats it as prior `"off"` regardless of
     `--info`, re-sends `on`, and its own (uninterrupted) stop confirms
     the restore and clears `pending_restore`.
  7b. That same RST-mid-restore failure (not just the EOF-while-streaming
      case in 8) also kicks off the bounded background retry on its own —
      no explicit start()/stop() needed for the flag to self-heal.
  8. An unexpected EOF while streaming (not mid-restore) -> `state ==
     "error"` with the reboot/link-loss message, `pending_restore`
     populated, and a background retry (fast backoffs) reconnects and
     confirms the restore on its own — `pending_restore` empties out AND
     state clears back to `"idle"` without any explicit start()/stop()
     from the caller.
  8b. A `monitor:console` broadcast actually reflects a `pending_restore`
      change and the state-back-to-idle transition (asserted on the
      broadcast stream itself, not just the session's attributes).
  9. `start()` interrupts a background restore retry (state `"error"`,
     task still running) and launches the fresh session in the SAME call
     — exactly one new connection, never two concurrent sessions, and the
     operator never has to click start() twice.
  9b. `start()` landing in the narrow window where `_run()` already set
      `state == "idle"` but the task itself is not yet `done()` still
      launches a session, rather than silently no-op'ing.
  10. Connecting to a listener that never sends a banner (the real shape
      of "another client already holds the console" — `net_console.cpp`
      never calls `accept()` for a second client) -> `"console busy"`.
  10b. `stop()` racing a connect-phase failure (e.g. that same "console
       busy") settles to `"idle"`, not `"error"` — the operator asked to
       stop, so a connect failure after that is not worth surfacing.
  11. A successful session with a real (non-empty) password never leaks
      that password into status or any captured envelope, start to stop.
  12. REST layer (`sse_routes/monitor.py`): GET status / POST start / POST
      stop (now a prompt return, not a synchronous restore), idempotency,
      and a 503 when no session is wired.
  13. `WireMonitor`'s console ring never evicts RF frames, and `page()`
      merges both rings ordered by the shared `seq` counter.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import secrets
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import FastAPI

from .commands.constants import has_console
from .node_console import NodeConsoleSession
from .sse_routes.monitor import build_monitor_router
from .wire_monitor import RING_MAXLEN, WireMonitor

if TYPE_CHECKING:
    from .sse_handler import SSEManager

RecordFn = Any  # (label: str, ok: bool) -> None, kept untyped like sibling suites

# Fast timing knobs for this suite only — production (main.py) never passes
# these, see node_console.py's constructor docstring.
_FAST_TIMINGS: dict[str, Any] = {
    "info_reply_window_s": 0.3,
    "idle_flush_s": 0.15,
    "flag_command_gap_s": 0.05,
    "close_drain_s": 0.2,
    "handshake_timeout_s": 0.3,
}


class _FakeConsole:
    """A stand-in for `net_console.cpp`, bound to 127.0.0.1:0.

    Reads every command **one byte at a time**, with a small per-byte
    delay, echoing each byte back as it is read — mirroring
    `serial_command_esp32.cpp`'s one-byte-per-main-loop-iteration
    read+echo — and only executes a command once its `\\n` has actually
    been read. Keeps its own `flags` state (answered by `--info`), so
    tests assert on what the node actually did, not just what the client
    sent.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - test fixture, one knob per scenario this suite exercises
        self,
        password: str = "",
        loradebug: str = "off",
        txcapture: str = "off",
        close_after_command: str | None = None,
        byte_delay_s: float = 0.003,
        send_banner: bool = True,
    ) -> None:
        self.password = password
        self.flags: dict[str, str] = {"loradebug": loradebug, "txcapture": txcapture}
        self.close_after_command = close_after_command
        self._close_after_fired = False
        self.byte_delay_s = byte_delay_s
        self.send_banner = send_banner
        self.commands: list[str] = []
        self.connections_accepted = 0
        self.current_writer: asyncio.StreamWriter | None = None
        self._server: asyncio.Server | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self.host = "127.0.0.1"
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._handler_tasks):
            task.cancel()
        for task in list(self._handler_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def send_raw(self, data: bytes) -> None:
        writer = self.current_writer
        assert writer is not None
        writer.write(data)
        await writer.drain()

    async def send_line(self, text: str) -> None:
        await self.send_raw((text + "\r\n").encode())

    def _info_reply(self) -> bytes:
        return (
            f"...DEBUG off ...LORADEBUG {self.flags['loradebug']} ...NBRDEBUG off "
            f"...TXCAPTURE {self.flags['txcapture']} ...GPSDEBUG off/0 ...SOFTSERDEBUG off\r\n"
        ).encode()

    def _execute(self, cmd: str) -> None:
        for name in ("loradebug", "txcapture"):
            if cmd == f"--{name} on":
                self.flags[name] = "on"
            elif cmd == f"--{name} off":
                self.flags[name] = "off"

    async def _read_command_line(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> str | None:
        """Read + echo one command line BYTE BY BYTE. Returns `None` on
        EOF (including mid-line — an incomplete command is never
        executed, same as the real firmware never seeing the trailing
        `\\n`)."""
        line = bytearray()
        while True:
            b = await reader.read(1)
            if not b:
                return None
            await asyncio.sleep(self.byte_delay_s)
            try:
                writer.write(b)
                await writer.drain()
            except (ConnectionError, OSError):
                return None
            if b == b"\n":
                return line.decode(errors="replace").strip()
            line += b

    async def _authenticate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """Complete the NONCE/HMAC (or no-password) handshake. Returns
        whether the connection should proceed to the command loop."""
        if not self.password:
            writer.write(b"OK\r\nMeshCom console\r\n")
            await writer.drain()
            return True
        nonce = secrets.token_hex(16)
        writer.write(f"NONCE: {nonce}\r\n".encode())
        await writer.drain()
        resp = await reader.readline()
        digest = hmac.new(self.password.encode(), bytes.fromhex(nonce), hashlib.sha256).hexdigest()
        if resp.strip().decode(errors="replace") != digest:
            writer.write(b"FAIL\r\n")
            await writer.drain()
            writer.close()
            return False
        writer.write(b"OK\r\nMeshCom console\r\n")
        await writer.drain()
        return True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        self.connections_accepted += 1
        self.current_writer = writer
        try:
            if not self.send_banner:
                # Simulate a busy node: net_console.cpp never calls
                # accept() at the application level for a second client,
                # so nothing is ever written here.
                await asyncio.sleep(30)
                return
            if not await self._authenticate(reader, writer):
                return

            while True:
                cmd = await self._read_command_line(reader, writer)
                if cmd is None:
                    return
                if not cmd:
                    continue
                self.commands.append(cmd)
                self._execute(cmd)
                if cmd == "--info":
                    writer.write(self._info_reply())
                    await writer.drain()
                if (
                    not self._close_after_fired
                    and self.close_after_command is not None
                    and cmd == self.close_after_command
                ):
                    self._close_after_fired = True
                    writer.close()
                    return
        except (ConnectionError, OSError):
            return
        finally:
            if self.current_writer is writer:
                self.current_writer = None
            if task is not None:
                self._handler_tasks.discard(task)


async def _wait_until(predicate: Any, timeout_s: float = 3.0, interval: float = 0.05) -> bool:
    """Poll `predicate()` until truthy or `timeout` elapses. Returns whether
    it became true — never raises, so a failing wait shows up as a normal
    assertion failure in the caller instead of an exception mid-suite."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def _new_session(
    wire_monitor: WireMonitor, console: _FakeConsole, **kwargs: Any
) -> NodeConsoleSession:
    # kwargs override _FAST_TIMINGS' defaults (e.g. a test tightening
    # idle_flush_s further) rather than colliding with them.
    timings = {**_FAST_TIMINGS, **kwargs}
    return NodeConsoleSession(
        wire_monitor,
        host=console.host,
        port=console.port,
        password=timings.pop("password", ""),
        max_session_s=timings.pop("max_session_s", 1800),
        **timings,
    )


def _console_texts(wire_monitor: WireMonitor) -> list[str]:
    """Every captured console-ring frame's `msg` text (lines and notices)."""
    return [str(env["frame"].get("msg", "")) for env in wire_monitor._console_ring]


class _RecordingSSEManager:
    """Records every broadcast event verbatim — used to assert something
    was actually broadcast (not just that an attribute changed)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def broadcast_event(self, event: str, data: Any) -> None:
        self.events.append((event, data))


class _YieldingSSEManager:
    """A broadcast that actually yields to the event loop for `delay_s`
    before returning — a real SSE fan-out (many client queues) does the
    same. This is what exposed the `_stream_lines` read_task-loss bug: a
    task pending when the yield begins can complete DURING it."""

    def __init__(self, delay_s: float = 0.001) -> None:
        self.delay_s = delay_s
        self.events: list[tuple[str, Any]] = []

    async def broadcast_event(self, event: str, data: Any) -> None:
        await asyncio.sleep(self.delay_s)
        self.events.append((event, data))


# ── 1. Wrong password ────────────────────────────────────────────────────


async def _test_wrong_password(record: RecordFn) -> None:
    console = _FakeConsole(password="right-password")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console, password="wrong-password")

        await session.start()
        reached = await _wait_until(lambda: session.state in ("error", "idle"))
        record("wrong password: session reaches a terminal state", reached)
        record("wrong password: state is 'error'", session.state == "error")
        record(
            "wrong password: error mentions authentication",
            bool(session.error) and "auth" in (session.error or "").lower(),
        )

        blob = json.dumps(session.status()) + "\n".join(_console_texts(wire_monitor))
        record(
            "wrong password: neither password ever appears in status/notices",
            "right-password" not in blob and "wrong-password" not in blob,
        )
    finally:
        await console.stop()


# ── 2. Flag skip-if-on, restore WITH CONFIRMATION ────────────────────────


async def _test_flags_set_and_restored(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="off", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console)

        await session.start()
        record("no-password: reaches active", await _wait_until(lambda: session.state == "active"))
        record(
            "no-password: --loradebug on is sent (prior state was off)",
            await _wait_until(lambda: "--loradebug on" in console.commands),
        )
        # Give the (short, fast-timing) --info collection window a moment to
        # fully close out before asserting a negative.
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)
        record(
            "no-password: --txcapture on is NOT sent (already on)",
            "--txcapture on" not in console.commands,
        )
        record(
            "no-password: prior_flags recorded correctly",
            session.prior_flags == {"loradebug": "off", "txcapture": "on"},
        )
        record(
            "no-password: the fake's loradebug flag is actually on",
            console.flags["loradebug"] == "on",
        )

        status = await session.stop()
        record(
            "no-password: stop() returns promptly (not yet idle)",
            status["state"] in ("stopping", "idle"),
        )
        record(
            "no-password: session eventually settles to idle",
            await _wait_until(lambda: session.state == "idle", timeout_s=5.0),
        )
        record(
            "no-password: only --loradebug off is sent at restore",
            "--loradebug off" in console.commands and "--txcapture off" not in console.commands,
        )
        record(
            "no-password: the fake's loradebug flag is CONFIRMED back off",
            console.flags["loradebug"] == "off",
        )
        record(
            "no-password: the untouched txcapture flag is still on",
            console.flags["txcapture"] == "on",
        )
        record(
            "no-password: pending_restore is empty after a confirmed restore",
            session.pending_restore == set(),
        )
        record(
            "no-password: status() exposes pending_restore",
            session.status()["pending_restore"] == [],
        )
    finally:
        await console.stop()


# ── 3. Console lines + partial-line flush ────────────────────────────────


async def _test_lines_and_partial_flush(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="on", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console)

        await session.start()
        record("lines: reaches active", await _wait_until(lambda: session.state == "active"))
        # Both flags already on: no --set commands, so the session is already
        # in the main read loop very quickly.
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)

        line_text = "[MC-DBG] RX frame decoded ok"
        await console.send_line(line_text)
        record(
            "lines: a full console line arrives as a link=console envelope",
            await _wait_until(lambda: line_text in _console_texts(wire_monitor)),
        )
        envelope = next(
            env for env in wire_monitor._console_ring if env["frame"].get("msg") == line_text
        )
        record(
            "lines: envelope has the contract shape (link/dir/verdict/frame.type/src)",
            envelope["link"] == "console"
            and envelope["dir"] == "rx"
            and envelope["verdict"] == "shown"
            and envelope["frame"]["type"] == "con"
            and envelope["frame"]["src"] == console.host
            and "notice" not in envelope["frame"],
        )

        # Partial line: no trailing \n. Must be flushed after ~idle_flush_s
        # of silence rather than waiting forever for a newline that never
        # comes on this particular line.
        await console.send_raw(b"partial line with no newline yet")
        record(
            "lines: an unterminated partial line is flushed after idle silence",
            await _wait_until(
                lambda: "partial line with no newline yet" in _console_texts(wire_monitor),
                timeout_s=3.0,
            ),
        )

        await session.stop()
        await _wait_until(lambda: session.state == "idle", timeout_s=5.0)
    finally:
        await console.stop()


# ── 3b. No data loss when a partial-line flush races a yielding broadcast ──


async def _test_stream_lines_no_data_loss_across_yielding_broadcast(record: RecordFn) -> None:
    """item 1: `_stream_lines` used to open a FRESH `reader.read(4096)`
    task every loop iteration. On an idle timeout with a partial line
    pending, flushing that partial line (`_emit_line` -> `capture` ->
    `broadcast_event`) yields to the event loop — and if the still-pending
    read task completed DURING that yield, the loop's `finally` saw an
    already-`done()` task and silently discarded its chunk instead of
    consuming it. Reproduced deterministically here by forcing a partial
    (unterminated) fragment right before every real line, with a
    broadcast stub that yields longer than the idle-flush window so the
    vulnerable overlap is essentially guaranteed each trial."""
    console = _FakeConsole(loradebug="on", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        wire_monitor.sse_manager = _YieldingSSEManager(delay_s=0.03)
        session = _new_session(wire_monitor, console, idle_flush_s=0.01)

        await session.start()
        record("no-loss: reaches active", await _wait_until(lambda: session.state == "active"))
        # A longer settle than other tests use: right after activation there
        # can still be an in-flight notice broadcast (e.g. "flags already
        # set") working through the yielding stub, and starting trials
        # before that's fully drained is a warm-up artifact, not the race
        # this test targets.
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.5)

        trials = 40
        expected = [f"L{i:04d}" for i in range(trials)]
        for i, text in enumerate(expected):
            # An unterminated fragment: guarantees buf is non-empty when
            # the idle timeout fires next.
            await console.send_raw(f"P{i:04d}".encode())
            # Past idle_flush_s: the timeout branch is now flushing "P####"
            # and its broadcast (0.03s) is in flight.
            await asyncio.sleep(session._idle_flush_s + 0.005)
            # Land squarely inside that still-in-flight broadcast window.
            await console.send_line(text)
            # Let this trial fully settle (flush + emit) before the next.
            await asyncio.sleep(0.06)

        record(
            "no-loss: every line sent during a yielding broadcast still lands in the ring",
            await _wait_until(
                lambda: all(t in _console_texts(wire_monitor) for t in expected), timeout_s=10.0
            ),
        )
        got = _console_texts(wire_monitor)
        missing = [t for t in expected if t not in got]
        record(f"no-loss: 0 of {trials} lines lost (was: {len(missing)} missing)", not missing)

        await session.stop()
        await _wait_until(lambda: session.state == "idle", timeout_s=5.0)
    finally:
        await console.stop()


# ── 4. max_session_s auto-stop, restored and confirmed ───────────────────


async def _test_max_session_auto_stop(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="off", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console, max_session_s=1)

        await session.start()
        record("max_session: reaches active", await _wait_until(lambda: session.state == "active"))
        record(
            "max_session: auto-stops back to idle on its own",
            await _wait_until(lambda: session.state == "idle", timeout_s=5.0),
        )
        record(
            "max_session: restores AND confirms the flag it changed before stopping",
            console.flags["loradebug"] == "off",
        )
        record(
            "max_session: pending_restore is empty",
            session.pending_restore == set(),
        )
    finally:
        await console.stop()


# ── 5. shutdown() blocks until the restore is confirmed ─────────────────


async def _test_shutdown_restores(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="off", txcapture="off")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console)

        await session.start()
        record("shutdown: reaches active", await _wait_until(lambda: session.state == "active"))
        record(
            "shutdown: both flags turned on",
            await _wait_until(lambda: console.flags == {"loradebug": "on", "txcapture": "on"}),
        )

        await session.shutdown()
        record(
            "shutdown: session is idle immediately after shutdown() returns (it blocked)",
            session.state == "idle",
        )
        record(
            "shutdown: both flags are CONFIRMED restored off",
            console.flags == {"loradebug": "off", "txcapture": "off"},
        )
    finally:
        await console.stop()


# ── 6. stop() during the --info probe window sends nothing ──────────────


async def _test_stop_during_info_window_sends_nothing(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="off", txcapture="off")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console)

        await session.start()
        record(
            "stop-during-probe: reaches active",
            await _wait_until(lambda: session.state == "active"),
        )
        # Land the stop as close to the state flip as possible: the --info
        # collection window always runs its full info_reply_window_s
        # regardless of how fast the node answers, so this reliably lands
        # inside it.
        await session.stop()
        record(
            "stop-during-probe: settles to idle",
            await _wait_until(lambda: session.state == "idle", timeout_s=5.0),
        )
        record(
            "stop-during-probe: no on command was ever sent",
            not any(c.endswith(" on") for c in console.commands),
        )
        record(
            "stop-during-probe: the fake's flag state is untouched",
            console.flags == {"loradebug": "off", "txcapture": "off"},
        )
        record(
            "stop-during-probe: nothing was ever pending to restore",
            session.pending_restore == set(),
        )
    finally:
        await console.stop()


# ── 7. RST right after the first restore command ─────────────────────────


async def _test_rst_after_restore_command_pending_then_recovered(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="off", txcapture="on", close_after_command="--loradebug off")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        # item 5: _graceful_stop's own failure path now also kicks off the
        # background retry, so `_task` stays alive underneath — give it a
        # fast (but non-zero) backoff so it doesn't race ahead of the
        # `pending_restore == {"loradebug"}` assertion below, and so this
        # test doesn't pay the real (5s) production backoff.
        session = _new_session(wire_monitor, console, restore_retry_backoffs_s=(0.5, 0.5, 0.5))

        await session.start()
        record("rst: reaches active", await _wait_until(lambda: session.state == "active"))
        record(
            "rst: --loradebug on is sent",
            await _wait_until(lambda: "--loradebug on" in console.commands),
        )
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)

        await session.stop()
        record(
            "rst: session lands in error after the node RSTs mid-restore",
            await _wait_until(lambda: session.state == "error", timeout_s=5.0),
        )
        record(
            "rst: pending_restore contains exactly the affected flag",
            session.pending_restore == {"loradebug"},
        )
        record(
            "rst: error message names the flag that may still be on",
            "loradebug" in (session.error or ""),
        )
        record(
            "rst: status() reflects pending_restore",
            session.status()["pending_restore"] == ["loradebug"],
        )

        # Next session: pending_restore forces prior "off" regardless of
        # --info, so it re-sends `on` and (this time, uninterrupted)
        # confirms the restore at its own stop.
        await session.start()
        record(
            "rst: next session reaches active",
            await _wait_until(lambda: session.state == "active", timeout_s=5.0),
        )
        record(
            "rst: next session re-sends --loradebug on (treated as prior off)",
            await _wait_until(lambda: console.commands.count("--loradebug on") == 2),
        )
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)

        await session.stop()
        record(
            "rst: next session settles to idle",
            await _wait_until(lambda: session.state == "idle", timeout_s=5.0),
        )
        record(
            "rst: pending_restore cleared once confirmed",
            session.pending_restore == set(),
        )
        record(
            "rst: the fake's flag is confirmed off again",
            console.flags["loradebug"] == "off",
        )
    finally:
        await console.stop()


# ── 7b. _graceful_stop's own restore failure also triggers a retry ──────


async def _test_graceful_stop_failure_triggers_background_retry(record: RecordFn) -> None:
    """item 5: a restore failure discovered during `_graceful_stop`
    (connection lost mid-restore — the RST case, not just the
    EOF-while-streaming path in test 8) must also kick off the bounded
    background retry, so the flag self-heals with NO further start()/
    stop() calls from the operator."""
    console = _FakeConsole(loradebug="off", txcapture="on", close_after_command="--loradebug off")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console, restore_retry_backoffs_s=(0.2, 0.2, 0.2))

        await session.start()
        record(
            "graceful-stop-retry: reaches active",
            await _wait_until(lambda: session.state == "active"),
        )
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)

        await session.stop()
        record(
            "graceful-stop-retry: lands in error after the node RSTs mid-restore",
            await _wait_until(lambda: session.state == "error", timeout_s=5.0),
        )
        record(
            "graceful-stop-retry: self-heals via the background retry alone",
            await _wait_until(
                lambda: session.pending_restore == set() and console.flags["loradebug"] == "off",
                timeout_s=5.0,
            ),
        )
        record(
            "graceful-stop-retry: state clears back to idle on its own",
            await _wait_until(lambda: session.state == "idle", timeout_s=3.0),
        )
    finally:
        await console.stop()


# ── 8. Unexpected EOF while streaming: background retry recovers ────────


async def _test_eof_while_active_background_retry_succeeds(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="off", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console, restore_retry_backoffs_s=(0.2, 0.2, 0.2))

        await session.start()
        record("eof-retry: reaches active", await _wait_until(lambda: session.state == "active"))
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)
        record("eof-retry: loradebug turned on", console.flags["loradebug"] == "on")

        writer = console.current_writer
        assert writer is not None
        writer.close()

        record(
            "eof-retry: session lands in error after the unexpected EOF",
            await _wait_until(lambda: session.state == "error", timeout_s=3.0),
        )
        record(
            "eof-retry: error names reboot/link loss, not a takeover",
            "reboot" in (session.error or "").lower(),
        )
        record(
            "eof-retry: loradebug recorded pending restore",
            "loradebug" in session.pending_restore,
        )

        record(
            "eof-retry: background retry reconnects and confirms the restore",
            await _wait_until(lambda: console.flags["loradebug"] == "off", timeout_s=5.0),
        )
        record(
            "eof-retry: pending_restore clears once the background retry confirms",
            await _wait_until(lambda: session.pending_restore == set(), timeout_s=2.0),
        )
        record(
            "eof-retry: session automatically clears back to idle on a successful retry"
            " (item 3 — no explicit stop()/start() needed)",
            await _wait_until(lambda: session.state == "idle", timeout_s=2.0),
        )
        record("eof-retry: error is cleared too", session.error is None)
    finally:
        await console.stop()


# ── 8b. pending_restore changes are broadcast even without a state change ──


async def _test_pending_restore_change_is_broadcast(record: RecordFn) -> None:
    """item 3: a successful background restore retry used to leave the
    LAST `monitor:console` broadcast stuck showing the stale
    `pending_restore` list and `state: "error"` — nothing re-broadcasts
    just because `pending_restore` changed on its own. Assert on the
    broadcast STREAM itself, not just the session's attributes."""
    console = _FakeConsole(loradebug="off", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        recorder = _RecordingSSEManager()
        wire_monitor.sse_manager = recorder
        session = _new_session(wire_monitor, console, restore_retry_backoffs_s=(0.2, 0.2, 0.2))

        await session.start()
        await _wait_until(lambda: session.state == "active")
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)

        writer = console.current_writer
        assert writer is not None
        writer.close()
        await _wait_until(lambda: session.state == "error", timeout_s=3.0)

        record(
            "broadcast: an error broadcast carries the pending flag",
            await _wait_until(
                lambda: any(
                    event == "monitor:console" and data.get("pending_restore") == ["loradebug"]
                    for event, data in recorder.events
                ),
                timeout_s=3.0,
            ),
        )
        record(
            "broadcast: a LATER broadcast reflects pending_restore emptying out AND state"
            " going back to idle together (the same _set_state call)",
            await _wait_until(
                lambda: any(
                    event == "monitor:console"
                    and data.get("pending_restore") == []
                    and data.get("state") == "idle"
                    for event, data in recorder.events
                ),
                timeout_s=5.0,
            ),
        )
    finally:
        await console.stop()


# ── 9. start() interrupts a background retry and launches fresh IN-CALL ──


async def _test_start_from_error_launches_fresh_session_in_same_call(record: RecordFn) -> None:
    """item 2: start() while a background restore retry is in flight
    (state "error", task still running) interrupts it and — since it
    wakes almost immediately on the stop event — awaits it (bounded) and
    launches the new session in this SAME call, rather than leaving the
    operator's click a no-op that reports unchanged "error" and requires
    a second start(). Exactly one new connection results (the interrupted
    retry's own reconnect never happens); never two concurrent sessions."""
    console = _FakeConsole(loradebug="off", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console, restore_retry_backoffs_s=(5.0, 5.0, 5.0))

        await session.start()
        record("start-race: reaches active", await _wait_until(lambda: session.state == "active"))
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)

        writer = console.current_writer
        assert writer is not None
        writer.close()
        record(
            "start-race: session lands in error (background retry now pending, 5s backoff)",
            await _wait_until(lambda: session.state == "error", timeout_s=3.0),
        )
        connections_before = console.connections_accepted

        # start() well inside the 5s backoff: the retry has not reconnected
        # yet, so any NEW connection below can only be this call's own.
        start_status = await session.start()
        record(
            "start-race: start() from error launches a fresh session in the SAME call",
            start_status["state"] in ("connecting", "active"),
        )
        record(
            "start-race: the fresh session reaches active",
            await _wait_until(lambda: session.state == "active", timeout_s=3.0),
        )
        record(
            "start-race: exactly one new connection was made"
            " (the interrupted retry never got to reconnect)",
            console.connections_accepted == connections_before + 1,
        )

        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)
        await session.stop()
        record(
            "start-race: the fresh session settles to idle",
            await _wait_until(lambda: session.state == "idle", timeout_s=5.0),
        )
        record(
            "start-race: the pending flag was confirmed restored by the fresh session",
            console.flags["loradebug"] == "off",
        )
        record(
            "start-race: pending_restore is empty",
            session.pending_restore == set(),
        )
    finally:
        await console.stop()


# ── 9b. start() landing between state=idle and the task finishing ───────


async def _test_start_during_idle_tail_race(record: RecordFn) -> None:
    """item 6: `_run()` sets `state = "idle"` a few purely synchronous
    steps before the task object itself reports `done()`. A `start()`
    landing in that window used to see `_task` not `done()` yet, state
    `!= "error"`, and just return the (idle) status without launching
    anything — a silent no-op. Widen that window deterministically with a
    yielding broadcast stub and a background poller that fires `start()`
    the instant it observes the window, then assert it actually launched."""
    console = _FakeConsole(loradebug="off", txcapture="off")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        wire_monitor.sse_manager = _YieldingSSEManager(delay_s=0.05)
        session = _new_session(wire_monitor, console)

        await session.start()
        record(
            "idle-tail-race: reaches active", await _wait_until(lambda: session.state == "active")
        )
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.1)

        race_result: dict[str, Any] = {}

        async def _racer() -> None:
            while True:
                task = session._task
                if task is None or task.done():
                    return
                if session.state == "idle":
                    race_result["status"] = await session.start()
                    return
                await asyncio.sleep(0.001)

        racer_task = asyncio.ensure_future(_racer())
        await session.stop()
        await racer_task

        record(
            "idle-tail-race: the idle-but-not-done() window was observed and start() fired in it",
            "status" in race_result,
        )
        record(
            "idle-tail-race: that start() actually launched a session (not a silent no-op)",
            race_result.get("status", {}).get("state") in ("connecting", "active"),
        )
        record(
            "idle-tail-race: the session reaches active",
            await _wait_until(lambda: session.state == "active", timeout_s=3.0),
        )

        await session.stop()
        await _wait_until(lambda: session.state == "idle", timeout_s=5.0)
    finally:
        await console.stop()


# ── 10. Busy console: banner never arrives ───────────────────────────────


async def _test_console_busy_no_banner(record: RecordFn) -> None:
    console = _FakeConsole(send_banner=False)
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console)

        await session.start()
        record(
            "busy: session reaches error after the banner never arrives",
            await _wait_until(lambda: session.state == "error", timeout_s=3.0),
        )
        record(
            "busy: error message says busy, not a generic connect failure",
            "busy" in (session.error or "").lower(),
        )
    finally:
        await console.stop()


# ── 10b. stop() racing a connect-phase failure goes idle, not error ─────


async def _test_stop_during_connect_failure_goes_idle_not_error(record: RecordFn) -> None:
    """item 4: stop() called while `_connect()` is still in flight and
    about to fail (here: "console busy", banner never arrives) used to
    still land in `"error"` once that failure surfaced — even though the
    operator explicitly asked to stop. A connect-phase failure after a
    stop was requested is not an error worth surfacing: go idle."""
    console = _FakeConsole(send_banner=False)
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console)

        await session.start()
        record(
            "stop-during-connect-fail: start() leaves the session 'connecting'",
            session.state == "connecting",
        )

        await session.stop()
        record(
            "stop-during-connect-fail: settles to idle, NOT error, despite the connect"
            " eventually failing (busy)",
            await _wait_until(lambda: session.state == "idle", timeout_s=3.0),
        )
        record("stop-during-connect-fail: no error is recorded", session.error is None)
    finally:
        await console.stop()


# ── 11. Password never leaked (successful session) ──────────────────────


async def _test_password_never_leaked_on_success(record: RecordFn) -> None:
    secret = "S3cr3t-Node-Pass!"
    console = _FakeConsole(password=secret, loradebug="off", txcapture="off")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console, password=secret)

        await session.start()
        record(
            "password leak: session authenticates and reaches active",
            await _wait_until(lambda: session.state == "active"),
        )
        await asyncio.sleep(_FAST_TIMINGS["info_reply_window_s"] + 0.2)
        await session.stop()
        record(
            "password leak: session settles to idle",
            await _wait_until(lambda: session.state == "idle", timeout_s=5.0),
        )

        blob = json.dumps(session.status()) + "\n".join(_console_texts(wire_monitor))
        record(
            "password leak: the real password never appears anywhere captured", secret not in blob
        )
    finally:
        await console.stop()


# ── 12. REST layer ────────────────────────────────────────────────────────


def _rest_app(wire_monitor: WireMonitor | None, session: NodeConsoleSession | None) -> FastAPI:
    app = FastAPI()
    kwargs: dict[str, Any] = {"wire_monitor": wire_monitor}
    if session is not None:
        kwargs["node_console"] = session
    manager = SimpleNamespace(**kwargs)
    app.include_router(build_monitor_router(cast("SSEManager", manager)))
    return app


async def _test_rest_layer(record: RecordFn) -> None:
    console = _FakeConsole(loradebug="off", txcapture="on")
    await console.start()
    try:
        wire_monitor = WireMonitor()
        session = _new_session(wire_monitor, console)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(wire_monitor, None)), base_url="http://t"
        ) as client:
            resp = await client.get("/api/monitor/console")
            record("REST: no session wired -> 503", resp.status_code == 503)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_rest_app(wire_monitor, session)), base_url="http://t"
        ) as client:
            resp = await client.get("/api/monitor/console")
            body = resp.json()
            record(
                "REST: GET status starts idle, no password field, pending_restore present",
                resp.status_code == 200
                and body["state"] == "idle"
                and "password" not in body
                and body["pending_restore"] == [],
            )

            resp = await client.post("/api/monitor/console/start")
            record("REST: POST start -> 200", resp.status_code == 200)
            record(
                "REST: session reaches active after start",
                await _wait_until(lambda: session.state == "active"),
            )

            resp = await client.post("/api/monitor/console/start")
            record(
                "REST: POST start while active is idempotent (still active, no error)",
                resp.status_code == 200 and resp.json()["state"] == "active",
            )

            resp = await client.post("/api/monitor/console/stop")
            body = resp.json()
            record(
                "REST: POST stop -> 200, session moves off 'active' promptly",
                resp.status_code == 200 and body["state"] != "active",
            )
            record(
                "REST: session eventually settles to idle",
                await _wait_until(lambda: session.state == "idle", timeout_s=5.0),
            )

            resp = await client.post("/api/monitor/console/stop")
            record(
                "REST: POST stop while idle is idempotent",
                resp.status_code == 200 and resp.json()["state"] == "idle",
            )
    finally:
        await console.stop()


# ── 13. WireMonitor console ring / merge ──────────────────────────────────


async def _test_console_ring_does_not_evict_rf(record: RecordFn) -> None:
    monitor = WireMonitor()
    rf_seqs: list[int] = []
    for i in range(5):
        env = await monitor.capture("udp", "rx", "shown", None, {"i": i})
        rf_seqs.append(env["seq"])

    for i in range(2500):
        await monitor.capture("console", "rx", "shown", None, {"type": "con", "msg": str(i)})

    record(
        "console ring: 2500 console captures never evict the RF ring",
        [env["seq"] for env in monitor._ring] == rf_seqs,
    )
    record(
        "console ring: console ring itself is capped at RING_MAXLEN",
        len(monitor._console_ring) == RING_MAXLEN,
    )


async def _test_page_merges_both_rings_by_seq(record: RecordFn) -> None:
    monitor = WireMonitor()
    await monitor.capture("udp", "rx", "shown", None, {"n": "rf1"})  # seq 1
    await monitor.capture("console", "rx", "shown", None, {"type": "con", "msg": "c1"})  # seq 2
    await monitor.capture("udp", "rx", "shown", None, {"n": "rf2"})  # seq 3
    await monitor.capture("console", "rx", "shown", None, {"type": "con", "msg": "c2"})  # seq 4

    page = monitor.page()
    record(
        "page(): merges RF + console rings ordered by the shared seq counter",
        [(env["seq"], env["link"]) for env in page["frames"]]
        == [(1, "udp"), (2, "console"), (3, "udp"), (4, "console")],
    )
    record("page(): has_more is False with everything captured", page["has_more"] is False)


# ── entrypoint ────────────────────────────────────────────────────────────


async def run_node_console_tests() -> bool:
    """Return True iff every node console bridge regression case passes."""
    if has_console:
        print("\nTesting NodeConsoleSession (node debug console bridge):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'PASS' if ok else 'FAIL'} | {label}")

    await _test_wrong_password(_record)
    await _test_flags_set_and_restored(_record)
    await _test_lines_and_partial_flush(_record)
    await _test_stream_lines_no_data_loss_across_yielding_broadcast(_record)
    await _test_max_session_auto_stop(_record)
    await _test_shutdown_restores(_record)
    await _test_stop_during_info_window_sends_nothing(_record)
    await _test_rst_after_restore_command_pending_then_recovered(_record)
    await _test_graceful_stop_failure_triggers_background_retry(_record)
    await _test_eof_while_active_background_retry_succeeds(_record)
    await _test_pending_restore_change_is_broadcast(_record)
    await _test_start_from_error_launches_fresh_session_in_same_call(_record)
    await _test_start_during_idle_tail_race(_record)
    await _test_console_busy_no_banner(_record)
    await _test_stop_during_connect_failure_goes_idle_not_error(_record)
    await _test_password_never_leaked_on_success(_record)
    await _test_rest_layer(_record)
    await _test_console_ring_does_not_evict_rf(_record)
    await _test_page_merges_both_rings_by_seq(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\nNodeConsoleSession Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run_node_console_tests()) else 1)
