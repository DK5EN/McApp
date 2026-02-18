"""
BLE Service API - HTTP/SSE interface for remote BLE access.

This FastAPI application exposes BLE functionality via REST endpoints
and Server-Sent Events for real-time notifications.
"""

import asyncio
import base64
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .ble_adapter import BLEAdapter, ConnectionState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration from environment
API_KEY = os.getenv("BLE_SERVICE_API_KEY", "")
CORS_ORIGINS = os.getenv("BLE_SERVICE_CORS_ORIGINS", "*").split(",")
BLE_STATE_FILE = os.getenv("BLE_STATE_FILE", "/var/lib/mcapp/ble_state.json")
BLE_AUTO_CONNECT = os.getenv("BLE_AUTO_CONNECT", "true").lower() != "false"
AUTO_CONNECT_DELAY = int(os.getenv("BLE_AUTO_CONNECT_DELAY", "8"))

# Global state
ble_adapter: BLEAdapter | None = None
notification_queue: deque[dict] = deque(maxlen=1000)
notification_event = asyncio.Event()
_reconnect_task: asyncio.Task | None = None
_auto_connect_task: asyncio.Task | None = None
_user_disconnected: bool = False
_last_connected_mac: str | None = None


def crc16_ccitt(data: bytes) -> int:
    """Calculate CRC16-CCITT checksum (polynomial 0x1021)"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
            crc &= 0xFFFF
    return crc


def notification_callback(data: bytes):
    """Called when BLE notification received"""
    timestamp = int(time.time() * 1000)

    # Try to parse as JSON or binary
    notification = {
        "timestamp": timestamp,
        "raw_base64": base64.b64encode(data).decode('ascii'),
        "raw_hex": data.hex(),
    }

    # Attempt to decode
    try:
        if data.startswith(b'D{'):
            # JSON message
            json_str = data.rstrip(b'\x00').decode("utf-8")[1:]
            notification["parsed"] = json.loads(json_str)
            notification["format"] = "json"
        elif data.startswith(b'@'):
            # Binary mesh message
            notification["format"] = "binary"
            notification["prefix"] = data[:2].decode('ascii', errors='replace')

            # FCS validation (permissive mode - log warnings but continue processing)
            if len(data) >= 4:
                payload = data[:-2]
                fcs = int.from_bytes(data[-2:], byteorder='little')
                calced_fcs = crc16_ccitt(payload)
                fcs_ok = (calced_fcs == fcs)

                notification["fcs_ok"] = fcs_ok
                if not fcs_ok:
                    logger.debug(
                        "FCS mismatch: calculated=0x%04X, received=0x%04X",
                        calced_fcs, fcs
                    )
        else:
            notification["format"] = "unknown"
    except Exception as e:
        logger.warning("Notification decode error: %s", e)
        notification["format"] = "raw"

    notification_queue.append(notification)
    notification_event.set()
    logger.debug("Notification queued: %s", notification.get("format", "unknown"))


# --- State persistence ---

def _save_ble_state(mac: str, name: str | None = None) -> None:
    """Persist last-connected device to disk for restart recovery."""
    try:
        state = {
            "device_mac": mac,
            "device_name": name,
            "connected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = BLE_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, BLE_STATE_FILE)
        logger.info("Saved BLE state: %s (%s)", mac, name or "no name")
    except Exception as e:
        logger.warning("Failed to save BLE state: %s", e)


def _load_ble_state() -> str | None:
    """Load last-connected MAC from disk. Returns None if no state."""
    try:
        with open(BLE_STATE_FILE) as f:
            state = json.load(f)
        mac = state.get("device_mac")
        if mac:
            logger.info("Loaded BLE state: %s (%s)",
                        mac, state.get("device_name", "no name"))
        return mac
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def _clear_ble_state() -> None:
    """Remove state file (called on explicit user disconnect)."""
    try:
        os.unlink(BLE_STATE_FILE)
        logger.info("Cleared BLE state file")
    except FileNotFoundError:
        pass


# --- Connect + initialize helper ---

async def _connect_and_initialize(mac: str) -> bool:
    """Connect to device and run post-connect initialization. Returns True on success."""
    # Clean up stale bus before reconnect
    if ble_adapter.bus:
        try:
            ble_adapter.bus.disconnect()
        except Exception:
            pass
        ble_adapter.bus = None

    success = await ble_adapter.connect(mac)
    if success:
        await ble_adapter.start_notify()
        await ble_adapter.send_hello()
        await asyncio.sleep(1.0)
        await ble_adapter.query_extended_registers()
    return success


# --- Auto-reconnect / auto-connect ---

def _push_status_event(state: str, **kwargs):
    """Push a BLE status change event into the notification queue for SSE delivery."""
    event = {
        "event_type": "status",
        "state": state,
        "timestamp": int(time.time() * 1000),
        **kwargs,
    }
    notification_queue.append(event)
    notification_event.set()
    logger.info("Status event pushed: %s", state)


def _on_adapter_disconnect():
    """Called by BLEAdapter when an unexpected disconnect is detected"""
    global _reconnect_task
    _push_status_event("disconnected")
    if _user_disconnected:
        return
    logger.warning("Unexpected disconnect detected, scheduling auto-reconnect")
    # Schedule reconnect (can't await from sync callback)
    if _reconnect_task is None or _reconnect_task.done():
        _reconnect_task = asyncio.create_task(_auto_reconnect())


async def _auto_reconnect():
    """Attempt to reconnect with exponential backoff after unexpected disconnect."""
    mac = _last_connected_mac
    if not mac:
        logger.warning("No previous MAC address for auto-reconnect")
        return

    delays = [5, 10, 20, 60]
    for attempt, delay in enumerate(delays, 1):
        if _user_disconnected:
            logger.info("Auto-reconnect cancelled (user disconnected)")
            return

        logger.info("Auto-reconnect attempt %d/%d in %ds to %s",
                    attempt, len(delays), delay, mac)
        await asyncio.sleep(delay)

        if _user_disconnected:
            return
        if ble_adapter.is_connected:
            logger.info("Already reconnected, stopping auto-reconnect")
            return

        try:
            success = await _connect_and_initialize(mac)
            if success:
                logger.info("Auto-reconnect successful to %s", mac)
                _push_status_event("connected", device_address=mac)
                return
            else:
                logger.warning("Auto-reconnect attempt %d failed", attempt)
        except Exception as e:
            logger.warning("Auto-reconnect attempt %d error: %s", attempt, e)

    logger.error("Auto-reconnect exhausted all %d attempts for %s", len(delays), mac)


async def _startup_auto_connect():
    """Auto-connect to last-known device after service startup."""
    global _last_connected_mac, _user_disconnected

    mac = _load_ble_state()
    if not mac:
        logger.info("No saved BLE state — skipping auto-connect")
        return

    _last_connected_mac = mac
    _user_disconnected = False

    logger.info("Auto-connect: waiting %ds for Bluetooth hardware...", AUTO_CONNECT_DELAY)
    await asyncio.sleep(AUTO_CONNECT_DELAY)

    delays = [5, 10, 20, 60]
    for attempt, delay in enumerate(delays, 1):
        if _user_disconnected:
            logger.info("Startup auto-connect cancelled (user disconnected)")
            return
        if ble_adapter.is_connected:
            logger.info("Already connected, stopping startup auto-connect")
            return

        logger.info("Startup auto-connect attempt %d/%d to %s", attempt, len(delays), mac)
        try:
            success = await asyncio.wait_for(
                _connect_and_initialize(mac), timeout=30.0
            )
            if success:
                logger.info("Startup auto-connect successful to %s", mac)
                return
            else:
                logger.warning("Startup auto-connect attempt %d failed", attempt)
        except Exception as e:
            logger.warning("Startup auto-connect attempt %d error: %s", attempt, e)

        if attempt < len(delays):
            logger.info("Retrying in %ds...", delay)
            await asyncio.sleep(delay)

    logger.error("Startup auto-connect exhausted all attempts for %s", mac)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle"""
    global ble_adapter, _auto_connect_task

    logger.info("Starting BLE Service")
    if not API_KEY:
        logger.warning("No API key configured — BLE service is unauthenticated")
    ble_adapter = BLEAdapter(notification_callback=notification_callback)
    ble_adapter._disconnect_callback = _on_adapter_disconnect

    # Auto-connect to last-known device if enabled
    if BLE_AUTO_CONNECT:
        _auto_connect_task = asyncio.create_task(_startup_auto_connect())
    else:
        logger.info("Auto-connect disabled (BLE_AUTO_CONNECT=false)")

    yield

    # Cleanup
    logger.info("Shutting down BLE Service")
    if _auto_connect_task and not _auto_connect_task.done():
        _auto_connect_task.cancel()
    if _reconnect_task and not _reconnect_task.done():
        _reconnect_task.cancel()
    if ble_adapter and ble_adapter.is_connected:
        await ble_adapter.disconnect()


app = FastAPI(
    title="McApp BLE Service",
    description="Remote BLE access for MeshCom devices",
    version="0.1.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Authentication ---

async def verify_api_key(x_api_key: Annotated[str | None, Header()] = None):
    """Verify API key header"""
    if not API_KEY or API_KEY == "disabled":
        return True

    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


# --- Request/Response Models ---

class ConnectRequest(BaseModel):
    """Connection request"""
    device_address: str | None = None
    device_name: str | None = None


class SendRequest(BaseModel):
    """Send data request"""
    data_base64: str | None = None
    data_hex: str | None = None
    message: str | None = None
    group: str | None = None
    command: str | None = None


class StatusResponse(BaseModel):
    """Status response"""
    connected: bool
    state: str
    device_address: str | None = None
    device_name: str | None = None
    last_activity: float | None = None
    error: str | None = None


class DeviceResponse(BaseModel):
    """Device information"""
    name: str
    address: str
    rssi: int
    paired: bool
    known: bool = False


class ScanResponse(BaseModel):
    """Scan results"""
    devices: list[DeviceResponse]
    count: int


class ResultResponse(BaseModel):
    """Generic result response"""
    success: bool
    message: str


# --- API Endpoints ---

@app.get("/api/ble/status", response_model=StatusResponse)
async def get_status(_: bool = Depends(verify_api_key)):
    """Get current BLE connection status"""
    status = ble_adapter.status

    return StatusResponse(
        connected=ble_adapter.is_connected,
        state=status.state.value,
        device_address=status.device.address if status.device else None,
        device_name=status.device.name if status.device else None,
        last_activity=status.last_activity,
        error=status.error
    )


@app.get("/api/ble/devices", response_model=ScanResponse)
async def scan_devices(
    timeout: float = Query(default=5.0, ge=1.0, le=30.0),
    prefix: str = Query(default="MC-"),
    _: bool = Depends(verify_api_key)
):
    """Scan for BLE devices"""
    if ble_adapter._operation_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Another BLE operation is in progress"
        )
    if ble_adapter.is_connected:
        raise HTTPException(
            status_code=409,
            detail="Cannot scan while connected. Disconnect first."
        )

    try:
        devices = await ble_adapter.scan(timeout=timeout, prefix=prefix)
        return ScanResponse(
            devices=[
                DeviceResponse(
                    name=d.name,
                    address=d.address,
                    rssi=d.rssi,
                    paired=d.paired,
                    known=d.known
                )
                for d in devices
            ],
            count=len(devices)
        )
    except Exception as e:
        logger.error("Scan error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/connect", response_model=ResultResponse)
async def connect(request: ConnectRequest, _: bool = Depends(verify_api_key)):
    """Connect to a BLE device"""
    if not request.device_address and not request.device_name:
        raise HTTPException(
            status_code=400,
            detail="Either device_address or device_name required"
        )

    # If only name provided, scan for device
    mac = request.device_address
    if not mac and request.device_name:
        devices = await ble_adapter.scan(timeout=5.0)
        for device in devices:
            if device.name == request.device_name:
                mac = device.address
                break
        if not mac:
            raise HTTPException(
                status_code=404,
                detail=f"Device '{request.device_name}' not found"
            )

    try:
        global _user_disconnected, _last_connected_mac
        _user_disconnected = False
        success = await _connect_and_initialize(mac)
        if success:
            _last_connected_mac = mac
            _save_ble_state(mac, request.device_name)
            return ResultResponse(success=True, message=f"Connected to {mac}")
        else:
            return ResultResponse(success=False, message="Connection failed")
    except Exception as e:
        logger.error("Connect error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/disconnect", response_model=ResultResponse)
async def disconnect(_: bool = Depends(verify_api_key)):
    """Disconnect from current device (also resets ERROR state)"""
    global _user_disconnected, _reconnect_task, _auto_connect_task
    _user_disconnected = True
    _clear_ble_state()

    # Cancel any pending auto-reconnect or auto-connect
    if _auto_connect_task and not _auto_connect_task.done():
        _auto_connect_task.cancel()
        _auto_connect_task = None
    if _reconnect_task and not _reconnect_task.done():
        _reconnect_task.cancel()
        _reconnect_task = None

    if ble_adapter.status.state == ConnectionState.DISCONNECTED:
        return ResultResponse(success=True, message="Already disconnected")

    try:
        await ble_adapter.disconnect()
        return ResultResponse(success=True, message="Disconnected")
    except Exception as e:
        logger.error("Disconnect error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/send", response_model=ResultResponse)
async def send_data(request: SendRequest, _: bool = Depends(verify_api_key)):
    """Send data to connected device"""
    if not ble_adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        # Determine what to send
        if request.command:
            success = await ble_adapter.send_command(request.command)
        elif request.message is not None and request.group is not None:
            success = await ble_adapter.send_message(request.message, request.group)
        elif request.data_base64:
            data = base64.b64decode(request.data_base64)
            success = await ble_adapter.write(data)
        elif request.data_hex:
            data = bytes.fromhex(request.data_hex)
            success = await ble_adapter.write(data)
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide command, message+group, data_base64, or data_hex"
            )

        # If write failed, check if device disconnected during the write
        if not success and not ble_adapter.is_connected:
            raise HTTPException(status_code=409, detail="Not connected")

        if request.command:
            msg = f"Command sent: {request.command}" if success else "Send failed"
        elif request.message is not None and request.group is not None:
            msg = (f"Message sent to group {request.group}" if request.group
                   else "Message sent (broadcast)") if success else "Send failed"
        else:
            msg = f"Sent {len(data)} bytes" if success else "Send failed"

        return ResultResponse(success=success, message=msg)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Send error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/pair", response_model=ResultResponse)
async def pair_device(request: ConnectRequest, _: bool = Depends(verify_api_key)):
    """Pair with a BLE device"""
    if not request.device_address:
        raise HTTPException(status_code=400, detail="device_address required")

    if ble_adapter._operation_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Another BLE operation is in progress"
        )

    if ble_adapter.is_connected:
        raise HTTPException(
            status_code=409,
            detail="Disconnect before pairing"
        )

    try:
        success = await ble_adapter.pair(request.device_address)
        return ResultResponse(
            success=success,
            message=f"Paired with {request.device_address}" if success else "Pairing failed"
        )
    except Exception as e:
        logger.error("Pair error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/unpair", response_model=ResultResponse)
async def unpair_device(request: ConnectRequest, _: bool = Depends(verify_api_key)):
    """Unpair a BLE device"""
    if not request.device_address:
        raise HTTPException(status_code=400, detail="device_address required")

    if ble_adapter._operation_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Another BLE operation is in progress"
        )

    try:
        success = await ble_adapter.unpair(request.device_address)
        return ResultResponse(
            success=success,
            message=f"Unpaired {request.device_address}" if success else "Unpair failed"
        )
    except Exception as e:
        logger.error("Unpair error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/settime", response_model=ResultResponse)
async def set_device_time(_: bool = Depends(verify_api_key)):
    """Set current time on connected device"""
    if not ble_adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await ble_adapter.set_time()
        return ResultResponse(
            success=success,
            message="Time set" if success else "Failed to set time"
        )
    except Exception as e:
        logger.error("Set time error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/config/callsign", response_model=ResultResponse)
async def set_callsign(callsign: str, _: bool = Depends(verify_api_key)):
    """Set device callsign (0x50 message)"""
    if not ble_adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await ble_adapter.set_callsign(callsign)
        return ResultResponse(
            success=success,
            message=f"Callsign set to {callsign}" if success else "Failed to set callsign"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Set callsign error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/config/wifi", response_model=ResultResponse)
async def set_wifi(ssid: str, password: str, _: bool = Depends(verify_api_key)):
    """Configure WiFi credentials (0x55 message)"""
    if not ble_adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await ble_adapter.set_wifi(ssid, password)
        return ResultResponse(
            success=success,
            message=f"WiFi configured: {ssid}" if success else "Failed to configure WiFi"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Set WiFi error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/config/position", response_model=ResultResponse)
async def set_position(
    lat: float,
    lon: float,
    alt: int,
    save: bool = False,
    _: bool = Depends(verify_api_key)
):
    """Set GPS position (0x70/0x80/0x90 messages)"""
    if not ble_adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        # Send all three position messages
        success_lat = await ble_adapter.set_latitude(lat, save)
        await asyncio.sleep(0.2)
        success_lon = await ble_adapter.set_longitude(lon, save)
        await asyncio.sleep(0.2)
        success_alt = await ble_adapter.set_altitude(alt, save)

        success = success_lat and success_lon and success_alt
        return ResultResponse(
            success=success,
            message=f"Position set: ({lat}, {lon}, {alt}m)" if success else "Failed"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Set position error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/config/aprs", response_model=ResultResponse)
async def set_aprs_symbols(
    primary: str,
    secondary: str,
    _: bool = Depends(verify_api_key)
):
    """Set APRS symbol (0x95 message)"""
    if not ble_adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await ble_adapter.set_aprs_symbols(primary, secondary)
        return ResultResponse(
            success=success,
            message=f"APRS symbol set: {primary}{secondary}" if success else "Failed"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Set APRS symbols error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ble/config/save", response_model=ResultResponse)
async def save_config(_: bool = Depends(verify_api_key)):
    """
    Save configuration and reboot device (0xF0 message).

    WARNING: This will immediately reboot the device and disconnect BLE.
    """
    if not ble_adapter.is_connected:
        raise HTTPException(status_code=409, detail="Not connected")

    try:
        success = await ble_adapter.save_and_reboot()
        return ResultResponse(
            success=success,
            message="Device rebooting (settings saved)" if success else "Failed to save"
        )
    except Exception as e:
        logger.error("Save & reboot error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# --- SSE Notifications ---

@app.get("/api/ble/notifications")
async def stream_notifications(
    x_api_key: Annotated[str | None, Header()] = None
):
    """
    Server-Sent Events stream of BLE notifications.

    Connect to this endpoint to receive real-time BLE notifications.
    Each event contains:
    - timestamp: Unix timestamp in milliseconds
    - raw_base64: Raw notification data (base64 encoded)
    - raw_hex: Raw notification data (hex encoded)
    - format: "json", "binary", or "raw"
    - parsed: Parsed JSON data (if format is "json")
    """
    # Verify API key
    if API_KEY and API_KEY != "disabled":
        if x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")

    async def event_generator():
        """Generate SSE events from notification queue"""
        last_sent = 0

        # Send initial status
        yield {
            "event": "status",
            "data": json.dumps({
                "connected": ble_adapter.is_connected,
                "state": ble_adapter.status.state.value,
                "timestamp": int(time.time() * 1000)
            })
        }

        while True:
            # Wait for new notifications
            try:
                await asyncio.wait_for(notification_event.wait(), timeout=30.0)
                notification_event.clear()
            except asyncio.TimeoutError:
                # Send keepalive ping
                yield {
                    "event": "ping",
                    "data": json.dumps({"timestamp": int(time.time() * 1000)})
                }
                continue

            # Send all queued notifications/status events
            while notification_queue:
                notification = notification_queue.popleft()
                if notification["timestamp"] > last_sent:
                    last_sent = notification["timestamp"]
                    # Status events use "status" SSE event type
                    if notification.get("event_type") == "status":
                        yield {
                            "event": "status",
                            "data": json.dumps(notification)
                        }
                    else:
                        yield {
                            "event": "notification",
                            "data": json.dumps(notification)
                        }

    return EventSourceResponse(event_generator())


# --- Health Check ---

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "ble_connected": ble_adapter.is_connected if ble_adapter else False,
        "timestamp": int(time.time() * 1000)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("BLE_SERVICE_PORT", "8081")),
        reload=False
    )
