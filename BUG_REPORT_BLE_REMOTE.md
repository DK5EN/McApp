# BLE Remote Service - Phase 3 Implementation Gaps

**Severity:** MEDIUM (affects remote BLE deployments only)
**Affected Component:** `ble_service/` (remote BLE service)
**Created:** 2026-02-14

## Problem Statement

The BLE remote service (`ble_service/`) is missing all Phase 3 protocol features that were implemented in the main BLE handler (`src/mcapp/ble_handler.py`). This makes remote BLE mode incomplete and unsuitable for production use.

## Missing Features

### 1. Message Types 0x50-0x95, 0xF0

These 7 message types are implemented in `ble_handler.py` but missing in `ble_adapter.py`:

| Type | Purpose | Implementation Status |
|------|---------|----------------------|
| 0x50 | Set Callsign | ❌ Missing |
| 0x55 | WiFi Settings | ❌ Missing |
| 0x70 | Set Latitude | ❌ Missing |
| 0x80 | Set Longitude | ❌ Missing |
| 0x90 | Set Altitude | ❌ Missing |
| 0x95 | APRS Symbols | ❌ Missing |
| 0xF0 | Save & Reboot | ❌ Missing |

### 2. FCS Validation

Binary message checksum validation is missing from notification parsing.

### 3. Extended Register Queries

Auto-query of `--seset`, `--wifiset`, `--weather`, `--analogset` on connection is missing.

## Implementation Examples

### Step 1: Add Methods to `ble_service/src/ble_adapter.py`

Add these methods after the existing `set_time()` method (line 673):

```python
async def set_callsign(self, callsign: str) -> bool:
    """
    Set device callsign (0x50 message).

    Args:
        callsign: New callsign (e.g., "DL4GLE-10")

    Returns:
        True if successful
    """
    if not self.is_connected:
        raise RuntimeError("Not connected")

    # Validate callsign format (basic validation)
    if not callsign or len(callsign) > 15:
        raise ValueError("Callsign must be 1-15 characters")

    callsign_bytes = callsign.encode('utf-8')
    length = len(callsign_bytes) + 2

    if length > 247:  # MTU limit
        raise ValueError(f"Callsign too long: {length} bytes (max 247)")

    byte_array = length.to_bytes(1, 'big') + bytes([0x50]) + callsign_bytes
    return await self.write(bytes(byte_array))


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
    if not ssid or len(ssid) > 32:
        raise ValueError("SSID must be 1-32 characters")
    if len(password) > 63:
        raise ValueError("Password must be 0-63 characters")

    ssid_bytes = ssid.encode('utf-8')
    pwd_bytes = password.encode('utf-8')

    # Format: [SSID_len][SSID][PWD_len][PWD]
    byte_array = (
        bytes([len(ssid_bytes)]) + ssid_bytes +
        bytes([len(pwd_bytes)]) + pwd_bytes
    )
    length = len(byte_array) + 2

    if length > 247:  # MTU limit
        raise ValueError(f"WiFi config too long: {length} bytes (max 247)")

    byte_array = length.to_bytes(1, 'big') + bytes([0x55]) + byte_array
    return await self.write(bytes(byte_array))


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

    if not -90.0 <= lat <= 90.0:
        raise ValueError("Latitude must be between -90.0 and 90.0")

    import struct
    save_flag = 0x0A if save else 0x0B
    byte_array = struct.pack('<f', lat) + bytes([save_flag])
    length = len(byte_array) + 2

    byte_array = length.to_bytes(1, 'big') + bytes([0x70]) + byte_array
    return await self.write(bytes(byte_array))


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

    if not -180.0 <= lon <= 180.0:
        raise ValueError("Longitude must be between -180.0 and 180.0")

    import struct
    save_flag = 0x0A if save else 0x0B
    byte_array = struct.pack('<f', lon) + bytes([save_flag])
    length = len(byte_array) + 2

    byte_array = length.to_bytes(1, 'big') + bytes([0x80]) + byte_array
    return await self.write(bytes(byte_array))


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

    if not -1000 <= alt <= 10000:
        raise ValueError("Altitude must be between -1000 and 10000 meters")

    save_flag = 0x0A if save else 0x0B
    byte_array = alt.to_bytes(4, byteorder='little', signed=True) + bytes([save_flag])
    length = len(byte_array) + 2

    byte_array = length.to_bytes(1, 'big') + bytes([0x90]) + byte_array
    return await self.write(bytes(byte_array))


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

    byte_array = bytes([primary_byte, secondary_byte])
    length = len(byte_array) + 2

    byte_array = length.to_bytes(1, 'big') + bytes([0x95]) + byte_array
    return await self.write(bytes(byte_array))


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

    byte_array = bytes([0x02, 0xF0])  # Length=2, ID=0xF0, no data
    return await self.write(byte_array)


async def query_extended_registers(self):
    """
    Query extended device registers on connection.

    Sends: --seset, --wifiset, --weather, --analogset
    Note: --seset and --wifiset send multi-part responses (SE+S1, SW+S2)
    """
    if not self.is_connected:
        logger.warning("Cannot query registers: not connected")
        return

    commands = [
        ("--seset", 1.2),     # TYP: SE + S1 (multi-part)
        ("--wifiset", 1.2),   # TYP: SW + S2 (multi-part)
        ("--weather", 0.8),   # TYP: W
        ("--analogset", 0.8), # TYP: AN
    ]

    for cmd, delay in commands:
        try:
            await self.send_command(cmd)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.warning("Extended query %s failed: %s", cmd, e)
```

### Step 2: Add FastAPI Endpoints to `ble_service/src/main.py`

Add these endpoints after the existing `/api/ble/settime` endpoint (line 463):

```python
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
            message=f"Position set: ({lat}, {lon}, {alt}m)" if success else "Failed to set position"
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
            message=f"APRS symbol set: {primary}{secondary}" if success else "Failed to set symbol"
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
```

### Step 3: Add FCS Validation

Copy the CRC16-CCITT implementation from `src/mcapp/ble_handler.py` to `ble_service/src/ble_adapter.py`:

```python
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
```

Then modify `notification_callback()` in `ble_service/src/main.py` (around line 63):

```python
elif data.startswith(b'@'):
    # Binary mesh message
    notification["format"] = "binary"
    notification["prefix"] = data[:2].decode('ascii', errors='replace')

    # FCS validation (permissive mode)
    if len(data) >= 4:
        payload = data[:-2]
        fcs = int.from_bytes(data[-2:], byteorder='little')
        calced_fcs = crc16_ccitt(payload)
        fcs_ok = (calced_fcs == fcs)

        notification["fcs_ok"] = fcs_ok
        if not fcs_ok:
            logger.warning(
                "FCS mismatch: calculated=0x%04X, received=0x%04X",
                calced_fcs, fcs
            )
```

### Step 4: Add Extended Queries on Connect

Modify the `connect()` endpoint in `ble_service/src/main.py` (line 319):

```python
if success:
    _last_connected_mac = mac
    # Start notifications and send hello
    await ble_adapter.start_notify()
    await ble_adapter.send_hello()

    # Wait for hello handshake
    await asyncio.sleep(1.0)

    # Query extended registers (new)
    await ble_adapter.query_extended_registers()

    return ResultResponse(success=True, message=f"Connected to {mac}")
```

### Step 5: Update Module Docstring

Add multi-part response documentation to `ble_service/src/ble_adapter.py` (top of file):

```python
"""
BLE Adapter - D-Bus/BlueZ interface for BLE device communication.

This module provides a clean async interface to BlueZ via D-Bus,
handling device discovery, connection, GATT operations, and notifications.

Multi-Part Responses:
    Some MeshCom commands send MULTIPLE JSON notifications in sequence:
    - --seset sends TYP:SE followed by TYP:S1 (sensor settings, ~200ms apart)
    - --wifiset sends TYP:SW followed by TYP:S2 (WiFi settings, ~200ms apart)

    These are separate BLE notifications, not a single message. The
    notification callback will be invoked twice for each command.

Supported Message Types:
    0x10: Hello/Wakeup (send_hello)
    0x20: Time Sync (set_time)
    0x50: Set Callsign (set_callsign)
    0x55: WiFi Settings (set_wifi)
    0x70: Set Latitude (set_latitude)
    0x80: Set Longitude (set_longitude)
    0x90: Set Altitude (set_altitude)
    0x95: APRS Symbols (set_aprs_symbols)
    0xA0: Text Commands (send_command, send_message)
    0xF0: Save & Reboot (save_and_reboot)
"""
```

## Testing

After implementation, test with:

```bash
# Start BLE service
cd ble_service
uvicorn src.main:app --host 0.0.0.0 --port 8081

# Test callsign
curl -X POST "http://localhost:8081/api/ble/config/callsign?callsign=DL4GLE-10" \
  -H "X-API-Key: your-key"

# Test WiFi
curl -X POST "http://localhost:8081/api/ble/config/wifi?ssid=TestNet&password=secret123" \
  -H "X-API-Key: your-key"

# Test position
curl -X POST "http://localhost:8081/api/ble/config/position" \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{"lat": 48.1234, "lon": 11.5678, "alt": 500, "save": true}'

# Test save
curl -X POST "http://localhost:8081/api/ble/config/save" \
  -H "X-API-Key: your-key"
```

## Priority

**MEDIUM** - Only affects remote BLE deployments. Local BLE mode (production default) is complete.

## Workaround

Use local BLE mode (`MCAPP_BLE_MODE=local`) on Raspberry Pi instead of remote mode.
