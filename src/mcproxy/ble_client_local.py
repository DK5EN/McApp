"""
Local BLE Client - D-Bus/BlueZ implementation.

This module wraps the existing ble_handler.py functionality behind
the BLEClientBase interface for local Bluetooth access.
"""

import asyncio
import logging
import time
from typing import Callable

from .ble_client import BLEClientBase, BLEDevice, BLEMode, BLEStatus, ConnectionState

# Import existing BLE handler functions
from .ble_handler import (
    BLEClient as LegacyBLEClient,
    ble_connect as legacy_ble_connect,
    ble_disconnect as legacy_ble_disconnect,
    ble_pair as legacy_ble_pair,
    ble_unpair as legacy_ble_unpair,
    scan_ble_devices as legacy_scan_ble_devices,
    handle_ble_message as legacy_handle_ble_message,
    handle_a0_command as legacy_handle_a0_command,
    handle_set_command as legacy_handle_set_command,
    get_ble_client,
)

logger = logging.getLogger(__name__)


class BLEClientLocal(BLEClientBase):
    """
    Local BLE client using D-Bus/BlueZ.

    This wraps the existing ble_handler.py implementation to provide
    the unified BLEClientBase interface.
    """

    def __init__(
        self,
        notification_callback: Callable[[dict], None] | None = None,
        message_router=None,
        device_mac: str | None = None,
    ):
        super().__init__(notification_callback)
        self.message_router = message_router
        self.device_mac = device_mac
        self._status.mode = BLEMode.LOCAL

    async def scan(self, timeout: float = 5.0, prefix: str = "MC-") -> list[BLEDevice]:
        """Scan for BLE devices using BlueZ"""
        # The legacy scan function uses message_router for results
        # We'll need to capture them differently here
        # For now, delegate to the legacy function
        await legacy_scan_ble_devices(message_router=self.message_router)

        # The legacy implementation sends results via websocket
        # Return empty list - results come through message router
        return []

    async def connect(self, mac: str) -> bool:
        """Connect to device using legacy BLE handler"""
        try:
            await legacy_ble_connect(mac, message_router=self.message_router)

            # Check if connected
            client = get_ble_client()
            if client and client._connected:
                self._status.state = ConnectionState.CONNECTED
                self._status.device_address = mac
                return True
            else:
                self._status.state = ConnectionState.ERROR
                self._status.error = "Connection failed"
                return False

        except Exception as e:
            logger.error("Connect error: %s", e)
            self._status.state = ConnectionState.ERROR
            self._status.error = str(e)
            return False

    async def disconnect(self) -> bool:
        """Disconnect using legacy BLE handler"""
        try:
            await legacy_ble_disconnect(message_router=self.message_router)
            self._status.state = ConnectionState.DISCONNECTED
            self._status.device_address = None
            return True
        except Exception as e:
            logger.error("Disconnect error: %s", e)
            return False

    async def pair(self, mac: str) -> bool:
        """Pair with device using legacy handler"""
        try:
            # Legacy pair function doesn't return success status
            # We need to check manually
            await legacy_ble_pair(mac, None, message_router=self.message_router)
            return True
        except Exception as e:
            logger.error("Pair error: %s", e)
            return False

    async def unpair(self, mac: str) -> bool:
        """Unpair device using legacy handler"""
        try:
            await legacy_ble_unpair(mac, message_router=self.message_router)
            return True
        except Exception as e:
            logger.error("Unpair error: %s", e)
            return False

    async def send_message(self, msg: str, group: str) -> bool:
        """Send message through BLE"""
        try:
            await legacy_handle_ble_message(msg, group)
            return True
        except Exception as e:
            logger.error("Send message error: %s", e)
            return False

    async def send_command(self, cmd: str) -> bool:
        """Send A0 command through BLE"""
        try:
            await legacy_handle_a0_command(cmd)
            return True
        except Exception as e:
            logger.error("Send command error: %s", e)
            return False

    async def set_command(self, cmd: str) -> bool:
        """Send set command through BLE"""
        try:
            await legacy_handle_set_command(cmd)
            return True
        except Exception as e:
            logger.error("Set command error: %s", e)
            return False

    async def start(self) -> None:
        """Start local BLE client"""
        logger.info("Starting local BLE client")
        # The legacy handler manages its own state
        # Auto-connect if device_mac is set
        if self.device_mac:
            logger.info("Auto-connecting to %s", self.device_mac)
            await self.connect(self.device_mac)

    async def stop(self) -> None:
        """Stop local BLE client"""
        logger.info("Stopping local BLE client")
        if self.is_connected:
            await self.disconnect()

    @property
    def is_connected(self) -> bool:
        """Check connection status via legacy client"""
        client = get_ble_client()
        if client:
            connected = client._connected
            if connected:
                self._status.state = ConnectionState.CONNECTED
            return connected
        return False
