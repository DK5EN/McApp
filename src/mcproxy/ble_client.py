"""
BLE Client Abstraction Layer

This module provides a unified interface for BLE operations, supporting
multiple backends:
- local: Direct D-Bus/BlueZ (for running on Pi with Bluetooth hardware)
- remote: HTTP/SSE client (for remote BLE service)
- disabled: No-op stub (for testing without BLE)

Usage:
    from ble_client import create_ble_client, BLEMode

    # Create client based on config
    client = await create_ble_client(
        mode=BLEMode.REMOTE,
        remote_url="http://pi.local:8081",
        api_key="secret"
    )

    # Use unified API
    await client.connect("AA:BB:CC:DD:EE:FF")
    await client.send_message("Hello!", "20")
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class BLEMode(Enum):
    """BLE operation modes"""
    LOCAL = "local"       # Direct D-Bus/BlueZ
    REMOTE = "remote"     # HTTP/SSE to remote service
    DISABLED = "disabled" # No-op stub


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


@dataclass
class BLEStatus:
    """Current BLE status"""
    state: ConnectionState = ConnectionState.DISCONNECTED
    device_address: str | None = None
    device_name: str | None = None
    error: str | None = None
    mode: BLEMode = BLEMode.DISABLED


class BLEClientBase(ABC):
    """
    Abstract base class for BLE client implementations.

    All BLE operations go through this interface, allowing different
    backends to be swapped transparently.
    """

    def __init__(self, notification_callback: Callable[[dict], None] | None = None):
        """
        Initialize BLE client.

        Args:
            notification_callback: Async function called when BLE notification received.
                                   Receives parsed notification dict.
        """
        self.notification_callback = notification_callback
        self._status = BLEStatus()

    @property
    def status(self) -> BLEStatus:
        """Get current BLE status"""
        return self._status

    @property
    def is_connected(self) -> bool:
        """Check if connected to a device"""
        return self._status.state == ConnectionState.CONNECTED

    @abstractmethod
    async def scan(self, timeout: float = 5.0, prefix: str = "MC-") -> list[BLEDevice]:
        """
        Scan for BLE devices.

        Args:
            timeout: Scan duration in seconds
            prefix: Device name prefix filter

        Returns:
            List of discovered devices
        """
        pass

    @abstractmethod
    async def connect(self, mac: str) -> bool:
        """
        Connect to a BLE device.

        Args:
            mac: Device MAC address

        Returns:
            True if connection successful
        """
        pass

    @abstractmethod
    async def disconnect(self) -> bool:
        """
        Disconnect from current device.

        Returns:
            True if disconnection successful
        """
        pass

    @abstractmethod
    async def pair(self, mac: str) -> bool:
        """
        Pair with a BLE device.

        Args:
            mac: Device MAC address

        Returns:
            True if pairing successful
        """
        pass

    @abstractmethod
    async def unpair(self, mac: str) -> bool:
        """
        Remove pairing with a device.

        Args:
            mac: Device MAC address

        Returns:
            True if unpairing successful
        """
        pass

    @abstractmethod
    async def send_message(self, msg: str, group: str) -> bool:
        """
        Send a message to a MeshCom group.

        Args:
            msg: Message text
            group: Target group number or callsign

        Returns:
            True if send successful
        """
        pass

    @abstractmethod
    async def send_command(self, cmd: str) -> bool:
        """
        Send an A0 command to device.

        Args:
            cmd: Command string (e.g., "--pos info")

        Returns:
            True if send successful
        """
        pass

    @abstractmethod
    async def set_command(self, cmd: str) -> bool:
        """
        Send a set command to device.

        Args:
            cmd: Set command string (e.g., "--settime")

        Returns:
            True if send successful
        """
        pass

    @abstractmethod
    async def start(self) -> None:
        """
        Start the BLE client (connect notification handlers, etc.)
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the BLE client and clean up resources.
        """
        pass


async def create_ble_client(
    mode: BLEMode = BLEMode.DISABLED,
    notification_callback: Callable[[dict], None] | None = None,
    # Local mode options
    device_mac: str | None = None,
    # Remote mode options
    remote_url: str | None = None,
    api_key: str | None = None,
    # Message router for publishing
    message_router=None,
) -> BLEClientBase:
    """
    Factory function to create appropriate BLE client based on mode.

    Args:
        mode: BLE operation mode
        notification_callback: Callback for BLE notifications
        device_mac: Device MAC address (for auto-connect)
        remote_url: URL of remote BLE service
        api_key: API key for remote service
        message_router: MessageRouter instance for publishing status

    Returns:
        Configured BLE client instance
    """
    if mode == BLEMode.LOCAL:
        from .ble_client_local import BLEClientLocal
        client = BLEClientLocal(
            notification_callback=notification_callback,
            message_router=message_router,
            device_mac=device_mac,
        )
        logger.info("Created local BLE client (D-Bus/BlueZ)")

    elif mode == BLEMode.REMOTE:
        if not remote_url:
            raise ValueError("remote_url required for remote mode")
        from .ble_client_remote import BLEClientRemote
        client = BLEClientRemote(
            remote_url=remote_url,
            api_key=api_key,
            notification_callback=notification_callback,
            message_router=message_router,
        )
        logger.info("Created remote BLE client (HTTP/SSE) -> %s", remote_url)

    else:  # DISABLED
        from .ble_client_disabled import BLEClientDisabled
        client = BLEClientDisabled(
            notification_callback=notification_callback,
            message_router=message_router,
        )
        logger.info("Created disabled BLE client (no-op)")

    return client
