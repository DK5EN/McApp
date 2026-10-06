"""Node Admin service: RM1 (HMAC remote admin) send path, reply correlation, state.

Plan: doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md (§1 D1-D5, §3.4, §4-6).
Implements the `NodeAdminService` Protocol of `node_admin_types.py`. Pure core
is `remote_cmd`, persistence is the `NodeAdminMixin` of `storage/node_admin.py`;
this module owns the rules in between:

  * ONE command in flight per target, >= `RM_RATE_MS` after the last frame
    (command, sync or reply), and no sends while a lockout is likely (D3).
  * The counter and the tag exist only at allocation time, in one storage
    transaction; every refusal happens BEFORE it, so a refusal burns no value.
  * `send()` returns after the row insert. The transmit, the optional
    auto-sync wait and all reply processing run as tracked background tasks.
  * A reply is acted on (hwm, sync gate, broadcast) ONLY when the storage
    UPDATE reports a change, because the BLE and the UDP copy arrive about
    100 ms apart in independent tasks. `verified` is terminal.
  * State is computed here from timestamps (`compute_state`), never stored.

Never logged: passwords, keys, tags. A command may appear as `RM1 <ctr> <cmd>`.
All times are injected (`clock_ms`, `unix_s`, `sleep`) so tests drive them.
Units: `now_ms` / `*_at` are milliseconds, `unix_s` is seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from . import remote_cmd, util
from .commands.parsing import resolve_dst_target, strip_relay_path
from .node_admin_types import (
    NodeAdminBusyError,
    NodeAdminError,
    NodeAdminUnavailableError,
    TransmitFn,
)
from .secret_box import SecretBox, SecretBoxError

if TYPE_CHECKING:
    from .sqlite_storage import SQLiteStorage

logger = logging.getLogger(__name__)

SYNC_TIMEOUT_MS: Final = 90_000  # no verified sync reply by then: send the command anyway
LOCKOUT_WINDOW_MS: Final = 300_000  # firmware lockout is 5 min
LOCKOUT_SILENT_ROWS: Final = 2  # silent outcomes inside the window that mean "likely locked out"
UNIX_FLOOR_MIN_S: Final = 1_704_067_200  # 2024-01-01: firmware `clockUnix` plausibility bound
TX_MAX_LIMIT: Final = 30
REPLY_PREFIX: Final = "RM1 "
REPLY_MAX_CHARS: Final = 200
HISTORY_SCAN: Final = 100  # rows scanned to find the newest row of a target
HISTORY_LIMIT_MAX: Final = 1000

TRANSPORTS: Final = frozenset({"auto", "ble", "udp"})

STATES: Final = (
    "queued",
    "send_failed",
    "waiting",
    "no_reply",
    "verified",
    "bad_tag",
    "abandoned",
)

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]


def compute_state(row: dict[str, Any], now_ms: int) -> str:
    """Server-side state of one log row; never stored.

    `verified` wins over everything (a verified reply proves the frame arrived,
    even when the transport reported a timeout); `bad_tag` keeps the raw text
    as evidence but is not final; `waiting` becomes `no_reply` after
    `RM_REPLY_TIMEOUT_MS` and a late verified reply still flips it.
    """
    handed_off = row.get("handed_off_at")
    # A row whose hand-off bookkeeping never landed must not stay queued (and
    # block its target) for the process lifetime: it ages from `sent_at`.
    anchor = handed_off if handed_off is not None else row.get("sent_at")
    state = "queued"
    if anchor is not None and now_ms - int(anchor) >= remote_cmd.RM_REPLY_TIMEOUT_MS:
        state = "no_reply"
    elif handed_off is not None:
        state = "waiting"
    if row.get("result") == "abandoned" and not row.get("reply_text"):
        state = "abandoned"
    if row.get("verified") == 0 and row.get("reply_text"):
        state = "bad_tag"
    if row.get("send_error"):
        state = "send_failed"
    if row.get("verified") == 1:
        state = "verified"
    return state


def _aad(target: str) -> str:
    return f"node_admin.password:{target}"


def _last_frame_ms(rows: list[dict[str, Any]]) -> int | None:
    """Newest moment the node can have seen a frame from or to this target.

    A hand-off that the transport refused never reached the air, so it does
    not count; a reply (even a late one to an older row) is the node's own
    frame and always does.
    """
    best: int | None = None
    for r in rows:
        if r.get("reply_at") is not None:
            best = max(best or 0, int(r["reply_at"]))
        if r.get("handed_off_at") is not None and not r.get("send_error"):
            best = max(best or 0, int(r["handed_off_at"]))
    return best


def _lockout_until_ms(rows: list[dict[str, Any]], now_ms: int) -> int | None:
    """End of a likely node lockout, or None.

    The node counts silent rejects and locks RM for 5 min. Two rows of this
    target that ended `no_reply` with hand-offs less than `LOCKOUT_WINDOW_MS`
    apart may both have been counted: stay silent until 5 min after the later one.
    """
    silent = sorted(
        (
            int(r["handed_off_at"])
            for r in rows
            if compute_state(r, now_ms) == "no_reply" and r.get("handed_off_at") is not None
        ),
        reverse=True,
    )
    if len(silent) < LOCKOUT_SILENT_ROWS:
        return None
    later, earlier = silent[0], silent[1]
    if later - earlier >= LOCKOUT_WINDOW_MS or now_ms >= later + LOCKOUT_WINDOW_MS:
        return None
    return later + LOCKOUT_WINDOW_MS


@dataclass
class _Pending:
    """A command held back behind its target's auto-sync."""

    sync_log_id: int
    cmd: str
    args: str
    requested_transport: str
    released: asyncio.Event = field(default_factory=asyncio.Event)
    reply_at: int = 0


class NodeAdminService:
    """Satisfies the `node_admin_types.NodeAdminService` Protocol (see module docstring)."""

    def __init__(  # noqa: PLR0913, PLR0917 - constructor pinned by the Node Admin plan; main.py is wired against it
        self,
        storage: SQLiteStorage,
        secret_box: SecretBox,
        transmit: TransmitFn,
        attached_call: Callable[[], str | None],
        ble_connected: Callable[[], bool],
        broadcast: Broadcast,
        *,
        clock_ms: Callable[[], int] = util.now_ms,
        unix_s: Callable[[], int] = lambda: int(time.time()),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._storage = storage
        self._box = secret_box
        self._transmit = transmit
        self._attached_call = attached_call
        self._ble_connected = ble_connected
        self._broadcast = broadcast
        self._clock = clock_ms
        self._unix_s = unix_s
        self._sleep = sleep
        self._keys: dict[str, bytes] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._synced: set[str] = set()
        # Rows already asked again once (the node caches ONE reply, a second re-ask is a
        # replay strike). In memory: a row left open at restart is abandoned and cannot
        # be re-asked anyway.
        self._reasked: set[int] = set()
        self._pending: dict[str, _Pending] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.rejected_replies = 0

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Sweep rows a previous process left open, then cap the log.

        Housekeeping only, and it runs on EVERY box now that the feature is always
        on: it must never block startup (a locked DB or an I/O error here would
        otherwise stop mcapp from coming up). An unswept row ages to `no_reply`
        through `compute_state` anyway, so a failure is logged and skipped.
        """
        try:
            swept = await self._storage.abandon_stale_node_admin_rows(self._clock())
            pruned = await self._storage.prune_node_admin_log()
        except Exception:
            logger.warning("node admin start: housekeeping failed, skipped", exc_info=True)
            return
        if swept or pruned:
            logger.info("node admin start: %d stale rows abandoned, %d pruned", swept, pruned)

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._pending.clear()

    async def drain(self) -> None:
        """Await every in-flight background task (tests). A task parked on an
        injected sleep only finishes once the test lets that sleep complete."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── keys ───────────────────────────────────────────────────────────────

    def _lock(self, target: str) -> asyncio.Lock:
        lock = self._locks.get(target)
        if lock is None:
            lock = self._locks[target] = asyncio.Lock()
        return lock

    async def _get_key(self, target: str) -> bytes:
        """HMAC key of `target`: ValueError without a stored key,
        NodeAdminUnavailableError when the ciphertext cannot be read."""
        cached = self._keys.get(target)
        if cached is not None:
            return cached
        token = await self._storage.get_node_admin_key(target)
        if token is None:
            msg = f"no key stored for {target}"
            raise ValueError(msg)
        try:
            password = await asyncio.to_thread(self._box.decrypt, token, _aad(target))
            key = remote_cmd.derive_key(password)
        except (SecretBoxError, remote_cmd.RmError) as exc:
            logger.warning("node admin key for %s is unreadable (%s)", target, type(exc).__name__)
            msg = f"key for {target} is unreadable; re-enter the password"
            raise NodeAdminUnavailableError(msg) from exc
        self._keys[target] = key
        return key

    async def set_key(self, target: str, password: str, tx_max: int | None) -> None:
        t = remote_cmd.normalize_call(target)
        remote_cmd.validate_password(password)
        if tx_max is not None and (
            isinstance(tx_max, bool)
            or not isinstance(tx_max, int)
            or not 0 <= tx_max <= TX_MAX_LIMIT
        ):
            msg = f"tx_max must be an integer 0..{TX_MAX_LIMIT}"
            raise ValueError(msg)
        token = await asyncio.to_thread(self._box.encrypt, password, _aad(t))
        self._keys.pop(t, None)
        await self._storage.set_node_admin_key(t, token, tx_max)
        self._keys.pop(t, None)
        # A new password invalidates the earlier sync: the next command syncs again.
        self._synced.discard(t)
        logger.info("node admin key stored for %s", t)

    async def delete_key(self, target: str) -> None:
        t = remote_cmd.normalize_call(target)
        self._keys.pop(t, None)
        await self._storage.delete_node_admin_key(t)
        self._keys.pop(t, None)
        logger.info("node admin key removed for %s (counter state kept)", t)

    async def list_targets(self) -> list[dict[str, Any]]:
        now = self._clock()
        out: list[dict[str, Any]] = []
        for item in await self._storage.list_node_admin_targets():
            target = str(item["target"])
            unreadable = False
            if item["has_key"]:
                try:
                    await self._get_key(target)
                except (NodeAdminUnavailableError, ValueError):
                    unreadable = True
            rows = await self._storage.node_admin_history(target, HISTORY_SCAN)
            out.append(
                {
                    "target": target,
                    "has_key": bool(item["has_key"]),
                    "key_unreadable": unreadable,
                    "ctr": item["ctr"],
                    "last_hwm": item["last_hwm"],
                    "last_sync_at": item["last_sync_at"],
                    "tx_max": item["tx_max"],
                    "possible_lockout_until_ms": _lockout_until_ms(rows, now),
                }
            )
        return out

    # ── preparation shared by send / sync ──────────────────────────────────

    def _resolve_transport(self, requested: str) -> str:
        if requested not in TRANSPORTS:
            msg = f"transport must be one of {sorted(TRANSPORTS)}"
            raise ValueError(msg)
        connected = self._ble_connected()
        if requested == "ble" and not connected:
            msg = "BLE is not connected"
            raise NodeAdminUnavailableError(msg)
        if requested == "auto":
            return "ble" if connected else "udp"
        return requested

    def _src_call(self, target: str) -> str:
        """The attached node's call as the node holds it; refusal otherwise."""
        raw = self._attached_call()
        if not raw:
            msg = "the attached node's callsign is unknown"
            raise NodeAdminUnavailableError(msg)
        try:
            src = remote_cmd.normalize_call(raw)
        except ValueError as exc:
            msg = "the attached node's callsign is not usable for RM1"
            raise NodeAdminUnavailableError(msg) from exc
        if src == target:
            # The firmware refuses a DM to its own callsign.
            msg = "the target is the attached node itself"
            raise ValueError(msg)
        return src

    async def _tx_max(self, target: str) -> int:
        for item in await self._storage.list_node_admin_targets():
            if item["target"] == target:
                return int(item["tx_max"])
        return remote_cmd.TX_MAX_DEFAULT

    async def _node_tx_max(self, target: str) -> int | None:
        """The board maximum from the newest verified `status` reply carrying `p=`, or None."""
        for r in await self._storage.node_admin_verified_status_rows(target):
            board_max = remote_cmd.status_tx_power_max(str(r.get("result") or ""))
            if board_max is not None:
                return board_max
        return None

    async def _check_txpower(self, target: str, cmd: str, args: str) -> None:
        """Refuse `txpower` above the node's own maximum (a counted, silent reject there).

        The key's `tx_max` is only the operator's ceiling; the node bounds by its board's
        `TX_POWER_MAX`, which its `status` reply reports as `p=<cur>/<max>`. Unknown
        maximum: the key's `tx_max` alone, as before.
        """
        if cmd != "txpower" or not args.isdigit():
            return
        board_max = await self._node_tx_max(target)
        if board_max is not None and int(args) > board_max:
            msg = (
                f"txpower {int(args)} is above this node's maximum of {board_max} "
                "(from its last status reply)"
            )
            raise remote_cmd.RmError(msg)

    async def _check_busy(self, target: str, now: int) -> list[dict[str, Any]]:
        """Raise NodeAdminBusyError per D3; returns the scanned rows."""
        if target in self._pending:
            msg = "waiting for the automatic sync of this target"
            raise NodeAdminBusyError(msg)
        rows = await self._storage.node_admin_history(target, HISTORY_SCAN)
        for r in rows:
            if compute_state(r, now) in {"queued", "waiting"}:
                msg = f"a command to {target} is still in flight"
                raise NodeAdminBusyError(msg)
        last = _last_frame_ms(rows)
        if last is not None and now - last < remote_cmd.RM_RATE_MS:
            msg = f"the node accepts one frame per {remote_cmd.RM_RATE_MS // 1000} s"
            raise NodeAdminBusyError(msg)
        until = _lockout_until_ms(rows, now)
        if until is not None:
            msg = f"node may be locked out until {until}; no frames until then"
            raise NodeAdminBusyError(msg)
        return rows

    def _unix_floor(self) -> int:
        u = self._unix_s()
        return u if u >= UNIX_FLOOR_MIN_S else 0

    # ── send ───────────────────────────────────────────────────────────────

    async def send(self, target: str, cmd: str, args: str, transport: str) -> dict[str, Any]:
        t = remote_cmd.normalize_call(target)
        args = args or ""
        if cmd == "sync":
            if args:
                msg = "sync takes no arguments"
                raise ValueError(msg)
            return await self._sync(t, transport)
        key = await self._get_key(t)
        tx_max = await self._tx_max(t)
        remote_cmd.validate_command(cmd, args, tx_max)
        await self._check_txpower(t, cmd, args)
        src_call = self._src_call(t)
        resolved = self._resolve_transport(transport)
        async with self._lock(t):
            now = self._clock()
            await self._check_busy(t, now)
            if t not in self._synced:
                return await self._start_auto_sync(
                    t, key, src_call, resolved, now, tx_max, cmd, args, transport
                )
            return await self._allocate_and_transmit(t, key, src_call, resolved, tx_max, cmd, args)

    async def _allocate_and_transmit(  # noqa: PLR0913, PLR0917 - internal helper, one call per field
        self,
        target: str,
        key: bytes,
        src_call: str,
        transport: str,
        tx_max: int,
        cmd: str,
        args: str,
        warning: str | None = None,
    ) -> dict[str, Any]:
        """Allocate the counter + log row (one transaction), then hand off in the background."""
        # The single choke point of the direct and the held-behind-sync path: every
        # refusal belongs BEFORE the allocation, which burns a counter value.
        await self._check_txpower(target, cmd, args)
        now = self._clock()

        def build_text(ctr: int) -> str:
            return remote_cmd.build_command_text(key, target, src_call, ctr, cmd, args, tx_max)

        row = await self._storage.allocate_node_admin_command(
            target, src_call, cmd, args or None, transport, now, self._unix_floor(), build_text
        )
        logger.info("node admin %s: RM1 %d %s via %s", target, row["ctr"], cmd, transport)
        self._spawn(self._transmit_row(target, row["id"], transport, row["text"], warning))
        return {"log_id": row["id"], "ctr": row["ctr"], "text": row["text"]}

    async def _transmit_row(
        self, target: str, row_id: int, transport: str, text: str, warning: str | None = None
    ) -> None:
        """Background: transmit, stamp the hand-off (with the failure reason), broadcast."""
        try:
            try:
                result = await self._transmit(transport, target, text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("node admin transmit to %s raised", target)
                result = "transmit failed"
            await self._storage.mark_node_admin_handed_off(row_id, self._clock(), send_error=result)
            await self._broadcast_row(target, row_id, warning)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("node admin hand-off bookkeeping for %s failed", target)

    async def _broadcast_row(self, target: str, row_id: int, warning: str | None = None) -> None:
        """Emit `node_admin:reply` with the full row and its computed state."""
        row = await self._storage.get_node_admin_log_row(row_id)
        if row is None or row["target_call"] != target:
            return
        payload = {**row, "state": compute_state(row, self._clock())}
        if row.get("send_error"):
            # A frame that may never have gone on air is a valid, replayable
            # signed command (ctr > hwm); /events is not behind the LAN guard.
            payload["text"] = str(row["text"]).rsplit(" ", 1)[0]
        if warning:
            payload["warning"] = warning
        try:
            await self._broadcast("node_admin:reply", payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("node_admin:reply broadcast failed")

    # ── sync / auto-sync ───────────────────────────────────────────────────

    async def sync(self, target: str) -> dict[str, Any]:
        return await self._sync(remote_cmd.normalize_call(target), "auto")

    async def _sync(self, target: str, transport: str) -> dict[str, Any]:
        key = await self._get_key(target)
        tx_max = await self._tx_max(target)
        src_call = self._src_call(target)
        resolved = self._resolve_transport(transport)
        async with self._lock(target):
            now = self._clock()
            await self._check_busy(target, now)
            row_id, text = await self._insert_sync_row(target, key, src_call, resolved, now, tx_max)
        return {"log_id": row_id, "ctr": 0, "text": text}

    async def _insert_sync_row(  # noqa: PLR0913, PLR0917 - internal helper, one call per field
        self, target: str, key: bytes, src_call: str, transport: str, now: int, tx_max: int
    ) -> tuple[int, str]:
        text = remote_cmd.build_command_text(key, target, src_call, 0, "sync", "", tx_max)
        row_id = await self._storage.insert_node_admin_sync_row(
            target, src_call, transport, now, text
        )
        logger.info("node admin %s: RM1 0 sync via %s", target, transport)
        self._spawn(self._transmit_row(target, row_id, transport, text))
        return row_id, text

    async def _start_auto_sync(  # noqa: PLR0913, PLR0917 - internal helper, one call per field
        self,
        target: str,
        key: bytes,
        src_call: str,
        resolved: str,
        now: int,
        tx_max: int,
        cmd: str,
        args: str,
        requested_transport: str,
    ) -> dict[str, Any]:
        """First command to a target in this process: sync first, command behind it."""
        row_id, text = await self._insert_sync_row(target, key, src_call, resolved, now, tx_max)
        pend = _Pending(row_id, cmd, args, requested_transport)
        self._pending[target] = pend
        self._spawn(self._run_gate(target, pend))
        return {"log_id": row_id, "ctr": 0, "text": text, "pending": True}

    async def _wait_released(self, pend: _Pending) -> bool:
        """True when the verified sync reply arrived, False after `SYNC_TIMEOUT_MS`."""
        waiter = asyncio.ensure_future(pend.released.wait())
        timer = asyncio.ensure_future(self._sleep(SYNC_TIMEOUT_MS / 1000))
        try:
            await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
            return pend.released.is_set()
        finally:
            for f in (waiter, timer):
                f.cancel()
            await asyncio.gather(waiter, timer, return_exceptions=True)

    async def _run_gate(self, target: str, pend: _Pending) -> None:
        warning: str | None = None
        try:
            if await self._wait_released(pend):
                wait_s = (pend.reply_at + remote_cmd.RM_RATE_MS - self._clock()) / 1000
                if wait_s > 0:
                    await self._sleep(wait_s)
            else:
                warning = "no sync reply"
                self._synced.add(target)
                logger.warning(
                    "node admin %s: no sync reply in %d s, sending anyway",
                    target,
                    SYNC_TIMEOUT_MS // 1000,
                )
            await self._send_pending(target, pend, warning)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("node admin pending command for %s dropped", target)
            reason = (
                str(exc)[:80]
                if isinstance(exc, ValueError | NodeAdminError)
                else type(exc).__name__
            )
            await self._broadcast_row(target, pend.sync_log_id, f"command dropped: {reason}")
        finally:
            if self._pending.get(target) is pend:
                del self._pending[target]

    async def _send_pending(self, target: str, pend: _Pending, warning: str | None) -> None:
        async with self._lock(target):
            key = await self._get_key(target)
            tx_max = await self._tx_max(target)
            src_call = self._src_call(target)
            transport = self._resolve_transport(pend.requested_transport)
            # No Busy checks here: the user passed them at POST time and the
            # pending entry kept every other send out meanwhile.
            await self._allocate_and_transmit(
                target, key, src_call, transport, tx_max, pend.cmd, pend.args, warning
            )

    # ── re-ask ─────────────────────────────────────────────────────────────

    async def reask(self, log_id: int) -> dict[str, Any]:
        row = await self._storage.get_node_admin_log_row(log_id)
        if row is None:
            msg = f"unknown log id {log_id}"
            raise ValueError(msg)
        target = str(row["target_call"])
        async with self._lock(target):
            now = self._clock()
            rows = await self._storage.node_admin_history(target, HISTORY_SCAN)
            row = next((r for r in rows if r["id"] == log_id), row)
            self._check_reask(target, row, rows, now)
            transport = str(row["transport"] or "udp")
            if transport == "ble" and not self._ble_connected():
                msg = "BLE is not connected"
                raise NodeAdminUnavailableError(msg)
            # Restart the 120 s window and the 10 s spacing NOW, so a second
            # click cannot slip in while the transmit is still running. The row's
            # first hand-off was just checked against the node's 10 min cache and
            # this is its one re-ask, so the restamp cannot extend that window.
            self._reasked.add(log_id)
            await self._storage.mark_node_admin_handed_off(log_id, now)
            logger.info("node admin %s: re-ask RM1 %s %s", target, row["ctr"], row["cmd"])
            self._spawn(self._transmit_row(target, log_id, transport, str(row["text"])))
        return {"log_id": log_id}

    def _check_reask(
        self, target: str, row: dict[str, Any], rows: list[dict[str, Any]], now: int
    ) -> None:
        if target in self._pending:
            msg = "waiting for the automatic sync of this target"
            raise NodeAdminBusyError(msg)
        if not rows or rows[0]["id"] != row["id"]:
            msg = "only the newest row of a target can be asked again"
            raise NodeAdminBusyError(msg)
        if compute_state(row, now) not in {"waiting", "no_reply"}:
            msg = "this row is not waiting for a reply"
            raise NodeAdminBusyError(msg)
        if row["cmd"] in {"reboot", "sync"} or int(row["ctr"]) < 1:
            msg = f"{row['cmd']} is never asked again"
            raise NodeAdminBusyError(msg)
        if row["id"] in self._reasked:
            msg = "this row was already asked again once; the node caches only one reply"
            raise NodeAdminBusyError(msg)
        until = _lockout_until_ms(rows, now)
        if until is not None:
            msg = f"node may be locked out until {until}; no frames until then"
            raise NodeAdminBusyError(msg)
        handed_off = row.get("handed_off_at")
        if handed_off is None or now - int(handed_off) >= remote_cmd.RM_CACHE_MS:
            msg = "the node no longer caches this reply (10 min)"
            raise NodeAdminBusyError(msg)
        if now - int(handed_off) < remote_cmd.RM_REASK_MIN_MS:
            msg = (
                "replies take 10 to 30 s, ask again only after "
                f"{remote_cmd.RM_REASK_MIN_MS // 1000} s"
            )
            raise NodeAdminBusyError(msg)
        last = _last_frame_ms(rows)
        if last is not None and now - last < remote_cmd.RM_RATE_MS:
            msg = f"the node accepts one frame per {remote_cmd.RM_RATE_MS // 1000} s"
            raise NodeAdminBusyError(msg)

    # ── history ────────────────────────────────────────────────────────────

    async def history(self, target: str | None, limit: int) -> list[dict[str, Any]]:
        t = remote_cmd.normalize_call(target) if target else None
        limit = max(1, min(int(limit), HISTORY_LIMIT_MAX))
        now = self._clock()
        rows = await self._storage.node_admin_history(t, limit)
        return [{**r, "state": compute_state(r, now)} for r in rows]

    # ── replies ────────────────────────────────────────────────────────────

    async def on_reply(self, message: dict[str, Any]) -> None:
        """ReplyHook. Cheap prefilter here; everything else runs in a task so
        the ingest path never waits on the DB or the SecretBox."""
        msg = message.get("msg")
        if not isinstance(msg, str) or not msg.startswith(REPLY_PREFIX):
            return
        if len(msg) > REPLY_MAX_CHARS:
            self._warn_rejected(message, msg)
            return
        self._spawn(self._handle_reply(message))

    @staticmethod
    def _warn_rejected(message: dict[str, Any], msg: str) -> None:
        """One WARNING for a reply-shaped RM1 text the parser refuses (no tag, no text)."""
        reason = remote_cmd.reply_rejection(msg)
        if reason is not None:
            logger.warning(
                "node admin: RM1 reply from %s dropped, %s", str(message.get("src"))[:24], reason
            )

    async def _handle_reply(self, message: dict[str, Any]) -> None:
        try:
            await self._process_reply(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("node admin reply processing failed")

    async def _find_row(self, target: str, ctr: int, now: int, text: str) -> dict[str, Any] | None:
        if ctr != 0:
            return await self._storage.find_node_admin_log_row(target, ctr, now)
        # A sync reply carries no request freshness (ctr 0, tag over the hwm text only),
        # so it binds only to a sync row inside its own reply window.
        sync_rows = [
            r
            for r in await self._storage.node_admin_history(target, HISTORY_SCAN)
            if r["cmd"] == "sync" and int(r["ctr"]) == 0
        ]
        for r in sync_rows:
            # The other transport's copy of a reply that just verified a row: identical
            # text, a rate window apart at most. Dropped, never bound to an older row.
            if (
                r.get("verified") == 1
                and r.get("reply_text") == text
                and r.get("reply_at") is not None
                and now - int(r["reply_at"]) < remote_cmd.RM_RATE_MS
            ):
                return None
        row = await self._storage.find_node_admin_log_row(target, 0, now)
        if row is not None:
            return row
        # A forged (bad tag) sync reply closes the open sync row; the genuine
        # one must still be able to upgrade it. Only the newest sync row.
        if sync_rows and sync_rows[0].get("verified") == 0:
            r = sync_rows[0]
            anchor = r.get("handed_off_at")
            anchor = r.get("sent_at") if anchor is None else anchor
            if anchor is not None and now - int(anchor) < remote_cmd.RM_REPLY_TIMEOUT_MS:
                return r
        return None

    async def _last_hwm(self, target: str) -> int:
        for item in await self._storage.list_node_admin_targets():
            if item["target"] == target:
                return int(item["last_hwm"])
        return 0

    async def _correlate(
        self, message: dict[str, Any]
    ) -> tuple[remote_cmd.ParsedReply, dict[str, Any]] | None:
        """The parsed reply and the log row it belongs to, or None (ignore silently)."""
        msg, src, dst = message.get("msg"), message.get("src"), message.get("dst")
        if not isinstance(msg, str) or not isinstance(src, str) or not isinstance(dst, str):
            return None
        parsed = remote_cmd.parse_reply(msg)
        if parsed is None:
            self._warn_rejected(message, msg)
            return None
        try:
            target = remote_cmd.normalize_call(strip_relay_path(src))
            reply_dst = remote_cmd.normalize_call(resolve_dst_target(dst))
        except ValueError:
            return None
        now = self._clock()
        row = await self._find_row(target, parsed.ctr, now, util.strip_ack_suffix(msg))
        if row is None or int(row["ctr"]) != parsed.ctr or reply_dst != row["src_call"]:
            return None  # no such row, or an overheard reply for another SysOp
        if parsed.ctr == 0:
            # `ctr=<hwm>` of a genuine sync reply is never below what we already verified.
            hwm = remote_cmd.parse_sync_hwm(parsed.body) or 0
            stored = await self._last_hwm(target)
            if hwm < stored:
                logger.info(
                    "node admin %s: sync reply with ctr=%d below hwm %d ignored",
                    target,
                    hwm,
                    stored,
                )
                return None
        return parsed, row

    async def _process_reply(self, message: dict[str, Any]) -> None:
        found = await self._correlate(message)
        if found is None:
            return
        parsed, row = found
        target = str(row["target_call"])
        try:
            key = await self._get_key(target)
        except (NodeAdminUnavailableError, ValueError):
            return
        msg = str(message["msg"])
        text = util.strip_ack_suffix(msg)
        now = self._clock()
        verified = remote_cmd.verify_reply(key, target, row["src_call"], msg)
        if verified is None:
            changed = await self._storage.apply_node_admin_reply(
                row["id"], text, now, parsed.result, False
            )
            if changed:
                self.rejected_replies += 1
                logger.warning(
                    "node admin %s: unverifiable reply to RM1 %d (%d so far)",
                    target,
                    parsed.ctr,
                    self.rejected_replies,
                )
                await self._broadcast_row(target, row["id"])
            return
        changed = await self._storage.apply_node_admin_reply(
            row["id"], text, now, verified.result, True
        )
        if changed is not True:
            return  # the other transport's copy already did this
        await self._act_on_verified(target, row, verified, now)

    async def _act_on_verified(
        self, target: str, row: dict[str, Any], verified: remote_cmd.ParsedReply, now: int
    ) -> None:
        """hwm, sync gate, broadcast: runs once per verification, never for a bad tag."""
        if verified.ctr == 0:
            hwm = remote_cmd.parse_sync_hwm(verified.body)
            await self._storage.raise_node_admin_hwm(
                target, hwm if hwm is not None else 0, sync_at_ms=now
            )
        else:
            await self._storage.raise_node_admin_hwm(target, verified.ctr)
        self._synced.add(target)
        pend = self._pending.get(target)
        if verified.ctr == 0 and pend is not None and pend.sync_log_id == row["id"]:
            pend.reply_at = now
            pend.released.set()
        logger.info("node admin %s: verified reply to RM1 %d", target, verified.ctr)
        await self._broadcast_row(target, row["id"])
