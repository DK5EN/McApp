"""
BLE Adapter - D-Bus/BlueZ interface for BLE device communication.

This module provides a clean async interface to BlueZ via D-Bus,
handling device discovery, connection, GATT operations, and notifications.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.errors import DBusError, InterfaceNotFoundError
from dbus_next.service import ServiceInterface, method

logger = logging.getLogger(__name__)

# D-Bus constants
BLUEZ_SERVICE_NAME = "org.bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/com/mcproxy/agent"

# Nordic UART Service UUIDs
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write to device
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Read from device (notify)


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


class NoInputNoOutputAgent(ServiceInterface):
    """BlueZ pairing agent for headless operation"""

    def __init__(self):
        super().__init__('org.bluez.Agent1')

    @method()
    def Release(self):
        logger.info("Agent released")

    @method()
    def RequestPasskey(self, device: 'o') -> 'u':  # noqa: F821
        logger.info("Passkey requested for %s", device)
        return 0

    @method()
    def RequestPinCode(self, device: 'o') -> 's':  # noqa: F821
        logger.info("PIN requested for %s", device)
        return "000000"

    @method()
    def DisplayPinCode(self, device: 'o', pincode: 's'):  # noqa: F821
        logger.info("DisplayPinCode for %s: %s", device, pincode)

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):  # noqa: F821
        logger.info("Confirm passkey %s for %s", passkey, device)
        return

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):  # noqa: F821
        logger.info("Authorize service %s for %s", uuid, device)
        return

    @method()
    def Cancel(self):
        logger.info("Request cancelled")


class BLEAdapter:
    """
    Async BLE adapter using BlueZ D-Bus interface.

    Provides methods for device discovery, connection management,
    and GATT characteristic read/write/notify operations.
    """

    def __init__(
        self,
        read_uuid: str = NUS_TX_UUID,
        write_uuid: str = NUS_RX_UUID,
        hello_bytes: bytes = b'\x04\x10\x20\x30',
        notification_callback: Callable[[bytes], None] | None = None
    ):
        self.read_uuid = read_uuid
        self.write_uuid = write_uuid
        self.hello_bytes = hello_bytes
        self.notification_callback = notification_callback

        # D-Bus objects
        self.bus: MessageBus | None = None
        self.device_obj = None
        self.dev_iface = None
        self.props_iface = None
        self.read_char_obj = None
        self.read_char_iface = None
        self.read_props_iface = None
        self.write_char_iface = None

        # State
        self._status = BLEStatus()
        self._operation_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._connected_mac: str | None = None
        self._agent_registered: bool = False
        self._cancel_connect: bool = False

    @property
    def status(self) -> BLEStatus:
        """Get current adapter status"""
        return self._status

    @property
    def is_connected(self) -> bool:
        """Check if connected to a device"""
        return self._status.state == ConnectionState.CONNECTED

    def _mac_to_dbus_path(self, mac: str) -> str:
        """Convert MAC address to D-Bus device path"""
        return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"

    async def _ensure_bus(self):
        """Ensure D-Bus connection is established"""
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def scan(self, timeout: float = 5.0, prefix: str = "MC-") -> list[BLEDevice]:
        """
        Scan for BLE devices with optional name prefix filter.

        Args:
            timeout: Scan duration in seconds
            prefix: Device name prefix to filter (default: "MC-" for MeshCom)

        Returns:
            List of discovered BLEDevice objects
        """
        async with self._operation_lock:
            await self._ensure_bus()

            found_devices: dict[str, BLEDevice] = {}
            known_devices: list[BLEDevice] = []

            # Get adapter
            path = "/org/bluez/hci0"
            introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
            adapter_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)
            adapter = adapter_obj.get_interface(ADAPTER_INTERFACE)

            # Get object manager for existing devices
            obj_mgr = self.bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, "/",
                await self.bus.introspect(BLUEZ_SERVICE_NAME, "/")
            )
            obj_mgr_iface = obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)

            # Check known/paired devices first
            objects = await obj_mgr_iface.call_get_managed_objects()
            for obj_path, interfaces in objects.items():
                if DEVICE_INTERFACE in interfaces:
                    props = interfaces[DEVICE_INTERFACE]
                    name = props.get("Name", Variant("s", "")).value
                    addr = props.get("Address", Variant("s", "")).value
                    paired = props.get("Paired", Variant("b", False)).value
                    rssi = props.get("RSSI", Variant("n", 0)).value

                    if name.startswith(prefix):
                        device = BLEDevice(
                            name=name,
                            address=addr,
                            rssi=rssi,
                            paired=paired,
                            known=True,
                            path=obj_path
                        )
                        known_devices.append(device)

            # Setup handler for new devices during scan
            async def on_interfaces_added(path, interfaces):
                if DEVICE_INTERFACE in interfaces:
                    props = interfaces[DEVICE_INTERFACE]
                    name = props.get("Name", Variant("s", "")).value
                    if name.startswith(prefix):
                        addr = props.get("Address", Variant("s", "")).value
                        rssi = props.get("RSSI", Variant("n", 0)).value
                        found_devices[path] = BLEDevice(
                            name=name,
                            address=addr,
                            rssi=rssi,
                            paired=False,
                            path=path
                        )

            def on_interfaces_added_sync(path, interfaces):
                asyncio.create_task(on_interfaces_added(path, interfaces))

            obj_mgr_iface.on_interfaces_added(on_interfaces_added_sync)

            # Start discovery
            logger.info("Starting BLE scan (timeout=%.1fs, prefix='%s')", timeout, prefix)
            await adapter.call_start_discovery()

            try:
                await asyncio.sleep(timeout)
            finally:
                await adapter.call_stop_discovery()

            # Combine results
            all_devices = known_devices + list(found_devices.values())
            logger.info("Scan complete: %d known, %d discovered",
                       len(known_devices), len(found_devices))

            return all_devices

    async def connect(self, mac: str, max_retries: int = 3) -> bool:
        """
        Connect to a BLE device by MAC address.

        Args:
            mac: Device MAC address (e.g., "AA:BB:CC:DD:EE:FF")
            max_retries: Number of connection attempts

        Returns:
            True if connection successful
        """
        async with self._operation_lock:
            self._cancel_connect = False

            if self.is_connected:
                if self._connected_mac == mac:
                    logger.info("Already connected to %s", mac)
                    return True
                else:
                    logger.warning("Connected to different device, disconnecting first")
                    await self._disconnect_internal()

            self._status.state = ConnectionState.CONNECTING
            self._status.error = None
            path = self._mac_to_dbus_path(mac)

            for attempt in range(max_retries):
                if self._cancel_connect:
                    logger.info("Connection cancelled by disconnect request")
                    break

                try:
                    await self._attempt_connection(mac, path)
                    self._connected_mac = mac
                    self._status.state = ConnectionState.CONNECTED
                    self._status.device = BLEDevice(name="", address=mac)
                    self._status.last_activity = time.time()

                    # Start keepalive
                    self._start_keepalive()

                    logger.info("Connected to %s", mac)
                    return True

                except Exception as e:
                    logger.warning("Connection attempt %d/%d failed: %s",
                                 attempt + 1, max_retries, e)
                    if attempt < max_retries - 1:
                        await self._cleanup_failed_connection()
                        await asyncio.sleep(1)

            await self._cleanup_failed_connection()

            if self._cancel_connect:
                self._status.state = ConnectionState.DISCONNECTED
                self._status.error = None
            else:
                self._status.state = ConnectionState.ERROR
                self._status.error = f"Connection failed after {max_retries} attempts"
            return False

    async def _attempt_connection(self, mac: str, path: str):
        """Single connection attempt with stale BlueZ state handling"""
        await self._ensure_bus()

        introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
        self.device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)

        try:
            self.dev_iface = self.device_obj.get_interface(DEVICE_INTERFACE)
        except InterfaceNotFoundError as e:
            raise ConnectionError(f"Device not found or not paired: {e}") from e

        self.props_iface = self.device_obj.get_interface(PROPERTIES_INTERFACE)

        # Check if BlueZ has stale connection (e.g. after device hard reboot)
        try:
            connected = (await self.props_iface.call_get(
                DEVICE_INTERFACE, "Connected"
            )).value
            if connected:
                logger.warning("BlueZ reports connected (possibly stale), forcing disconnect")
                try:
                    await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=5.0)
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        except Exception:
            pass

        # Attempt connection
        try:
            await asyncio.wait_for(self.dev_iface.call_connect(), timeout=10.0)
        except asyncio.TimeoutError:
            raise ConnectionError("Connection timeout after 10 seconds")
        except DBusError as e:
            if "In Progress" in str(e):
                # BlueZ has a pending connection from a previous attempt
                logger.warning("Stale 'In Progress' in BlueZ, clearing before retry")
                try:
                    await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=3.0)
                except Exception:
                    pass
                raise ConnectionError("Cleared stale BlueZ state, will retry") from e
            raise ConnectionError(f"Connect failed: {e}") from e

        # Wait for services to resolve
        if not await self._wait_for_services_resolved(timeout=10.0):
            raise ConnectionError("Services not resolved within timeout")

        # Find GATT characteristics
        await self._find_characteristics(path)

        if not self.read_char_iface or not self.write_char_iface:
            raise ConnectionError("Required GATT characteristics not found")

        self.read_props_iface = self.read_char_obj.get_interface(PROPERTIES_INTERFACE)

    async def _wait_for_services_resolved(self, timeout: float = 10.0) -> bool:
        """Wait for BLE services to be discovered"""
        start = time.time()

        while (time.time() - start) < timeout:
            try:
                resolved = (await self.props_iface.call_get(
                    DEVICE_INTERFACE, "ServicesResolved"
                )).value
                if resolved:
                    return True
                await asyncio.sleep(0.5)
            except DBusError:
                await asyncio.sleep(0.5)

        return False

    async def _find_characteristics(self, device_path: str):
        """Find read and write GATT characteristics"""
        self.read_char_obj, self.read_char_iface = await self._find_gatt_characteristic(
            device_path, self.read_uuid
        )
        _, self.write_char_iface = await self._find_gatt_characteristic(
            device_path, self.write_uuid
        )

    async def _find_gatt_characteristic(self, path: str, target_uuid: str):
        """Recursively find GATT characteristic by UUID"""
        try:
            introspect = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
        except Exception:
            return None, None

        for node in introspect.nodes:
            child_path = f"{path}/{node.name}"
            try:
                child_introspect = await self.bus.introspect(BLUEZ_SERVICE_NAME, child_path)
                child_obj = self.bus.get_proxy_object(
                    BLUEZ_SERVICE_NAME, child_path, child_introspect
                )

                props_iface = child_obj.get_interface(PROPERTIES_INTERFACE)
                props = await props_iface.call_get_all(GATT_CHARACTERISTIC_INTERFACE)

                uuid = props.get("UUID").value.lower()
                if uuid == target_uuid.lower():
                    char_iface = child_obj.get_interface(GATT_CHARACTERISTIC_INTERFACE)
                    return child_obj, char_iface

            except Exception:
                # Recursive search
                obj, iface = await self._find_gatt_characteristic(child_path, target_uuid)
                if iface:
                    return obj, iface

        return None, None

    async def _cleanup_failed_connection(self):
        """Clean up after failed connection — guaranteed to reset state"""
        try:
            if self.dev_iface:
                try:
                    await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=3.0)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Cleanup error: %s", e)
        finally:
            if self.bus:
                try:
                    self.bus.disconnect()
                except Exception:
                    pass
            self._reset_state()

    def _reset_state(self):
        """Reset all state variables"""
        self.bus = None
        self.device_obj = None
        self.dev_iface = None
        self.props_iface = None
        self.read_char_obj = None
        self.read_char_iface = None
        self.read_props_iface = None
        self.write_char_iface = None
        self._connected_mac = None
        self._agent_registered = False

    async def disconnect(self) -> bool:
        """Disconnect from current device (also cancels in-progress connections)"""
        self._cancel_connect = True
        async with self._operation_lock:
            return await self._disconnect_internal()

    async def _disconnect_internal(self) -> bool:
        """Internal disconnect without lock"""
        if self._status.state == ConnectionState.DISCONNECTED:
            return True

        self._status.state = ConnectionState.DISCONNECTING

        # Stop keepalive
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None

        # Stop notifications
        try:
            await self._stop_notify()
        except Exception as e:
            logger.warning("Error stopping notifications: %s", e)

        # Disconnect
        try:
            if self.dev_iface:
                await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=3.0)
        except Exception as e:
            logger.warning("Disconnect error: %s", e)

        # Clean up
        if self.bus:
            try:
                self.bus.disconnect()
            except Exception:
                pass

        self._reset_state()
        self._status.state = ConnectionState.DISCONNECTED
        self._status.device = None

        logger.info("Disconnected")
        return True

    async def start_notify(self):
        """Start receiving notifications from device"""
        if not self.is_connected or not self.read_char_iface:
            raise RuntimeError("Not connected")

        # Check if already notifying
        is_notifying = (await self.read_props_iface.call_get(
            GATT_CHARACTERISTIC_INTERFACE, "Notifying"
        )).value

        if is_notifying:
            logger.info("Already notifying")
            return

        # Setup notification handler
        self.read_props_iface.on_properties_changed(self._on_props_changed)
        await self.read_char_iface.call_start_notify()

        logger.info("Notifications started")

    async def _stop_notify(self):
        """Stop notifications"""
        if not self.read_char_iface:
            return

        try:
            if self.read_props_iface:
                self.read_props_iface.off_properties_changed(self._on_props_changed)
        except Exception:
            pass

        try:
            await self.read_char_iface.call_stop_notify()
        except DBusError as e:
            if "No notify session started" not in str(e):
                raise

    async def _on_props_changed(self, iface: str, changed: dict, invalidated: list):
        """Handle property changes (notifications)"""
        if iface != GATT_CHARACTERISTIC_INTERFACE:
            return

        if "Value" in changed:
            value = bytes(changed["Value"].value)
            self._status.last_activity = time.time()

            if self.notification_callback:
                try:
                    self.notification_callback(value)
                except Exception as e:
                    logger.error("Notification callback error: %s", e)

    async def write(self, data: bytes) -> bool:
        """
        Write data to device.

        Args:
            data: Raw bytes to write

        Returns:
            True if write successful
        """
        if not self.is_connected or not self.write_char_iface:
            raise RuntimeError("Not connected")

        try:
            await asyncio.wait_for(
                self.write_char_iface.call_write_value(data, {}),
                timeout=5.0
            )
            self._status.last_activity = time.time()
            return True
        except asyncio.TimeoutError:
            logger.error("Write timeout")
            return False
        except Exception as e:
            logger.error("Write error: %s", e)
            return False

    async def send_message(self, msg: str, group: str) -> bool:
        """
        Send a message to a MeshCom group.

        Args:
            msg: Message text
            group: Target group number or callsign

        Returns:
            True if send successful
        """
        message = "{" + group + "}" + msg
        byte_array = bytearray(message.encode('utf-8'))
        length = len(byte_array) + 2
        byte_array = length.to_bytes(1, 'big') + bytes([0xA0]) + byte_array

        return await self.write(bytes(byte_array))

    async def send_hello(self) -> bool:
        """Send hello/wakeup command to device"""
        if not self.is_connected:
            return False
        return await self.write(self.hello_bytes)

    async def send_command(self, cmd: str) -> bool:
        """
        Send an A0 command to device.

        Args:
            cmd: Command string (e.g., "--pos info", "--reboot")

        Returns:
            True if send successful
        """
        byte_array = bytearray(cmd.encode('utf-8'))
        length = len(byte_array) + 2
        byte_array = length.to_bytes(1, 'big') + bytes([0xA0]) + byte_array

        return await self.write(bytes(byte_array))

    async def set_time(self) -> bool:
        """Set current time on device"""
        now = int(time.time())
        data = 6 .to_bytes(1, 'big') + bytes([0x20]) + now.to_bytes(4, byteorder='little')
        return await self.write(data)

    def _start_keepalive(self):
        """Start keepalive task"""
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        """Send periodic keepalive commands"""
        try:
            while self.is_connected:
                await asyncio.sleep(300)  # 5 minutes
                if self.is_connected:
                    logger.debug("Sending keepalive")
                    await self.send_command("--pos info")
        except asyncio.CancelledError:
            pass

    async def pair(self, mac: str) -> bool:
        """
        Pair with a BLE device.

        Args:
            mac: Device MAC address

        Returns:
            True if pairing successful
        """
        async with self._operation_lock:
            await self._ensure_bus()

            path = self._mac_to_dbus_path(mac)

            # Register agent (once per bus lifetime)
            if not self._agent_registered:
                agent = NoInputNoOutputAgent()
                self.bus.export(AGENT_PATH, agent)

                manager_obj = self.bus.get_proxy_object(
                    BLUEZ_SERVICE_NAME, "/org/bluez",
                    await self.bus.introspect(BLUEZ_SERVICE_NAME, "/org/bluez")
                )
                agent_manager = manager_obj.get_interface("org.bluez.AgentManager1")
                await agent_manager.call_register_agent(AGENT_PATH, "KeyboardDisplay")
                await agent_manager.call_request_default_agent(AGENT_PATH)
                self._agent_registered = True

            # Pair
            dev_obj = self.bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, path,
                await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
            )

            try:
                dev_iface = dev_obj.get_interface(DEVICE_INTERFACE)
            except InterfaceNotFoundError:
                logger.error("Device not found: %s", mac)
                return False

            try:
                await dev_iface.call_pair()
                await dev_iface.set_trusted(True)

                is_paired = await dev_iface.get_paired()
                logger.info("Paired with %s: %s", mac, is_paired)

                # Disconnect after pairing
                await asyncio.sleep(2)
                try:
                    await dev_iface.call_disconnect()
                except Exception:
                    pass

                return is_paired

            except Exception as e:
                logger.error("Pairing failed: %s", e)
                return False

    async def unpair(self, mac: str) -> bool:
        """
        Remove pairing with a device.

        Args:
            mac: Device MAC address

        Returns:
            True if unpairing successful
        """
        async with self._operation_lock:
            await self._ensure_bus()

            device_path = self._mac_to_dbus_path(mac)
            adapter_path = "/org/bluez/hci0"

            adapter_obj = self.bus.get_proxy_object(
                BLUEZ_SERVICE_NAME, adapter_path,
                await self.bus.introspect(BLUEZ_SERVICE_NAME, adapter_path)
            )
            adapter_iface = adapter_obj.get_interface(ADAPTER_INTERFACE)

            try:
                await adapter_iface.call_remove_device(device_path)
                logger.info("Unpaired device: %s", mac)
                return True
            except DBusError as e:
                logger.error("Unpair failed: %s", e)
                return False
