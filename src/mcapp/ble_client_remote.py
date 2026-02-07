"""
Remote BLE Client - HTTP/SSE implementation.

This module connects to a remote BLE service via HTTP REST API
and Server-Sent Events for notifications.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Callable
from urllib.parse import urljoin

import aiohttp
from aiohttp_sse_client import client as sse_client

from .ble_client import BLEClientBase, BLEDevice, BLEMode, BLEStatus, ConnectionState
from .ble_handler import decode_binary_message, decode_json_message, dispatcher

logger = logging.getLogger(__name__)


class BLEClientRemote(BLEClientBase):
    """
    Remote BLE client using HTTP/SSE.

    Connects to a remote BLE service running on hardware with
    Bluetooth support (e.g., Raspberry Pi).
    """

    def __init__(
        self,
        remote_url: str,
        api_key: str | None = None,
        notification_callback: Callable[[dict], None] | None = None,
        message_router=None,
        timeout: float = 30.0,
    ):
        super().__init__(notification_callback)
        self.remote_url = remote_url.rstrip('/')
        self.api_key = api_key
        self.message_router = message_router
        self.timeout = timeout

        self._session: aiohttp.ClientSession | None = None
        self._sse_task: asyncio.Task | None = None
        self._running = False
        self._status.mode = BLEMode.REMOTE
        self._last_connect_attempt: float = 0
        self._connect_cooldown: float = 15.0

    def _headers(self) -> dict:
        """Get request headers with API key"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _ensure_session(self):
        """Ensure HTTP session exists"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def _reset_session(self):
        """Close and recreate the HTTP session"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        await self._ensure_session()

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        retries: int = 2,
        retry_delay: float = 1.5,
        request_timeout: float | None = None,
        quiet: bool = False,
    ) -> dict:
        """Make HTTP request to remote service, with retry on 409 (busy) and connection errors"""
        await self._ensure_session()

        url = urljoin(self.remote_url, endpoint)
        timeout = (
            aiohttp.ClientTimeout(total=request_timeout) if request_timeout else None
        )

        for attempt in range(1 + retries):
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=data if data else None,
                    timeout=timeout,
                ) as response:
                    response_data = await response.json()

                    if response.status == 409 and attempt < retries:
                        logger.info(
                            "BLE busy (409), retry %d/%d in %.1fs: %s",
                            attempt + 1, retries, retry_delay, endpoint
                        )
                        await asyncio.sleep(retry_delay)
                        continue

                    if response.status >= 400:
                        error_msg = response_data.get('detail', 'Unknown error')
                        raise RuntimeError(f"API error ({response.status}): {error_msg}")

                    return response_data

            except aiohttp.ClientError as e:
                if attempt < retries:
                    log = logger.debug if quiet else logger.warning
                    log(
                        "HTTP request failed (%s), retry %d/%d: %s",
                        e, attempt + 1, retries, endpoint
                    )
                    await self._reset_session()
                    await asyncio.sleep(retry_delay)
                    continue
                log_final = logger.debug if quiet else logger.error
                log_final("HTTP request failed after %d attempts: %s", retries + 1, e)
                raise RuntimeError(f"Connection error: {e}") from e

        raise RuntimeError("BLE service busy after retries")

    async def _publish_status(self, command: str, result: str, msg: str):
        """Publish BLE status through message router"""
        if self.message_router:
            await self.message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE',
                'TYP': 'remote',
                'command': command,
                'result': result,
                'msg': msg,
                'timestamp': int(time.time() * 1000)
            })

    async def scan(self, timeout: float = 5.0, prefix: str = "MC-") -> list[BLEDevice]:
        """Scan for devices via remote service"""
        try:
            await self._publish_status('scan BLE', 'info', 'Starting remote scan...')

            response = await self._request(
                'GET',
                f'/api/ble/devices?timeout={timeout}&prefix={prefix}'
            )

            devices = [
                BLEDevice(
                    name=d['name'],
                    address=d['address'],
                    rssi=d['rssi'],
                    paired=d['paired'],
                    known=d.get('known', False)
                )
                for d in response.get('devices', [])
            ]

            await self._publish_status(
                'scan BLE result',
                'ok',
                f'Found {len(devices)} devices'
            )

            return devices

        except Exception as e:
            logger.error("Scan error: %s", e)
            await self._publish_status('scan BLE result', 'error', str(e))
            return []

    async def connect(self, mac: str) -> bool:
        """Connect to device via remote service"""
        # Guard: don't stack connect attempts (webapp auto-sends on SSE reconnect)
        if self._status.state == ConnectionState.CONNECTING:
            logger.info("Connect already in progress, ignoring duplicate request")
            return False

        # Cooldown after recent failure to prevent rapid-fire retry loops
        elapsed = time.time() - self._last_connect_attempt
        if self._last_connect_attempt and elapsed < self._connect_cooldown:
            remaining = self._connect_cooldown - elapsed
            logger.info("Connect cooldown active (%.0fs remaining), skipping", remaining)
            return False

        try:
            self._status.state = ConnectionState.CONNECTING
            self._last_connect_attempt = time.time()
            await self._publish_status('connect BLE', 'info', f'Connecting to {mac}...')

            response = await self._request(
                'POST',
                '/api/ble/connect',
                {'device_address': mac},
                retries=0,  # BLE service has internal retries; don't retry 409 here
                request_timeout=45.0,  # Allow for 3×10s BLE attempts + cleanup
            )

            success = response.get('success', False)

            if success:
                self._status.state = ConnectionState.CONNECTED
                self._status.device_address = mac
                self._last_connect_attempt = 0  # Reset cooldown on success
                await self._publish_status('connect BLE result', 'ok', f'Connected to {mac}')
            else:
                self._status.state = ConnectionState.ERROR
                self._status.error = response.get('message', 'Connection failed')
                await self._publish_status(
                    'connect BLE result',
                    'error',
                    self._status.error
                )

            return success

        except Exception as e:
            logger.error("Connect error: %s", e)
            self._status.state = ConnectionState.ERROR
            self._status.error = str(e)
            await self._publish_status('connect BLE result', 'error', str(e))
            return False

    async def disconnect(self) -> bool:
        """Disconnect via remote service"""
        try:
            self._status.state = ConnectionState.DISCONNECTING
            await self._publish_status('disconnect BLE', 'info', 'Disconnecting...')

            response = await self._request('POST', '/api/ble/disconnect')
            success = response.get('success', False)

            self._status.state = ConnectionState.DISCONNECTED
            self._status.device_address = None
            await self._publish_status('disconnect BLE result', 'ok', 'Disconnected')

            return success

        except Exception as e:
            logger.error("Disconnect error: %s", e)
            await self._publish_status('disconnect BLE result', 'error', str(e))
            return False

    async def pair(self, mac: str) -> bool:
        """Pair with device via remote service"""
        try:
            await self._publish_status('pair BLE', 'info', f'Pairing with {mac}...')

            response = await self._request(
                'POST',
                '/api/ble/pair',
                {'device_address': mac}
            )

            success = response.get('success', False)
            result = 'ok' if success else 'error'
            await self._publish_status('pair BLE result', result, response.get('message', ''))

            return success

        except Exception as e:
            logger.error("Pair error: %s", e)
            await self._publish_status('pair BLE result', 'error', str(e))
            return False

    async def unpair(self, mac: str) -> bool:
        """Unpair device via remote service"""
        try:
            await self._publish_status('unpair BLE', 'info', f'Unpairing {mac}...')

            response = await self._request(
                'POST',
                '/api/ble/unpair',
                {'device_address': mac}
            )

            success = response.get('success', False)
            result = 'ok' if success else 'error'
            await self._publish_status('unpair BLE result', result, response.get('message', ''))

            return success

        except Exception as e:
            logger.error("Unpair error: %s", e)
            await self._publish_status('unpair BLE result', 'error', str(e))
            return False

    async def send_message(self, msg: str, group: str) -> bool:
        """Send message via remote service"""
        if not self.is_connected:
            return False

        try:
            response = await self._request(
                'POST',
                '/api/ble/send',
                {'message': msg, 'group': group}
            )
            return response.get('success', False)

        except Exception as e:
            logger.error("Send message error: %s", e)
            return False

    async def send_command(self, cmd: str) -> bool:
        """Send A0 command via remote service"""
        if not self.is_connected:
            return False

        try:
            response = await self._request(
                'POST',
                '/api/ble/send',
                {'command': cmd}
            )
            return response.get('success', False)

        except Exception as e:
            logger.error("Send command error: %s", e)
            return False

    async def set_command(self, cmd: str) -> bool:
        """Send set command via remote service"""
        if cmd == "--settime":
            try:
                response = await self._request('POST', '/api/ble/settime')
                return response.get('success', False)
            except Exception as e:
                logger.error("Set time error: %s", e)
                return False
        else:
            # For other set commands, send as regular command
            return await self.send_command(cmd)

    async def start(self) -> None:
        """Start the remote BLE client and SSE notification stream"""
        logger.info("Starting remote BLE client -> %s", self.remote_url)
        self._running = True

        # Check connection to remote service
        try:
            await self._ensure_session()
            status = await self._request('GET', '/api/ble/status', retries=4, quiet=True)
            logger.info("Remote service status: %s", status.get('state', 'unknown'))

            # Update local status based on remote
            if status.get('connected'):
                self._status.state = ConnectionState.CONNECTED
                self._status.device_address = status.get('device_address')
            else:
                self._status.state = ConnectionState.DISCONNECTED

        except Exception as e:
            logger.warning("Remote BLE service not ready yet: %s (SSE loop will retry)", e)
            await self._publish_status(
                'remote connect',
                'error',
                f'Cannot reach BLE service at {self.remote_url}: {e}'
            )

        # Always start SSE notification stream — it has its own reconnection logic
        self._sse_task = asyncio.create_task(self._sse_loop())

    async def stop(self) -> None:
        """Stop the remote BLE client"""
        logger.info("Stopping remote BLE client")
        self._running = False

        # Stop SSE task
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
            self._sse_task = None

        # Close HTTP session
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _sse_loop(self):
        """SSE notification listener loop"""
        url = urljoin(self.remote_url, '/api/ble/notifications')
        headers = {}
        if self.api_key:
            headers['X-API-Key'] = self.api_key

        while self._running:
            try:
                logger.info("Connecting to SSE stream: %s", url)

                timeout = aiohttp.ClientTimeout(total=None, sock_read=90)
                async with sse_client.EventSource(
                    url, headers=headers, timeout=timeout
                ) as event_source:
                    async for event in event_source:
                        if not self._running:
                            break

                        if event.type == 'notification':
                            await self._handle_notification(event.data)
                        elif event.type == 'status':
                            await self._handle_status(event.data)
                        elif event.type == 'ping':
                            logger.debug("SSE ping received")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    if not hasattr(self, '_sse_backoff'):
                        self._sse_backoff = 5
                    logger.warning("SSE connection error: %s, reconnecting in %ds...",
                                   e, self._sse_backoff)
                    await asyncio.sleep(self._sse_backoff)
                    self._sse_backoff = min(self._sse_backoff * 2, 60)
                else:
                    break
            else:
                # Reset backoff on clean exit from async for (shouldn't normally happen)
                self._sse_backoff = 5

    async def _handle_notification(self, data: str):
        """Handle incoming SSE notification"""
        try:
            notification = json.loads(data)

            # Publish through message router if available
            if self.message_router:
                # Transform to match expected format
                output = self._transform_notification(notification)
                if output:
                    await self.message_router.publish('ble', 'ble_notification', output)

            # Call direct callback if set
            if self.notification_callback:
                self.notification_callback(notification)

        except json.JSONDecodeError as e:
            logger.warning("Invalid SSE notification JSON: %s", e)
        except Exception as e:
            logger.error("Notification handling error: %s", e)

    def _get_own_callsign(self) -> str:
        """Get own callsign from message router if available."""
        return getattr(self.message_router, 'my_callsign', '') if self.message_router else ''

    def _transform_notification(self, notification: dict) -> dict | None:
        """Transform SSE notification to match local BLE handler format"""
        own_call = self._get_own_callsign()
        if notification.get('format') == 'json' and 'parsed' in notification:
            # JSON notification - run through dispatcher like local mode
            parsed = notification['parsed']
            output = dispatcher(parsed, own_call)
            if output:
                output['timestamp'] = notification.get('timestamp', int(time.time() * 1000))
                if output.get('transformer') not in ('generic_ble', 'mh'):
                    output['src_type'] = 'ble_remote'
                return output
            # Fallback: return parsed directly if dispatcher returns None
            parsed['timestamp'] = notification.get('timestamp', int(time.time() * 1000))
            parsed['src_type'] = 'ble_remote'
            return parsed

        elif notification.get('format') == 'binary':
            # Decode binary the same way local BLE handler does
            raw_b64 = notification.get('raw_base64')
            if raw_b64:
                try:
                    raw_bytes = base64.b64decode(raw_b64)
                    if raw_bytes.startswith(b'@'):
                        decoded = decode_binary_message(raw_bytes)
                        output = dispatcher(decoded, own_call)
                        if output:
                            if output.get('transformer') not in ('generic_ble', 'mh'):
                                output['src_type'] = 'ble_remote'
                            output['timestamp'] = notification.get(
                                'timestamp', int(time.time() * 1000)
                            )
                            return output
                    elif raw_bytes.startswith(b'D{'):
                        decoded = decode_json_message(raw_bytes)
                        output = dispatcher(decoded, own_call)
                        if output:
                            if output.get('transformer') not in ('generic_ble', 'mh'):
                                output['src_type'] = 'ble_remote'
                            output['timestamp'] = notification.get(
                                'timestamp', int(time.time() * 1000)
                            )
                            return output
                except Exception as e:
                    logger.warning("Failed to decode binary notification: %s", e)
            # Fallback: return raw if decoding failed
            return {
                'src_type': 'ble_remote',
                'format': 'binary',
                'raw_base64': raw_b64,
                'raw_hex': notification.get('raw_hex'),
                'timestamp': notification.get('timestamp', int(time.time() * 1000))
            }

        else:
            # Unknown format - pass through
            notification['src_type'] = 'ble_remote'
            return notification

    async def _handle_status(self, data: str):
        """Handle SSE status update"""
        try:
            status = json.loads(data)
            logger.debug("Remote status update: %s", status.get('state'))

            # Update local status
            state_str = status.get('state', 'disconnected')
            try:
                self._status.state = ConnectionState(state_str)
            except ValueError:
                self._status.state = ConnectionState.DISCONNECTED

        except Exception as e:
            logger.warning("Status update error: %s", e)

    @property
    def is_connected(self) -> bool:
        """Check connection status from cached state"""
        return self._status.state == ConnectionState.CONNECTED

    async def refresh_status(self) -> BLEStatus:
        """Refresh status from remote service"""
        try:
            response = await self._request('GET', '/api/ble/status')

            if response.get('connected'):
                self._status.state = ConnectionState.CONNECTED
                self._status.device_address = response.get('device_address')
                self._status.device_name = response.get('device_name')
            else:
                self._status.state = ConnectionState(response.get('state', 'disconnected'))
                self._status.device_address = None

            self._status.error = response.get('error')
            return self._status

        except Exception as e:
            logger.error("Status refresh error: %s", e)
            self._status.error = str(e)
            return self._status
