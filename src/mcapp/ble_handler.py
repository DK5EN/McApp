#!/usr/bin/env python3
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from struct import pack, unpack
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.errors import DBusError, InterfaceNotFoundError
from dbus_next.service import ServiceInterface, method
from timezonefinder import TimezoneFinder

"""
BLE Handler - D-Bus/BlueZ Bluetooth Low Energy Interface

This module provides direct Bluetooth Low Energy connectivity using the
BlueZ D-Bus API. It handles:

- BLE device discovery, pairing, and connection management
- GATT characteristic read/write operations
- Binary and JSON message encoding/decoding for MeshCom protocol
- APRS position and telemetry parsing
- Multi-part configuration responses (SE+S1, SW+S2)

Multi-Part Configuration Responses:

Some BLE configuration queries return data split across TWO separate
notifications, sent sequentially with ~200ms delay between parts:

- SE (sensor settings) → followed by S1 (extended sensor data)
- SW (WiFi settings) → followed by S2 (extended WiFi data)

These pairs are NOT correlated in the backend. Each notification is
processed independently via `dispatcher()` and published to the frontend
as separate SSE events. The frontend is responsible for merging related
pairs if needed.

This behavior occurs when querying device configuration via:
- `--seset` command (triggers SE + S1 sequence)
- `--wifiset` command (triggers SW + S2 sequence)

See `_query_ble_registers()` in main.py for query implementation.
See `dispatcher()` for SE/S1/SW/S2 message routing.

Architecture:
- BLEClient class: Connection lifecycle, GATT I/O, keep-alive
- Decoders: Binary/JSON message parsing
- Transformers: Convert raw BLE data to standardized message dicts
- Dispatcher: Route messages by type to appropriate transformer
"""

logger = logging.getLogger(__name__)

VERSION = "v0.48.0"

has_console = sys.stdout.isatty()

# DBus constants
BLUEZ_SERVICE_NAME = "org.bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/com/example/agent"

# BLE Protocol Constants
MAX_BLE_MTU = 247  # Maximum BLE packet size in bytes (per MeshCom spec)

# Timing Constants (seconds)
BLE_CONNECT_TIMEOUT = 10.0          # Device connection timeout
BLE_SERVICES_CHECK_INTERVAL = 0.5   # Service resolution polling interval
BLE_KEEPALIVE_INTERVAL = 30.0       # Keep-alive message interval
BLE_HELLO_DELAY = 1.0                # Delay after hello handshake
BLE_COMMAND_RETRY_BACKOFF = 0.5     # Base delay for retry backoff
BLE_DISCONNECT_DELAY = 2.0          # Delay before disconnect operations
BLE_RECONNECT_DELAY = 3.0           # Delay before reconnection attempts

# Global client instance (managed by this module)
client = None

# Console detection
has_console = sys.stdout.isatty()


def calc_fcs(msg: bytes) -> int:
    """Calculate frame checksum"""
    fcs = 0
    for x in range(0, len(msg)):
        fcs = fcs + msg[x]

    # SWAP MSB/LSB
    fcs = ((fcs & 0xFF00) >> 8) | ((fcs & 0xFF) << 8)

    return fcs


def hex_msg_id(msg_id: int) -> str:
    """Convert message ID to hex string"""
    return f"{msg_id:08X}"


def ascii_char(val: int) -> str:
    """Convert value to ASCII character"""
    return chr(val)


def strip_prefix(msg: str, prefix: str = ":") -> str:
    """Strip prefix from message if present"""
    return msg[1:] if msg.startswith(prefix) else msg


def decode_json_message(byte_msg: bytes) -> dict[str, Any] | None:
    try:
        json_str = byte_msg.rstrip(b'\x00').decode("utf-8")[1:]
        return json.loads(json_str)

    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"Error decoding JSON message: {e}")
        return None


def decode_binary_message(byte_msg: bytes) -> dict[str, Any] | str:
    # little-endian unpack
    raw_header = byte_msg[1:7]
    [payload_type, msg_id, max_hop_raw] = unpack('<BIB', raw_header)

    # Bit shift operations
    max_hop = max_hop_raw & 0x0F
    mesh_info = max_hop_raw >> 4

    # Calculate frame checksum
    calced_fcs = calc_fcs(byte_msg[1:-11])

    remaining_msg = byte_msg[7:].rstrip(b'\x00')  # Extract data after hop count byte

    if byte_msg[:2] == b'@A':  # Check if this is an ACK frame
        # ACK Message Format: [0x41] [MSG_ID-4] [FLAGS] [ACK_MSG_ID-4] [ACK_TYPE] [0x00]

        # Decode FLAGS byte
        server_flag = bool(max_hop_raw & 0x80)  # Bit 7: Server Flag
        hop_count = max_hop_raw & 0x7F  # Bits 0-6: Hop Count

        # Extract ACK-specific fields
        if len(byte_msg) >= 12:
            # ACK_MSG_ID (original message ID being acknowledged)
            [ack_id] = unpack('<I', byte_msg[6:10])

            # ACK_TYPE
            ack_type = byte_msg[10] if len(byte_msg) > 10 else 0
            if ack_type == 0x00:
                ack_type_text = "Node ACK"
            elif ack_type == 0x01:
                ack_type_text = "Gateway ACK"
            else:
                ack_type_text = f"Unknown ({ack_type})"

            # Extract Gateway ID and ACK ID from msg_id
            if ack_type == 0x01:
                gateway_id = (msg_id >> 10) & 0x3FFFFF  # Bits 31-10: Gateway ID (22 Bits)
                ack_id_part = msg_id & 0x3FF  # Bits 9-0: ACK ID (10 Bits)
            else:
                gateway_id = None
                ack_id_part = None
        else:
            # Fallback for legacy implementation
            [ack_id] = unpack('<I', byte_msg[-5:-1])
            ack_type = None
            ack_type_text = None
            server_flag = None
            hop_count = max_hop
            gateway_id = None
            ack_id_part = None

        # Display message as hex
        [message] = unpack(f'<{len(remaining_msg)}s', remaining_msg)
        message = message.hex().upper()

        json_obj = {
            "payload_type": payload_type,
            "msg_id": msg_id,
            "max_hop": max_hop,
            "mesh_info": mesh_info,
            "message": message,
            "ack_id": ack_id,
            "ack_type": ack_type,
            "ack_type_text": ack_type_text,
            "server_flag": server_flag,
            "hop_count": hop_count,
            "gateway_id": gateway_id,
            "ack_id_part": ack_id_part
        }

        # Remove None values for cleaner JSON
        json_obj = {k: v for k, v in json_obj.items() if v is not None}

        return json_obj

    elif bytes(byte_msg[:2]) in {b'@:', b'@!'}:

      split_idx = remaining_msg.find(b'>')
      if split_idx == -1:
        return "Invalid routing format"

      path = remaining_msg[:split_idx+1].decode("utf-8", errors="ignore")
      remaining_msg = remaining_msg[split_idx + 1:]

      # Extrahiere Dest-Type (`dt`)
      if payload_type == 58:
        split_idx = remaining_msg.find(b':')
      elif payload_type == 33:
        split_idx = remaining_msg.find(b'*')+1
      else:
        print(f"Payload type not matched! {payload_type}")

      if split_idx == -1:
         return "Destination not found"

      dest = remaining_msg[:split_idx].decode("utf-8", errors="ignore")

      raw = remaining_msg[split_idx:remaining_msg.find(b'\00')]
      message = raw.decode("utf-8", errors="ignore").strip()

      # Extract binary footer (fixed structure at end of message)
      [zero, hardware_id, lora_mod, fcs, fw, lasthw, fw_sub, ending, time_ms] = unpack(
          '<BBBHBBBBI', byte_msg[-14:-1]
      )


      # Split lasthw byte into hardware ID and last sending flag
      last_hw_id = lasthw & 0x7F        # Bits 0-6: Hardware-Typ (0-127)
      last_sending = bool(lasthw & 0x80) # Bit 7: Last Sending Flag (True/False)

      # Verify frame checksum
      fcs_ok = (calced_fcs == fcs)

      # FCS validation (permissive mode - log at debug level, continue processing)
      if not fcs_ok:
          logger.debug(
              "Frame checksum mismatch: calculated=0x%04X, received=0x%04X, msg_id=%s",
              calced_fcs, fcs, format(msg_id, '08X')
          )
          # Permissive mode: log at debug level but continue processing
          # TODO: Add config flag ENFORCE_FCS_VALIDATION for strict mode

      #if message.startswith(":{CET}"):
      #  dest_type = "Datum & Zeit Broadcast an alle"

      #elif path.startswith("response"):
      #  dest_type = "user input response"

      #elif message.startswith("!"):
      #  dest_type = "Positionsmeldung"

      #elif dest == "*":
      #  dest_type = "Broadcast an alle"

      #elif dest.isdigit():
      #  dest_type = f"Gruppennachricht an {dest}"

      #else:
      #  dest_type = f"Direktnachricht an {dest}"

#      json_obj = {k: v for k, v in locals().items() if k in [
#          "payload_type",
#          "msg_id",
#          "max_hop",
#          "mesh_info",
#          "dest_type",
#          "path",
#          "dest",
#          "message",
#          "hardware_id",
#          "lora_mod",
#          "fcs",
#          "fcs_ok",
#          "fw",
#          "fw_subver",
#          "lasthw",
#          "time_ms",
#          "ending"
#          ]}

      json_obj = {k: v for k, v in locals().items() if k in [
          "payload_type",
          "msg_id",
          "max_hop",
          "mesh_info",
          "path",
          "dest",
          "message",
          "hardware_id",
          "lora_mod",
          "fw",
          "fw_sub",
          "last_hw_id",
          "last_sending"
          ]}

      return json_obj

    else:
       return "Invalid mesh format"


def get_timezone_info(lat: float, lon: float) -> dict[str, Any] | None:
    """Get timezone information for coordinates"""
    tf = TimezoneFinder()
    tz_name = tf.timezone_at(lat=lat, lng=lon)

    if not tz_name:
        print("❌ Could not determine timezone")
        return None

    # Use system time (UTC) and apply tz_name
    now_utc = datetime.utcnow()
    dt_local = datetime.fromtimestamp(now_utc.timestamp(), ZoneInfo(tz_name))

    return {
        "timezone": tz_name,
        "offset_hours": dt_local.utcoffset().total_seconds() / 3600
    }


def timestamp_from_date_time(date: str, time_str: str) -> int:
    """Convert date and time strings to timestamp"""
    dt_str = f"{date} {time_str}"
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt = datetime.strptime("1970-01-01 00:00:00", "%Y-%m-%d %H:%M:%S")

    return int(dt.timestamp() * 1000)


def safe_timestamp_from_dict(input_dict: dict[str, Any]) -> int | None:
    """Safely extract timestamp from dict with various formats"""
    date_str = input_dict.get("DATE")
    time_str = input_dict.get("TIME")

    if not date_str:
        return None

    try:
        # Case 1: Full datetime string in DATE field
        if " " in date_str and not time_str:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        # Case 2: Separate DATE and TIME fields
        elif time_str:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        # Case 3: Date only, assume midnight
        else:
            dt = datetime.strptime(date_str, "%Y-%m-%d")

        timestamp_ms = int(dt.timestamp() * 1000)
        return timestamp_ms

    except Exception as e:
        print(f"❌ Failed to parse date/time: {e}")
        return None


def node_time_checker(node_timestamp: int, typ: str = "") -> float:
    """Check time difference between node and current time"""
    current_time = int(time.time() * 1000)  # current time in ms

    time_delta_ms = current_time - node_timestamp
    time_delta_s = time_delta_ms / 1000

    if abs(time_delta_s) > 60:
        print("⏱️ Time difference > 60 seconds")
        # Human-readable time
        current_dt = datetime.fromtimestamp(current_time / 1000)
        node_dt = datetime.fromtimestamp(node_timestamp / 1000)

        print("curr ", current_dt.strftime("%d %b %Y %H:%M:%S"))
        print("node ", node_dt.strftime("%d %b %Y %H:%M:%S"))

    return time_delta_s


def parse_aprs_position(message: str) -> dict[str, Any] | None:
    """Parse APRS position format"""
    import re
    # Extended APRS position format with optional symbol and symbol group
    match = re.match(
        r"!(\d{2})(\d{2}\.\d{2})([NS])([/\\])(\d{3})(\d{2}\.\d{2})([EW])([ -~]?)",
        message
    )
    if not match:
        return None

    lat_deg, lat_min, lat_dir, symbol_group, lon_deg, lon_min, lon_dir, symbol = match.groups()

    lat = int(lat_deg) + float(lat_min) / 60
    lon = int(lon_deg) + float(lon_min) / 60

    if lat_dir == 'S':
        lat = -lat
    if lon_dir == 'W':
        lon = -lon

    result = {
        "transformer2": "APRS",
        "lat": round(lat, 4),
        #"lat_dir": lat_dir,
        "lon": round(lon, 4),
        #"lon_dir": lon_dir,
        "aprs_symbol": symbol or "?",
        "aprs_symbol_group": symbol_group,
    }

    # Altitude in feet: /A=001526
    alt_match = re.search(r"/A=(\d{6})", message)
    if alt_match:
        altitude_ft = int(alt_match.group(1))
        result["alt"] = round(altitude_ft * 0.3048)

    # Battery level: /B=085
    battery_match = re.search(r"/B=(\d{3})", message)
    if battery_match:
        result["batt"] = int(battery_match.group(1))

    # Groups: /R=...;...;...
    group_match = re.search(r"/R=((?:\d{1,5};?){1,6})", message)
    if group_match:
        groups = group_match.group(1).split(";")
        for i, g in enumerate(groups):
            if g.isdigit():
                result[f"group_{i}"] = int(g)

    # Weather fields from weather stations (e.g. DK5EN-12)
    # /P=940.3 (QFE), /H=42.1 (humidity), /T=22.6 (temp), /Q=956.9 (QNH)
    weather_fields = {
        "temp1": r"/T=([\d.]+)",
        "hum": r"/H=([\d.]+)",
        "qfe": r"/P=([\d.]+)",
        "qnh": r"/Q=([\d.]+)",
    }
    for field, pattern in weather_fields.items():
        m = re.search(pattern, message)
        if m:
            try:
                result[field] = float(m.group(1))
            except ValueError:
                pass

    return result


def parse_aprs_telemetry(message: str) -> dict[str, Any] | None:
    """Parse APRS T# telemetry format.

    Format: T#seq,v1,v2,v3,v4,v5,bits
    MeshCom convention: v1=qfe, v2=temp1, v3=hum, v4=qnh, v5=co2
    """
    import re
    match = re.match(
        r'T#(\d+),([\d.]+),([\d.]+),([\d.]+),([\d.]+),([\d.]+),(\d+)',
        message,
    )
    if not match:
        return None

    seq, v1, v2, v3, v4, v5, _bits = match.groups()

    result = {"tele_seq": int(seq)}
    try:
        result["qfe"] = float(v1)
        result["temp1"] = float(v2)
        result["hum"] = float(v3)
        result["qnh"] = float(v4)
        v5_val = float(v5)
        if v5_val > 0:
            result["co2"] = int(v5_val)
    except ValueError:
        pass

    return result


def transform_tele(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    """Transform a BLE telemetry message (APRS T# format)."""
    tele = parse_aprs_telemetry(input_dict.get("message", "")) or {}
    src, _ = split_path(input_dict["path"], own_callsign)
    if not src and own_callsign:
        src = own_callsign
    return {
        "transformer": "tele",
        "type": "tele",
        "src": src,
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "msg": input_dict.get("message", ""),
        "hw_id": input_dict.get("hardware_id"),
        **tele,
        **transform_common_fields(input_dict, own_callsign),
    }


def split_path(path: str, own_callsign: str = "") -> tuple[str, str]:
    """Split BLE path into (src, via), stripping own callsign.

    path: e.g. "DL8DD-7,DK5EN-99>" or "DO7TW-1,DB0FHR-12,DK5EN-99>"
    Returns: ("DL8DD-7", "") or ("DO7TW-1", "DO7TW-1,DB0FHR-12")
    """
    parts = path.rstrip(">").strip().split(",")
    if own_callsign:
        filtered = [p for p in parts if p.upper() != own_callsign.upper()]
    else:
        filtered = parts
    src = filtered[0] if filtered else parts[0]
    via = ",".join(filtered)
    return src, via


def transform_common_fields(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    _, via = split_path(input_dict.get("path", ""), own_callsign)
    return {
        "transformer1": "common_fields",
        "src_type": "ble",
        #"firmware": str(input_dict.get("fw","")) + ascii_char(input_dict.get("fw_subver")),
        "firmware": input_dict.get("fw",""),
        #"fw_sub": input_dict.get("fw_sub"),
        "fw_sub": ascii_char(input_dict.get("fw_sub")) if input_dict.get("fw_sub") else None,
        "via": via,
        "max_hop": input_dict.get("max_hop"),
        "mesh_info": input_dict.get("mesh_info"),
        "lora_mod": input_dict.get("lora_mod"),
        "last_hw_id": input_dict.get("last_hw_id"),
        "last_sending": input_dict.get("last_sending"),
        "timestamp": int(time.time() * 1000),
    }


def transform_msg(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    src, _ = split_path(input_dict["path"], own_callsign)
    return {
        "transformer": "msg",
        "src_type": "ble",
        "type": "msg",
        **input_dict,
        "src": src,
        "dst": input_dict["dest"],
        "msg": strip_prefix(input_dict["message"]),
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "hw_id": input_dict["hardware_id"],
        **transform_common_fields(input_dict, own_callsign)
    }


def transform_ack(input_dict: dict[str, Any]) -> dict[str, Any]:
    return {
       "transformer": "ack",
       "src_type": "ble",
       "type": "ack",
       **input_dict,
       "msg_id": format(input_dict.get("msg_id"), '08X'),
       "ack_id": format(input_dict.get("ack_id"), '08X'),
       "timestamp": int(time.time() * 1000)
    }


def transform_pos(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any]:
    aprs = parse_aprs_position(input_dict["message"]) or {}
    src, _ = split_path(input_dict["path"], own_callsign)
    return {
        "transformer": "pos",
        "type": "pos",
        "src": src,
        "msg_id": hex_msg_id(input_dict["msg_id"]),
        "msg": input_dict["message"],
        "hw_id": input_dict.get("hardware_id"),
        **aprs,
        **transform_common_fields(input_dict, own_callsign)
    }


def transform_mh(input_dict: dict[str, Any]) -> dict[str, Any]:
    node_timestamp = timestamp_from_date_time(input_dict["DATE"], input_dict["TIME"])
    return {
        "transformer": "mh",
        "src_type": "ble",
        "type": "pos",
        "src": input_dict["CALL"],
        "rssi": input_dict.get("RSSI"),
        "snr": input_dict.get("SNR"),
        "hw_id": input_dict.get("HW"),
        "lora_mod": input_dict.get("MOD"),
        "mesh": input_dict.get("MESH"),
        "node_timestamp": node_timestamp,
        "timestamp": node_timestamp
    }


def transform_ble(input_dict: dict[str, Any]) -> dict[str, Any]:
    return{
        "transformer": "generic_ble",
        "src_type": "BLE",
         **input_dict,
        "timestamp": int(time.time() * 1000)
     }


def dispatcher(input_dict: dict[str, Any], own_callsign: str = "") -> dict[str, Any] | None:
    """
    Route BLE messages to appropriate transformer based on type.

    Multi-Part Configuration Responses:
    - SE + S1: Sensor settings (arrive ~200ms apart)
    - SW + S2: WiFi settings (arrive ~200ms apart)

    Each notification is processed independently and published via separate SSE events.
    Frontend must merge if combined display is needed.

    Args:
        input_dict: Decoded BLE message
        own_callsign: Station callsign for filtering relay paths

    Returns:
        Transformed message dict, or None if type not recognized
    """
    if "TYP" in input_dict:
        if input_dict["TYP"] == "MH":
            return transform_mh(input_dict)
        elif input_dict["TYP"] in [
            "I", "SN", "G", "SA", "W", "IO", "TM", "AN", "SE", "SW", "S1", "S2",
        ]:
            logger.debug("BLE JSON TYP=%s", input_dict["TYP"])
            return transform_ble(input_dict)
        else:
            if has_console:
                print("Type not found!", input_dict)

    elif input_dict.get("payload_type") == 58:
        return transform_msg(input_dict, own_callsign)

    elif input_dict.get("payload_type") == 33:
        msg = input_dict.get("message", "")
        if msg.startswith("T#"):
            return transform_tele(input_dict, own_callsign)
        return transform_pos(input_dict, own_callsign)

    elif input_dict.get("payload_type") == 65:
        return transform_ack(input_dict)
        #print(json.dumps(input_dict, indent=2, ensure_ascii=False))
        #transformed = transform_ack(input_dict)
        #print(json.dumps(transformed, indent=2, ensure_ascii=False))
        #return transformed

    else:
        print(f"Unknown payload_type or TYP: {input_dict}")


async def notification_handler(clean_msg: bytes, message_router: Any | None = None) -> None:
    """
    Process incoming BLE GATT characteristic notifications.

    Decodes raw bytes into structured message dicts and publishes to the
    message router for storage and broadcast to connected clients.

    Message Format Detection:
        - Prefix 'D{': JSON config/status message → decode_json_message()
        - Prefix '@A': Binary ACK → decode_binary_message()
        - Prefix '@:' or '@!': Binary mesh message → decode_binary_message()

    Multi-Part Configuration Responses:
    ------------------------------------
    Configuration queries (--seset, --wifiset) trigger TWO sequential notifications:

    Example: `--seset` query flow:
    1. Device sends TYP="SE" (sensor config: temp sensor type, pressure offset)
       → notification_handler() → dispatcher() → transform_ble() → publish
    2. ~200ms delay
    3. Device sends TYP="S1" (extended: altitude, calibration data)
       → notification_handler() → dispatcher() → transform_ble() → publish

    Each notification is independent (no correlation ID). Frontend receives both
    via SSE and must merge if needed.

    Timing Behavior:
    - Delay between parts: ~200ms (firmware-dependent, not guaranteed)
    - Order is guaranteed: SE before S1, SW before S2
    - No timeout handling needed (if S1/S2 missing, first part is still valid data)

    Args:
        clean_msg: Raw bytes from GATT notification
        message_router: Optional MessageRouter for publishing decoded messages

    Side Effects:
        - Publishes messages via message_router.publish()
        - Updates GPS cache (for TYP="G")
        - Logs routine messages at DEBUG, non-routine at INFO
    """
    # JSON messages start with 'D{'
    if clean_msg.startswith(b'D{'):

         var = decode_json_message(clean_msg)

         try:
           typ = var.get('TYP')

           if typ == 'MH': # MH update
             output = dispatcher(var)
             if message_router:
                    await message_router.publish('ble', 'ble_notification', output)

           elif typ == "SA": # APRS.fi Info
             output = dispatcher(var)
             if message_router:
                   await message_router.publish('ble', 'ble_notification', output)

           elif typ == "G": # GPS Info
             global client
             if client and client._connected:
                 await client.process_gps_message(var)
             output = dispatcher(var)
             if message_router:
                    await message_router.publish('ble', 'ble_notification', output)

           elif typ == "W": # Wetter Info
             output = dispatcher(var)
             if message_router:
                   await message_router.publish('ble', 'ble_notification', output)

           # Multi-part config responses: SE+S1 (sensor), SW+S2 (WiFi)
           # These arrive as separate notifications ~200ms apart, processed independently
           elif typ in ["SN", "SE", "SW", "I", "IO", "TM", "AN",
                       "S1", "S2"]:
                output = dispatcher(var)
                if message_router:
                    await message_router.publish('ble', 'ble_notification', output)

           elif typ == "CONFFIN": # Configuration finished, no more data available
             if message_router:
                    await message_router.publish('ble', 'ble_status', {
                        'src_type': 'BLE',
                        'TYP': 'blueZ',
                        'command': 'conffin',
                        'result': 'ok',
                        'msg': "✅ finished sending config",
                        'timestamp': int(time.time() * 1000)
                    })

           else:
             if has_console:
                print("Type unknown",var)

         except KeyError:
             print("error", var)

    # Binary messages start with '@'
    elif clean_msg.startswith(b'@'):
      message = decode_binary_message(clean_msg)

      own_call = getattr(message_router, 'my_callsign', '') if message_router else ''
      if isinstance(message, dict):
          pt = message.get("payload_type", 0)
          msg = message.get("message", "")
          is_routine = pt == 33 or (pt == 58 and msg[:6] in (":{CET}", ":{UTC}"))
          _log = logger.debug if is_routine else logger.info
          _log(
              "BLE binary: :%s %s %03d %d/%d LH:%02X %s%s %s",
              format(message.get("msg_id", 0), "08X"),
              message.get("mesh_info", ""),
              pt,
              message.get("max_hop", 0),
              message.get("max_hop", 0),
              message.get("last_hw_id", 0),
              message.get("path", ""),
              message.get("dest", ""),
              msg,
          )
      output = dispatcher(message, own_call)
      if message_router:
            await message_router.publish('ble', 'ble_notification', output)

    else:
        print("Unknown message type.")


class TimeSyncTask:
    def __init__(self, coro_fn: Callable[[float, float], Any]) -> None:
        self._coro_fn = coro_fn
        self._event = asyncio.Event()
        self._running = False
        self._task = None

        self.lat = None
        self.lon = None

    def trigger(self, lat: float, lon: float) -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self._set_data, lat, lon)

    def _set_data(self, lat: float, lon: float) -> None:
        self.lat = lat
        self.lon = lon
        self._event.set()

    async def runner(self) -> None:
        self._running = True
        while self._running:
            await self._event.wait()
            self._event.clear()

            if not self._running:
               break

            if None in (self.lat, self.lon):
                print("Warning: missing input data, skipping task")
                continue

            try:
              if self._running:
                await self._coro_fn(self.lat, self.lon)
            except Exception as e:
                print(f"Error during async task: {e}")

    def start(self) -> None:
        self._task = asyncio.create_task(self.runner())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass  # Expected when we cancel
        self._task = None


class BLEClient:
    def __init__(
        self,
        mac: str,
        read_uuid: str,
        write_uuid: str,
        hello_bytes: bytes | None = None,
        message_router: Any | None = None
    ) -> None:
        """
        Initialize BLE client.

        Args:
            mac: Device MAC address
            read_uuid: GATT characteristic UUID for reading (RX)
            write_uuid: GATT characteristic UUID for writing (TX)
            hello_bytes: Initial handshake message sent after connection.
                        Default for MeshCom: b'\x04\x10\x20\x30'
                        Format: [Length][MsgID][Data...]
                        - 0x04: Total length (1 + 1 + 2 = 4 bytes)
                        - 0x10: Message ID (Hello)
                        - 0x20 0x30: Data payload (2 bytes)
            message_router: Router for publishing messages
        """
        self.mac = mac
        self.read_uuid = read_uuid
        self.write_uuid = write_uuid
        self.hello_bytes = hello_bytes or b'\x00'
        self.message_router = message_router
        self.path = self._mac_to_dbus_path(mac)
        self.bus = None
        self.device_obj = None
        self.dev_iface = None
        self.read_char_iface = None
        self.read_props_iface = None
        self.write_char_iface = None
        self.props_iface = None
        self._on_value_change_cb = None
        self._connect_lock = asyncio.Lock()
        self._connected = False
        self._keepalive_task = None
        self._time_sync = None

    def _mac_to_dbus_path(self, mac: str) -> str:
        """Convert MAC address to D-Bus device path"""
        return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"


    async def connect(self, max_retries: int = 3) -> None:
        """Connect to BLE device with retry logic and proper error handling"""
        async with self._connect_lock:
            if self._connected:
                if has_console:
                    print(f"🔁 Connection to {self.mac} already established")
                return

            last_error = None
            for attempt in range(max_retries):
                try:
                    await self._attempt_connection()
                    return  # Success
                except Exception as e:
                    last_error = e
                    wait_time = min(2 ** attempt, 8)
                    logger.warning("BLE connect attempt %d/%d failed: %s",
                                   attempt + 1, max_retries, e)
                    if attempt < max_retries - 1:
                        msg = f"Attempt {attempt + 1} failed, retrying in {wait_time}s..."
                        await self._publish_status('connect BLE', 'info', msg)
                        await asyncio.sleep(wait_time)
                        await self._cleanup_failed_connection()

            # All attempts failed
            logger.error("BLE connection failed after %d attempts: %s", max_retries, last_error)
            await self._publish_status('connect BLE result', 'error',
                                     f"Connection failed after {max_retries} attempts")
            self._connected = False


    async def _attempt_connection(self) -> None:
        """Single connection attempt - extracted from current connect() method"""
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, self.path)
        self.device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, self.path, introspection)

        try:
            self.dev_iface = self.device_obj.get_interface(DEVICE_INTERFACE)
        except InterfaceNotFoundError as e:
            raise ConnectionError(f"Interface not found, device not paired: {e}")

        self.props_iface = self.device_obj.get_interface(PROPERTIES_INTERFACE)

        try:
            connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        except DBusError as e:
            raise ConnectionError(f"Error checking connection state: {e}")

        if not connected:
            try:
                # Add timeout to prevent hanging
                await asyncio.wait_for(
                    self.dev_iface.call_connect(), timeout=BLE_CONNECT_TIMEOUT
                )
                if has_console:
                    print(f"✅ connected to {self.mac}")
            except asyncio.TimeoutError:
                raise ConnectionError("Connection timeout after 10 seconds")
            except DBusError as e:
                raise ConnectionError(f"Connect failed: {e}")
        else:
            if has_console:
                print(f"🔁 Connection to {self.mac} already established")

        if has_console:
            print("🔍 Waiting for service discovery...")

        services_resolved = await self._wait_for_services_resolved(
            timeout=BLE_CONNECT_TIMEOUT
        )
        if not services_resolved:
            raise ConnectionError("Services not resolved within 10 seconds")

        if has_console:
            print("✅ All services discovered and resolved")

        await self._find_characteristics()

        if not self.read_char_iface or not self.write_char_iface:
            raise ConnectionError("Characteristics not found - device not properly paired")

        self.read_props_iface = self.read_char_obj.get_interface(PROPERTIES_INTERFACE)

        # Verify services are resolved
        #try:
        #    services_resolved = (
        #        await self.props_iface.call_get(DEVICE_INTERFACE, "ServicesResolved")
        #    ).value
        #    if not services_resolved:
        #        # Wait a bit for services to resolve
        #        await asyncio.sleep(2)
        #        services_resolved = (
        #            await self.props_iface.call_get(DEVICE_INTERFACE, "ServicesResolved")
        #        ).value
        #        if not services_resolved:
        #            raise ConnectionError("Services not resolved after connection")
        #except DBusError as e:
        #    if has_console:
        #        print(f"⚠️ Warning: Could not check ServicesResolved: {e}")

        self._connected = True
        await self._publish_status(
            'connect BLE result', 'ok', "connection established, downloading config .."
        )

        # Start background tasks
        if has_console:
            print("▶️  Starting time sync task ..")
        self._time_sync = TimeSyncTask(self._handle_timesync)
        self._time_sync.start()

        if has_console:
            print("▶️  Starting keep alive ..")
        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._send_keepalive())

    async def _wait_for_services_resolved(
        self, timeout: float = BLE_CONNECT_TIMEOUT
    ) -> bool:
        """Wait for BLE services to be discovered and resolved"""
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            try:
                services_resolved = (
                    await self.props_iface.call_get(DEVICE_INTERFACE, "ServicesResolved")
                ).value
                if services_resolved:
                    if has_console:
                        print(f"🔍 Services resolved after {time.time() - start_time:.1f}s")
                    return True

                # Still waiting - check periodically
                await asyncio.sleep(BLE_SERVICES_CHECK_INTERVAL)

            except DBusError as e:
                if has_console:
                    print(f"⚠️ Error checking ServicesResolved: {e}")
                await asyncio.sleep(BLE_SERVICES_CHECK_INTERVAL)

        return False


    async def _cleanup_failed_connection(self) -> None:
        """Clean up after a failed connection attempt"""
        try:
            if self.dev_iface:
                try:
                    await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=3.0)
                except Exception:
                    pass  # Ignore errors during cleanup

            if self.bus:
                self.bus.disconnect()

            # Reset all state
            self.bus = None
            self.device_obj = None
            self.dev_iface = None
            self.read_char_iface = None
            self.read_props_iface = None
            self.write_char_iface = None
            self.props_iface = None
            self._connected = False

            # Stop background tasks if they exist
            if self._time_sync is not None:
                await self._time_sync.stop()
                self._time_sync = None

            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                try:
                    await self._keepalive_task
                except asyncio.CancelledError:
                    pass
                self._keepalive_task = None

        except Exception as e:
            if has_console:
                print(f"⚠️ Error during cleanup: {e}")


    async def _publish_status(self, command: str, result: str, msg: str) -> None:
        """Helper method to publish BLE status messages through router"""
        if self.message_router:
            status_message = {
                'src_type': 'BLE',
                'TYP': 'blueZ',
                'command': command,
                'result': result,
                'msg': msg,
                "timestamp": int(time.time() * 1000)
            }
            await self.message_router.publish('ble', 'ble_status', status_message)
        else:
            # Fallback to console if no router
            print(f"BLE {command}: {result} - {msg}")

    async def _send_to_websocket(self, message: dict[str, Any]) -> None:
        """Helper method to send messages to websocket through router"""
        if self.message_router:
            await self.message_router.publish('ble', 'websocket_message', message)
        else:
            print(f"BLE message (no router): {message}")

    async def _find_characteristics(self) -> None:
        self.read_char_obj, self.read_char_iface = await self._find_gatt_characteristic(
            self.bus, self.path, self.read_uuid)
        self.write_char_obj, self.write_char_iface = await self._find_gatt_characteristic(
            self.bus, self.path, self.write_uuid)

    async def _find_gatt_characteristic(
        self, bus: Any, path: str, target_uuid: str
    ) -> tuple[Any, Any]:
        """Find GATT characteristic by UUID in the device tree"""
        try:
            introspect = await bus.introspect(BLUEZ_SERVICE_NAME, path)
        except Exception:
            return None, None

        for node in introspect.nodes:
            child_path = f"{path}/{node.name}"
            try:
                introspection = await bus.introspect(BLUEZ_SERVICE_NAME, child_path)
                child_obj = bus.get_proxy_object(
                    BLUEZ_SERVICE_NAME, child_path, introspection
                )

                props_iface = child_obj.get_interface(PROPERTIES_INTERFACE)
                props = await props_iface.call_get_all(GATT_CHARACTERISTIC_INTERFACE)

                uuid = props.get("UUID").value.lower()
                if uuid == target_uuid.lower():
                    char_iface = child_obj.get_interface(GATT_CHARACTERISTIC_INTERFACE)
                    return child_obj, char_iface

            except Exception:
                # Recursive search in child nodes
                obj, iface = await self._find_gatt_characteristic(bus, child_path, target_uuid)
                if iface:
                    return obj, iface

        return None, None

    async def start_notify(self, on_change: Callable[[bytes], None] | None = None) -> None:
        if not self._connected:
           return

        is_notifying = (
            await self.read_props_iface.call_get(GATT_CHARACTERISTIC_INTERFACE, "Notifying")
        ).value
        if is_notifying:
           if has_console:
              print("Notify already active, skipping duplicate registration")
           return

        if not self.bus:
           return

        if not self.read_char_iface:
            raise Exception("read_char_iface nicht initialisiert")

        try:
            if on_change:
                self._on_value_change_cb = on_change

            self.read_props_iface.on_properties_changed(self._on_props_changed)
            await self.read_char_iface.call_start_notify()

            is_notifying = (
                await self.read_props_iface.call_get(
                    GATT_CHARACTERISTIC_INTERFACE, "Notifying"
                )
            ).value

            if has_console:
               print(f"📡 Notify: {is_notifying}")
        except DBusError as e:
            print(f"⚠️ StartNotify failed: {e}")

    async def _on_props_changed(
        self, iface: str, changed: dict[str, Any], invalidated: list[str]
    ) -> None:
      if iface != GATT_CHARACTERISTIC_INTERFACE:
        return

      if "Value" in changed:
        new_value = changed["Value"].value

        await notification_handler(new_value, message_router=self.message_router)

        if self._on_value_change_cb:
            self._on_value_change_cb(new_value)

    async def stop_notify(self) -> None:
        if not self.bus:
           return

        if not self.read_char_iface:
           return

        try:
           if self.read_props_iface:
               try:
                   # Try to remove the callback handler
                   self.read_props_iface.off_properties_changed(self._on_props_changed)
               except AttributeError:
                   pass
               except Exception:
                   pass

           await self.read_char_iface.call_stop_notify()

           print("🛑 Notify stopped")
           await self._publish_status('disconnect','info', "unsubscribe from messages ..")

        except DBusError as e:
            if "No notify session started" in str(e):
                if has_console:
                   print("ℹ️ No active notify session – ignored")
            else:
                raise

    async def send_hello(self) -> None:
        """Send hello handshake message to device"""
        if not self.bus:
           logger.debug("BLE not connected, skipping send")
           return

        try:
            connected = (
                await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")
            ).value
            if not connected:
                logger.warning("Connection lost, cannot send hello")
                await self._publish_status('send hello', 'error', "❌ Connection lost")
                await self.disconnect()
                await self.close()
                return

            if self.write_char_iface:
                await self.write_char_iface.call_write_value(self.hello_bytes, {})
                await self._publish_status('conf load', 'info', ".. waking up device ..")
                if has_console:
                    print("📨 Hello sent ..")
            else:
                logger.debug("No write characteristic available")

        except Exception as e:
            logger.error("Failed to send hello: %s", e)
            await self._publish_status('send hello', 'error', f"❌ Send failed: {e}")
            raise

    async def send_message(self, msg: str, grp: str) -> None:
        if not self.bus:
           logger.debug("BLE not connected, skipping send")
           return

        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           print("🛑 connection lost, can't send ..")
           await self._publish_status('send message','error', "❌ connection lost")

           await self.disconnect()
           await self.close()
           return

        message = "{" + grp + "}" + msg
        byte_array = bytearray(message.encode('utf-8'))

        laenge = len(byte_array) + 2

        # Validate MTU limit before sending
        if laenge > MAX_BLE_MTU:
            error_msg = (
                f"Message too long: {laenge} bytes (max {MAX_BLE_MTU}). "
                f"Message will be truncated or lost."
            )
            logger.error(error_msg)
            await self._publish_status('send message', 'error', f"❌ {error_msg}")
            raise ValueError(error_msg)

        byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

        if self.write_char_iface:
            try:
              await asyncio.wait_for(
                  self.write_char_iface.call_write_value(byte_array, {}), timeout=5
              )
            except asyncio.TimeoutError:
              print("🕓 Timeout beim Schreiben an BLE-Device")
              await self._publish_status('send message','error', "❌ Timeout on write")
            except Exception as e:
              print(f"💥 Error writing to BLE: {e}")
              await self._publish_status('send message','error', f"❌ BLE write error {e}")
        else:
            logger.debug("No write characteristic available")

    async def a0_commands(self, cmd: str) -> None:
        """Send A0 (text) command to device"""
        if not self.bus:
           logger.debug("BLE not connected, skipping send")
           return

        try:
            await self._check_conn()

            byte_array = bytearray(cmd.encode('utf-8'))
            laenge = len(byte_array) + 2

            # Validate MTU limit before sending
            if laenge > MAX_BLE_MTU:
                error_msg = (
                    f"Command too long: {laenge} bytes (max {MAX_BLE_MTU}). "
                    f"Command will be truncated or lost."
                )
                logger.error(error_msg)
                await self._publish_status('send command', 'error', f"❌ {error_msg}")
                raise ValueError(error_msg)

            byte_array = laenge.to_bytes(1, 'big') +  bytes ([0xA0]) + byte_array

            if self.write_char_iface:
                await self.write_char_iface.call_write_value(byte_array, {})
                if has_console:
                    print(f"📨 Command sent: {cmd}")
                logger.debug("A0 command sent: %s", cmd)
            else:
                logger.warning("No write characteristic available")

        except ValueError:
            # MTU validation error - already logged and published
            raise
        except Exception as e:
            logger.error("Failed to send A0 command '%s': %s", cmd, e)
            await self._publish_status('send command', 'error', f"❌ Send failed: {e}")
            raise

    async def set_commands(self, cmd: str) -> None:
       """
       Send configuration commands using binary message types.

       Supports:
       - --settime: 0x20 message with UNIX timestamp
       - --save: 0xA0 text command to save settings
       - --reboot: 0xA0 text command to reboot device
       - --savereboot: 0xF0 binary message to save & reboot

       Args:
           cmd: Command string (e.g., "--settime", "--save", "--savereboot")
       """
       laenge = 0
       byte_array = None

       if not self.bus:
          return

       await self._check_conn()

       if has_console:
          print("✅ ready to send")

       # ID = 0x20 Timestamp from phone [4B]
       if cmd == "--settime":
         cmd_byte = bytes([0x20])

         now = int(time.time())  # current time in seconds
         byte_array = now.to_bytes(4, byteorder='little')

         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') +  cmd_byte + byte_array

         if has_console:
            print(f"Current time {now}")
            print("to hex:", ' '.join(f"{b:02X}" for b in byte_array))

       # ID = 0xF0 Save & Reboot [no data]
       elif cmd == "--savereboot":
         cmd_byte = bytes([0xF0])
         # No data payload for 0xF0
         laenge = 2  # length + message ID only
         byte_array = laenge.to_bytes(1, 'big') + cmd_byte

         if has_console:
            print("💾 Saving settings to flash and rebooting device")
            print("to hex:", ' '.join(f"{b:02X}" for b in byte_array))

       # Send --save or --reboot as A0 text commands
       elif cmd in ["--save", "--reboot"]:
         # These use A0 message type (text command)
         await self.a0_commands(cmd)
         return  # Early return, a0_commands handles sending

       # 0x50 - Set Callsign
       elif cmd.startswith("--setcall "):
         callsign = cmd.split(maxsplit=1)[1].strip()
         callsign_bytes = callsign.encode('utf-8')

         if len(callsign_bytes) > 20:
            logger.error("Callsign too long: %d bytes (max 20)", len(callsign_bytes))
            await self._publish_status('set command', 'error', "❌ Callsign too long")
            return

         call_len = len(callsign_bytes)
         byte_array = bytes([call_len]) + callsign_bytes
         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') + bytes([0x50]) + byte_array

       # 0x55 - WiFi Settings
       elif "--setssid" in cmd and "--setpwd" in cmd:
         parts = cmd.split()
         try:
            ssid_idx = parts.index("--setssid") + 1
            pwd_idx = parts.index("--setpwd") + 1
            ssid = parts[ssid_idx]
            pwd = parts[pwd_idx]
         except (ValueError, IndexError) as e:
            logger.error("Invalid WiFi command format: %s", e)
            await self._publish_status('set command', 'error', "❌ Invalid format")
            return

         ssid_bytes = ssid.encode('utf-8')
         pwd_bytes = pwd.encode('utf-8')

         if len(ssid_bytes) > 32 or len(pwd_bytes) > 63:
            logger.error("SSID or password too long")
            await self._publish_status('set command', 'error', "❌ SSID/pwd too long")
            return

         byte_array = (bytes([len(ssid_bytes)]) + ssid_bytes +
                       bytes([len(pwd_bytes)]) + pwd_bytes)
         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') + bytes([0x55]) + byte_array

       # 0x70 - Set Latitude
       elif cmd.startswith("--setlat "):
         parts = cmd.split()
         try:
            lat = float(parts[1])
         except (ValueError, IndexError) as e:
            logger.error("Invalid latitude format: %s", e)
            await self._publish_status('set command', 'error', "❌ Invalid latitude")
            return

         if not (-90.0 <= lat <= 90.0):
            logger.error("Latitude out of range: %f", lat)
            await self._publish_status('set command', 'error', "❌ Lat out of range")
            return

         save_flag = 0x0A if "--save" in cmd else 0x0B

         byte_array = pack('<f', lat) + bytes([save_flag])
         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') + bytes([0x70]) + byte_array

       # 0x80 - Set Longitude
       elif cmd.startswith("--setlon "):
         parts = cmd.split()
         try:
            lon = float(parts[1])
         except (ValueError, IndexError) as e:
            logger.error("Invalid longitude format: %s", e)
            await self._publish_status('set command', 'error', "❌ Invalid longitude")
            return

         if not (-180.0 <= lon <= 180.0):
            logger.error("Longitude out of range: %f", lon)
            await self._publish_status('set command', 'error', "❌ Lon out of range")
            return

         save_flag = 0x0A if "--save" in cmd else 0x0B

         byte_array = pack('<f', lon) + bytes([save_flag])
         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') + bytes([0x80]) + byte_array

       # 0x90 - Set Altitude
       elif cmd.startswith("--setalt "):
         parts = cmd.split()
         try:
            alt = int(parts[1])
         except (ValueError, IndexError) as e:
            logger.error("Invalid altitude format: %s", e)
            await self._publish_status('set command', 'error', "❌ Invalid altitude")
            return

         if not (-500 <= alt <= 9000):
            logger.error("Altitude out of range: %d", alt)
            await self._publish_status('set command', 'error', "❌ Alt out of range")
            return

         save_flag = 0x0A if "--save" in cmd else 0x0B

         byte_array = alt.to_bytes(4, byteorder='little', signed=True) + bytes([save_flag])
         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') + bytes([0x90]) + byte_array

       # 0x95 - APRS Symbols
       elif cmd.startswith("--setsym "):
         parts = cmd.split()
         if len(parts) < 2 or len(parts[1]) != 2:
            logger.error("Invalid symbol format: must be 2 characters")
            await self._publish_status('set command', 'error', "❌ Invalid symbol")
            return

         symbols = parts[1]
         primary = ord(symbols[0])
         secondary = ord(symbols[1])

         if primary not in (ord('/'), ord('\\')):
            logger.error("Invalid APRS symbol table: must be / or \\")
            await self._publish_status('set command', 'error', "❌ Invalid symbol table")
            return

         byte_array = bytes([primary, secondary])
         laenge = len(byte_array) + 2
         byte_array = laenge.to_bytes(1, 'big') + bytes([0x95]) + byte_array

       else:
          logger.warning("Command %s not implemented in set_commands", cmd)
          print(f"❌ {cmd} not yet implemented")
          return  # Early return if command not recognized

       # Validate MTU limit before sending (for binary messages)
       if byte_array and len(byte_array) > MAX_BLE_MTU:
           error_msg = (
               f"Set command too long: {len(byte_array)} bytes (max {MAX_BLE_MTU}). "
               f"Message will be truncated or lost."
           )
           logger.error(error_msg)
           await self._publish_status('set command', 'error', f"❌ {error_msg}")
           raise ValueError(error_msg)

       try:
           if self.write_char_iface and byte_array:
               await self.write_char_iface.call_write_value(byte_array, {})
               if has_console:
                   print(f"📨 Message sent: {' '.join(f'{b:02X}' for b in byte_array)}")
               await self._publish_status(
                   'set command', 'ok', f"✅ Command {cmd} sent successfully"
               )
               logger.debug("Set command sent: %s", cmd)
           else:
               logger.warning("No write characteristic available for command: %s", cmd)

       except Exception as e:
           logger.error("Failed to send set command '%s': %s", cmd, e)
           await self._publish_status('set command', 'error', f"❌ Send failed: {e}")
           raise

    async def save_settings(self) -> None:
        """
        Save current device settings to flash memory.

        Uses --save A0 command. Settings are persistent across reboots.
        Does NOT reboot the device.
        """
        await self.set_commands("--save")
        logger.info("Device settings saved to flash")

    async def reboot_device(self) -> None:
        """
        Reboot the device without saving settings.

        Uses --reboot A0 command. Unsaved settings will be lost.
        """
        await self.set_commands("--reboot")
        logger.info("Device reboot command sent")

    async def save_and_reboot(self) -> None:
        """
        Save settings to flash and reboot device in one operation.

        Uses 0xF0 binary message. This is the recommended way to persist
        configuration changes as it's atomic (saves then reboots).

        Per firmware spec: Most configuration commands require --save or
        0xF0 message to persist to flash, otherwise settings are lost on reboot.
        """
        await self.set_commands("--savereboot")
        logger.info("Device save & reboot command sent (0xF0)")

    async def _check_conn(self) -> None:
        if not self.props_iface:
            return
        connected = (await self.props_iface.call_get(DEVICE_INTERFACE, "Connected")).value
        if not connected:
           logger.warning("BLE connection lost to %s", self.mac)
           await self.stop_notify()
           try:
               await self.dev_iface.call_disconnect()
           except Exception:
               pass
           await self.close()
           self._connected = False

    async def _send_keepalive(self) -> None:
        backoff = 300  # start at 5 minutes
        max_backoff = 1800  # max 30 minutes
        consecutive_failures = 0
        try:
            while self._connected:
                await asyncio.sleep(backoff)
                if not self._connected:
                    break
                try:
                    props = await self.props_iface.call_get_all(DEVICE_INTERFACE)
                    if not props["ServicesResolved"].value:
                       await self._check_conn()
                       consecutive_failures += 1
                       backoff = min(backoff * 2, max_backoff)
                       if consecutive_failures == 1:
                           logger.warning("BLE device unreachable, will retry with backoff")
                    else:
                      await self.a0_commands("--pos")
                      consecutive_failures = 0
                      backoff = 300

                except Exception as e:
                    consecutive_failures += 1
                    if consecutive_failures <= 3:
                        logger.warning("Keepalive error: %s", e)
                    backoff = min(backoff * 2, max_backoff)
        except asyncio.CancelledError:
            pass

    async def disconnect(self) -> None:
        if not self.dev_iface:
            if has_console:
               print("⬇️  not connected - can't disconnect ..")
            return
        try:
            await self._publish_status('disconnect','info', "⬇️  disconnecting ..")

            if self._time_sync is not None:
                await self._time_sync.stop()
                self._time_sync = None

            if self._keepalive_task:
               self._keepalive_task.cancel()
               try:
                  await self._keepalive_task
               except asyncio.CancelledError:
                   pass
               self._keepalive_task = None

            await self.stop_notify()

            try:
                await asyncio.wait_for(self.dev_iface.call_disconnect(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass

            await self._publish_status('disconnect','ok', "✅ disconnected")
            print(f"🧹 Disconnected from {self.mac}")


        except DBusError as e:
            await self._publish_status('disconnect','error', f"❌ disconnect error {e}")
            if has_console:
               print(f"⚠️ Disconnect failed: {e}")


    async def close(self) -> None:
        if self._time_sync is not None:
            await self._time_sync.stop()
            self._time_sync = None

        if self.bus:
            await asyncio.sleep(1.0)

            try:
                 self.bus.disconnect()
            except Exception:
                 pass

        else:
           return

        self.bus = None
        self._connected = False



    async def _handle_timesync(self, lat: float, lon: float) -> None:
        """Time sync handler that uses BLE client methods instead of global functions"""
        if has_console:
            print("adjusting time on node ..", lat, lon)

        await asyncio.sleep(3)

        if lon == 0 or lat == 0:
            if has_console:
                print("Lon/Lat not set, fallback on Raspberry Pi TZ info")
            # Use local timezone
            is_dst = time.daylight and time.localtime().tm_isdst
            offset_sec = time.altzone if is_dst else time.timezone
            offset = -offset_sec / 3600
            tz_name = "Local"
        else:
            tz = get_timezone_info(lat, lon)
            offset = tz.get("offset_hours")
            tz_name = tz.get("timezone")

        if has_console:
            print("TZ UTC Offset", offset, "TZ name", tz_name)

        print("Time offset detected, correcting time")
        # Use instance methods instead of global handle_command
        await self.a0_commands(f"--utcoff {offset}")
        await asyncio.sleep(2)
        await self.set_commands("--settime")

    def _should_trigger_time_sync(self, message_dict: dict[str, Any]) -> bool:
        """Check if this GPS message should trigger time sync"""
        if message_dict.get("TYP") != "G":
            return False

        # Check if we have valid coordinates
        lat = message_dict.get("LAT", 0)
        lon = message_dict.get("LON", 0)

        if lat == 0 and lon == 0:
            return False

        # Check time delta (reuse existing logic)
        node_timestamp = safe_timestamp_from_dict(message_dict)
        if node_timestamp is None:
            return False

        time_delta = node_time_checker(node_timestamp, "G")
        return abs(time_delta) > 60  # Same threshold as before

    async def process_gps_message(self, message_dict: dict[str, Any]) -> None:
        """Process GPS message and trigger time sync if needed - called from notification_handler"""
        if self._should_trigger_time_sync(message_dict):
            lat = message_dict.get("LAT")
            lon = message_dict.get("LON")

            if self._time_sync is not None:
                self._time_sync.trigger(lat, lon)
            else:
                print("Warning: time_sync not initialized")

    def _normalize_variant(self, value: Any) -> Any:
      if isinstance(value, Variant):
        return self._normalize_variant(value.value)
      elif isinstance(value, dict):
        return {k: self._normalize_variant(v) for k, v in value.items()}
      elif isinstance(value, list):
        return [self._normalize_variant(v) for v in value]
      elif isinstance(value, bytes):
        return value.hex()
      else:
        return value

    async def scan_ble_devices(self, timeout: float = 5.0) -> None:
      #Helper function
      async def _interfaces_added(path, interfaces):
        if DEVICE_INTERFACE in interfaces:
            props = interfaces[DEVICE_INTERFACE]
            name = props.get("Name", Variant("s", "")).value
            if name.startswith("MC-"):
              addr = props.get("Address", Variant("s", "")).value
              rssi = props.get("RSSI", Variant("n", 0)).value
              self.found_devices[path] = (name, addr, rssi)

      await self._publish_status('scan BLE', 'info', 'command started')

      if self.bus is None:
          self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
      else:
          print("❌ already connected, no scanning possible ..")
          await self._publish_status(
              'scan BLE result', 'error', "already connected, no scanning possible"
          )

          return

      if has_console:
         print("🔍 Starting native BLE scan via BlueZ... timout =",timeout)
      await self._publish_status('scan BLE', 'info', f'🔍 BLE scan active... timeout = {timeout}')

      path = "/org/bluez/hci0"

      introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, path)
      device_obj = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, introspection)
      self.adapter = device_obj.get_interface(ADAPTER_INTERFACE)

      # Track discovered devices
      self.found_devices = {}
      # Event zur Synchronisation
      found_mc_event = asyncio.Event()

      # Listen to InterfacesAdded signal
      root_introspection = await self.bus.introspect(BLUEZ_SERVICE_NAME, "/")
      self.obj_mgr = self.bus.get_proxy_object(BLUEZ_SERVICE_NAME, "/", root_introspection)
      self.obj_mgr_iface = self.obj_mgr.get_interface(OBJECT_MANAGER_INTERFACE)

      objects = await self.obj_mgr_iface.call_get_managed_objects()

      device_count = 0
      for path, interfaces in objects.items():
        if DEVICE_INTERFACE in interfaces:
          device_count += 1
          props = interfaces[DEVICE_INTERFACE]
          name = props.get("Name", Variant("s", "")).value
          addr = props.get("Address", Variant("s", "")).value
          paired = props.get("Paired", Variant("b", False)).value
          busy = False
          interfaces[DEVICE_INTERFACE]["Busy"] = Variant("b", busy)

          if has_console:
            print(f"💾 Found device: {name} ({addr}, paired={paired}, busy={busy})")

      objects["TYP"] = "blueZknown"
      msg=transform_ble(self._normalize_variant(objects))
      await self._send_to_websocket(msg)

      if has_console:
         print(f"\n✅ Found {device_count} known device(s):")
      await self._publish_status('scan BLE', 'info', f".. found {device_count} known device(s) ..")

      #Handler installieren
      def on_interfaces_added_sync(path, interfaces):
          asyncio.create_task(_interfaces_added(path, interfaces))

      self.obj_mgr_iface.on_interfaces_added(on_interfaces_added_sync)

      # Start discovery
      await self.adapter.call_start_discovery()

      try:
         await asyncio.wait_for(found_mc_event.wait(), timeout)
      except asyncio.TimeoutError:
         print("\n")

      await self.adapter.call_stop_discovery()

      if has_console:
        print(f"\n✅ Scan complete. Not paired {len(self.found_devices)} device(s)")
      device_count = len(self.found_devices)
      await self._publish_status(
          'scan BLE', 'info', f"✅ Scan complete, {device_count} not paired device(s)"
      )

      for path, (name, addr, rssi) in self.found_devices.items():
          if has_console:
             print(f"🔹 {name} | Address: {addr} | RSSI: {rssi}")

      self.found_devices["TYP"] = "blueZunKnown"
      msg=transform_ble(self._normalize_variant(self.found_devices))
      await self._send_to_websocket(msg)

      await self.close()
      await asyncio.sleep(2)


class NoInputNoOutputAgent(ServiceInterface):
    def __init__(self) -> None:
        super().__init__('org.bluez.Agent1')

    @method()
    def Release(self) -> None:
        if has_console:
           print("Agent released")

    @method()
    def RequestPasskey(self, device: 'o') -> 'u':  # noqa: F821
       print(f"Passkey requested for {device}")
       return 0

    @method()
    def RequestPinCode(self, device: 'o') -> 's':  # noqa: F821
        print(f"PIN requested for {device}")
        return "000000"

    @method()
    def DisplayPinCode(self, device: 'o', pincode: 's'):  # noqa: F821
        print(f"DisplayPinCode for {device}: {pincode}")

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):  # noqa: F821
        print(f"Confirm passkey {passkey} for {device}")
        return

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):  # noqa: F821
        print(f"Authorize service {uuid} for {device}")
        return

    @method()
    def Cancel(self) -> None:
        print("Request cancelled")


# Module-level functions

async def ble_pair(mac: str, BLE_Pin: str | None, message_router: Any | None = None) -> None:
    path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Register agent
    agent = NoInputNoOutputAgent()
    bus.export(AGENT_PATH, agent)

    bluez_introspection = await bus.introspect(BLUEZ_SERVICE_NAME, "/org/bluez")
    manager_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, "/org/bluez", bluez_introspection)
    agent_manager = manager_obj.get_interface("org.bluez.AgentManager1")
    await agent_manager.call_register_agent(AGENT_PATH, "KeyboardDisplay")
    await agent_manager.call_request_default_agent(AGENT_PATH)

    # Pair device
    dev_introspection = await bus.introspect(BLUEZ_SERVICE_NAME, path)
    dev_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, path, dev_introspection)

    try:
        dev_iface = dev_obj.get_interface(DEVICE_INTERFACE)
    except InterfaceNotFoundError as e:
        print("❌ Error, device not found!")
        await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE pair result',
                'result': 'error', 'msg': f"❌ device not found {mac}: {e}",
                'timestamp': int(time.time() * 1000)
            })
        return

    try:
        await dev_iface.call_pair()
        if has_console:
           print(f"✅ Successfully paired with {mac}")

        await dev_iface.set_trusted(True)
        if has_console:
           print(f"🔐 Device {mac} marked as trusted.")

        is_paired = await dev_iface.get_paired()
        if has_console:
           print(f"📎 Paired state of {mac}: {is_paired}")

        is_trusted = await dev_iface.get_trusted()
        if has_console:
           print(f"Trust state: {is_trusted}")

        is_bonded = await dev_iface.get_bonded()
        if has_console:
           print(f"Bond state: {is_bonded}")

        await asyncio.sleep(2)
        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'ble_pair result',
                'result': 'ok', 'msg': f"✅ Successfully paired {mac}",
                'timestamp': int(time.time() * 1000)
            })

        try:
           await dev_iface.call_disconnect()
           print(f"🔌 Disconnected from {mac} after pairing.")
        except Exception as e:
           print(f"⚠️ Could not disconnect from {mac}: {e}")

    except Exception as e:
        print(f"❌ Failed to pair with {mac}: {e}")
        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE pair result',
                'result': 'error', 'msg': f"❌ failed to pair {mac}: {e}",
                'timestamp': int(time.time() * 1000)
            })


async def ble_unpair(mac: str, message_router: Any | None = None) -> None:
    if has_console:
       print(f"🧹 Unpairing {mac} using blueZ ...")

    device_path = f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"
    adapter_path = "/org/bluez/hci0"

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Unpairing logic
    adapter_obj = bus.get_proxy_object(BLUEZ_SERVICE_NAME, adapter_path,
                                   await bus.introspect(BLUEZ_SERVICE_NAME, adapter_path))
    adapter_iface = adapter_obj.get_interface("org.bluez.Adapter1")

    try:
      await adapter_iface.call_remove_device(device_path)
    except DBusError as e:
      print(f"❌ device {mac}",e)
      if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE unpair result',
                'result': 'error', 'msg': f"❌ device {mac}",
                'timestamp': int(time.time() * 1000)
            })
      return

    print(f"🧹 Unpaired device {mac}")
    if message_router:
        await message_router.publish('ble', 'ble_status', {
            'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'BLE unpair',
            'result': 'ok', 'msg': f"✅ Unpaired device {mac}",
            'timestamp': int(time.time() * 1000)
        })


async def ble_connect(MAC: str, message_router: Any | None = None) -> None:
    global client

    if client is None:
        # MeshCom BLE UART service UUIDs (Nordic UART Service)
        # Hello handshake: [0x04][0x10][0x20][0x30]
        # - 0x04 = length (4 bytes total: 1 length + 1 msg_id + 2 data)
        # - 0x10 = message ID (Hello)
        # - 0x20 0x30 = data payload
        # Required before device will process A0 commands (per firmware spec)
        client = BLEClient(
            mac=MAC,
            read_uuid="6e400003-b5a3-f393-e0a9-e50e24dcca9e",  # RX (notifications)
            write_uuid="6e400002-b5a3-f393-e0a9-e50e24dcca9e",  # TX (write)
            hello_bytes=b'\x04\x10\x20\x30',  # CORRECT: length includes itself
            message_router=message_router
        )

    if not client._connected:
      await client.connect()

      if client._connected:
        await client.start_notify()
        await client.send_hello()

    else:
      if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE',
                'TYP': 'blueZ',
                'command': 'connect BLE result',
                'result': 'info',
                'msg': "BLE connection already running",
                "timestamp": int(time.time() * 1000)
            })

      if has_console:
         print("can't connect, already connected")


async def ble_disconnect(message_router: Any | None = None) -> None:
    global client
    if client is None:
      return

    if client._connected:
      await client.disconnect()
      await client.close()
      client = None
    else:
      if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': 'disconnect BLE result',
                'result': 'error', 'msg': "can't disconnect, already disconnected",
                'timestamp': int(time.time() * 1000)
            })

      if has_console:
         print("❌ can't disconnect, already disconnected")


async def scan_ble_devices(message_router: Any | None = None) -> None:
    scanclient = BLEClient(
        mac ="",
        read_uuid = "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
        write_uuid = "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
        hello_bytes = b'\x04\x10\x20\x30',
        message_router=message_router
    )
    await scanclient.scan_ble_devices()


async def backend_resolve_ip(hostname: str, message_router: Any | None = None) -> None:
    import socket
    loop = asyncio.get_event_loop()

    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
        ip = infos[0][4][0]
        if has_console:
           print(f"Resolved IP: {ip}")

        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': "resolve-ip",
                'result': "ok", 'msg': ip, 'timestamp': int(time.time() * 1000)
            })

    except Exception as e:
        if has_console:
           print(f"Error resolving IP: {e}")
        if message_router:
            await message_router.publish('ble', 'ble_status', {
                'src_type': 'BLE', 'TYP': 'blueZ', 'command': "resolve-ip",
                'result': "error", 'msg': str(e), 'timestamp': int(time.time() * 1000)
            })


# Functions to access the global client
def get_ble_client() -> BLEClient | None:
    """Get the current BLE client instance"""
    return client

async def handle_ble_message(msg: str, grp: str) -> None:
    """Handle messages through global client"""
    global client
    if client is not None:
        await client.send_message(msg, grp)
    else:
        print("BLE client not connected")


async def handle_a0_command(command: str) -> None:
    """Handle A0 commands through global client"""
    global client
    if client is not None:
        await client.a0_commands(command)
    else:
        print("BLE client not connected")


async def handle_set_command(command: str) -> None:
    """Handle set commands through global client"""
    global client
    if client is not None:
        await client.set_commands(command)
    else:
        print("BLE client not connected")
