"""bleak (CoreBluetooth / BlueZ-via-bleak) transport for the BLE service.

Implements the three transport hooks of `BleSessionBase` plus the backend
surface of `ble_types.BleBackend`. bleak is imported lazily inside methods so
this module imports on hosts without bleak; `scanner_cls` / `client_cls` are
test seams that default to the real classes.

Design: doc/2026-10-31_1230-ble-service-macos-backend-design.md (section 3).
"""

import asyncio
import contextlib
import importlib
import logging
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

from .ble_session import BleSessionBase
from .ble_types import (
    DEVICE_NOT_FOUND_MSG,
    MESHCOM_NAME_PREFIX,
    NUS_RX_UUID,
    NUS_TX_UUID,
    OPEN_HELLO,
    BackendUnsupportedError,
    BLEDevice,
    ConnectionState,
    EnsureConnectedResult,
)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 20.0
FIND_TIMEOUT_S = 10.0
DISCONNECT_TIMEOUT_S = 10.0

PERMISSION_HELP = (
    "macOS denied Bluetooth access to the application this process runs under. "
    "CoreBluetooth grants it per application (the host terminal or launcher), not "
    "per python/uv: System Settings > Privacy & Security > Bluetooth > enable the "
    "terminal. If it is not listed, quit the terminal completely (Cmd-Q), relaunch "
    "and retry so the prompt reappears."
)


class BleakAdapter(BleSessionBase):
    """BLE backend on top of bleak."""

    name: Literal["bluez", "bleak"] = "bleak"
    address_kind: Literal["mac", "uuid"] = "uuid" if sys.platform == "darwin" else "mac"
    capabilities: Mapping[str, bool] = {"pair": False, "unpair": False}

    def __init__(  # noqa: PLR0913 - mirrors BLEAdapter plus two test seams
        self,
        read_uuid: str = NUS_TX_UUID,
        write_uuid: str = NUS_RX_UUID,
        hello_bytes: bytes = OPEN_HELLO,
        notification_callback: Callable[[bytes], None] | None = None,
        register_lookup: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
        *,
        scanner_cls: Any = None,
        client_cls: Any = None,
    ) -> None:
        super().__init__(hello_bytes, notification_callback, register_lookup)
        self.read_uuid = read_uuid
        self.write_uuid = write_uuid
        self._scanner_cls = scanner_cls
        self._client_cls = client_cls
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._operation_lock = asyncio.Lock()
        self._cancel_connect = False
        self._notify_ready = False
        self._early: list[bytes] = []
        self._permission_logged = False

    # -- bleak seams --------------------------------------------------------

    def _scanner(self) -> Any:
        if self._scanner_cls is None:
            self._scanner_cls = importlib.import_module("bleak").BleakScanner
        return self._scanner_cls

    def _new_client(self, device: Any) -> Any:
        if self._client_cls is None:
            self._client_cls = importlib.import_module("bleak").BleakClient
        return self._client_cls(
            device, disconnected_callback=self._on_bleak_disconnect, timeout=CONNECT_TIMEOUT_S
        )

    # -- transport hooks ----------------------------------------------------

    def _gatt_ready(self) -> bool:
        return self._client is not None and bool(self._client.is_connected)

    async def _gatt_write(self, data: bytes) -> None:
        await self._client.write_gatt_char(self.write_uuid, data, response=True)

    def _drop_transport_handles(self) -> None:
        self._client = None
        self._notify_ready = False
        self._early.clear()

    # -- backend surface ----------------------------------------------------

    @property
    def is_busy(self) -> bool:
        return self._operation_lock.locked()

    def normalize_address(self, address: str) -> str:
        return address.strip().upper()

    def reset_bus(self) -> None:
        """No bus to reset on bleak."""

    async def pair(self, mac: str) -> bool:  # noqa: ARG002 - backend protocol signature
        raise BackendUnsupportedError(
            "macOS pairs through a system dialog on first encrypted access"
        )

    async def unpair(self, mac: str) -> bool:  # noqa: ARG002 - backend protocol signature
        raise BackendUnsupportedError("remove the node in System Settings > Bluetooth")

    async def scan(
        self,
        timeout: float = 5.0,  # noqa: ASYNC109 - public API takes timeout
        prefix: str = MESHCOM_NAME_PREFIX,
    ) -> list[BLEDevice]:
        async with self._operation_lock:
            found = await self._scanner().discover(timeout=timeout, return_adv=True)
        want = prefix.lower()
        devices: list[BLEDevice] = []
        for dev, adv in found.values():
            name = dev.name or getattr(adv, "local_name", None) or ""
            if not name.lower().startswith(want):
                continue
            addr = self.normalize_address(dev.address)
            devices.append(
                BLEDevice(
                    name=name,
                    address=addr,
                    rssi=int(getattr(adv, "rssi", 0) or 0),
                    paired=False,  # CoreBluetooth exposes no bond state
                    connected=self.is_connected and self._connected_mac == addr,
                    known=self._connected_mac == addr,
                )
            )
        return devices

    async def _connect_once(self, mac: str) -> None:
        """One connect attempt; leaves `self._client` set on success."""
        dev = await self._scanner().find_device_by_address(mac, timeout=FIND_TIMEOUT_S)
        if dev is None:
            raise ConnectionError(DEVICE_NOT_FOUND_MSG)
        client = self._new_client(dev)
        self._client = client
        self._notify_ready = False
        self._early.clear()
        await client.connect()

    def _mark_connected(self, mac: str) -> None:
        self._connected_mac = mac
        self._status.state = ConnectionState.CONNECTED
        self._status.error = None
        self._last_a0_write_monotonic = None
        self._status.device = BLEDevice(name="", address=mac, connected=True)
        self._status.last_activity = time.time()

    async def _discard_client(self) -> None:
        client, self._client = self._client, None
        self._notify_ready = False
        self._early.clear()
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), timeout=DISCONNECT_TIMEOUT_S)

    def _note_failure(self, exc: BaseException) -> None:
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        if not self._permission_logged and (
            "BluetoothNotAvailable" in text or "denied" in low or "not authorized" in low
        ):
            self._permission_logged = True
            logger.error("%s (%s)", PERMISSION_HELP, text)

    async def connect(self, mac: str, max_retries: int = 3) -> bool:
        mac = self.normalize_address(mac)
        async with self._operation_lock:
            self._cancel_connect = False
            if self.is_connected:
                if self._connected_mac == mac:
                    return True
                await self._disconnect_internal()
            self._status.state = ConnectionState.CONNECTING
            self._status.error = None
            for attempt in range(max_retries):
                if self._cancel_connect:
                    break
                try:
                    await self._connect_once(mac)
                except Exception as e:
                    logger.warning(
                        "Connection attempt %d/%d failed: %s", attempt + 1, max_retries, e
                    )
                    self._note_failure(e)
                    await self._discard_client()
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                else:
                    self._mark_connected(mac)
                    return True
            cancelled = self._cancel_connect
            self._status.state = (
                ConnectionState.DISCONNECTED if cancelled else ConnectionState.ERROR
            )
            self._status.error = (
                None if cancelled else f"Connection failed after {max_retries} attempts"
            )
            return False

    async def ensure_connected(
        self,
        mac: str,
        pin: int | None = None,  # noqa: ARG002 - backend protocol signature
        *,
        user_initiated: bool = False,  # noqa: ARG002 - accepted and ignored
    ) -> EnsureConnectedResult:
        mac = self.normalize_address(mac)
        async with self._operation_lock:
            if self.is_connected and self._connected_mac == mac and self._notify_ready:
                return EnsureConnectedResult(success=True, stage="already_connected")
            if self.is_connected:
                await self._disconnect_internal()
            self._cancel_connect = False
            self._status.state = ConnectionState.CONNECTING
            self._status.error = None
            try:
                await self._connect_once(mac)
            except Exception as e:
                return await self._fail("connect", e)
            self._mark_connected(mac)
            try:
                await self._start_notify_locked()
            except Exception as e:
                return await self._fail("gatt", e)
            return EnsureConnectedResult(success=True, stage="connected")

    async def _fail(self, stage: str, exc: Exception) -> EnsureConnectedResult:
        logger.warning("ensure_connected failed at %s: %s", stage, exc)
        self._note_failure(exc)
        await self._discard_client()
        self._connected_mac = None
        self._status.device = None
        self._status.state = ConnectionState.ERROR
        self._status.error = str(exc)
        return EnsureConnectedResult(
            success=False, stage=stage, error_name=type(exc).__name__, error_text=str(exc)
        )

    async def disconnect(self) -> bool:
        self._cancel_connect = True
        async with self._operation_lock:
            return await self._disconnect_internal()

    async def _disconnect_internal(self) -> bool:
        if self._status.state == ConnectionState.DISCONNECTED:
            return True
        self._status.state = ConnectionState.DISCONNECTING
        for attr in ("_keepalive_task", "_dst_check_task"):
            task = getattr(self, attr)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            setattr(self, attr, None)
        client = self._client
        if client is not None:
            with contextlib.suppress(Exception):
                await client.stop_notify(self.read_uuid)
        await self._discard_client()  # cancelPeripheralConnection on macOS
        self._connected_mac = None
        self._status.device = None
        self._last_a0_write_monotonic = None
        self._status.state = ConnectionState.DISCONNECTED
        return True

    async def start_notify(self) -> None:
        await self._start_notify_locked()

    async def _start_notify_locked(self) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("Not connected")
        self._loop = asyncio.get_running_loop()
        await client.start_notify(self.read_uuid, self._on_raw_notification)
        self._notify_ready = True
        early, self._early = self._early, []
        for data in early:
            self._deliver(data)
        mtu = getattr(client, "mtu_size", None)
        if mtu:
            logger.info("BLE MTU: %s", mtu)

    # -- callbacks ----------------------------------------------------------

    def _on_raw_notification(self, _char: Any, data: bytearray) -> None:
        payload = bytes(data)
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._on_notification, payload)
        else:
            self._on_notification(payload)

    def _on_notification(self, payload: bytes) -> None:
        if not self._notify_ready:
            self._early.append(payload)
            return
        self._deliver(payload)

    def _deliver(self, payload: bytes) -> None:
        if self.notification_callback is None:
            return
        try:
            self.notification_callback(payload)
        except Exception:
            logger.exception("Notification callback error")

    def _on_bleak_disconnect(self, client: Any) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._handle_bleak_disconnect, client)
        else:
            self._handle_bleak_disconnect(client)

    def _handle_bleak_disconnect(self, client: Any) -> None:
        # Our own disconnect()/failure cleanup moves state off CONNECTED first.
        if client is not self._client or self._status.state != ConnectionState.CONNECTED:
            return
        self._on_disconnect_detected()
