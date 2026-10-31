"""Dbus-free values, types and the backend contract of the BLE service.

Everything here is importable without the D-Bus library (or any other transport
library), so `main.py` and the tests can use it on hosts where BlueZ does not
exist. `ble_adapter` re-exports these names unchanged.
"""

import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Literal, Protocol

MESHCOM_NAME_PREFIX = "MC-"  # not shared with src/mcapp — ble_service is a separate process

# Timing constants (seconds) shared by the command layer of every backend.
WRITE_TIMEOUT_S = 5.0
KEEPALIVE_INTERVAL_S = 300
DST_CHECK_INTERVAL_S = 3600
REGISTER_QUERY_DELAY_S = 0.8
# set_time(): pause between the node-offset command and the 0x20 timestamp so
# the firmware has applied the offset before it converts UTC to local.
TIME_SYNC_SETTLE_S = 0.3
# set_time() node-TZ handling (doc/2026-10-04_1600-node-tz-implementation-plan.md
# D3/D5): how long to wait for the SN1 push after `--nodeset` (classification
# probe) and after `--settz` / the follow-up `--nodeset` (verification).
NODE_TZ_PROBE_TIMEOUT_S = 3.0
NODE_TZ_VERIFY_TIMEOUT_S = 2.0
# The firmware drains its whole BLE RX queue into one global `textbuff_phone`
# and acts on it once per main-loop pass (esp32_main.cpp:3058-3084,
# nrf52_main.cpp:1642-1670): two 0xA0 (MsgType.TEXT_COMMAND) writes landing in
# the same pass silently lose all but the last. `write()` serialises via
# `_write_lock` but that only prevents concurrent D-Bus calls -- it adds no
# gap between them. This floor is the loop period (unmeasured) rounded up
# from a 200 ms guess plus margin. Only TEXT_COMMAND frames share this
# firmware state; the binary config frames (0x20/0x50/0x55/0x70/0x80/0x90/
# 0x95/0xF0) do not and are never delayed.
A0_MIN_GAP_S = 0.3

# The exact marker _attempt_connection() raises into on InterfaceNotFoundError
# (a MAC BlueZ has no D-Bus object for yet). Shared with _is_device_not_found_error
# below so the two can't drift apart.
DEVICE_NOT_FOUND_MSG = "Device not found or not paired"

OPEN_HELLO = b"\x04\x10\x20\x30"

# Nordic UART Service UUIDs
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write to device
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Read from device (notify)


def build_hello_bytes(pin: int) -> bytes:
    """Build the BLE hello message for the given PIN.

    pin == 0: 4-byte open hello (no authentication).
    pin 100000-999999: 36-byte hello with SHA-256 of zero-padded 6-digit PIN string.
    The firmware hashes the ASCII string e.g. b"123456", not the integer bytes.
    """
    if pin > 0:
        digest = hashlib.sha256(f"{pin:06d}".encode()).digest()
        return bytes([0x24, 0x10, 0x20, 0x30]) + digest
    return OPEN_HELLO


class MsgType(IntEnum):
    """GATT write-frame type byte (see module docstring's Supported Message Types)."""

    TIME_SYNC = 0x20
    SET_CALLSIGN = 0x50
    SET_WIFI = 0x55
    SET_LATITUDE = 0x70
    SET_LONGITUDE = 0x80
    SET_ALTITUDE = 0x90
    SET_APRS_SYMBOLS = 0x95
    TEXT_COMMAND = 0xA0
    SAVE_AND_REBOOT = 0xF0


# save_and_reboot-style trailer byte on set_latitude/longitude/altitude: whether
# the new value is persisted to flash immediately or kept RAM-only until a
# separate --save / MsgType.SAVE_AND_REBOOT.
SAVE_TO_FLASH = 0x0A
RAM_ONLY = 0x0B


# Index of the type byte in a `_frame()`-built buffer ([length, type,
# ...payload]) -- used by `BLEAdapter.write()` to recognise a TEXT_COMMAND
# frame for A0_MIN_GAP_S without re-deriving the frame layout there.
_FRAME_TYPE_BYTE_INDEX = 1


def _frame(msg_type: MsgType, payload: bytes = b"") -> bytes:
    """Build a GATT write frame: length-byte + type-byte + payload.

    `length` is len(payload) + 2 — it counts the type byte and the payload,
    matching the firmware's frame-length convention (see module docstring).
    """
    length = len(payload) + 2
    return length.to_bytes(1, "big") + bytes([msg_type]) + payload


class ConnectionState(Enum):
    """BLE connection states"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


@dataclass
class BLEDevice:
    """Discovered BLE device information"""

    name: str
    address: str
    rssi: int = 0
    paired: bool = False
    connected: bool = False
    known: bool = False
    path: str = ""


@dataclass
class BLEStatus:
    """Current BLE adapter status"""

    state: ConnectionState = ConnectionState.DISCONNECTED
    device: BLEDevice | None = None
    error: str | None = None
    last_activity: float = field(default_factory=time.time)


@dataclass
class EnsureConnectedResult:
    """Structured outcome of `BLEAdapter.ensure_connected()`.

    Wave B (`ble_service/src/main.py`) consumes `stage`/`error_name` to build
    a machine-readable `error_code` for the HTTP layer instead of pattern-
    matching a free-text message, so this shape is the contract between the
    two waves -- treat field names/meanings as load-bearing.

    success: True iff the device ends up connected, Trusted, and subscribed
        to notifications -- either just now, or already (`stage ==
        "already_connected"`).
    stage: where the operation landed.
        - "already_connected" / "connected": success.
        - "connect": the Connect() stage failed (after the internal
          scan-and-retry-once for a not-yet-known MAC).
        - "pair": on-demand GATT-layer pairing failed.
        - "gatt" / "gatt_post_pair": subscribing to notifications failed for
          a reason other than "needs pairing", or failed again even after a
          successful on-demand pair.
    error_name: the D-Bus error NAME (e.g. "org.bluez.Error.Failed") on
        failure, the Python exception type name for a non-D-Bus failure (e.g.
        "TimeoutError"), or the synthetic "PairingNotEstablished" for the one
        failure BlueZ reports without raising at all. Never None on failure --
        Wave B may rely on that; always None on success.
    error_text: the D-Bus error text / exception message, for logging. None
        on success.

    A failure result also implies the session has been torn down: the connect
    stage cleans up via `_cleanup_failed_connection`, the pair/GATT stages via
    `_teardown_after_post_connect_failure`. There is no "returned False but
    the link is still up" state for a caller to reason about.
    """

    success: bool
    stage: str
    error_name: str | None = None
    error_text: str | None = None


class BackendUnsupportedError(Exception):
    """Raised by a backend for an operation its platform cannot do (pair/unpair)."""


class BleBackend(Protocol):
    """The adapter surface `main.py` uses; implemented by every BLE backend."""

    name: Literal["bluez", "bleak"]
    address_kind: Literal["mac", "uuid"]
    capabilities: Mapping[str, bool]
    hello_bytes: bytes
    pairing_passkey: int
    last_time_sync_branch: str

    @property
    def status(self) -> BLEStatus: ...
    @property
    def is_connected(self) -> bool: ...
    @property
    def is_busy(self) -> bool: ...

    def normalize_address(self, address: str) -> str: ...
    def reset_bus(self) -> None: ...
    async def scan(
        self,
        timeout: float = 5.0,  # noqa: ASYNC109 - public API takes timeout
        prefix: str = MESHCOM_NAME_PREFIX,
    ) -> list[BLEDevice]: ...
    async def connect(self, mac: str, max_retries: int = 3) -> bool: ...
    async def ensure_connected(
        self, mac: str, pin: int | None = None, *, user_initiated: bool = False
    ) -> EnsureConnectedResult: ...
    async def disconnect(self) -> bool: ...
    async def start_notify(self) -> None: ...
    async def pair(self, mac: str) -> bool: ...
    async def unpair(self, mac: str) -> bool: ...
    async def write(self, data: bytes) -> bool: ...
    async def send_message(self, msg: str, group: str) -> bool: ...
    async def send_command(self, cmd: str) -> bool: ...
    async def send_hello(self) -> bool: ...
    async def set_time(self) -> bool: ...
    async def set_callsign(self, callsign: str) -> bool: ...
    async def set_wifi(self, ssid: str, password: str) -> bool: ...
    async def set_latitude(self, lat: float, save: bool = False) -> bool: ...
    async def set_longitude(self, lon: float, save: bool = False) -> bool: ...
    async def set_altitude(self, alt: int, save: bool = False) -> bool: ...
    async def set_aprs_symbols(self, primary: str, secondary: str) -> bool: ...
    async def save_and_reboot(self) -> bool: ...
    async def query_extended_registers(self) -> None: ...
    def note_register(self, typ: str) -> None: ...
    def arm_register(self, typ: str) -> None: ...
    async def wait_for_register(self, typ: str, timeout_s: float) -> bool: ...
