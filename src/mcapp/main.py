#!/usr/bin/env python3
import asyncio
import json
import os
import re
import signal
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# BLE client abstraction - supports local, remote, and disabled modes
from .ble_client import BLEMode, ConnectionState, create_ble_client
from .commands import create_command_handler
from .config_loader import (
    BLE_SERVICE_URL,
    MESHCOM_UDP_PORT,
    SSE_HOST,
    SSE_PORT,
    Config,
    hours_to_dd_hhmm,
)

# New modular imports
from .logging_setup import get_logger, setup_logging
from .logging_setup import has_console as check_console
from .udp_handler import UDPHandler

# Optional imports for new features
try:
    from .sse_handler import create_sse_manager
    SSE_AVAILABLE = True
except ImportError:
    SSE_AVAILABLE = False

from . import __version__
from .sqlite_storage import SQLiteStorage, create_sqlite_storage

VERSION = f"v{__version__}"

# BLE Register Query Timing Constants (seconds)
BLE_HELLO_WAIT = 1.0                    # Wait after hello handshake before queries
BLE_QUERY_DELAY_STANDARD = 0.8         # Delay between standard register queries
BLE_QUERY_DELAY_MULTIPART = 1.2        # Delay for multi-part responses (SE+S1, SW+S2)
BLE_RETRY_BASE_DELAY = 0.5             # Base delay for exponential backoff retries

# Module logger
logger = get_logger(__name__)


def debug_signal_handler(signum, frame):
    """Print stack trace when USR1 signal received"""
    logger.info("=" * 60)
    logger.info("DEBUG: Stack trace at hang point:")
    logger.info("=" * 60)
    traceback.print_stack(frame)
    logger.info("=" * 60)


block_list = [
  "response",
  "OE0XXX-99",
]

class MessageRouter:
    def __init__(self, message_storage_handler=None):
        self._subscribers = defaultdict(list)
        self._protocols = {}
        self.storage_handler = message_storage_handler
        self.my_callsign = None
        self.validator = None
        self._logger = get_logger(f"{__name__}.MessageRouter")

        if message_storage_handler:
            self.subscribe('mesh_message', self._storage_handler)
            self.subscribe('ble_notification', self._storage_handler)

        self.subscribe('ble_message', self._ble_message_handler)
        self.subscribe('udp_message', self._udp_message_handler)

    def set_callsign(self, callsign):
        """Set the callsign from config"""
        self.my_callsign = callsign.upper()
        self.validator = MessageValidator(self.my_callsign)
        self._logger.info("Callsign set to '%s', validator initialized", self.my_callsign)

    # --- Publish Helper Methods ---
    async def publish_ble_status(self, command: str, result: str, msg: str):
        """Standardized BLE status publishing"""
        await self.publish('ble', 'ble_status', {
            'src_type': 'BLE',
            'TYP': 'blueZ',
            'command': command,
            'result': result,
            'msg': msg,
            'timestamp': int(time.time() * 1000)
        })

    async def publish_system_message(self, msg: str, msg_type: str = 'info'):
        """Publish system message to websocket clients"""
        await self.publish('system', 'websocket_message', {
            'src_type': 'system',
            'type': msg_type,
            'msg': msg,
            'timestamp': int(time.time() * 1000)
        })

    async def publish_error(self, msg: str, source: str = 'system'):
        """Publish error message to websocket clients"""
        await self.publish(source, 'websocket_message', {
            'src_type': 'system',
            'type': 'error',
            'msg': msg,
            'timestamp': int(time.time() * 1000)
        })


    def test_suppression_logic(self):
        """Test suppression logic based on the table scenarios"""
        self._logger.info("Testing Suppression Logic:")
        self._logger.info("=" * 50)

        test_cases = [
            # (src, dst, msg, expected_suppression, description)
            (self.my_callsign, "20", "!WX", True, "Group ohne Target → lokal"),
            (self.my_callsign, "20", "!WX OE5HWN-12", False, "Group mit anderem Target → senden"),
            (self.my_callsign, "20", f"!WX {self.my_callsign}",
             True, "Group mit meinem Target → lokal"),
            (self.my_callsign, "TEST", "!WX",
             True, "Test-Gruppe ohne Target → lokal"),
            (self.my_callsign, "TEST", "!WX OE5HWN-12",
             False, "Test-Gruppe mit anderem Target → senden"),
            (self.my_callsign, "OE5HWN-12", "!TIME",
             True, "Persönlich ohne Target → lokal"),
            (self.my_callsign, "OE5HWN-12", "!TIME OE5HWN-12",
             False, "Persönlich mit Target (gleich dst) → senden"),
            (self.my_callsign, "OE5HWN-12", f"!TIME {self.my_callsign}",
             True, "Persönlich mit Target (ich) → lokal"),
            (self.my_callsign, "*", "!WX", True, "Ungültiges Ziel → suppress"),
            (self.my_callsign, "ALL", "!WX", True, "Ungültiges Ziel → suppress"),
            ("OE5HWN-12", "20", "!WX", False, "Nicht unsere Message → nicht suppessen"),
        ]

        results = []
        for src, dst, msg, expected, description in test_cases:
            test_data = {'src': src, 'dst': dst, 'msg': msg}
            normalized = self.validator.normalize_message_data(test_data)
            actual = self.validator.should_suppress_outbound(normalized)

            status = "✅ PASS" if actual == expected else "❌ FAIL"
            reason = self.validator.get_suppression_reason(normalized)

            results.append((status, description, actual, expected, reason))

            if has_console:
                print(f"{status} | {description}")
                print(f"     {src}→{dst} '{msg}' → {actual} (expected: {expected})")
                print(f"     Reason: {reason}")
                print()

        # Summary
        passed = sum(1 for r in results if r[0].startswith("✅"))
        total = len(results)

        if has_console:
            print(f"🧪 Test Summary: {passed}/{total} tests passed")
            if passed == total:
                print("🎉 All suppression tests passed!")
            else:
                print("⚠️ Some tests failed - check logic!")
            print("=" * 50)

        return passed == total

    def log_message_routing_decision(self, message_data, decision_type, action, reason):
        """Centralized logging for message routing decisions"""
        if not has_console:
            return

        src = message_data.get('src', 'unknown')
        dst = message_data.get('dst', 'unknown')
        raw_msg = message_data.get('msg', '')
        msg = raw_msg[:20] + ('...' if len(raw_msg) > 20 else '')

        print(f"🔄 {decision_type}: {src}→{dst} '{msg}' → {action} ({reason})")

    async def _storage_handler(self, routed_message):
        """Handle message storage for all routed messages"""
        if self.storage_handler:
            message_data = routed_message['data']

            src = message_data.get('src', '').split(',')[0].upper()
            if self._is_callsign_blocked(src):
                if has_console:
                    print(f"🚫 Blocked message from {src}")
                return

            raw_json = json.dumps(message_data)
            await self.storage_handler.store_message(message_data, raw_json)

    def _is_callsign_blocked(self, callsign):
        """Check if callsign is blocked"""
        # Get blocked list from CommandHandler
        command_handler = self.get_protocol('commands')
        if hasattr(command_handler, 'blocked_callsigns'):
            return callsign in command_handler.blocked_callsigns
        return False

    def register_protocol(self, name: str, handler):
        """Register a protocol handler (UDP, BLE, WebSocket)"""
        self._protocols[name] = handler
        self._logger.info("Registered protocol '%s'", name)

    def subscribe(self, message_type: str, handler_func):
        """Subscribe to specific message types"""
        self._subscribers[message_type].append(handler_func)
        if has_console:
            print(f"MessageRouter: {handler_func.__name__} subscribed to '{message_type}'")

    async def publish(self, source: str, message_type: str, data: dict):
        """Publish message from one protocol to all subscribers"""
        # Add routing metadata
        routed_message = {
            'source': source,
            'type': message_type,
            'data': data,
            'timestamp': int(time.time() * 1000)
        }

        # Send to all subscribers of this message type
        for handler in self._subscribers[message_type]:
            try:
                await handler(routed_message)

            except Exception as e:
                self._logger.error(
                    "Failed to route %s to %s: %s",
                    message_type, handler.__name__, e, exc_info=True
                )

    def get_protocol(self, name: str):
        """Get a registered protocol handler"""
        return self._protocols.get(name)

    def list_subscriptions(self):
        """Debug: List all current subscriptions"""
        if has_console:
            print("MessageRouter subscriptions:")
            for msg_type, handlers in self._subscribers.items():
                handler_names = [h.__name__ for h in handlers]
                print(f"  {msg_type}: {handler_names}")

    async def route_command(
        self, command: str, websocket=None, MAC=None,
        BLE_Pin=None, data=None, **kwargs
    ):
      """Route commands to appropriate protocol handlers"""
      if has_console:
        print(f"MessageRouter: Routing command '{command}'")

      try:
        # Smart initial payload (paginated)
        if command == "smart_initial":
            await self._handle_smart_initial_command(websocket)

        elif command == "summary":
            await self._handle_summary_command(websocket)

        elif command == "get_messages_page":
            await self._handle_messages_page_command(websocket, data or {})

        # Message dump commands (legacy clients redirect to smart_initial)
        elif command in ["send message dump", "send pos dump"]:
            await self._handle_smart_initial_command(websocket)

        elif command == "mheard dump":
            await self._handle_mheard_dump_command(websocket)

        # BLE commands
        elif command == "scan BLE":
            await self._handle_ble_scan_command()

        elif command == "BLE info":
            await self._handle_ble_info_command(websocket)

        elif command == "pair BLE":
            await self._handle_ble_pair_command(MAC, BLE_Pin)

        elif command == "unpair BLE":
            await self._handle_ble_unpair_command(MAC)

        elif command == "disconnect BLE":
            await self._handle_ble_disconnect_command()

        elif command == "connect BLE":
            await self._handle_ble_connect_command(MAC, websocket)

        elif command == "resolve-ip":
            await self._handle_resolve_ip_command(MAC)

        # Device commands (--commands)
        elif command.startswith("--setboostedgain"):
            await self._handle_device_a0_command(command)

        elif command.startswith("--set") or command.startswith("--sym"):
            await self._handle_device_set_command(command)

        elif command.startswith("--"):
            await self._handle_device_a0_command(command)

        else:
            print(f"MessageRouter: Unknown command '{command}'")
            if websocket:
                error_msg = {
                    'src_type': 'system',
                    'type': 'error',
                    'msg': f"Unknown command: {command}",
                    'timestamp': int(time.time() * 1000)
                }
                await self.publish('router', 'websocket_message', error_msg)

      except Exception as e:
        print(f"MessageRouter ERROR: Failed to route command '{command}': {e}")
        if websocket:
            error_msg = {
                'src_type': 'system',
                'type': 'error',
                'msg': f"Command failed: {command} - {str(e)}",
                'timestamp': int(time.time() * 1000)
            }
            await self.publish('router', 'websocket_message', error_msg)

    async def _handle_smart_initial_command(self, websocket):
        """Handle smart initial payload - sends only last N messages per dst + summary."""
        if hasattr(self.storage_handler, 'get_smart_initial_with_summary'):
            initial_data, summary = (
                await self.storage_handler.get_smart_initial_with_summary()
            )
        else:
            initial_data = await self.storage_handler.get_smart_initial()
            summary = await self.storage_handler.get_summary()
        acks_list = initial_data.get("acks", [])

        if has_console:
            print(f"📦 smart_initial sending: "
                  f"{len(initial_data['messages'])} msgs, "
                  f"{len(initial_data['positions'])} pos, "
                  f"{len(acks_list)} acks")
            if acks_list:
                for a in acks_list[:5]:
                    print(f"  ACK: {a[:120]}")

        payload = {
            "type": "response",
            "msg": "smart_initial",
            "data": {
                "messages": initial_data["messages"],
                "positions": initial_data["positions"],
                "acks": acks_list,
            },
        }
        await self.publish(
            'router', 'websocket_direct', {'websocket': websocket, 'data': payload}
        )
        summary_payload = {
            "type": "response",
            "msg": "summary",
            "data": summary,
        }
        if websocket:
            await self.publish(
                'router', 'websocket_direct',
                {'websocket': websocket, 'data': summary_payload},
            )
        else:
            await self.publish('router', 'websocket_message', summary_payload)

        # Send persisted read counts for unread badge sync
        if hasattr(self.storage_handler, 'get_read_counts'):
            read_counts = await self.storage_handler.get_read_counts()
            if read_counts:
                rc_payload = {
                    "type": "response",
                    "msg": "read_counts",
                    "data": read_counts,
                }
                if websocket:
                    await self.publish(
                        'router', 'websocket_direct',
                        {'websocket': websocket, 'data': rc_payload},
                    )
                else:
                    await self.publish('router', 'websocket_message', rc_payload)

        # Send persisted hidden destinations for group visibility sync
        if hasattr(self.storage_handler, 'get_hidden_destinations'):
            hidden_dsts = await self.storage_handler.get_hidden_destinations()
            if hidden_dsts:
                hd_payload = {
                    "type": "response",
                    "msg": "hidden_destinations",
                    "data": hidden_dsts,
                }
                if websocket:
                    await self.publish(
                        'router', 'websocket_direct',
                        {'websocket': websocket, 'data': hd_payload},
                    )
                else:
                    await self.publish('router', 'websocket_message', hd_payload)

        # Send persisted blocked texts for message text filtering
        if hasattr(self.storage_handler, 'get_blocked_texts'):
            blocked_texts = await self.storage_handler.get_blocked_texts()
            if blocked_texts:
                bt_payload = {
                    "type": "response",
                    "msg": "blocked_texts",
                    "data": blocked_texts,
                }
                if websocket:
                    await self.publish(
                        'router', 'websocket_direct',
                        {'websocket': websocket, 'data': bt_payload},
                    )
                else:
                    await self.publish('router', 'websocket_message', bt_payload)

    async def _handle_summary_command(self, websocket):
        """Handle summary command - sends message counts per destination."""
        summary = await self.storage_handler.get_summary()
        payload = {
            "type": "response",
            "msg": "summary",
            "data": summary,
        }
        if websocket:
            await self.publish(
                'router', 'websocket_direct', {'websocket': websocket, 'data': payload}
            )
        else:
            await self.publish('router', 'websocket_message', payload)

    async def _handle_messages_page_command(self, websocket, params):
        """Handle paginated message fetch."""
        dst = params.get('dst', '*')
        before = params.get('before', int(time.time() * 1000))
        limit = min(params.get('limit', 20), 100)
        src = params.get('src')  # Own callsign for DM conversation pagination

        page_data = await self.storage_handler.get_messages_page(dst, before, limit, src=src)
        payload = {
            "type": "response",
            "msg": "messages_page",
            "dst": dst,
            "data": page_data["messages"],
            "has_more": page_data["has_more"],
        }
        if websocket:
            await self.publish(
                'router', 'websocket_direct', {'websocket': websocket, 'data': payload}
            )
        else:
            await self.publish('router', 'websocket_message', payload)

    async def _handle_mheard_dump_command(self, websocket):
        """Handle mheard dump command"""
        # Create progress callback that sends updates to the requesting client
        async def progress_callback(stage, detail, callsign=None):
            progress_msg = {
                "type": "progress",
                "msg": "mheard progress",
                "stage": stage,
                "detail": detail,
            }
            if callsign:
                progress_msg["callsign"] = callsign
            if websocket:
                await self.publish(
                    'router', 'websocket_direct',
                    {'websocket': websocket, 'data': progress_msg}
                )
            else:
                await self.publish('router', 'websocket_message', progress_msg)

        # Use the parallel version
        mheard = await self.storage_handler.process_mheard_store_parallel(
            progress_callback=progress_callback
        )
        payload = {
            "type": "response",
            "msg": "mheard stats",
            "data": mheard
        }
        if websocket:
            await self.publish(
                'router', 'websocket_direct',
                {'websocket': websocket, 'data': payload}
            )
        else:
            # SSE client — broadcast to all connected clients
            await self.publish('router', 'websocket_message', payload)

    # BLE command handlers - route through ble_client abstraction
    def _get_ble_client(self):
        """Get the BLE client from registered protocols"""
        return self.get_protocol('ble_client')

    async def _send_ble_command_with_retry(
        self,
        client,
        cmd: str,
        max_retries: int = 3,
        base_delay: float = BLE_RETRY_BASE_DELAY
    ) -> bool:
        """
        Send BLE command with exponential backoff retry.

        BLE is inherently unreliable (interference, distance, packet loss).
        This helper retries failed commands with exponential backoff to
        improve reliability.

        Args:
            client: BLE client instance
            cmd: Command to send (e.g., "--info", "--pos")
            max_retries: Maximum number of retry attempts (default: 3)
            base_delay: Base delay in seconds for exponential backoff

        Returns:
            True if command sent successfully (on any attempt)
            False if all attempts failed

        Retry timing uses exponential backoff (base_delay * 2^attempt)
        """
        for attempt in range(max_retries):
            try:
                await client.send_command(cmd)
                if attempt > 0:
                    logger.info("Command %s succeeded on attempt %d/%d",
                              cmd, attempt + 1, max_retries)
                return True

            except Exception as e:
                if attempt < max_retries - 1:
                    # Calculate exponential backoff delay
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "Command %s failed (attempt %d/%d), retrying in %.1fs: %s",
                        cmd, attempt + 1, max_retries, delay, e
                    )
                    await asyncio.sleep(delay)
                else:
                    # Final attempt failed
                    logger.error(
                        "Command %s failed after %d attempts: %s",
                        cmd, max_retries, e
                    )
                    return False

        return False  # All attempts exhausted

    async def _query_ble_registers(self, wait_for_hello: bool = True, sync_time: bool = True):
        """
        Query BLE device registers NOT auto-sent by the device on connect.

        The device auto-sends 8 registers on BLE connection:
        I, SN, G, SA, SE+S1, SW+S2, W, AN

        This method only queries the remaining registers:
        - --io: TYP: IO (GPIO status)
        - --tel: TYP: TM (telemetry config)

        Args:
            wait_for_hello: If True, wait 1s before querying (ensure hello complete)
            sync_time: If True, sync device time on new connections
        """
        client = self._get_ble_client()
        if not client:
            return

        # CRITICAL: MeshCom firmware requires 0x10 hello message before
        # processing A0 commands. Wait for device to process hello handshake.
        # Per firmware docs: "The phone app must send 0x10 hello message
        # before other commands will be processed."
        if wait_for_hello:
            logger.debug("Waiting for hello handshake to complete")
            await asyncio.sleep(BLE_HELLO_WAIT)

            # Automatically sync device time after hello handshake completes.
            # Per firmware spec (page 898): "Send 0x20 with UNIX timestamp to
            # synchronize device clock (especially important for devices without
            # GPS or RTC battery)."
            if sync_time:
                try:
                    await client.set_command("--settime")
                    logger.info("Device time synchronized after connection")
                except Exception as e:
                    logger.warning("Time sync failed (non-critical): %s", e)

        # Only query registers NOT auto-sent by device on connect.
        # Device auto-sends: I, SN, G, SA, SE+S1, SW+S2, W, AN
        non_auto_registers = [
            ('--io', BLE_QUERY_DELAY_STANDARD),    # TYP: IO (GPIO status)
            ('--tel', BLE_QUERY_DELAY_STANDARD),   # TYP: TM (telemetry config)
        ]

        for cmd, delay in non_auto_registers:
            success = await self._send_ble_command_with_retry(client, cmd)
            if not success:
                logger.warning("Register query %s failed (non-critical)", cmd)
            await asyncio.sleep(delay)

        logger.debug("Register queries complete (IO + TM)")

    async def _handle_ble_scan_command(self):
        """Handle BLE scan command"""
        client = self._get_ble_client()
        if client:
            devices = await client.scan()
            ts = int(time.time() * 1000)

            paired = [d for d in devices if d.known]
            unpaired = [d for d in devices if not d.known]

            known_msg = {'src_type': 'BLE', 'TYP': 'blueZknown', 'timestamp': ts}
            for d in paired:
                path = f"/org/bluez/hci0/dev_{d.address.replace(':', '_')}"
                known_msg[path] = {
                    'org.bluez.Device1': {
                        'Name': d.name,
                        'Address': d.address,
                        'Paired': True,
                        'Connected': getattr(d, 'connected', False),
                        'Busy': False,
                    }
                }
            await self.publish('ble', 'ble_status', known_msg)

            unknown_msg = {'src_type': 'BLE', 'TYP': 'blueZunKnown', 'timestamp': ts}
            for d in unpaired:
                path = f"/org/bluez/hci0/dev_{d.address.replace(':', '_')}"
                unknown_msg[path] = [d.name, d.address, d.rssi]
            await self.publish('ble', 'ble_status', unknown_msg)
        else:
            logger.warning("BLE client not available for scan")

    async def _handle_ble_pair_command(self, MAC, BLE_Pin):
        """Handle BLE pair command"""
        client = self._get_ble_client()
        if client:
            await client.pair(MAC)
        else:
            logger.warning("BLE client not available for pair")

    async def _handle_ble_unpair_command(self, MAC):
        """Handle BLE unpair command"""
        client = self._get_ble_client()
        if client:
            await client.unpair(MAC)
        else:
            logger.warning("BLE client not available for unpair")

    async def _handle_ble_connect_command(self, MAC, websocket=None):
        """Handle BLE connect command"""
        client = self._get_ble_client()
        if not client:
            logger.warning("BLE client not available for connect")
            return

        # Check if already connected — skip reconnect, just query registers
        if hasattr(client, 'refresh_status'):
            status = await client.refresh_status()
        else:
            status = client.status

        already_connected = status.state == ConnectionState.CONNECTED

        if not already_connected:
            await client.connect(MAC)
            # Note: hello handshake is sent during connect()
            # _query_ble_registers will wait for it to complete
            # Re-check status after connect
            if hasattr(client, 'refresh_status'):
                status = await client.refresh_status()
            else:
                status = client.status

        if status.state == ConnectionState.CONNECTED:
            # Query registers: wait for hello if just connected, skip wait if already connected
            await self._query_ble_registers(wait_for_hello=not already_connected)
            # Send connection info (device_name, device_address) to frontend
            # Don't query registers again - we just did it above
            await self._handle_ble_info_command(websocket, query_registers=False)

    async def _handle_ble_disconnect_command(self):
        """Handle BLE disconnect command"""
        client = self._get_ble_client()
        if client:
            await client.disconnect()
        else:
            logger.warning("BLE client not available for disconnect")

    async def _handle_ble_info_command(self, websocket, query_registers: bool = True):
        """
        Handle BLE info command - send current BLE status to requesting client.

        Args:
            websocket: WebSocket to send response to (None = broadcast via SSE)
            query_registers: Whether to query device registers (default True).
                            Set to False when called after connection to avoid
                            duplicate queries (connect handler already queries).
        """
        client = self._get_ble_client()
        if not client:
            logger.warning("BLE client not available for info")
            return

        # Refresh from remote service to avoid stale/racing local cache
        if hasattr(client, 'refresh_status'):
            status = await client.refresh_status()
        else:
            status = client.status
        is_connected = status.state == ConnectionState.CONNECTED

        if is_connected:
            ble_info = {
                'src_type': 'BLE',
                'TYP': 'blueZ',
                'command': 'connect BLE result',
                'result': 'ok',
                'msg': 'BLE connection already running',
                'device_address': status.device_address,
                'device_name': status.device_name,
                'mode': status.mode.value,
                'timestamp': int(time.time() * 1000),
            }
        else:
            ble_info = {
                'src_type': 'BLE',
                'TYP': 'blueZ',
                'command': 'disconnect',
                'result': 'ok',
                'msg': 'BLE not connected',
                'timestamp': int(time.time() * 1000),
            }

        if websocket:
            await self.publish('router', 'websocket_direct', {
                'websocket': websocket, 'data': ble_info
            })
        else:
            await self.publish('ble', 'ble_status', ble_info)

        # Request register dump from device so frontend gets config data
        # Only if requested (avoid duplicate queries when called after connection)
        if is_connected and query_registers:
            await self._query_ble_registers(wait_for_hello=False)

    async def _backend_resolve_ip(self, hostname: str) -> None:
        """Resolve hostname to IP address and publish result."""
        import socket
        loop = asyncio.get_running_loop()

        try:
            infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
            ip = infos[0][4][0]
            logger.info("Resolved %s to %s", hostname, ip)

            await self.publish('ble', 'ble_status', {
                'src_type': 'BLE',
                'TYP': 'blueZ',
                'command': "resolve-ip",
                'result': "ok",
                'msg': ip,
                'timestamp': int(time.time() * 1000)
            })
        except Exception as e:
            logger.error("Failed to resolve %s: %s", hostname, e)
            await self.publish('ble', 'ble_status', {
                'src_type': 'BLE',
                'TYP': 'blueZ',
                'command': "resolve-ip",
                'result': "error",
                'msg': str(e),
                'timestamp': int(time.time() * 1000)
            })

    async def _handle_resolve_ip_command(self, hostname):
        """Handle resolve IP command"""
        await self._backend_resolve_ip(hostname)

    # Device command handlers - route through ble_client abstraction
    async def _handle_device_a0_command(self, command):
        """Handle device A0 commands (--pos, --reboot, etc.)"""
        client = self._get_ble_client()
        if client:
            await client.send_command(command)
        else:
            logger.warning("BLE client not available for A0 command")

    async def _handle_device_set_command(self, command):
        """Handle device set commands (--settime, --setCALL, etc.)"""
        client = self._get_ble_client()
        if client:
            await client.set_command(command)
        else:
            logger.warning("BLE client not available for set command")

    def _should_suppress_outbound(self, message_data):
        """Check if outbound message should be suppressed using validator"""
        if not self.validator:
            if has_console:
                print("⚠️ Validator not initialized, no suppression")
            return False

        suppress = self.validator.should_suppress_outbound(message_data)

        if has_console:
            reason = self.validator.get_suppression_reason(message_data)
            action = "SUPPRESS" if suppress else "FORWARD"
            print(f"🔄 Suppression decision: {action} - {reason}")

        return suppress

    async def _udp_message_handler(self, routed_message):
        """Handle UDP messages from WebSocket and route to UDP handler"""
        message_data = routed_message['data']

        # EARLY NORMALIZATION - ab hier alles uppercase
        normalized_data = self.validator.normalize_message_data(message_data)

        # Add our callsign if missing
        if not normalized_data.get('src') and self.my_callsign:
            normalized_data['src'] = self.my_callsign

        if has_console:
            print(f"📡 UDP Handler: Processing '{normalized_data.get('msg')}'"
                  f" from {normalized_data.get('src')}"
                  f" to {normalized_data.get('dst')}")

        if self._should_suppress_outbound(normalized_data):
            reason = self.validator.get_suppression_reason(normalized_data)
            self.log_message_routing_decision(
                normalized_data, "UDP_SUPPRESSION", "SUPPRESS", reason
            )

            synthetic_message = self._create_synthetic_message(normalized_data, 'udp')
            await self._route_to_command_handler(synthetic_message)
            return

        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(normalized_data, 'udp')

        if is_self_message:
            if has_console:
                print("📡 UDP Handler: Self-message handled, not sending to mesh")
            return

        # External message - send to mesh network
        if has_console:
            print("📡 UDP Handler: Sending external message to mesh network")

        udp_handler = self.get_protocol('udp')

        if udp_handler:
            try:
                await udp_handler.send_message(normalized_data)
                if has_console:
                    print("📡 UDP message sent successfully to mesh network")
            except Exception as e:
                print(f"📡 UDP message send failed: {e}")
                await self.publish('system', 'websocket_message', {
                    'src_type': 'system',
                    'type': 'error',
                    'msg': f"Failed to send UDP message: {e}",
                    'timestamp': int(time.time() * 1000)
                })
        else:
            print("📡 UDP handler not available, can't send message")
            await self.publish('system', 'websocket_message', {
                'src_type': 'system',
                'type': 'error',
                'msg': "UDP handler not available",
                'timestamp': int(time.time() * 1000)
            })


    async def _ble_message_handler(self, routed_message):
        """Handle BLE messages from WebSocket and route to BLE client"""

        message_data = routed_message['data']

        # EARLY NORMALIZATION - ab hier alles uppercase
        normalized_data = self.validator.normalize_message_data(message_data)

        # Add our callsign if missing
        if not normalized_data.get('src') and self.my_callsign:
            normalized_data['src'] = self.my_callsign

        msg = normalized_data.get('msg')
        dst = normalized_data.get('dst')

        self._logger.debug(
            "BLE Handler: msg='%s' src='%s' dst='%s'",
            msg, normalized_data.get('src'), dst
        )

        if has_console:
            print(f"📱 BLE Handler: Processing '{msg}'"
                  f" from {normalized_data.get('src')} to '{dst}'")

        suppress = self._should_suppress_outbound(normalized_data)
        self._logger.debug("BLE Handler: suppress=%s", suppress)

        if suppress:
            reason = self.validator.get_suppression_reason(normalized_data)
            self.log_message_routing_decision(
                normalized_data, "BLE_SUPPRESSION", "SUPPRESS", reason
            )

            synthetic_message = self._create_synthetic_message(normalized_data, 'ble')
            await self._route_to_command_handler(synthetic_message)
            return

        # Check if this is a self-message first
        is_self_message = await self._handle_outgoing_message(normalized_data, 'ble')

        if is_self_message:
            if has_console:
                print("📱 BLE Handler: Self-message handled, not sending to device")
            return

        # External message - send to BLE device
        if has_console:
            print("📱 BLE Handler: Sending external message to BLE device")
        client = self._get_ble_client()
        if client:
            await client.send_message(msg, dst)
        else:
            logger.warning("BLE client not available, cannot send message")

    def _is_message_to_self(self, message_data):
        """Check if message is addressed to our own callsign (assumes normalized data)"""
        if not self.my_callsign:
            return False
        dst = message_data.get('dst', '')
        return dst == self.my_callsign

    def _create_synthetic_message(self, original_message, protocol_type='udp'):
        """Create a synthetic message that looks like it came from LoRa (uses normalized data)"""
        current_time = int(time.time())
        msg_id = f"{current_time:08X}"

        return {
            'src': original_message.get('src'),  # Already uppercase
            'dst': original_message.get('dst'),  # Already uppercase
            'msg': original_message.get('msg'),
            'msg_id': msg_id,
            'type': 'msg',
            'src_type': protocol_type,
            'timestamp': current_time * 1000
        }

    async def _handle_outgoing_message(self, message_data, protocol_type='udp'):
        """Unified handler for outgoing messages - handles self-message detection"""

        if self._is_message_to_self(message_data):
            if has_console:
                print(f"🔄 MessageRouter: Detected self-message to "
                      f"{message_data.get('dst')}, routing to CommandHandler only")
            synthetic_message = self._create_synthetic_message(message_data)
            await self._route_to_command_handler(synthetic_message)
            return True  # Indicates message was handled as self-message

        return False  # Indicates message should be sent to external protocol

    async def _route_to_command_handler(self, synthetic_message):
        """Route synthetic message to CommandHandler"""
        if has_console:
            print(f"🔄 MessageRouter: Creating synthetic message: {synthetic_message}")

        routed_message = {
            'source': 'self',
            'type': 'ble_notification',
            'data': synthetic_message,
            'timestamp': int(time.time() * 1000)
        }

        if has_console:
            print("🔄 MessageRouter: Routing to CommandHandler subscribers...")
            subs = len(self._subscribers['ble_notification'])
            print(f"🔄 MessageRouter: Available subscribers"
                  f" for 'ble_notification': {subs}")

        # Find CommandHandler subscribers
        for handler in self._subscribers['ble_notification']:
            try:
                await handler(routed_message)
                if has_console:
                    print("🔄 MessageRouter: Routed self-message to CommandHandler")
            except Exception as e:
                print(f"MessageRouter ERROR: Failed to route self-message: {e}")


class MessageValidator:
    """Centralized message validation and normalization"""

    def __init__(self, my_callsign):
        self.my_callsign = my_callsign.upper()

    def normalize_message_data(self, message_data):
        """Normalize message data - uppercase and validate early"""
        normalized = message_data.copy()

        # Defensive uppercase normalization
        src_raw = message_data.get('src', '').strip()
        dst_raw = message_data.get('dst', '').strip()
        msg_raw = message_data.get('msg', '').strip()

        # Handle comma-separated src (path routing)
        src = src_raw.split(',')[0].upper() if ',' in src_raw else src_raw.upper()
        dst = dst_raw.upper()

        # Normalize command to uppercase while preserving structure
        msg = msg_raw.upper() if msg_raw.startswith('!') else msg_raw

        normalized.update({
            'src': src,
            'dst': dst,
            'msg': msg
        })

        if has_console and (src != src_raw or dst != dst_raw):
            print(f"🔧 Normalized: src='{src_raw}'→'{src}', dst='{dst_raw}'→'{dst}'")

        return normalized

    def extract_target_callsign(self, msg):
        """Extract target callsign from command message.

        Priority:
        1. Explicit target: parameter (scanned anywhere in message)
        2. Fallback: first standalone callsign (right-to-left, skip key:value)

        Commands that never have targets: GROUP, KB, TOPIC
        """
        if not msg or not msg.startswith('!'):
            return None

        msg_upper = msg.upper().strip()
        parts = msg_upper.split()

        if len(parts) < 2:
            return None

        command = parts[0][1:]

        # Commands that NEVER have targets (admin-only, local state)
        if command in ['GROUP', 'KB', 'TOPIC']:
            return None

        # Callsign pattern: requires letter + digit, min 3 chars
        callsign_pattern = r'^(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]{3,8}(-\d{1,2})?$'

        # Priority 1: Explicit target:CALLSIGN parameter (scanned anywhere)
        for part in parts[1:]:
            if part.startswith('TARGET:'):
                potential = part[7:]  # Remove 'TARGET:' prefix
                if potential in ['LOCAL', '']:
                    return None  # Explicit local execution
                if re.match(callsign_pattern, potential):
                    return potential
                return None  # Invalid target format

        # Priority 2: Positional fallback (right-to-left, skip key:value pairs)
        for part in reversed(parts[1:]):
            if ':' in part:
                continue  # Skip key:value arguments
            potential = part.strip()
            if re.match(callsign_pattern, potential):
                return potential

        return None

    def is_group(self, dst):
        """Check if destination is a group"""
        if not dst:
            return False

        # Special group 'TEST'
        if dst.upper() == 'TEST':
            return True

        # Numeric groups: 1-99999
        if dst.isdigit():
            try:
                group_num = int(dst)
                return 1 <= group_num <= 99999
            except ValueError:
                return False

        return False

    def is_valid_destination(self, dst):
        """Validate destination format (assumes already uppercase)"""
        if not dst:
            if has_console:
                print("🔍 Invalid dst: empty")
            return False

        # Invalid destinations from table
        invalid_destinations = ['*', 'ALL', '']
        if dst in invalid_destinations:
            if has_console:
                print(f"🔍 Invalid dst: '{dst}' in blacklist")
            return False

        # Valid: callsign pattern
        if re.match(r'^[A-Z0-9]{2,8}(-\d{1,2})?$', dst):
            if has_console:
                print(f"🔍 Valid dst: '{dst}' matches callsign pattern")
            return True

        # Valid: group pattern
        if self.is_group(dst):
            if has_console:
                print(f"🔍 Valid dst: '{dst}' is group")
            return True

        if has_console:
            print(f"🔍 Invalid dst: '{dst}' no pattern match")

        return False

    def is_command(self, msg):
        """Check if message is a command"""
        return msg and msg.startswith('!')

    def is_self_message(self, src, dst):
        """Check if message is from us to us"""
        return src == self.my_callsign and dst == self.my_callsign


    def should_suppress_outbound(self, message_data):
        """Implement simplified suppression logic from table"""
        src = message_data.get('src', '')
        dst = message_data.get('dst', '')
        msg = message_data.get('msg', '')

        if has_console:
            print(f"🔍 Suppression check: src='{src}', dst='{dst}', msg='{msg[:20]}...'")

        # Only check our own outgoing commands
        if src != self.my_callsign:
            if has_console:
                print(f"🔍 → NOT our message ({src} != {self.my_callsign}) - NO SUPPRESSION")
            return False

        # Must be a command
        if not self.is_command(msg):
            if has_console:
                print("🔍 → Not a command - NO SUPPRESSION")
            return False

        # Invalid destinations always suppress
        if not self.is_valid_destination(dst):
            if has_console:
                print(f"🔍 → Invalid destination '{dst}' - SUPPRESS")
            return True

        target = self.extract_target_callsign(msg)

        # No target → execute locally
        if not target:
            if has_console:
                print(f"🔍 → No target in '{msg}' - SUPPRESS (local execution)")
            return True

        # Target is us → execute locally
        if target == self.my_callsign:
            if has_console:
                print(f"🔍 → Target is us ({target}) - SUPPRESS (local execution)")
            return True

        # Target is someone else → send to mesh
        if has_console:
            print(f"🔍 → Target is '{target}' (not us) - NO SUPPRESSION (send to mesh)")
        return False

    def get_suppression_reason(self, message_data):
        """Get human-readable reason for suppression decision"""
        src = message_data.get('src', '')
        dst = message_data.get('dst', '')
        msg = message_data.get('msg', '')

        if src != self.my_callsign:
            return f"Not our message ({src})"

        if not self.is_command(msg):
            return "Not a command"

        if not self.is_valid_destination(dst):
            return f"Invalid destination ({dst})"

        target = self.extract_target_callsign(msg)

        if not target:
            return "No target → local execution"

        if target == self.my_callsign:
            return f"Target is us ({target}) → local execution"

        return f"Target is {target} → send to mesh"


async def main():
    # Initialize SQLite storage backend
    logger.info("Database: %s", cfg.storage.db_path)
    storage_handler = await create_sqlite_storage(cfg.storage.db_path)
    # One-time migration: import mcdump.json into SQLite, then rename to prevent re-import
    dump_path = Path("mcdump.json")
    if dump_path.exists():
        count = await storage_handler.load_dump(str(dump_path))
        migrated_path = dump_path.with_suffix(".json.migrated")
        dump_path.rename(migrated_path)
        logger.info("Migrated dump file → %s (%d messages imported)", migrated_path, count)
    await storage_handler.prune_messages(
        cfg.storage.prune_hours, block_list,
        prune_hours_pos=cfg.storage.prune_hours_pos,
        prune_hours_ack=cfg.storage.prune_hours_ack,
    )

    message_router = MessageRouter(storage_handler)
    message_router.set_callsign(cfg.call_sign)
    message_router.cached_gps = None  # {lat, lon} — set when BLE device sends TYP="G"
    message_router.cached_ble_registers = {}  # {TYP: dict} — cached on ble_notification

    async def _cache_ble_register(routed_message):
        """Cache BLE register notifications for serving on SSE reconnect."""
        data = routed_message['data']
        typ = data.get("TYP")
        if typ in ("I", "SN", "G", "SA", "SE", "S1", "SW", "S2", "W", "AN", "IO", "TM"):
            message_router.cached_ble_registers[typ] = data

    message_router.subscribe("ble_notification", _cache_ble_register)

    async def _clear_ble_cache_on_disconnect(routed_message):
        """Clear BLE register cache when device disconnects."""
        data = routed_message['data']
        cmd = data.get("command", "")
        if "disconnect" in cmd and data.get("result") in ("ok", "lost"):
            message_router.cached_ble_registers.clear()
            logger.info("BLE register cache cleared (disconnect)")

    message_router.subscribe("ble_status", _clear_ble_cache_on_disconnect)

    async def _cache_gps(routed_message):
        """Cache GPS from BLE device and update weather service."""
        data = routed_message['data']
        if data.get("TYP") != "G":
            return
        lat = data.get("LAT", 0)
        lon = data.get("LON", 0)
        if lat != 0 and lon != 0:
            message_router.cached_gps = {"lat": lat, "lon": lon}
            # Update weather service if available
            cmd_handler = message_router.get_protocol('commands')
            if cmd_handler:
                cmd_handler.lat = lat
                cmd_handler.lon = lon
                if cmd_handler.weather_service:
                    cmd_handler.weather_service.update_location(lat, lon)

    message_router.subscribe("ble_notification", _cache_gps)

    # Command Handler Plugin
    command_handler = create_command_handler(
        message_router,
        storage_handler,
        cfg.call_sign,
        cfg.location.latitude,
        cfg.location.longitude,
        cfg.location.station_name,
        cfg.user_info_text
    )
    message_router.register_protocol('commands', command_handler)

    # UDP Handler
    udp_handler = UDPHandler(
        listen_port=MESHCOM_UDP_PORT,
        target_host=cfg.udp.target,
        target_port=MESHCOM_UDP_PORT,
        message_callback=None,
        message_router=message_router
    )
    message_router.register_protocol('udp', udp_handler)

    # SSE Manager (REST API + Server-Sent Events)
    sse_manager = None
    if SSE_AVAILABLE:
        weather_service = getattr(command_handler, 'weather_service', None)
        sse_manager = create_sse_manager(
            SSE_HOST, SSE_PORT, message_router, weather_service
        )
        if sse_manager:
            message_router.register_protocol('sse', sse_manager)
    else:
        logger.warning("FastAPI/Uvicorn not installed — SSE transport unavailable")

    # Start UDP early — before BLE init which can block for seconds on Pi,
    # ensuring the health check finds port 1799 listening promptly.
    await udp_handler.start_listening()

    # BLE Client (supports local, remote, disabled modes)
    ble_client = None
    try:
        ble_mode = BLEMode(cfg.ble.mode)
    except ValueError:
        logger.warning("Invalid BLE mode '%s', defaulting to 'disabled'", cfg.ble.mode)
        ble_mode = BLEMode.DISABLED

    logger.info("BLE mode: %s", ble_mode.value)

    if ble_mode != BLEMode.DISABLED:
        try:
            ble_url = os.getenv("MCAPP_BLE_URL", BLE_SERVICE_URL)
            ble_client = await create_ble_client(
                mode=ble_mode,
                remote_url=ble_url if ble_mode == BLEMode.REMOTE else None,
                api_key=cfg.ble.api_key if ble_mode == BLEMode.REMOTE else None,
                message_router=message_router,
            )
            message_router.register_protocol('ble_client', ble_client)
            await ble_client.start()
        except Exception as e:
            logger.error("Failed to initialize BLE client: %s", e)
            logger.info("Falling back to disabled BLE mode")
            ble_mode = BLEMode.DISABLED
            ble_client = await create_ble_client(
                mode=BLEMode.DISABLED,
                message_router=message_router,
            )
    else:
        # Create disabled stub
        ble_client = await create_ble_client(
            mode=BLEMode.DISABLED,
            message_router=message_router,
        )
        await ble_client.start()

    # Start SSE server if enabled
    if sse_manager:
        await sse_manager.start_server()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def stdin_reader():
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            if line.strip() == "q":
                loop.call_soon_threadsafe(stop_event.set)
                break

    # Signal handling with fallback
    _first_signal_time = None

    def handle_shutdown(signum=None, frame=None):
        nonlocal _first_signal_time
        logger.info("Signal %s received, stopping proxy service ..", signum or 'SIGINT')
        if stop_event.is_set():
            now = time.monotonic()
            # Ignore duplicate signals within 5s of first (asyncio can double-fire)
            # Only force-exit if user deliberately sends a second signal after 5s
            if _first_signal_time and (now - _first_signal_time) < 5.0:
                logger.debug(
                    "Ignoring duplicate signal (%.1fs after first)",
                    now - _first_signal_time,
                )
                return
            elapsed = now - _first_signal_time if _first_signal_time else 0
            logger.warning(
                "Force shutdown - second signal received after %.0fs",
                elapsed,
            )
            os._exit(1)
        _first_signal_time = time.monotonic()
        stop_event.set()

    # Try asyncio signal handlers first (preferred)
    try:
        loop.add_signal_handler(signal.SIGINT, handle_shutdown)
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown)
        signal_method = "asyncio"
    except Exception as e:
        logger.warning("Could not set asyncio signal handlers: %s", e)
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal_method = "traditional"

    logger.debug("Signal handling: %s", signal_method)

    if sys.stdin.isatty():
        logger.info("Press 'q' + Enter to stop and save")
        loop.run_in_executor(None, stdin_reader)

    logger.info("UDP-Listen %d, Target MeshCom %s", MESHCOM_UDP_PORT, cfg.udp.target)
    logger.info("MessageRouter: %d message types, %d protocols",
                len(message_router._subscribers), len(message_router._protocols))
    if sse_manager:
        logger.info("SSE server available at http://%s:%d/events", SSE_HOST, SSE_PORT)

    # Log BLE configuration
    if ble_mode == BLEMode.REMOTE:
        logger.info("BLE: remote mode -> %s", os.getenv("MCAPP_BLE_URL", BLE_SERVICE_URL))
    elif ble_mode == BLEMode.LOCAL:
        logger.info("BLE: local mode (D-Bus/BlueZ)")
    else:
        logger.info("BLE: disabled")

    suppression_passed = True  # Default values
    command_handler_passed = True

    if check_console():
        logger.info("Running suppression logic tests...")
        suppression_passed = message_router.test_suppression_logic()

        logger.info("Running command handler test suite...")
        command_handler_passed = await command_handler.run_all_tests()

        if suppression_passed and command_handler_passed:
            logger.info("All tests passed! System ready.")
        else:
            logger.warning("Some tests failed. Check implementation.")

### unit tests

    # Nightly pruning task — runs at 04:00 local time
    async def _nightly_prune():
        """Background task: prune old messages daily at 04:00."""
        while not stop_event.is_set():
            now = datetime.now()
            tomorrow_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if tomorrow_4am <= now:
                tomorrow_4am += timedelta(days=1)
            wait_seconds = (tomorrow_4am - now).total_seconds()
            logger.info("Next DB prune scheduled in %.0fh at 04:00", wait_seconds / 3600)

            # Wait until 04:00 or stop event, whichever comes first
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # Timer expired — time to prune

            if stop_event.is_set():
                break

            logger.info("Starting nightly DB prune...")
            try:
                remaining = await storage_handler.prune_messages(
                    cfg.storage.prune_hours, block_list,
                    prune_hours_pos=cfg.storage.prune_hours_pos,
                    prune_hours_ack=cfg.storage.prune_hours_ack,
                )
                await storage_handler.aggregate_hourly_buckets()
                logger.info("Nightly prune complete: %d messages remaining", remaining)
            except Exception as e:
                logger.error("Nightly prune failed: %s", e)

    prune_task = asyncio.create_task(_nightly_prune())

    await stop_event.wait()

    # Cancel the nightly prune task
    prune_task.cancel()
    try:
        await prune_task
    except asyncio.CancelledError:
        pass

    logger.info("Stopping proxy server, saving to disc ..")

    try:
        # Step 1: Clean up beacons
        logger.info("Stopping beacon tasks...")
        await asyncio.wait_for(
            command_handler.cleanup_topic_beacons(),
            timeout=5.0
        )
    except asyncio.TimeoutError:
        logger.warning("Beacon cleanup timeout")

    # Clean shutdown sequence with timeouts
    try:
        # Step 2: Stop BLE client with timeout
        logger.info("Stopping BLE client...")
        if ble_client:
            await asyncio.wait_for(ble_client.stop(), timeout=5.0)
        else:
            # Fallback to legacy disconnect
            await asyncio.wait_for(
                message_router.route_command("disconnect BLE"),
                timeout=5.0
            )
    except asyncio.TimeoutError:
        logger.warning("BLE disconnect timeout")

    try:
        # Step 3: Stop UDP handler
        logger.info("Stopping UDP handler...")
        await asyncio.wait_for(udp_handler.stop_listening(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("UDP stop timeout")

    # Step 4: Stop SSE server if running
    if sse_manager:
        try:
            logger.info("Stopping SSE server...")
            await asyncio.wait_for(sse_manager.stop_server(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("SSE stop timeout")

    logger.info("All services stopped")

    logger.info("Shutdown complete")

    # Force clean process exit after successful cleanup
    os._exit(0)


def run():
    """Entry point for mcapp CLI."""
    global cfg, has_console, is_dev

    # Determine if we have a console
    has_console = sys.stdout.isatty()

    # Setup logging first
    is_dev = os.getenv("MCAPP_ENV") == "dev"
    setup_logging(verbose=is_dev, simple_format=True)

    if is_dev:
        logger.info("*** Debug and DEV Environment detected ***")

    # Load configuration using new config loader
    cfg = Config.load()

    # Log configuration summary
    logger.info("WX Service for %s (location from GPS device)",
                cfg.location.station_name or "unnamed")
    logger.info(
        "Retention: msgs %s, pos/ack %s",
        hours_to_dd_hhmm(cfg.storage.prune_hours),
        hours_to_dd_hhmm(cfg.storage.prune_hours_pos),
    )
    logger.info("SQLite storage: %s (max %d MB)", cfg.storage.db_path,
                SQLiteStorage.MAX_DB_SIZE_MB)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Manually stopped with Ctrl+C")
    except Exception as e:
        logger.exception("Unexpected error: %s", e)


if __name__ == "__main__":
    run()

