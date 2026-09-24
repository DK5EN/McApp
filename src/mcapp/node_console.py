"""Node debug console bridge (MeshCom firmware TCP net console, port 2323).

Bridges MCProxy to a MeshCom node's `net_console.cpp` debug console (ESP32
only — nRF52 nodes have no such console, so a connect attempt against one
simply fails, reported as an ordinary connect error). Turning it on
(`--loradebug on` / `--txcapture on`) streams the firmware's internal
decision trace and raw-frame capture into the existing RF Monitor flow
(`wire_monitor.WireMonitor`) as `link="console"` envelopes, so the webapp's
`/monitor` can show it live over the same `wire:frame` SSE channel, plus
session-lifecycle notices (`notice: True` on the same frame shape).

Protocol (`MeshCom-Firmware-DEV-Main/src/net_console.cpp` header comment):
the server sends either `NONCE: <32 hex>\\r\\n` (password set) — the client
replies with `hex(HMAC-SHA256(password, bytes.fromhex(nonce)))` + `\\n` and
the server answers `OK\\r\\n<banner>` or `FAIL\\r\\n` + close — or, with no
password configured, `OK\\r\\n<banner>` directly. Reference client:
`MeshCom-Firmware-DEV-Main/tools/meshlogger.py`'s `Console` class, mirrored
here for the handshake and the flag save/restore semantics — with one
deliberate behavioural difference from that script: meshlogger sends every
managed flag unconditionally and restores every flag whose prior state it
found; this session only sends `--<flag> on` for a flag that is not already
on (skip an on-air command that would be a no-op), and at stop only restores
the flags it actually turned on this session (`_flags_needing_restore`),
never a flag left alone at start.

**Single client, no takeover.** `net_console.cpp`'s `loopNetConsole` never
calls `accept()` while a client is authenticated — a second connection
attempt is left sitting in the TCP backlog with no banner ever sent, which
is how "the console is busy" actually manifests here: not a rejected
connection, but a banner that never arrives (`_connect` reports this as
`ConsoleBusyError` once `HANDSHAKE_TIMEOUT_S` elapses with no `NONCE:`/`OK`
line). Nobody can take our session over while it holds the socket. An
unexpected EOF while active therefore means the *node* went away — reboot,
link loss, or an operator's `stopNetConsole` — never another client
stealing the connection.

**Restore is confirmed, not assumed.** The node reads the console one byte
per main-loop iteration and echoes each byte back
(`serial_command_esp32.cpp`), so a restore command that is written and
`drain()`-ed is not proof the node actually acted on it — a close racing
that echo can get the bytes discarded by the node's own TCP stack before
they are read. Every restore therefore sends the `off` command(s) and then
re-probes with `--info` to confirm each flag now reads `off`
(`_restore_with_confirmation`), retrying the whole batch once. A flag that
still won't confirm goes into `pending_restore` — persisted on the session
across restarts, *not* reset by `start()` — so the next session treats it
as prior `off` regardless of what `--info` claims (we know we turned it
on) and restores it again, and an EOF while a flag is pending gets its own
bounded background retry (`_background_restore_retry`, backoff 5/30/60 s).
The close itself is always graceful (`_close_gracefully`): `write_eof()`,
then drain and discard whatever the node still sends until it closes or
~2 s pass, then `close()` — never a hard close while the node might still
be writing the very echo the restore commands are waiting on.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import re
from typing import TYPE_CHECKING, Any

from .logging_setup import get_logger
from .util import now_ms

if TYPE_CHECKING:
    from .wire_monitor import WireMonitor

logger = get_logger(__name__)

CONNECT_TIMEOUT_S = 5.0
HANDSHAKE_TIMEOUT_S = 5.0
# How long to collect an `--info` reply before parsing flag state out of it
# (mirrors meshlogger's 4 s probe window). Used both for the startup probe
# and every post-restore confirmation probe.
INFO_REPLY_WINDOW_S = 4.0
# Gap between successive `--<flag> on`/`off` commands.
FLAG_COMMAND_GAP_S = 1.0
# Read timeout in the main loop: doubles as the partial-line flush interval
# (a pending partial line is flushed once this long passes with nothing
# more arriving) and the max-session poll granularity — the stop request
# itself wakes the loop immediately rather than waiting on this.
IDLE_FLUSH_S = 1.0
# How long a graceful close waits for the node to finish writing (its last
# echo, any trailing trace lines) after write_eof(), before closing anyway.
CLOSE_DRAIN_S = 2.0
MAX_LINE_CHARS = 1000
# Safety net: force-flush an unterminated line that has grown this large even
# though data keeps arriving, so a runaway stream cannot grow the buffer
# without bound.
MAX_PARTIAL_BUFFER_BYTES = 8192
STOP_JOIN_TIMEOUT_S = 20.0
# How long start() waits for a just-idled _run() task to actually finish
# unwinding (state flips to "idle" a few synchronous steps before the task
# itself is done() — see start()'s "idle" branch) before giving up and
# refusing to launch a second concurrent task.
IDLE_TAIL_JOIN_S = 2.0
# Background restore-retry backoff after an unexpected EOF leaves a flag
# pending (item 4): bounded, cancelled by stop()/shutdown().
RESTORE_RETRY_BACKOFFS_S: tuple[float, ...] = (5.0, 30.0, 60.0)

# Flags this session manages, in the order commands are sent/restored.
FLAG_NAMES: tuple[str, ...] = ("loradebug", "txcapture")

_FLAG_STATE_RE = {
    name: re.compile(rb"\.\.\." + name.upper().encode("ascii") + rb" (on|off)")
    for name in FLAG_NAMES
}

_EOF_ERROR = "console connection closed by node (reboot or link loss?)"
_BUSY_ERROR = "console busy (another client connected?) or node not accepting connections"


class ConsoleBusyError(ConnectionError):
    """The console banner never arrived within `HANDSHAKE_TIMEOUT_S`.

    `net_console.cpp` never calls `accept()` for a new client while another
    is authenticated, so a second connection attempt is accepted at the TCP
    level (it can sit in the listen backlog) but never gets a banner — this,
    not a refused connection, is what "the console is busy" looks like on
    the wire.
    """


class NodeConsoleSession:
    """One `/monitor` DBG session against a node's debug console.

    Constructed once in `build_app` (`main.py`) and wired to the live
    `WireMonitor` (console lines/notices) — the SSE broadcast of state
    changes (`monitor:console`) goes through `wire_monitor.sse_manager`,
    the same attribute `WireMonitor.capture` itself uses, so no separate
    `SSEManager` reference is needed here. `host`/`port`/`password`/
    `max_session_s` are read from config at construction.
    `start()`/`stop()` are idempotent and safe to call repeatedly from the
    REST layer (`sse_routes/monitor.py`).
    """

    def __init__(  # noqa: PLR0913 - the timing knobs are an independent testability seam (see docstring above)
        self,
        wire_monitor: WireMonitor,
        host: str,
        port: int,
        password: str,
        max_session_s: int,
        *,
        info_reply_window_s: float = INFO_REPLY_WINDOW_S,
        idle_flush_s: float = IDLE_FLUSH_S,
        flag_command_gap_s: float = FLAG_COMMAND_GAP_S,
        close_drain_s: float = CLOSE_DRAIN_S,
        handshake_timeout_s: float = HANDSHAKE_TIMEOUT_S,
        restore_retry_backoffs_s: tuple[float, ...] = RESTORE_RETRY_BACKOFFS_S,
    ) -> None:
        self._wire_monitor = wire_monitor
        self.host = host
        self.port = port
        self._password = password
        self.max_session_s = max_session_s
        # Timing knobs, overridable per-instance (production wiring in
        # main.py never passes these — only `node_console_tests.py` does, to
        # run the same handshake/flag/idle-flush logic against a local fake
        # console server without paying the real-node-shaped windows.
        self._info_reply_window_s = info_reply_window_s
        self._idle_flush_s = idle_flush_s
        self._flag_command_gap_s = flag_command_gap_s
        self._close_drain_s = close_drain_s
        self._handshake_timeout_s = handshake_timeout_s
        self._restore_retry_backoffs_s = restore_retry_backoffs_s

        self.state: str = "idle"
        self.error: str | None = None
        self.since: int | None = None
        self.prior_flags: dict[str, str | None] = dict.fromkeys(FLAG_NAMES)
        # A flag we turned on and could not *confirm* restored — survives
        # across sessions on purpose (see module docstring). Never reset by
        # start(); only discard()/update() ever touch it.
        self.pending_restore: set[str] = set()

        # Flags this session itself actually sent an `on` command for (as
        # opposed to `prior_flags`, which also records flags found already
        # on). Reset every start() — this is what `_flags_needing_restore`
        # is keyed on, so a session that never sent a command (e.g. stopped
        # during the --info probe) never tries to "restore" anything.
        self._flags_turned_on: set[str] = set()

        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None

    # ── status / REST-facing surface (sse_routes/monitor.py) ────────────

    def status(self) -> dict[str, Any]:
        """`{state, host, port, since, error, prior_flags, pending_restore,
        max_session_s}` — never includes the password."""
        return {
            "state": self.state,
            "host": self.host,
            "port": self.port,
            "since": self.since,
            "error": self.error,
            "prior_flags": dict(self.prior_flags),
            "pending_restore": sorted(self.pending_restore),
            "max_session_s": self.max_session_s,
        }

    async def start(self) -> dict[str, Any]:
        """Never launches a second session on top of a genuinely live one
        (`"connecting"`/`"active"`/`"stopping"`) — the caller gets back
        whatever is currently happening. Two cases where `self._task` is
        technically still not `done()` are NOT a live session, and both
        are resolved in this same call rather than making the operator
        call `start()` twice:

        - `"error"`: a background restore retry (`_background_restore_retry`)
          may be in flight. It is interrupted (stop event) and awaited —
          it wakes almost immediately — before launching the new session,
          which will restore any flags still in `pending_restore` anyway
          (`_apply_flags` forces prior `"off"` for them).
        - `"idle"`: `_run()` flips state to `"idle"` a few purely
          synchronous steps before the task object itself reports
          `done()` — briefly awaiting that tail (bounded,
          `IDLE_TAIL_JOIN_S`) avoids silently refusing a `start()` that
          landed in that window while still reporting `"idle"`.
        """
        if self._task is not None and not self._task.done():
            task = self._task
            if self.state == "error":
                self._stop_event.set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=STOP_JOIN_TIMEOUT_S)
                if not task.done():
                    task.cancel()
                    self._writer = None
            elif self.state == "idle":
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=IDLE_TAIL_JOIN_S)
                if not task.done():
                    return self.status()
            else:
                return self.status()
        self.error = None
        self.since = None
        self.prior_flags = dict.fromkeys(FLAG_NAMES)
        self._flags_turned_on = set()
        self._stop_event = asyncio.Event()
        await self._set_state("connecting")
        self._task = asyncio.create_task(self._run())
        return self.status()

    async def stop(self) -> dict[str, Any]:
        """Returns promptly for a live session: sets the stop event and
        reports `"stopping"` (or whatever state the session is already
        moving through) — the actual restore/close happens in the
        background and its progress arrives via `monitor:console` SSE
        broadcasts and console notices, not by blocking this call.

        From `"error"` this instead awaits the still-running task
        (bounded, `STOP_JOIN_TIMEOUT_S`, via `_stop_and_await`) before
        returning idle, because `"error"` can mean a background restore
        retry is in flight (item 4) and the caller wants a settled
        outcome, not a stale error while retries continue underneath.
        """
        if self.state == "idle":
            return self.status()
        if self.state != "error":
            self._stop_event.set()
            if self.state != "stopping":
                await self._set_state("stopping")
            return self.status()
        return await self._stop_and_await(STOP_JOIN_TIMEOUT_S)

    async def _stop_and_await(self, bound_s: float) -> dict[str, Any]:
        """Set the stop event and block until the session task finishes or
        `bound_s` elapses, then force state back to idle. Shared by
        `stop()`'s `"error"` branch (item 3 — a background restore retry
        may be running) and `shutdown()` (item 6 — always needs a settled
        outcome before the process exits, regardless of state)."""
        self._stop_event.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=bound_s)
            except TimeoutError:
                logger.warning(
                    "NodeConsoleSession: session task did not finish within %ss", bound_s
                )
                task.cancel()
                self._writer = None
        if self.state != "idle":
            self.error = None
            self.since = None
            await self._set_state("idle")
        return self.status()

    async def shutdown(self) -> None:
        """Best-effort stop for app shutdown (`main.py`'s
        `_shutdown_services`) — always awaits completion (bounded,
        `STOP_JOIN_TIMEOUT_S` ~20 s, via `_stop_and_await`) so flags are
        restored before process exit, regardless of what `stop()`'s
        prompt-return behaviour would otherwise report. Never raises, so a
        wedged console session can never block the rest of the shutdown
        ladder."""
        try:
            if self.state == "idle":
                return
            await self._stop_and_await(STOP_JOIN_TIMEOUT_S)
        except Exception:
            logger.exception("NodeConsoleSession.shutdown: stop() failed")

    # ── notices / capture plumbing ───────────────────────────────────────

    async def _set_state(self, state: str, error: str | None = None) -> None:
        self.state = state
        if error is not None:
            self.error = error
        try:
            await self._wire_monitor.capture(
                "console", "rx", "shown", None, self._notice_frame(f"state: {state}")
            )
        except Exception:
            logger.exception("NodeConsoleSession: state-change notice capture failed")
        await self._broadcast_status()

    async def _broadcast_status(self) -> None:
        """Broadcast the current `status()` as `monitor:console`, without a
        state transition or a console notice line. `_set_state` calls this
        for every transition; call it directly when something the status
        payload carries changed WITHOUT a state transition — `pending_restore`
        shrinking or growing during a background restore retry is the only
        current case (see `_background_restore_retry`)."""
        try:
            sse_manager = self._wire_monitor.sse_manager
            if sse_manager is not None:
                await sse_manager.broadcast_event("monitor:console", self.status())
        except Exception:
            logger.exception("NodeConsoleSession: monitor:console broadcast failed")

    def _notice_frame(self, msg: str) -> dict[str, Any]:
        return {"type": "con", "msg": msg, "src": self.host, "notice": True}

    async def _notice(self, msg: str) -> None:
        try:
            frame = self._notice_frame(msg)
            await self._wire_monitor.capture("console", "rx", "shown", None, frame)
        except Exception:
            logger.exception("NodeConsoleSession: notice capture failed (%s)", msg)

    async def _emit_line(self, text: str) -> None:
        text = text.rstrip("\r")
        if not text:
            return
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS]
        try:
            await self._wire_monitor.capture(
                "console", "rx", "shown", None, {"type": "con", "msg": text, "src": self.host}
            )
        except Exception:
            logger.exception("NodeConsoleSession: line capture failed")

    async def _fail(self, message: str) -> None:
        logger.warning("NodeConsoleSession: %s", message)
        await self._set_state("error", error=message)

    async def _fail_or_stop(self, message: str) -> None:
        """Like `_fail`, except when `stop()` already requested a stop
        before this failure happened (e.g. `stop()` raced a `_connect()`
        that then times out, or hits "console busy"): the operator asked
        to stop, so a connect-phase failure is not an error worth
        surfacing — go idle instead of `"error"`."""
        if self._stop_event.is_set():
            logger.info(
                "NodeConsoleSession: %s (stop already requested — going idle, not error)", message
            )
            self.error = None
            self.since = None
            await self._set_state("idle")
            return
        await self._fail(message)

    async def _maybe_start_background_restore_retry(self) -> None:
        if not self.pending_restore:
            return
        # A fresh event: `_background_restore_retry` treats the stop event
        # firing as "abandon retries", but the CURRENT event may already be
        # set — this can be called right after a user-initiated stop()
        # (`_graceful_stop`'s failure branches), whose OWN stop_event.set()
        # is what got the session here in the first place and has already
        # fully served its purpose. Without this reset, that stale "set"
        # state made the retry abandon on its very first check, every time
        # (item 5's bug: a restore failure discovered during a user stop
        # never actually retried). Only a NEW stop()/shutdown()/start()
        # from here on should be able to interrupt this retry.
        self._stop_event = asyncio.Event()
        await self._background_restore_retry()

    # ── handshake ─────────────────────────────────────────────────────

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
        """Open the TCP connection and complete the NONCE/HMAC (or
        no-password) handshake. Returns `(reader, writer, pending)`, where
        `pending` is any banner bytes already read past the handshake line.
        The writer is closed explicitly on every failure path below (a
        timed-out banner, EOF, a rejected password, an unrecognised
        banner), so no caller has to remember to clean up a
        partially-established connection.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=CONNECT_TIMEOUT_S
        )
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = await asyncio.wait_for(reader.read(256), timeout=self._handshake_timeout_s)
            except TimeoutError as exc:
                writer.close()
                raise ConsoleBusyError(_BUSY_ERROR) from exc
            if not chunk:
                writer.close()
                raise ConnectionError("connection closed before the console banner arrived")
            buf += chunk
        line, _, rest = buf.partition(b"\n")
        text = line.decode("utf-8", errors="replace").strip()

        if text.startswith("NONCE:"):
            nonce_hex = text.split(":", 1)[1].strip()
            digest = hmac.new(
                self._password.encode("utf-8"), bytes.fromhex(nonce_hex), hashlib.sha256
            ).hexdigest()
            writer.write((digest + "\n").encode("utf-8"))
            await writer.drain()
            resp = b""
            while b"\n" not in resp:
                chunk = await asyncio.wait_for(reader.read(256), timeout=self._handshake_timeout_s)
                if not chunk:
                    writer.close()
                    raise ConnectionError("connection closed during authentication")
                resp += chunk
            resp_line, _, resp_rest = resp.partition(b"\n")
            if not resp_line.decode("utf-8", errors="replace").strip().startswith("OK"):
                writer.close()
                raise PermissionError("authentication rejected (wrong password?)")
            return reader, writer, resp_rest
        if text.startswith("OK"):
            return reader, writer, rest
        writer.close()
        raise ConnectionError(f"unexpected console banner: {text!r}")

    # ── flags ─────────────────────────────────────────────────────────

    async def _read_info_reply(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, pending: bytes
    ) -> tuple[dict[str, str | None], bytes]:
        """Send `--info`, collect the reply for `info_reply_window_s`,
        stream every complete line it contains through the normal
        console-line path, and return the parsed LORADEBUG/TXCAPTURE state
        plus the unterminated leftover to prime the next read with. Shared
        by the startup probe (`_apply_flags`) and every post-restore
        confirmation probe (`_restore_with_confirmation`) so both parse the
        reply identically. Raises `ConnectionError` if the node closes
        while the reply is still being collected.
        """
        writer.write(b"--info\n")
        await writer.drain()
        buf = pending
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._info_reply_window_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            except TimeoutError:
                break
            if not chunk:
                raise ConnectionError("closed while waiting for the --info reply")
            buf += chunk

        flags: dict[str, str | None] = {}
        for name in FLAG_NAMES:
            match = _FLAG_STATE_RE[name].search(buf)
            flags[name] = match.group(1).decode("ascii") if match else None

        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            await self._emit_line(line.decode("utf-8", errors="replace"))
        return flags, buf

    async def _apply_flags(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, pending: bytes
    ) -> bytes:
        """Probe with `--info`, turn on whichever managed flag is not
        already on, and record what was sent (`_flags_turned_on`) so stop
        knows what to restore. A flag in `pending_restore` is treated as
        prior `"off"` regardless of what `--info` reports — we already
        know we turned it on in an earlier session and could not confirm
        the restore, so this session must drive it through the same
        on/restore-with-confirmation cycle again.

        Checks the stop event before sending any `on` command: a stop
        requested while this probe was in flight means no command is sent
        at all, and the caller goes straight to closing.
        """
        polled, buf = await self._read_info_reply(reader, writer, pending)
        to_set: list[str] = []
        for name in FLAG_NAMES:
            prior = "off" if name in self.pending_restore else polled.get(name)
            self.prior_flags[name] = prior
            if prior != "on":
                to_set.append(name)

        if self._stop_event.is_set():
            await self._notice("stop requested during --info probe — no flag commands sent")
            return buf

        for i, name in enumerate(to_set):
            writer.write(f"--{name} on\n".encode())
            await writer.drain()
            self._flags_turned_on.add(name)
            if i < len(to_set) - 1:
                await asyncio.sleep(self._flag_command_gap_s)

        if to_set:
            summary = ", ".join(f"{n} (was {self.prior_flags[n] or 'unknown'})" for n in to_set)
            await self._notice(f"flags set: {summary}")
        else:
            await self._notice("flags already set: no commands sent")
        return buf

    def _flags_needing_restore(self) -> list[str]:
        """Only a flag THIS SESSION actually sent an `on` command for gets
        restored — a flag left alone at start (already on, or a probe that
        never got to send anything because a stop raced it) is left alone
        at stop too."""
        return [name for name in FLAG_NAMES if name in self._flags_turned_on]

    async def _restore_with_confirmation(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        buf: bytes,
        to_restore: list[str],
    ) -> tuple[bool, list[str], bytes]:
        """Send the `off` command for every flag in `to_restore`, then
        confirm via a fresh `--info` that each one now reads off — never
        trust the write+drain succeeding as proof the node acted on it
        (see module docstring: byte-at-a-time echo, close-races-echo).
        Retries the whole batch once. A flag that confirms off is removed
        from `pending_restore`; a flag still on after both attempts is
        added to it — so a partial failure never loses track of the flags
        that DID confirm. Returns `(all_confirmed, still_on, leftover_buf)`
        and can raise `ConnectionError`/`OSError` if the node drops the
        connection mid-restore.
        """
        if not to_restore:
            await self._notice("no flags to restore")
            return True, [], buf

        still_on = list(to_restore)
        for attempt in range(2):
            for i, name in enumerate(still_on):
                writer.write(f"--{name} off\n".encode())
                await writer.drain()
                if i < len(still_on) - 1:
                    await asyncio.sleep(self._flag_command_gap_s)

            polled, buf = await self._read_info_reply(reader, writer, buf)
            newly_confirmed = [n for n in still_on if polled.get(n) == "off"]
            for n in newly_confirmed:
                self.pending_restore.discard(n)
            still_on = [n for n in still_on if n not in newly_confirmed]

            if not still_on:
                await self._notice("flags restored: " + ", ".join(f"{n} off" for n in to_restore))
                return True, [], buf
            if attempt == 0:
                await self._notice(f"restore not confirmed for {', '.join(still_on)} — retrying")

        self.pending_restore.update(still_on)
        return False, still_on, buf

    # ── close ─────────────────────────────────────────────────────────

    async def _close_gracefully(
        self, writer: asyncio.StreamWriter | None, reader: asyncio.StreamReader | None = None
    ) -> None:
        """`write_eof()` (half-close our side), then drain and discard
        whatever the node still sends — its echo of our last byte-at-a-time
        command, any trailing trace lines — until it closes or
        `close_drain_s` passes, THEN `close()`. Never a hard close while
        the node might still be writing: that is exactly what raced the
        node's echo and left flags on before this rework.
        """
        if writer is None:
            return
        try:
            if writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
        except OSError:
            with contextlib.suppress(OSError):
                writer.close()
            return

        if reader is not None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._close_drain_s
            with contextlib.suppress(OSError):
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
                    except TimeoutError:
                        break
                    if not chunk:
                        break
        with contextlib.suppress(OSError):
            writer.close()

    # ── main loop ─────────────────────────────────────────────────────

    async def _split_lines(self, buf: bytes) -> bytes:
        """Emit every complete line in `buf`, returning the unterminated
        leftover to prime the next read with."""
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            await self._emit_line(line.decode("utf-8", errors="replace"))
        return buf

    @staticmethod
    async def _cancel_and_await(task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _consume_read_result(
        self, read_task: asyncio.Task[bytes], done: set[asyncio.Task[Any]], buf: bytes
    ) -> bytes:
        """Fold one `asyncio.wait` outcome into `buf`: a completed read
        appends and re-splits lines (raising `ConnectionError` on EOF, and
        force-flushing past `MAX_PARTIAL_BUFFER_BYTES`); a timeout instead
        flushes whatever partial line is already pending."""
        if read_task not in done:
            if buf:
                await self._emit_line(buf.decode("utf-8", errors="replace"))
                buf = b""
            return buf

        chunk = read_task.result()
        if chunk == b"":
            raise ConnectionError(_EOF_ERROR)
        buf = await self._split_lines(buf + chunk)
        if len(buf) > MAX_PARTIAL_BUFFER_BYTES:
            await self._emit_line(buf.decode("utf-8", errors="replace"))
            buf = b""
        return buf

    async def _stream_lines(self, reader: asyncio.StreamReader, buf: bytes) -> None:
        """Split `buf`/incoming chunks into lines, flushing a pending
        partial line after `idle_flush_s` of silence. Wakes on the stop
        event immediately (`asyncio.wait` on the read vs. the event,
        rather than polling once per `idle_flush_s`) so `stop()` is
        responsive even with a slow/quiet console. Returns normally on an
        explicit stop request or max-session timeout; raises
        `ConnectionError` on EOF.

        Keeps exactly ONE `reader.read(4096)` task pending across loop
        iterations, replacing it only once its result has actually been
        consumed. A fresh task per iteration (the previous shape) could
        complete DURING the `await` inside `_consume_read_result`'s partial-
        line flush (`_emit_line` → `capture` → a yielding `broadcast_event`)
        — the `finally` cleanup then saw an already-`done()` task and
        silently dropped its chunk instead of cancelling anything, losing
        whatever line(s) it carried. A stop landing while a chunk is
        sitting unconsumed on exit is fine to drop (see `_cancel_and_await`)
        — only the timeout-driven, still-looping case was the bug.
        """
        buf = await self._split_lines(buf)

        stop_task: asyncio.Task[bool] = asyncio.ensure_future(self._stop_event.wait())
        read_task: asyncio.Task[bytes] = asyncio.ensure_future(reader.read(4096))
        try:
            while True:
                if self.since is not None and now_ms() - self.since >= self.max_session_s * 1000:
                    await self._notice(
                        f"max session length reached ({self.max_session_s}s) — stopping"
                    )
                    return

                done, _pending = await asyncio.wait(
                    {read_task, stop_task},
                    timeout=self._idle_flush_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    return
                consumed = read_task in done
                buf = await self._consume_read_result(read_task, done, buf)
                if consumed:
                    read_task = asyncio.ensure_future(reader.read(4096))
        finally:
            await self._cancel_and_await(read_task)
            await self._cancel_and_await(stop_task)

    async def _graceful_stop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Stop (requested or max-session) while the connection is still
        open: restore-with-confirmation on the SAME connection, then close
        gracefully regardless of whether the restore confirmed."""
        if self.state != "stopping":
            await self._set_state("stopping")
        to_restore = self._flags_needing_restore()
        try:
            confirmed, still_on, _buf = await self._restore_with_confirmation(
                reader, writer, b"", to_restore
            )
        except (ConnectionError, OSError) as exc:
            self.pending_restore.update(to_restore)
            self._flags_turned_on = set()
            self._writer = None
            self.since = None
            with contextlib.suppress(OSError):
                writer.close()
            restore_note = ", ".join(sorted(to_restore)) if to_restore else "none"
            await self._set_state(
                "error",
                error=(
                    f"console connection lost while restoring flags ({exc}); "
                    f"flags may still be on: {restore_note}"
                ),
            )
            await self._maybe_start_background_restore_retry()
            return

        self._flags_turned_on = set()
        await self._close_gracefully(writer, reader)
        self._writer = None
        self.since = None
        if confirmed:
            await self._set_state("idle")
        else:
            await self._set_state("error", error=f"flags may still be on: {', '.join(still_on)}")
            await self._maybe_start_background_restore_retry()

    async def _background_restore_retry(self) -> None:
        """Bounded, backed-off retry of whatever is in `pending_restore`
        after an unexpected EOF (item 4). Reconnects, restores-with-
        confirmation, closes gracefully, and stops as soon as either
        `pending_restore` empties out or `stop()`/`shutdown()` sets the
        stop event (checked between every backoff step). Runs inside `_run`
        — i.e. as part of `self._task` — so `start()`/`stop()`'s
        "task still running" checks see it as a live session, not idle.
        """
        for delay in self._restore_retry_backoffs_s:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            else:
                return  # stop()/shutdown() requested — abandon retries

            if not self.pending_restore:
                return
            to_restore = sorted(self.pending_restore)
            try:
                reader, writer, pending = await self._connect()
            except Exception as exc:
                await self._notice(f"background restore retry: connect failed ({exc})")
                continue
            try:
                confirmed, still_on, _buf = await self._restore_with_confirmation(
                    reader, writer, pending, to_restore
                )
            except (ConnectionError, OSError) as exc:
                await self._notice(f"background restore retry: connection lost ({exc})")
                with contextlib.suppress(OSError):
                    writer.close()
                # A partial confirm may have already happened inside that
                # call before it lost the connection — pending_restore can
                # have shrunk even though this attempt overall failed.
                await self._broadcast_status()
                continue
            await self._close_gracefully(writer, reader)
            if confirmed:
                await self._notice("background restore retry succeeded: " + ", ".join(to_restore))
                # The retry resolved what put us in "error" — clear it and
                # go idle rather than leaving a stale error (and a stale
                # pending_restore in the last broadcast) after the fact.
                self.error = None
                self.since = None
                await self._set_state("idle")
                return
            await self._notice(
                "background restore retry: still unconfirmed for " + ", ".join(still_on)
            )
            await self._broadcast_status()

    async def _handle_unexpected_eof(self, writer: asyncio.StreamWriter | None) -> None:
        """EOF while active means the node itself went away — reboot, link
        loss, or an operator's `stopNetConsole` (see module docstring for
        why this is never "another client took over"). State goes to
        `"error"`; any flag this session had turned on and not yet
        restored goes into `pending_restore`, and — if that leaves
        anything pending — a bounded background retry is kicked off.
        """
        if writer is not None:
            with contextlib.suppress(OSError):
                writer.close()
        self._writer = None
        to_restore = self._flags_needing_restore()
        if to_restore:
            self.pending_restore.update(to_restore)
        self._flags_turned_on = set()
        await self._set_state("error", error=_EOF_ERROR)
        await self._maybe_start_background_restore_retry()

    async def _run(self) -> None:
        writer: asyncio.StreamWriter | None = None
        try:
            try:
                reader, writer, pending = await self._connect()
            except TimeoutError:
                await self._fail_or_stop("connect timed out")
                return
            except ConsoleBusyError as exc:
                await self._fail_or_stop(str(exc))
                return
            except PermissionError as exc:
                await self._fail_or_stop(str(exc))
                return
            except (ConnectionError, OSError, ValueError) as exc:
                await self._fail_or_stop(f"connect failed: {exc}")
                return

            self._writer = writer
            self.since = now_ms()
            if self._stop_event.is_set():
                # stop() raced the connect/handshake: nothing was ever
                # activated or touched, so there is nothing to restore —
                # close and go idle without ever reporting "active".
                await self._notice("stop requested during connect — closing without activating")
                await self._close_gracefully(writer, reader)
                self._writer = None
                writer = None
                self.since = None
                await self._set_state("idle")
                return
            await self._set_state("active")
            await self._notice(f"connected to {self.host}:{self.port}")

            try:
                buf = await self._apply_flags(reader, writer, pending)
                if not self._stop_event.is_set():
                    await self._stream_lines(reader, buf)
            except ConnectionError:
                await self._handle_unexpected_eof(writer)
                writer = None
                return

            await self._graceful_stop(reader, writer)
            writer = None
        except Exception:
            logger.exception("NodeConsoleSession: session loop failed unexpectedly")
            await self._fail("internal error — see server logs")
        finally:
            if writer is not None:
                with contextlib.suppress(OSError):
                    writer.close()
            self._writer = None
