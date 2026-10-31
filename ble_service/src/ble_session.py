"""Transport-independent command/session layer of the BLE service.

`BleSessionBase` holds everything that only needs a raw GATT write plus three
transport hooks: framing and the A0 write gap, the `send_*` / `set_*` family,
register waits, the node-TZ aware time sync, and the keepalive / DST loops.
`BLEAdapter` (BlueZ) inherits it; it imports no transport library.

Hooks a subclass must provide:

- `_gatt_ready() -> bool`: the write characteristic is usable,
- `async _gatt_write(data)`: one raw GATT write (the base adds lock, gap, timeout),
- `_drop_transport_handles()`: forget the transport's connection objects.
"""

import asyncio
import logging
import struct
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from . import node_tz
from .ble_types import (
    _FRAME_TYPE_BYTE_INDEX,
    A0_MIN_GAP_S,
    DST_CHECK_INTERVAL_S,
    KEEPALIVE_INTERVAL_S,
    NODE_TZ_PROBE_TIMEOUT_S,
    NODE_TZ_VERIFY_TIMEOUT_S,
    OPEN_HELLO,
    RAM_ONLY,
    REGISTER_QUERY_DELAY_S,
    SAVE_TO_FLASH,
    TIME_SYNC_SETTLE_S,
    WRITE_TIMEOUT_S,
    BLEStatus,
    ConnectionState,
    MsgType,
    _frame,
)

# Firmware storage bound, not an arbitrary MCProxy choice: `node_call` is a
# fixed `char[10]` on every platform (nrf52/WisBlock-API.h, esp32/esp32_flash.h)
# and is filled via `snprintf(..., sizeof(node_call), ...)`, so anything past 9
# characters is silently truncated in flash rather than rejected. The old
# value here (15) was looser than that real ceiling -- a 10-15 char callsign
# would have been accepted here only to come back truncated from the device.
_MAX_CALLSIGN_LEN = 9
_BLE_MTU_LIMIT = 247
_MAX_SSID_LEN = 32
_MAX_WIFI_PASSWORD_LEN = 63
# The wire format's inner length-prefix byte (set_callsign, set_wifi) is a
# single unsigned byte -- this is the hard ceiling that field can express,
# independent of any single field's own MTU/firmware-storage bound.
_INNER_LENGTH_BYTE_MAX = 0xFF
# `_frame()`'s length prefix is a SINGLE byte: length = len(payload) + 2 must
# fit in it, i.e. the largest value `int.to_bytes(1, "big")` can represent.
# This is a different, larger boundary than `_BLE_MTU_LIMIT` (247) -- that one
# is a real ATT-MTU-derived cap enforced only for set_callsign/set_wifi; this
# one is the hard ceiling the wire framing itself cannot exceed no matter what
# the negotiated MTU is. Exceeding it used to raise a bare `OverflowError`
# from deep inside `int.to_bytes()`, with no indication at the call site of
# what went wrong -- see `BLEAdapter.send_command`'s length pre-check.
_FRAME_LENGTH_PREFIX_MAX = 255

# ACK attribution opt-in (firmware proposal docs/ack-wer-hat-quittiert.md
# §5.5): the node's "first ACK only" gate falls only while this session flag
# is set. It is VOLATILE on the node -- reset on BLE disconnect -- so the
# official phone app, which never sends it, keeps the legacy one-frame
# behaviour on the same node. McApp is idempotent for repeated ACK frames
# (storage/ingest.py `_record_message_ack`), so it opts in on every connect.
# Pre-attribution firmware answers `--wrong command --ackinfo on` on the
# command-back channel, which is logged and otherwise harmless.
ACK_ATTRIBUTION_COMMAND = "--ackinfo on"

logger = logging.getLogger(__name__)


class BleSessionBase:
    """Command/session layer shared by every BLE transport backend."""

    def __init__(
        self,
        hello_bytes: bytes = OPEN_HELLO,
        notification_callback: Callable[[bytes], None] | None = None,
        register_lookup: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> None:
        self.hello_bytes = hello_bytes
        self.notification_callback = notification_callback
        # Read access to the service's register cache (main.py passes
        # `lambda: state.register_cache`); the adapter never imports `main`.
        # Without one nothing can be classified, so set_time() stays on its
        # fail-safe branch (see `node_tz.classify`).
        self._register_lookup = register_lookup
        # One event per register TYP, tripped by `note_register` from the
        # cache setter; see `arm_register` / `wait_for_register`.
        self._register_events: dict[str, asyncio.Event] = {}
        # Which set_time() branch ran last (utcoff / settz / rule / unknown),
        # reported by POST /api/ble/settime.
        self.last_time_sync_branch: str = ""
        # NimBLE pairing passkey returned by the BlueZ agent during pair().
        # 0 means open pairing (firmware bt_code == 0). 100000-999999 means
        # the firmware will require this exact value via RequestPasskey.
        self.pairing_passkey: int = 0

        # State
        self._status = BLEStatus()
        self._write_lock = asyncio.Lock()
        # monotonic timestamp of the last successful TEXT_COMMAND (0xA0)
        # write; see A0_MIN_GAP_S. None means "no prior A0 write this
        # session" -- never wait on it.
        self._last_a0_write_monotonic: float | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._dst_check_task: asyncio.Task[None] | None = None
        self._last_utc_offset: float | None = None
        self._connected_mac: str | None = None
        self._disconnect_callback: Callable[[], None] | None = None

    # -- transport hooks ----------------------------------------------------

    def _gatt_ready(self) -> bool:
        """True when the write characteristic can be written to."""
        raise NotImplementedError

    async def _gatt_write(self, data: bytes) -> None:
        """One raw GATT write of `data`; raises on failure."""
        raise NotImplementedError

    def _drop_transport_handles(self) -> None:
        """Forget the transport's connection objects after a lost link."""
        raise NotImplementedError

    @property
    def status(self) -> BLEStatus:
        """Get current adapter status"""
        return self._status

    @property
    def is_connected(self) -> bool:
        """Check if connected to a device"""
        return self._status.state == ConnectionState.CONNECTED

    async def write(self, data: bytes) -> bool:
        """
        Write data to device. Serialized via write lock to prevent
        concurrent GATT writes that cause "In Progress" D-Bus errors.

        Args:
            data: Raw bytes to write

        Returns:
            True if write successful
        """
        if not self.is_connected or not self._gatt_ready():
            raise RuntimeError("Not connected")

        # `_frame()`'s layout is [length, type, ...payload]: data[1] is the
        # type byte. Only TEXT_COMMAND (0xA0) shares the firmware's single
        # `textbuff_phone` buffer -- see A0_MIN_GAP_S.
        is_a0 = (
            len(data) > _FRAME_TYPE_BYTE_INDEX
            and data[_FRAME_TYPE_BYTE_INDEX] == MsgType.TEXT_COMMAND
        )

        async with self._write_lock:
            if is_a0 and self._last_a0_write_monotonic is not None:
                elapsed = time.monotonic() - self._last_a0_write_monotonic
                remaining = A0_MIN_GAP_S - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)

            try:
                await asyncio.wait_for(self._gatt_write(data), timeout=WRITE_TIMEOUT_S)
                self._status.last_activity = time.time()
            except TimeoutError:
                logger.exception("Write timeout")
                return False
            except Exception as e:
                error_str = str(e)
                logger.exception("Write error: %s", error_str)
                if "Not connected" in error_str:
                    self._on_disconnect_detected()
                return False

            else:
                if is_a0:
                    self._last_a0_write_monotonic = time.monotonic()
                return True

    def _on_disconnect_detected(self) -> None:
        """Handle unexpected disconnect (write failure or D-Bus signal)"""
        if self._status.state == ConnectionState.DISCONNECTED:
            return  # Already handled
        logger.warning("Disconnect detected, updating state")
        self._status.state = ConnectionState.DISCONNECTED
        self._status.device = None
        self._status.error = "Connection lost"
        self._connected_mac = None
        self._last_a0_write_monotonic = None

        # Cancel keepalive and DST check
        for task_attr in ("_keepalive_task", "_dst_check_task"):
            task = getattr(self, task_attr)
            if task and not task.done():
                task.cancel()
            setattr(self, task_attr, None)

        self._drop_transport_handles()

        if self._disconnect_callback:
            try:
                self._disconnect_callback()
            except Exception:
                logger.exception("Disconnect callback error")

    async def send_message(self, msg: str, group: str) -> bool:
        """
        Send a message to a MeshCom group.

        Args:
            msg: Message text
            group: Target group number or callsign

        Returns:
            True if send successful

        Raises:
            ValueError: the framed message is too long for `_frame()`'s one-byte
                length prefix. Same defect and same cap as `send_command` above --
                this path builds a TEXT_COMMAND frame the identical way, so it
                inherited the identical bare `OverflowError` from `int.to_bytes()`.
        """
        message = "{" + group + "}" + msg
        message_bytes = message.encode("utf-8")
        length = len(message_bytes) + 2
        if length > _FRAME_LENGTH_PREFIX_MAX:
            raise ValueError(
                f"Message too long: {length} bytes (max {_FRAME_LENGTH_PREFIX_MAX}) -- "
                f"the GATT write frame's length prefix is a single byte and cannot "
                f"encode it."
            )
        return await self.write(_frame(MsgType.TEXT_COMMAND, message_bytes))

    async def send_hello(self) -> bool:
        """Send hello/wakeup command to device"""
        if not self.is_connected:
            return False
        # Hello starts the node's register burst, SN1 included. A cache that
        # outlives the connection (same MAC) holds the PREVIOUS session's SN1,
        # so forget its arrival: set_time() trusts a cached TZ state other than
        # "rule" only once a fresh SN1 has landed after this point.
        self.arm_register("SN1")
        return await self.write(self.hello_bytes)

    async def send_command(self, cmd: str) -> bool:
        """
        Send an A0 command to device.

        Args:
            cmd: Command string (e.g., "--pos", "--info", "--reboot")

        Returns:
            True if send successful

        Raises:
            ValueError: `cmd` is too long for `_frame()`'s one-byte length
                prefix (payload cap 253 bytes -- see `_FRAME_LENGTH_PREFIX_MAX`).
                Without this check the overflow surfaced as a bare
                `OverflowError` raised deep inside `int.to_bytes()`, with
                nothing at this boundary explaining what went wrong.
        """
        cmd_bytes = cmd.encode("utf-8")
        length = len(cmd_bytes) + 2
        if length > _FRAME_LENGTH_PREFIX_MAX:
            raise ValueError(
                f"Command too long: {length} bytes (max {_FRAME_LENGTH_PREFIX_MAX}) -- "
                f"the GATT write frame's length prefix is a single byte and cannot "
                f"represent more than {_FRAME_LENGTH_PREFIX_MAX - 2} bytes of payload"
            )
        return await self.write(_frame(MsgType.TEXT_COMMAND, cmd_bytes))

    def note_register(self, typ: str) -> None:
        """Record that register `typ` just arrived and trip its waiters. Called
        by the register-cache setter in main.py right after it stores a frame.
        An arrival before anyone armed still counts: it is how `_classify_node`
        learns the cached SN1 is from this session."""
        self._register_events.setdefault(typ, asyncio.Event()).set()

    def arm_register(self, typ: str) -> None:
        """Forget earlier arrivals of `typ`: the next `wait_for_register` only
        returns for a frame that lands after this call. Arm BEFORE sending the
        command whose answer is awaited -- the push can land while the write
        is still being awaited."""
        self._register_events.setdefault(typ, asyncio.Event()).clear()

    async def wait_for_register(self, typ: str, timeout_s: float) -> bool:
        """True if register `typ` arrived since `arm_register(typ)`, False on
        timeout. The caller then reads the value from the register cache."""
        event = self._register_events.setdefault(typ, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except TimeoutError:
            return False
        return True

    def _cached_registers(self) -> Mapping[str, Mapping[str, Any]]:
        return self._register_lookup() if self._register_lookup is not None else {}

    async def _send_nodeset(self) -> bool:
        """Arm SN1, then ask the node for its settings (`--nodeset`: SN and SN1
        are pushed back to back). True if the command was written; the caller
        waits for the answer."""
        self.arm_register("SN1")
        return await self.send_command("--nodeset")

    async def _wait_for_tz(self, rule: str, timeout_s: float) -> bool:
        """True once an SN1 showing `TZ == rule` arrives within `timeout_s`.

        An SN1 with another TZ does not end the wait: it may be a late answer
        to an earlier `--nodeset` of ours, sent before the node saw `--settz`.
        Each such frame re-arms the event and the wait goes on to the deadline.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0 or not await self.wait_for_register("SN1", remaining):
                return False
            if self._cached_tz() == rule:
                return True
            self.arm_register("SN1")

    async def _push_host_tz(self, rule: str) -> bool:
        """`--settz <rule>`, confirmed through the SN1 push (decision D5).

        Success is invisible over BLE except as `SN1.TZ == rule`; a rejected
        rule changes nothing and pushes nothing. So: wait for an SN1 that shows
        the rule, and if none does, ask once with `--nodeset` and wait again.
        False means "not verified", not "rejected": the node may have taken it.
        """
        self.arm_register("SN1")
        if not await self.send_command(f"--settz {rule}"):
            return False
        if await self._wait_for_tz(rule, NODE_TZ_VERIFY_TIMEOUT_S):
            return True
        if not await self._send_nodeset():
            return False
        return await self._wait_for_tz(rule, NODE_TZ_VERIFY_TIMEOUT_S)

    def _cached_tz(self) -> object:
        return self._cached_registers().get("SN1", {}).get("TZ")

    def _sn1_is_fresh(self) -> bool:
        """An SN1 arrived since the last arm (hello, probe, DST check)."""
        event = self._register_events.get("SN1")
        return event is not None and event.is_set()

    async def _classify_node(self) -> node_tz.NodeTz:
        """Settle what the node is BEFORE any offset command goes out (D3).

        The register cache survives a reconnect to the same MAC, so a cached
        SN1 may describe the previous session, and the fresh one lands several
        seconds after hello (the HTTP connect route reaches set_time() at about
        4.4 s, the burst is still arriving). Only a cached RULE is trusted
        unconditionally: acting on it sends 0x20 only, which is safe even when
        stale. Any other state needs an SN1 that arrived since hello; without
        one, ask with `--nodeset` and wait for it.
        """
        node = node_tz.classify(self._cached_registers())
        if node.state is node_tz.TzState.RULE or (
            node.state is not node_tz.TzState.UNKNOWN and self._sn1_is_fresh()
        ):
            return node
        logger.info("Node TZ state unknown or possibly stale, asking with --nodeset")
        sent = await self._send_nodeset()
        if sent:
            await self.wait_for_register("SN1", NODE_TZ_PROBE_TIMEOUT_S)
        # `probed` only if the question went out: a failed write says nothing
        # about whether the node would have answered with SN1.
        node = node_tz.classify(self._cached_registers(), probed=sent)
        if node.state is node_tz.TzState.EMPTY and not self._sn1_is_fresh():
            # A stale "no rule" and an unanswered probe: the node may have a
            # rule by now, and a TZ-01 node always answers --nodeset.
            return node_tz.NodeTz(node_tz.TzState.UNKNOWN)
        return node

    async def _send_utc_offset(self) -> None:
        """Fixed offset for firmware without a TZ rule -- the pre-TZ-01 path."""
        # Calculate current UTC offset of the system timezone (handles DST).
        # utcoffset() is `timedelta | None` on the general `datetime` type
        # (None only for naive datetimes); `local_now` is always tz-aware
        # here since it's derived via astimezone() from datetime.now(UTC),
        # so the `or timedelta()` fallback never actually triggers.
        local_now = datetime.now(UTC).astimezone()
        utc_offset_hours = (local_now.utcoffset() or timedelta()).total_seconds() / 3600

        # Send UTC offset first so the firmware applies it to the timestamp
        offset_cmd = f"--utcoff {utc_offset_hours:+.1f}"
        logger.info("Syncing UTC offset: %s", offset_cmd)
        if await self.send_command(offset_cmd):
            self._last_utc_offset = utc_offset_hours
        else:
            logger.warning("Failed to send UTC offset (continuing with time sync)")

    async def set_time(self) -> bool:
        """Set current time on device, and its UTC offset where that is safe.

        Firmware with TZ-01 keeps a POSIX TZ rule and `--utcoff` CLEARS it
        (contract doc section 5), so the node is classified first and the
        branch taken is recorded in `last_time_sync_branch`:

        - `utcoff`: firmware without TZ-01, or a TZ-01 node with no rule and
          nothing to push. `--utcoff` via A0, then the timestamp -- the
          behaviour from before TZ-01, byte for byte. This is what keeps the
          DST watcher (`_last_utc_offset`) running.
        - `rule`: the node has a rule and derives its own offset. Timestamp
          only; the rule is never overwritten and `_last_utc_offset` is
          cleared so the DST watcher stays idle.
        - `settz`: TZ-01 node with no rule, policy `host_if_unset`: the host's
          rule is pushed once (`--settz`) and confirmed through SN1.
        - `settz_unverified`: the push could not be confirmed. Timestamp only,
          warning, DST watcher idle. No `--utcoff`: the node may have accepted
          the rule, and `--utcoff` would clear it.
        - `unknown`: the node could not be classified in time. Timestamp only:
          UTC is always right, a wrong `--utcoff` can destroy a rule, a
          missing one cannot. The next connect retries.

        The timestamp (0x20) is UTC in every branch.
        """
        node = await self._classify_node()
        branch = "utcoff"
        if node.state is node_tz.TzState.RULE:
            branch = "rule"
            logger.info("Node follows TZ rule %s, time sync without --utcoff", node.rule)
        elif node.state is node_tz.TzState.UNKNOWN:
            branch = "unknown"
            logger.warning(
                "Node TZ support could not be determined; sending time only, no --utcoff"
            )
        elif node.state is node_tz.TzState.EMPTY and node_tz.resolve_policy() == "host_if_unset":
            rule = node_tz.host_node_tz()
            if rule is not None:
                if await self._push_host_tz(rule):
                    branch = "settz"
                    logger.info("Node had no TZ rule, set %s", rule)
                else:
                    branch = "settz_unverified"
                    logger.warning(
                        "Could not verify TZ rule %s on the node; sending time only, no --utcoff",
                        rule,
                    )

        if branch == "utcoff":
            await self._send_utc_offset()
        else:
            # The node derives (or will derive) its own offset; a stale value
            # from an earlier session would make the DST watcher fire.
            self._last_utc_offset = None
        self.last_time_sync_branch = branch

        if branch in ("utcoff", "settz"):
            await asyncio.sleep(TIME_SYNC_SETTLE_S)

        # Send Unix timestamp
        now = int(time.time())
        return await self.write(_frame(MsgType.TIME_SYNC, now.to_bytes(4, byteorder="little")))

    async def set_callsign(self, callsign: str) -> bool:
        """
        Set device callsign (0x50 message).

        Args:
            callsign: New callsign (e.g., "DL4GLE-10")

        Returns:
            True if successful

        Wire format (`phone_commands.cpp:283,439-452`): `[len][0x50][len_byte]
        [callsign bytes]` -- the firmware reads `conf_data[2]` as the
        callsign's length and the callsign itself from offset 3. Omitting that
        inner length byte (the bug this method used to have) does not just
        misparse the length -- it eats the callsign's own first byte as the
        length field over an otherwise zero-filled buffer, so "DK5EN-98" was
        silently stored as "K5EN-98".
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        # Validate callsign format
        if not callsign or len(callsign) > _MAX_CALLSIGN_LEN:
            raise ValueError(f"Callsign must be 1-{_MAX_CALLSIGN_LEN} characters")

        callsign_bytes = callsign.encode("utf-8")
        if len(callsign_bytes) > _INNER_LENGTH_BYTE_MAX:
            # Unreachable today (_MAX_CALLSIGN_LEN=9 caps this well below 255
            # even for multi-byte UTF-8), but the wire format's inner length
            # is a single byte -- guard it explicitly rather than silently
            # truncating/wrapping if that constant is ever loosened.
            raise ValueError(
                f"Callsign too long for the wire format's 1-byte inner length field: "
                f"{len(callsign_bytes)} bytes (max {_INNER_LENGTH_BYTE_MAX})"
            )

        # Wire format: 1B callsign length, then the callsign bytes.
        payload = bytes([len(callsign_bytes)]) + callsign_bytes
        length = len(payload) + 2

        if length > _BLE_MTU_LIMIT:
            raise ValueError(f"Callsign too long: {length} bytes (max {_BLE_MTU_LIMIT})")

        return await self.write(_frame(MsgType.SET_CALLSIGN, payload))

    async def set_wifi(self, ssid: str, password: str) -> bool:
        """
        Set WiFi credentials (0x55 message).

        Args:
            ssid: WiFi network name
            password: WiFi password

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        # Validate lengths
        if not ssid or len(ssid) > _MAX_SSID_LEN:
            raise ValueError("SSID must be 1-32 characters")
        if len(password) > _MAX_WIFI_PASSWORD_LEN:
            raise ValueError("Password must be 0-63 characters")

        ssid_bytes = ssid.encode("utf-8")
        pwd_bytes = password.encode("utf-8")

        # Wire format: SSID_len byte, SSID bytes, PWD_len byte, PWD bytes
        payload = bytes([len(ssid_bytes)]) + ssid_bytes + bytes([len(pwd_bytes)]) + pwd_bytes
        length = len(payload) + 2

        if length > _BLE_MTU_LIMIT:
            raise ValueError(f"WiFi config too long: {length} bytes (max 247)")

        return await self.write(_frame(MsgType.SET_WIFI, payload))

    async def set_latitude(self, lat: float, save: bool = False) -> bool:
        """
        Set device latitude (0x70 message).

        Args:
            lat: Latitude in decimal degrees (-90.0 to 90.0)
            save: If True, persist to flash (requires --save or 0xF0 after)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if not -90.0 <= lat <= 90.0:  # noqa: PLR2004 - geographic bound
            raise ValueError("Latitude must be between -90.0 and 90.0")

        save_flag = SAVE_TO_FLASH if save else RAM_ONLY
        payload = struct.pack("<f", lat) + bytes([save_flag])
        return await self.write(_frame(MsgType.SET_LATITUDE, payload))

    async def set_longitude(self, lon: float, save: bool = False) -> bool:
        """
        Set device longitude (0x80 message).

        Args:
            lon: Longitude in decimal degrees (-180.0 to 180.0)
            save: If True, persist to flash (requires --save or 0xF0 after)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if not -180.0 <= lon <= 180.0:  # noqa: PLR2004 - geographic bound
            raise ValueError("Longitude must be between -180.0 and 180.0")

        save_flag = SAVE_TO_FLASH if save else RAM_ONLY
        payload = struct.pack("<f", lon) + bytes([save_flag])
        return await self.write(_frame(MsgType.SET_LONGITUDE, payload))

    async def set_altitude(self, alt: int, save: bool = False) -> bool:
        """
        Set device altitude (0x90 message).

        Args:
            alt: Altitude in meters (-1000 to 10000)
            save: If True, persist to flash (requires --save or 0xF0 after)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if not -1000 <= alt <= 10000:  # noqa: PLR2004 - altitude bound
            raise ValueError("Altitude must be between -1000 and 10000 meters")

        save_flag = SAVE_TO_FLASH if save else RAM_ONLY
        payload = alt.to_bytes(4, byteorder="little", signed=True) + bytes([save_flag])
        return await self.write(_frame(MsgType.SET_ALTITUDE, payload))

    async def set_aprs_symbols(self, primary: str, secondary: str) -> bool:
        """
        Set APRS symbol table and code (0x95 message).

        Args:
            primary: Primary symbol table (e.g., "/")
            secondary: Symbol code (e.g., "O" for balloon)

        Returns:
            True if successful
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        if len(primary) != 1 or len(secondary) != 1:
            raise ValueError("Symbols must be single characters")

        primary_byte = ord(primary)
        secondary_byte = ord(secondary)

        payload = bytes([primary_byte, secondary_byte])
        return await self.write(_frame(MsgType.SET_APRS_SYMBOLS, payload))

    async def save_and_reboot(self) -> bool:
        """
        Save settings to flash and reboot device (0xF0 message).

        IMPORTANT: This command will reboot the device immediately.
        All configuration changes will be lost unless this is called.

        Returns:
            True if command sent successfully
        """
        if not self.is_connected:
            raise RuntimeError("Not connected")

        return await self.write(_frame(MsgType.SAVE_AND_REBOOT))  # length=2, no payload

    async def query_extended_registers(self) -> None:
        """
        Query device registers NOT auto-sent on connection.

        The device auto-sends: I+IS1, SN+SN1, G, SA, SE+S1, SW+S2, W, AN
        This only queries: IO (GPIO status) and TM (telemetry config).

        The same burst carries the one per-session setting McApp needs on
        the node: `ACK_ATTRIBUTION_COMMAND`, sent FIRST so that every ACK
        frame of this session -- including the acks for messages sent while
        the register replies are still arriving -- is already ungated.
        """
        if not self.is_connected:
            logger.warning("Cannot query registers: not connected")
            return

        commands = [
            (ACK_ATTRIBUTION_COMMAND, REGISTER_QUERY_DELAY_S),  # session flag, see constant
            ("--io", REGISTER_QUERY_DELAY_S),  # TYP: IO (GPIO status)
            ("--tel", REGISTER_QUERY_DELAY_S),  # TYP: TM (telemetry config)
        ]

        for cmd, delay in commands:
            try:
                await self.send_command(cmd)
                await asyncio.sleep(delay)
            except Exception as e:
                logger.warning("Extended query %s failed: %s", cmd, e)

    def _start_keepalive(self) -> None:
        """Start keepalive task"""
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Send periodic keepalive commands"""
        try:
            while self.is_connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if self.is_connected:
                    logger.debug("Sending keepalive")
                    await self.send_command("--pos")
        except asyncio.CancelledError:
            pass

    def _start_dst_check(self) -> None:
        """Start periodic DST transition check"""
        if self._dst_check_task and not self._dst_check_task.done():
            return
        self._dst_check_task = asyncio.create_task(self._dst_check_loop())

    async def _dst_check_loop(self) -> None:
        """Check hourly for DST transitions and update device UTC offset."""

        try:
            while self.is_connected:
                await asyncio.sleep(DST_CHECK_INTERVAL_S)
                if not self.is_connected:
                    break

                # See set_time()'s comment: utcoffset() is `timedelta | None`
                # on the general `datetime` type, but local_now is always
                # tz-aware here, so the `or timedelta()` fallback never
                # actually triggers.
                local_now = datetime.now(UTC).astimezone()
                current_offset = (local_now.utcoffset() or timedelta()).total_seconds() / 3600

                if self._last_utc_offset is not None and current_offset != self._last_utc_offset:
                    logger.info(
                        "DST transition detected: UTC%+.1f -> UTC%+.1f",
                        self._last_utc_offset,
                        current_offset,
                    )
                    # A fresh SN1 decides what set_time() may send: a rule set
                    # on the node since the last sync must not be wiped.
                    self.arm_register("SN1")
                    await self.set_time()
        except asyncio.CancelledError:
            pass
