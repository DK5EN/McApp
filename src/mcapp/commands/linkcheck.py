"""LinkCheckMixin: McApp-driven `{ping}`/`{pong}` link check sessions.

See `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md` §1.2-§1.5 for the wire
protocol and correlation scheme this module drives, and
`doc/archive/2026-08-13_1500-linkcheck-implementation-plan.md` §3 for the design.
This mirrors `commands/ctcping.py`'s session-engine idioms (state dict keyed
by target, injectable timeout, tracked background tasks with done-callback
discards, callsign/blocklist validation) against a different wire protocol —
see that file for the structural precedent. Do not edit `ctcping.py`.

Correlation, in one sentence: we send `{ping}`, the firmware echoes it back
to us (`src_type:"node"`) carrying the hex `msg_id` we didn't know until now,
and the target's `{pong}` embeds that same id in decimal — `linkcheck.parse()`
and `linkcheck.normalise_id()` (../linkcheck.py) already do the hex/decimal
and sign normalisation; this module only tracks which attempt is waiting for
which id.

That echo only exists on Extern-UDP, and only when the node's EXT IP points
at THIS box. The ping itself goes out over BLE whenever a BLE client is
connected (`_linkcheck_send_topic`), so it is transmitted even with EXTUDP
off; the pong can then only reach us over BLE, and there is no echo. For that
case a pong is also accepted when it answers a ping minted BY OUR NODE: every firmware
msg_id carries `_GW_ID & 0x3FFFFF` in its top 22 bits, and we know our node's
`_GW_ID` from the BLE `I` register (or from any echo). See
`_match_linkcheck_attempt` for the exact rule and what it cannot tell apart.

Which transport the pong arrived on does not matter for "did it answer over
RF": an Extern-UDP `src_type:"lora"` copy and a BLE copy without the
server flag are both the node hearing the target on air. Only the Extern-UDP
copy carries RSSI/SNR (a BLE text frame has no signal footer), so a BLE-first
resolve waits `linkcheck_signal_grace` for the UDP copy to add the signal.

No round-trip time is reported here on purpose (ADR §1.5.4) — `response_ms`
is queueing-dominated and must never be labelled RTT by a caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .. import linkcheck
from ..logging_setup import get_logger
from ..util import now_ms
from ._base import CommandHandlerBase
from .constants import CALLSIGN_STRICT_RE

logger = get_logger(__name__)

# Derived from three live on-air exchanges (ADR §1.5): 23.8 s, 42.6 s, 29.3 s.
# The firmware retransmits our ping every 40 s up to 3 times (MAX_RETRANSMIT
# 3, ~120 s total, ADR §1.4 point 7), so a shorter timeout would falsely fail
# a perfectly good link before the first retransmit is even answered. Do not
# shorten this without new on-air measurements.
LINKCHECK_ATTEMPT_TIMEOUT_S = 90.0

# A pong matching a known ping id that arrives after LINKCHECK_ATTEMPT_TIMEOUT_S
# but within this window is still reported, tagged `late=True`, rather than
# silently discarded — see `_handle_linkcheck_pong` below. Beyond this window a
# match is treated as too old to trust (id reuse across sessions is unlikely
# but not impossible over minutes) and is ignored like an unknown id.
LINKCHECK_LATE_WINDOW_S = 180.0

# Mirrors ctcping's _MAX_PING_REPEAT; each attempt is up to ~4 keyings under
# the operator's licence (ADR §1.4 point 7), so this cap is server-side and
# enforced by rejection, never silent clamping.
_MAX_LINKCHECK_ATTEMPTS = 5

_MAX_CONCURRENT_LINKCHECKS = 3

# Applied per target after a session ends naturally (COMPLETED/TIMEOUT/ERROR).
# An explicit `stop_link_check()` does NOT set this — a user-initiated stop
# must allow an immediate restart of the same target.
LINKCHECK_COOLDOWN_S = 60.0

# extudp_functions.cpp:266 silently drops any dst outside 1-9 characters.
_MAX_TARGET_CALLSIGN_LEN = 9

# How long an attempt resolved by a signal-less BLE pong keeps the driver
# waiting for the Extern-UDP "lora" copy of the same pong, which carries the
# RSSI/SNR. The two copies of one frame land ~40-170 ms apart (CLAUDE.md,
# Unread Cursors); a BLE-only box simply ends the attempt this much later.
LINKCHECK_SIGNAL_GRACE_S = 2.0


class _PongPath(StrEnum):
    """What a pong copy says about HOW the target's answer reached our node."""

    RF_SIGNAL = "rf_signal"  # Extern-UDP src_type "lora": on air, with RSSI/SNR
    RF = "rf"  # BLE, no server flag: on air, but no signal footer
    INTERNET = "internet"  # Extern-UDP src_type "udp", or BLE with msg_server


def _pong_path(frame: linkcheck.LinkCheckFrame) -> _PongPath | None:
    """Classify one pong copy; `None` for a copy that says nothing (our own
    node's `src_type:"node"` sentinel, or an unknown src_type)."""
    if frame.src_type == "lora":
        return _PongPath.RF_SIGNAL
    if frame.src_type == "udp":
        return _PongPath.INTERNET
    if frame.src_type == "ble":
        return _PongPath.INTERNET if frame.msg_server else _PongPath.RF
    return None


class LinkCheckStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class LinkCheckAttempt:
    seq: int
    sent_ms: int
    ping_id: int | None = None  # normalised unsigned-32, learned from the echo
    resolved: bool = False
    timed_out: bool = False
    rssi: int | None = None
    snr: float | None = None
    response_ms: int | None = None
    hops: int | None = None
    late: bool = False
    # Set when an internet-path pong (Extern-UDP src_type:"udp", or a BLE copy
    # carrying the server flag) matches this attempt's id — ADR §1.5.1. Never
    # itself resolves the attempt; a later RF copy still resolves it normally
    # and reports this as True.
    internet_reply: bool = False
    # "echo" when ping_id came from our node's Extern-UDP echo, "node_id" when
    # the pong was matched through our node's identity instead (no echo).
    correlation: str | None = None
    # Pending "wake the driver" timer while a signal-less BLE resolve waits
    # for the Extern-UDP copy (LINKCHECK_SIGNAL_GRACE_S).
    grace_handle: asyncio.TimerHandle | None = None


def _cancel_grace(attempt: LinkCheckAttempt) -> None:
    if attempt.grace_handle is not None:
        attempt.grace_handle.cancel()
        attempt.grace_handle = None


@dataclass
class LinkCheckSession:
    target: str
    requester: str
    total: int
    started_ms: int
    attempts: list[LinkCheckAttempt] = field(default_factory=list)
    status: LinkCheckStatus = LinkCheckStatus.RUNNING
    driver_task: asyncio.Task[None] | None = None


class LinkCheckMixin(CommandHandlerBase):
    """Mixin providing McApp-driven link check ({ping}/{pong}) sessions."""

    def _init_linkcheck(self) -> None:
        """Initialize link check state. Called from CommandHandler.__init__."""
        self.link_sessions: dict[str, LinkCheckSession] = {}
        self.linkcheck_timeout = LINKCHECK_ATTEMPT_TIMEOUT_S
        self._linkcheck_bg_tasks: set[asyncio.Task[Any]] = set()
        # time.monotonic() end-of-cooldown per target; never wall-clock, so an
        # NTP step can't shorten or extend a cooldown.
        self._linkcheck_cooldown_until: dict[str, float] = {}
        # One asyncio.Event per target with an in-flight attempt, so a pong
        # (or the timeout) can wake the driver loop immediately instead of
        # polling. Attempts within one session are strictly sequential, so at
        # most one entry per target exists at any time.
        self._linkcheck_wake: dict[str, asyncio.Event] = {}
        self.linkcheck_signal_grace = LINKCHECK_SIGNAL_GRACE_S
        # Our node's `_GW_ID & 0x3FFFFF`, i.e. the top 22 bits of every msg_id
        # it mints. Learned from the BLE `I` register or from any echo; None
        # until then, which disables the node-id correlation path entirely.
        self._linkcheck_node_prefix: int | None = None
        # Pong tokens already credited to an attempt, with their monotonic
        # expiry. The node-id path has no ping id to compare against, so this
        # is what stops a straggler copy of an OLD pong (a retransmitted ping
        # answered again, same token) from resolving a later attempt.
        self._linkcheck_claimed_tokens: dict[int, float] = {}

    # ── Public API ────────────────────────────────────────────────────────

    async def start_link_check(  # noqa: PLR0911 - validation ladder kept intact, mirrors ctcping.handle_ctcping
        self, target: str, count: int, requester: str
    ) -> tuple[bool, str]:
        """Validate and start a link check session against `target`.

        Returns `(ok, human_message)`. Rejects rather than clamps out-of-range
        input — this transmits under the operator's licence from an
        unauthenticated endpoint (ADR §4.2), so caps are enforced here, not
        merely suggested in a UI.
        """
        call = target.strip().upper() if isinstance(target, str) else ""

        if not call or not CALLSIGN_STRICT_RE.match(call):
            return False, "Invalid target callsign"

        if call == self.my_callsign:
            return False, (
                f"Cannot link-check {call}: the firmware refuses a DM to its own "
                "callsign, so a self-check can never work"
            )

        if hasattr(self, "blocked_callsigns") and call in self.blocked_callsigns:
            return False, f"Target {call} is blocked"

        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not (1 <= count <= _MAX_LINKCHECK_ATTEMPTS)
        ):
            return False, f"Attempt count must be between 1 and {_MAX_LINKCHECK_ATTEMPTS}"

        if call in self.link_sessions:
            return False, f"A link check to {call} is already running"

        cooldown_until = self._linkcheck_cooldown_until.get(call)
        if cooldown_until is not None and time.monotonic() < cooldown_until:
            remaining = cooldown_until - time.monotonic()
            return False, f"{call} is in cooldown, try again in {remaining:.0f}s"

        if len(self.link_sessions) >= _MAX_CONCURRENT_LINKCHECKS:
            return False, "Too many concurrent link checks running"

        # CALLSIGN_STRICT_RE already bounds every match to 3-9 chars, so this
        # is a defensive belt-and-braces check against _MAX_TARGET_CALLSIGN_LEN.
        if not (1 <= len(call) <= _MAX_TARGET_CALLSIGN_LEN):
            return False, "Target callsign length is not valid for the firmware"

        # No `await` between the checks above and the insert below — two
        # concurrent requests for the same target, or two racing the
        # concurrency cap, must not both pass.
        session = LinkCheckSession(
            target=call,
            requester=requester,
            total=count,
            started_ms=now_ms(),
        )
        self.link_sessions[call] = session
        driver_task = asyncio.create_task(self._run_link_check_session(call))
        session.driver_task = driver_task
        self._linkcheck_bg_tasks.add(driver_task)
        driver_task.add_done_callback(self._linkcheck_bg_tasks.discard)

        return True, f"Link check to {call} started: {count} attempt(s)"

    async def stop_link_check(self, target: str) -> bool:
        """Cancel a running session. Returns False if none is running.

        Awaits full task cancellation before returning, so the target key is
        never released while a stale timeout could still fire into a freshly
        restarted session.
        """
        call = target.strip().upper() if isinstance(target, str) else target
        session = self.link_sessions.get(call)
        if session is None:
            return False

        session.status = LinkCheckStatus.STOPPED
        task = session.driver_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # The driver task's own `finally` releases the key; this is a
        # defensive no-op in the normal case and a safety net if a session
        # somehow had no attached task.
        self.link_sessions.pop(call, None)
        return True

    def linkcheck_snapshot(self) -> list[dict[str, Any]]:
        """JSON-safe view of every running (or just-ended) session."""
        snapshot: list[dict[str, Any]] = []
        for target, session in self.link_sessions.items():
            snapshot.append(
                {
                    "target": target,
                    "requester": session.requester,
                    "total": session.total,
                    "started_ms": session.started_ms,
                    "status": session.status.value,
                    "attempts": [
                        {
                            "seq": attempt.seq,
                            "sent_ms": attempt.sent_ms,
                            "ping_id": attempt.ping_id,
                            "resolved": attempt.resolved,
                            "timed_out": attempt.timed_out,
                            "rssi": attempt.rssi,
                            "snr": attempt.snr,
                            "response_ms": attempt.response_ms,
                            "hops": attempt.hops,
                            "late": attempt.late,
                            "internet_reply": attempt.internet_reply,
                        }
                        for attempt in session.attempts
                    ],
                }
            )
        return snapshot

    async def handle_link_check_frame(self, message_data: dict[str, Any]) -> None:
        """Inbound hook: feed an Extern-UDP frame that may be a ping echo or pong.

        Never raises into the caller — this runs on the inbound message path
        (port 1799 is unauthenticated and attacker-shaped).
        """
        try:
            frame = linkcheck.parse(message_data)
            if frame is None:
                return

            if frame.kind is linkcheck.LinkCheckKind.PING and frame.src_type == "node":
                self._handle_linkcheck_echo(frame)
            elif frame.kind is linkcheck.LinkCheckKind.PONG:
                await self._handle_linkcheck_pong(frame)
        except Exception:
            logger.exception("Error handling link check frame")

    def note_linkcheck_node_register(self, register: dict[str, Any]) -> None:
        """Learn our node's msg_id prefix from its BLE `I` register (`ID` is
        the firmware's `_GW_ID`). Sent on every BLE connect; a non-int `ID`
        leaves what we already know untouched."""
        prefix = linkcheck.node_prefix_of_gw_id(register.get("ID"))
        if prefix is not None:
            self._linkcheck_node_prefix = prefix

    async def stop_linkcheck(self) -> None:
        """Shutdown: cancel every in-flight session and clear all state."""
        tasks = [s.driver_task for s in self.link_sessions.values() if s.driver_task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Defensive: pick up anything not reachable via link_sessions (there
        # should be none, given every create_task site above is tracked).
        stray = [t for t in self._linkcheck_bg_tasks if not t.done()]
        for task in stray:
            task.cancel()
        if stray:
            await asyncio.gather(*stray, return_exceptions=True)

        self.link_sessions.clear()
        self._linkcheck_wake.clear()
        self._linkcheck_cooldown_until.clear()

    # ── Internal: correlation ────────────────────────────────────────────

    def _handle_linkcheck_echo(self, frame: linkcheck.LinkCheckFrame) -> None:
        """A `src_type:"node"` echo of our own outgoing ping: learn its msg_id.

        Attempts within a session are strictly sequential (ATTEMPTS ARE
        SEQUENTIAL, not on a fixed interval — see `_run_link_check_session`),
        so the most recent attempt is always the one still missing a
        `ping_id`; no need to scan the whole list. The echo is our own node's
        frame, so its id also names our node (`node_prefix_of_msg_id`).
        """
        echo_id = linkcheck.normalise_id(frame.msg_id)
        if echo_id is not None:
            self._linkcheck_node_prefix = linkcheck.node_prefix_of_msg_id(echo_id)
        session = self.link_sessions.get(frame.dst.strip().upper())
        if session is None or not session.attempts:
            return
        attempt = session.attempts[-1]
        if attempt.ping_id is None:
            attempt.ping_id = echo_id

    def _match_linkcheck_attempt(
        self, frame: linkcheck.LinkCheckFrame, token: int
    ) -> tuple[str, LinkCheckAttempt, str] | None:
        """Find the attempt a pong answers: `(target, attempt, correlation)`.

        1. `"echo"` — an attempt whose ping id (learned from the Extern-UDP
           echo, or pinned by an earlier node-id match) equals the token.
           Exact, and the only path that can tell two of our pings apart.
        2. `"node_id"` — no attempt knows the token, but it was minted by OUR
           node (top 22 bits == our `_GW_ID`), the pong comes from the station
           this session is pinging, and that session's current attempt never
           learned its id. This is the no-echo case (Extern-UDP pointed
           elsewhere). It proves "the target answered a ping from our node",
           not "answered THIS ping": a ping another client sent through the
           same node, or a late answer to an earlier timed-out attempt, would
           match too. Tokens already credited are refused
           (`_linkcheck_claimed_tokens`), so one pong never counts twice.
        """
        late_window_ms = int(LINKCHECK_LATE_WINDOW_S * 1000)
        for target, session in self.link_sessions.items():
            for attempt in session.attempts:
                if attempt.ping_id != token:
                    continue
                # Too old to trust as this attempt's reply; keep scanning in
                # case of a genuine (if unlikely) id collision.
                if now_ms() - attempt.sent_ms > late_window_ms:
                    continue
                return target, attempt, "echo"

        prefix = self._linkcheck_node_prefix
        if prefix is None or linkcheck.node_prefix_of_msg_id(token) != prefix:
            return None
        self._expire_linkcheck_tokens()
        if token in self._linkcheck_claimed_tokens:
            return None
        target = frame.origin.strip().upper()
        waiting = self.link_sessions.get(target)
        if waiting is None or not waiting.attempts:
            return None
        current = waiting.attempts[-1]
        if current.ping_id is not None or current.resolved:
            return None
        return target, current, "node_id"

    def _expire_linkcheck_tokens(self) -> None:
        now = time.monotonic()
        for token in [t for t, until in self._linkcheck_claimed_tokens.items() if until <= now]:
            del self._linkcheck_claimed_tokens[token]

    def _wake_linkcheck_driver(self, target: str, attempt: LinkCheckAttempt, delay: float) -> None:
        """Wake the driver waiting on `attempt` now (`delay <= 0`) or after
        `delay` seconds; a later call replaces a pending delayed wake."""
        _cancel_grace(attempt)
        wake = self._linkcheck_wake.get(target)
        if wake is None:
            return
        if delay <= 0:
            wake.set()
        else:
            attempt.grace_handle = asyncio.get_running_loop().call_later(delay, wake.set)

    def _linkcheck_send_topic(self) -> str:
        """BLE when a BLE client is connected, else Extern-UDP.

        BLE works in both node configurations: `sendMessage()` still echoes
        the ping to Extern-UDP when the node's EXT IP points here, so the
        exact echo correlation is kept. Extern-UDP only works while the node
        has EXTUDP enabled — with it off the node never reads the socket
        (`esp32_main.cpp`: `if(bEXTUDP) getExternUDP();`) and the ping is
        silently never transmitted, while the modal still says "ping sent".
        """
        router = self.message_router
        get_protocol = getattr(router, "get_protocol", None)
        client = get_protocol("ble_client") if callable(get_protocol) else None
        if client is None:
            return "udp_message"
        try:
            value = client.is_connected
            connected = bool(value() if callable(value) else value)
        except Exception:
            connected = False
        return "ble_message" if connected else "udp_message"

    async def _emit_linkcheck_result(self, target: str, attempt: LinkCheckAttempt) -> None:
        await self._emit_linkcheck_event(
            "linkcheck_result",
            target=target,
            seq=attempt.seq,
            response_ms=attempt.response_ms,
            rssi=attempt.rssi,
            snr=attempt.snr,
            hops=attempt.hops,
            late=attempt.late,
            internet_reply=attempt.internet_reply,
        )

    async def _handle_linkcheck_pong(self, frame: linkcheck.LinkCheckFrame) -> None:
        """Credit a pong copy to the attempt it answers, from either transport."""
        token = frame.correlates_to
        if token is None:
            return
        match = self._match_linkcheck_attempt(frame, token)
        if match is None:
            # No attempt anywhere answers this id: unknown/foreign pong, ignored.
            return
        target, attempt, correlation = match

        path = _pong_path(frame)
        if path is None:
            logger.debug(
                "Ignoring pong with src_type=%r for %s seq=%d",
                frame.src_type,
                target,
                attempt.seq,
            )
            return
        if path is _PongPath.INTERNET:
            # The target is alive, but this copy is no evidence of an RF path
            # (ADR §1.5.1): record it and keep waiting for an RF copy.
            attempt.internet_reply = True
            logger.debug(
                "Internet-path pong for %s seq=%d, still waiting for an RF copy",
                target,
                attempt.seq,
            )
            return

        if attempt.resolved:
            # A second RF copy. The one worth having is the Extern-UDP copy
            # adding the RSSI/SNR a BLE copy could not carry.
            if path is _PongPath.RF_SIGNAL and attempt.rssi is None and attempt.snr is None:
                attempt.rssi = frame.rssi
                attempt.snr = frame.snr
                attempt.hops = frame.hops
                await self._emit_linkcheck_result(target, attempt)
                self._wake_linkcheck_driver(target, attempt, 0)
            else:
                logger.debug(
                    "Duplicate link check pong for %s seq=%d, ignoring", target, attempt.seq
                )
            return

        response_ms = now_ms() - attempt.sent_ms
        attempt.resolved = True
        attempt.correlation = correlation
        if attempt.ping_id is None:
            # Pin the token: later copies of this pong now match exactly
            # ("echo" path) and enrich or dedupe instead of re-matching.
            attempt.ping_id = token
        self._linkcheck_claimed_tokens[token] = time.monotonic() + LINKCHECK_LATE_WINDOW_S
        attempt.response_ms = response_ms
        # Real signal only on the Extern-UDP "lora" copy; a BLE text frame
        # carries no RSSI/SNR footer at all.
        attempt.rssi = frame.rssi if path is _PongPath.RF_SIGNAL else None
        attempt.snr = frame.snr if path is _PongPath.RF_SIGNAL else None
        attempt.hops = frame.hops
        attempt.late = attempt.timed_out or response_ms > int(self.linkcheck_timeout * 1000)
        if correlation == "node_id":
            logger.info(
                "Link check pong for %s seq=%d matched by node id (no Extern-UDP echo seen)",
                target,
                attempt.seq,
            )

        await self._emit_linkcheck_result(target, attempt)
        grace = 0.0 if path is _PongPath.RF_SIGNAL else self.linkcheck_signal_grace
        self._wake_linkcheck_driver(target, attempt, grace)

    # ── Internal: driver ─────────────────────────────────────────────────

    async def _run_link_check_session(  # noqa: PLR0915 - sequential driver loop kept intact for clarity
        self, target: str
    ) -> None:
        """Drive one session's attempts sequentially: send, wait, repeat.

        Sequential by design — overlapping attempts would collide with the
        firmware's own 40 s retransmissions of the previous ping (ADR §1.4
        point 7).
        """
        session = self.link_sessions[target]
        sent = 0
        received = 0
        try:
            for seq in range(1, session.total + 1):
                if session.status != LinkCheckStatus.RUNNING:
                    break

                attempt = LinkCheckAttempt(seq=seq, sent_ms=now_ms())
                session.attempts.append(attempt)

                try:
                    await self.message_router.publish(
                        "linkcheck",
                        self._linkcheck_send_topic(),
                        {
                            "dst": target,
                            "msg": linkcheck.PING_PAYLOAD,
                            "src_type": "linkcheck",
                            "type": "msg",
                        },
                    )
                except OSError:
                    # udp_handler.send_message() deliberately propagates
                    # OSError; an uncaught one here would kill this task and
                    # strand `target` in link_sessions forever.
                    logger.exception("Link check send to %s failed", target)
                    session.status = LinkCheckStatus.ERROR
                    break

                sent += 1
                await self._emit_linkcheck_event("linkcheck_sent", target=target, seq=seq)

                wake = asyncio.Event()
                self._linkcheck_wake[target] = wake
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self.linkcheck_timeout)
                except TimeoutError:
                    pass
                finally:
                    self._linkcheck_wake.pop(target, None)
                    _cancel_grace(attempt)

                if attempt.resolved:
                    received += 1
                else:
                    attempt.timed_out = True
                    await self._emit_linkcheck_event(
                        "linkcheck_timeout",
                        target=target,
                        seq=seq,
                        internet_reply=attempt.internet_reply,
                    )

            if session.status == LinkCheckStatus.RUNNING:
                session.status = (
                    LinkCheckStatus.COMPLETED if received > 0 else LinkCheckStatus.TIMEOUT
                )

            await self._emit_linkcheck_event(
                "linkcheck_done",
                target=target,
                sent=sent,
                received=received,
                status=session.status.value,
            )
        except asyncio.CancelledError:
            session.status = LinkCheckStatus.STOPPED
            with contextlib.suppress(Exception):
                await self._emit_linkcheck_event(
                    "linkcheck_done",
                    target=target,
                    sent=sent,
                    received=received,
                    status=session.status.value,
                )
            raise
        except Exception:
            logger.exception("Link check session for %s crashed", target)
            session.status = LinkCheckStatus.ERROR
            with contextlib.suppress(Exception):
                await self._emit_linkcheck_event(
                    "linkcheck_done",
                    target=target,
                    sent=sent,
                    received=received,
                    status=session.status.value,
                )
        finally:
            self.link_sessions.pop(target, None)
            self._linkcheck_wake.pop(target, None)
            # A user-initiated stop allows an immediate restart; only a
            # natural end (completed/timeout/error) starts the cooldown.
            if session.status != LinkCheckStatus.STOPPED:
                self._linkcheck_cooldown_until[target] = time.monotonic() + LINKCHECK_COOLDOWN_S

    async def _emit_linkcheck_event(self, event: str, **payload: Any) -> None:
        try:
            await self.message_router.publish(
                "linkcheck", "linkcheck_event", {"event": event, **payload}
            )
        except Exception:
            logger.exception("Failed to emit link check event %s", event)
