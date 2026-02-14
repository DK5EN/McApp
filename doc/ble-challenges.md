# BLE Implementation Gap Analysis

**Date:** 2026-02-14
**Firmware Reference:** MeshCom 4.35k
**Documentation:** `doc/a0-commands.md`

## Executive Summary

This document identifies gaps, bugs, and potential issues in the McApp BLE implementation when compared against the official MeshCom firmware 4.35k BLE protocol specification.

**Overall Assessment:** The implementation is functional but has several critical issues that could cause reliability problems, missed responses, and incorrect behavior.

---

## Critical Issues

### 1. **HELLO Handshake Format Mismatch** 🔴

**Location:** `src/mcapp/ble_handler.py:1615`, `ble_handler.py:1667`

**Current Implementation:**
```python
hello_bytes=b'\x04\x10\x20\x30'
```

**Expected Format (per documentation):**
```
[Length 1B] [Message ID 1B] [Data...]
```

**Problem:**
- The hello bytes are `\x04\x10\x20\x30` (4 bytes total)
- According to the protocol: `[Length][0x10][0x20][0x30]`
- Length should be **4** (total message length)
- Current implementation sends: `0x04 0x10 0x20 0x30`
- This appears correct, BUT the documentation states the data for 0x10 should be **only** `0x20 0x30` (2 bytes)
- So the length should be `0x03` (1 byte length + 1 byte ID + 2 bytes data)

**Expected:** `b'\x03\x10\x20\x30'`
**Current:** `b'\x04\x10\x20\x30'`

**Impact:** Medium - Device may ignore the hello message due to length mismatch, though it seems to work in practice (possibly firmware is lenient).

**Recommendation:** Verify with actual device testing. If firmware strictly validates length, this will cause connection failures.

---

### 2. **Missing 0x10 Hello Protocol Enforcement** 🔴

**Location:** `src/mcapp/ble_handler.py`

**Problem:**
Per documentation (page 895): "The phone app must send `0x10` hello message before other commands will be processed."

**Current Implementation:**
- Hello is sent in `send_hello()` at line 1066
- Hello is called during connection at line 1624
- **However:** When querying registers via `_query_ble_registers()` in `main.py:494`, no verification is done that the hello was successful or that the device acknowledged it

**Current Flow:**
1. Connect to device
2. `send_hello()` fires off hello bytes (no wait for response)
3. Immediately start sending `--nodeset`, `--pos info`, etc.

**Expected Flow:**
1. Connect to device
2. Send hello
3. **Wait for device acknowledgment or delay**
4. Send configuration queries

**Impact:** High - Commands may be silently ignored if sent before device is ready to process them.

**Recommendation:** Add a delay (500-1000ms) after `send_hello()` before sending any A0 commands, or implement a proper handshake acknowledgment mechanism.

---

### 3. **`--pos info` Command Doesn't Exist** 🔴

**Location:** `src/mcapp/main.py:494`

**Current Code:**
```python
for cmd in ('--nodeset', '--pos info', '--aprsset', '--info'):
```

**Documentation:** The command is `--pos`, not `--pos info`

**Problem:**
- `--pos info` is not a valid command according to the documentation
- The correct command is just `--pos` (returns TYP: G)
- The firmware will likely ignore `--pos info` or treat it incorrectly

**Impact:** High - Position/GPS data will not be queried on frontend connection

**Expected Commands:**
```python
for cmd in ('--nodeset', '--pos', '--aprsset', '--info'):
```

**Recommendation:** Remove the ` info` suffix immediately.

---

### 4. **Multi-Part Response Handling Not Documented** 🟡

**Location:** `src/mcapp/ble_handler.py:556-559`

**Current Implementation:**
The dispatcher correctly lists all TYP responses including SE, S1, SW, S2:
```python
elif input_dict["TYP"] in [
    "I", "SN", "G", "SA", "W", "IO", "TM", "AN", "SE", "SW", "S1", "S2",
]:
```

**Documentation Warning (page 101):**
- `--seset` sends **TWO** responses: SE followed by S1
- `--wifiset` sends **TWO** responses: SW followed by S2

**Problem:**
- Code handles individual TYP messages correctly
- **BUT**: No explicit handling or documentation for the fact that one command triggers two separate BLE notifications
- Frontend may receive SE and S1 separately without knowing they belong together
- No test coverage for multi-part responses

**Impact:** Medium - Frontend may not properly associate S1 with SE, or S2 with SW

**Recommendation:**
1. Add explicit comments in code about multi-part responses
2. Test frontend behavior when receiving SE/S1 and SW/S2 pairs
3. Consider adding correlation logic (timestamp-based grouping) if frontend has issues

---

### 5. **Missing Message ID 0x44 vs 0x40 Distinction** 🟡

**Location:** `src/mcapp/ble_handler.py:67-75` (decode_json_message)

**Documentation (page 349):**
> "All TYP responses use data message flag `0x44` (not `0x40`)"

**Current Implementation:**
- JSON decoding strips the first byte: `json_str = byte_msg.rstrip(b'\x00').decode("utf-8")[1:]`
- No check for whether the message is `D{` (0x44) vs regular `0x40`

**Problem:**
- The implementation assumes all JSON messages start with `D{` and strips it
- This is correct, but there's no validation that the preceding byte was indeed `0x44`
- No differentiation between data messages (0x44) and text messages (0x40)

**Impact:** Low - Works in practice but lacks protocol fidelity

**Recommendation:** Add message type validation for robustness:
```python
def decode_json_message(byte_msg):
    # Check if this is a data message (0x44)
    if len(byte_msg) > 0 and byte_msg[0] == 0x44:
        # Data message
        json_str = byte_msg[1:].rstrip(b'\x00').decode("utf-8")
    elif byte_msg.startswith(b'D{'):
        # Legacy handling
        json_str = byte_msg.rstrip(b'\x00').decode("utf-8")[1:]
    else:
        raise ValueError(f"Unknown message format: {byte_msg[:2]}")

    return json.loads(json_str)
```

---

### 6. **Register Query Timing Issues** 🟡

**Location:** `src/mcapp/main.py:489-499`

**Current Implementation:**
```python
for cmd in ('--nodeset', '--pos info', '--aprsset', '--info'):
    try:
        await client.send_command(cmd)
        await asyncio.sleep(0.6)
    except Exception as e:
        logger.warning("Register query %s failed: %s", cmd, e)
```

**Problems:**

1. **0.6 second delay too short for multi-part responses**
   - `--seset` sends SE + S1 (requires ~200ms between)
   - `--wifiset` sends SW + S2 (requires ~200ms between)
   - 600ms may not be enough if device is busy or BLE is slow

2. **No response validation**
   - Commands are sent blindly
   - No check that responses were received
   - No timeout mechanism if device doesn't respond

3. **Missing comprehensive queries**
   - Only queries 4 basic registers: SN, G (broken), SA, I
   - Doesn't query: SE/S1, SW/S2, AN, TM, W, IO
   - Frontend may not have full device state

4. **Error handling too permissive**
   - Catches all exceptions and continues
   - Should retry or fail loudly on critical queries (like `--info`)

**Impact:** Medium - Incomplete device state, potential race conditions

**Recommendation:**
```python
async def _query_ble_registers(self):
    """Query BLE device config registers with proper timing and validation."""
    client = self._get_ble_client()
    if not client:
        return

    # Critical registers (always query these)
    critical_queries = [
        ('--info', 800),      # TYP: I (device info)
        ('--nodeset', 800),   # TYP: SN (node settings)
        ('--aprsset', 800),   # TYP: SA (APRS config)
        ('--pos', 800),       # TYP: G (GPS/position) - FIXED!
    ]

    # Extended registers (query if time permits)
    extended_queries = [
        ('--seset', 1200),    # TYP: SE + S1 (sensors, multi-part!)
        ('--wifiset', 1200),  # TYP: SW + S2 (WiFi, multi-part!)
        ('--weather', 800),   # TYP: W (sensor readings)
    ]

    for cmd, delay_ms in critical_queries:
        try:
            await client.send_command(cmd)
            await asyncio.sleep(delay_ms / 1000)
        except Exception as e:
            logger.error("Critical register query %s failed: %s", cmd, e)
            # Don't continue if critical queries fail
            raise

    # Optional: query extended registers
    for cmd, delay_ms in extended_queries:
        try:
            await client.send_command(cmd)
            await asyncio.sleep(delay_ms / 1000)
        except Exception as e:
            logger.warning("Extended register query %s failed: %s", cmd, e)
            # Continue even if extended queries fail
```

---

### 7. **No Timestamp Sync Implementation** 🟡

**Location:** `src/mcapp/ble_handler.py:1145-1169` (set_commands)

**Current Implementation:**
- `--settime` command exists and correctly sends 0x20 message with 4-byte little-endian UNIX timestamp
- **BUT**: This is only called when explicitly requested by user via frontend

**Documentation (page 898):**
> "Send `0x20` with UNIX timestamp to synchronize device clock (especially important for devices without GPS or RTC battery)."

**Problem:**
- No automatic time sync on connection
- Nodes without GPS or RTC will have incorrect timestamps
- Time-sensitive features (beacons, telemetry intervals) will be wrong

**Impact:** Medium - Affects nodes without GPS/RTC

**Recommendation:**
1. Add automatic time sync after hello handshake
2. Add to initial connection sequence:
```python
async def connect(self, mac: str) -> bool:
    await legacy_ble_connect(mac, message_router=self.message_router)
    if client and client._connected:
        # Send hello
        await client.send_hello()
        await asyncio.sleep(1.0)  # Wait for handshake

        # Auto-sync time
        await client.set_commands("--settime")

        self._status.state = ConnectionState.CONNECTED
        return True
```

---

### 8. **No Settings Persistence After Configuration** 🟡

**Location:** N/A (missing functionality)

**Documentation (page 893):**
> "Most configuration commands require `--save` or `0xF0` message to persist to flash, otherwise settings are lost on reboot."

**Problem:**
- Code can send configuration commands via `set_commands()`
- **No implementation for 0xF0 (Save & Reboot) message**
- **No `--save` command support**
- Settings changes made via frontend will be lost on device reboot

**Impact:** High - Configuration changes are temporary

**Recommendation:**
1. Implement 0xF0 message type:
```python
async def save_and_reboot(self):
    """Send 0xF0 save & reboot command"""
    if not self.bus:
        return

    # Message format: [Length][0xF0]
    byte_array = bytes([0x02, 0xF0])

    if self.write_char_iface:
        await self.write_char_iface.call_write_value(byte_array, {})
        logger.info("Sent save & reboot command")
```

2. Add explicit save command:
```python
async def save_settings(self):
    """Save settings without rebooting"""
    await self.a0_commands("--save")
```

3. Add UI option for "Save to Flash" after configuration changes

---

### 9. **Missing BLE MTU Size Validation** 🟡

**Location:** `src/mcapp/ble_handler.py:1103-1122`, `1131-1143`

**Documentation (page 892):**
> "BLE MTU Limit: Maximum BLE packet size is 247 bytes (MTU). Messages exceeding this will be truncated."

**Current Implementation:**
- Messages are constructed and sent without length validation
- No MTU check before sending

**Problem:**
- Long messages (e.g., `--setssid VeryLongWiFiNetworkName123456789...`) could exceed MTU
- Truncation would corrupt the message
- No error handling for oversized messages

**Impact:** Low - Most commands are short, but edge cases exist

**Recommendation:**
```python
MAX_BLE_MTU = 247

async def send_message(self, msg, grp):
    message = "{" + grp + "}" + msg
    byte_array = bytearray(message.encode('utf-8'))
    laenge = len(byte_array) + 2

    if laenge > MAX_BLE_MTU:
        logger.error("Message too long: %d bytes (max %d)", laenge, MAX_BLE_MTU)
        raise ValueError(f"Message exceeds BLE MTU: {laenge} > {MAX_BLE_MTU}")

    byte_array = laenge.to_bytes(1, 'big') + bytes([0xA0]) + byte_array
    # ... send
```

---

### 10. **Incomplete Command Coverage** 🟡

**Location:** `src/mcapp/ble_handler.py:1145-1300` (set_commands function)

**Current Implementation:**
Only implements:
- `--settime` (0x20 timestamp)

**Missing from Documentation:**
All other configuration commands that use non-0xA0 message types:

| Msg ID | Command | Status | Implementation Needed |
|--------|---------|--------|---------------------|
| 0x10 | Hello | ✅ Implemented | (in send_hello) |
| 0x20 | Timestamp | ✅ Implemented | (in set_commands) |
| 0x50 | Set Callsign | ❌ Missing | 1B length + callsign string |
| 0x55 | WiFi Settings | ❌ Missing | 1B SSID_len + SSID + 1B PWD_len + PWD |
| 0x70 | Set Latitude | ❌ Missing | 4B float + 1B save_flag |
| 0x80 | Set Longitude | ❌ Missing | 4B float + 1B save_flag |
| 0x90 | Set Altitude | ❌ Missing | 4B int + 1B save_flag |
| 0x95 | APRS Symbols | ❌ Missing | 1B primary + 1B secondary |
| 0xF0 | Save & Reboot | ❌ Missing | (none) |

**Impact:** Medium - Limited configuration capabilities via frontend

**Recommendation:**
Implement missing message types in `set_commands()`:

```python
async def set_commands(self, cmd):
    if not self.bus:
        return

    await self._check_conn()

    # Parse command and route to appropriate message type
    if cmd == "--settime":
        # Existing implementation (0x20)
        ...

    elif cmd.startswith("--setcall "):
        # 0x50: Set callsign
        callsign = cmd.split()[1].encode('utf-8')
        byte_array = bytes([len(callsign)]) + callsign
        laenge = len(byte_array) + 2
        byte_array = laenge.to_bytes(1, 'big') + bytes([0x50]) + byte_array

    elif cmd.startswith("--setssid ") and " --setpwd " in cmd:
        # 0x55: WiFi settings
        parts = cmd.split()
        ssid_idx = parts.index("--setssid") + 1
        pwd_idx = parts.index("--setpwd") + 1
        ssid = parts[ssid_idx].encode('utf-8')
        pwd = parts[pwd_idx].encode('utf-8')
        byte_array = bytes([len(ssid)]) + ssid + bytes([len(pwd)]) + pwd
        laenge = len(byte_array) + 2
        byte_array = laenge.to_bytes(1, 'big') + bytes([0x55]) + byte_array

    elif cmd.startswith("--setlat "):
        # 0x70: Set latitude
        lat = float(cmd.split()[1])
        save_flag = 0x0A if "--save" in cmd else 0x0B
        import struct
        byte_array = struct.pack('<f', lat) + bytes([save_flag])
        laenge = len(byte_array) + 2
        byte_array = laenge.to_bytes(1, 'big') + bytes([0x70]) + byte_array

    # ... implement 0x80, 0x90, 0x95, 0xF0

    else:
        # Default: send as 0xA0 command
        byte_array = bytearray(cmd.encode('utf-8'))
        laenge = len(byte_array) + 2
        byte_array = laenge.to_bytes(1, 'big') + bytes([0xA0]) + byte_array

    if self.write_char_iface:
        await self.write_char_iface.call_write_value(byte_array, {})
```

---

### 11. **No Retry Logic for Failed Commands** 🟡

**Location:** `src/mcapp/main.py:494-499`

**Problem:**
- Commands can fail silently
- No retry mechanism
- BLE is inherently unreliable (interference, distance, etc.)

**Impact:** Medium - Commands may be lost without user awareness

**Recommendation:**
```python
async def _send_command_with_retry(self, cmd: str, max_retries: int = 3):
    """Send command with exponential backoff retry"""
    client = self._get_ble_client()
    if not client:
        return False

    for attempt in range(max_retries):
        try:
            await client.send_command(cmd)
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                delay = 0.5 * (2 ** attempt)  # Exponential backoff
                logger.warning("Command %s failed (attempt %d/%d), retrying in %.1fs: %s",
                             cmd, attempt + 1, max_retries, delay, e)
                await asyncio.sleep(delay)
            else:
                logger.error("Command %s failed after %d attempts: %s",
                           cmd, max_retries, e)
                return False
```

---

### 12. **Binary Message Parsing Incomplete** 🟡

**Location:** `src/mcapp/ble_handler.py:78-220` (decode_binary_message)

**Current Implementation:**
- Parses @A (ACK frames)
- Parses @: and @! (binary mesh messages)

**Missing:**
- No parsing for other binary message types from device
- No validation of frame checksum (FCS) - it's calculated but not verified
- Incomplete error handling for malformed binary messages

**Documentation Issues:**
- Code has FCS validation logic: `fcs_ok = (calced_fcs == fcs)` at line 189
- **BUT**: `fcs_ok` is calculated but never used to reject invalid messages

**Impact:** Medium - Corrupted messages may be processed incorrectly

**Recommendation:**
```python
# After calculating fcs_ok at line 189
if not fcs_ok:
    logger.warning("Frame checksum failed: calculated=0x%04X, received=0x%04X",
                   calced_fcs, fcs)
    raise ValueError("Invalid frame checksum")
```

---

### 13. **Remote BLE Client Missing Multi-Part Handling** 🟡

**Location:** `src/mcapp/ble_client_remote.py`

**Problem:**
- Remote BLE client receives notifications via SSE
- No specific handling for multi-part responses (SE+S1, SW+S2)
- May not properly correlate related messages

**Impact:** Low - Likely works but lacks explicit handling

**Recommendation:**
- Ensure remote BLE service documentation mentions multi-part responses
- Add correlation logic if issues arise

---

## Missing Features

### 1. Advanced Query Commands Not Exposed

**Missing:** The following query commands exist in firmware but aren't used:

- `--weather` / `--wx` (TYP: W) - Real-time sensor readings
- `--tel` (TYP: TM) - Telemetry configuration
- `--analogset` (TYP: AN) - Analog input config
- `--io` (TYP: IO) - GPIO status
- `--seset` (TYP: SE + S1) - Full sensor config
- `--wifiset` (TYP: SW + S2) - Full WiFi config

**Impact:** Medium - Frontend has limited device visibility

**Recommendation:** Extend `_query_ble_registers()` to include these (see Issue #6)

---

### 2. No Spectrum Analysis Support

**Documentation:** Firmware supports spectrum scanning (page 451-468)

Commands: `--spectrum`, `--specstart`, `--specend`, `--specstep`, `--specsamples`

**Status:** Not implemented in McApp

**Impact:** Low - Niche feature

---

### 3. No Telemetry Configuration

**Documentation:** Full APRS telemetry system (page 604-629)

Commands: `--parm`, `--unit`, `--format`, `--eqns`, `--values`, `--ptime`

**Status:** Not implemented

**Impact:** Low - Advanced feature

---

### 4. No GPIO/Analog Control

**Documentation:** GPIO and analog input commands (page 633-662)

Commands: `--setio`, `--setout`, `--analog gpio`, etc.

**Status:** Not implemented

**Impact:** Low - Hardware-specific feature

---

## Code Quality Issues

### 1. Inconsistent Error Handling

- Some functions use try/except, others don't
- Error messages mix English and German
- No consistent error reporting to frontend

### 2. Magic Numbers

- Hardcoded delays: `0.6`, `1.0`, `30.0`
- No constants for BLE UUIDs (partially addressed)
- MTU limit (247) not defined as constant

### 3. No Type Hints in BLE Handler

- `ble_handler.py` predates type hint adoption
- Newer code (ble_client*.py) has proper type hints
- Inconsistent typing across modules

### 4. German Comments and Strings

- Many comments and error messages in German
- Makes code harder to maintain internationally
- Example: "Fehler beim Dekodieren der JSON-Nachricht"

---

## Testing Gaps

### 1. No Unit Tests for BLE Protocol

- No tests for message encoding/decoding
- No tests for multi-part response handling
- No tests for edge cases (MTU overflow, invalid messages)

### 2. No Integration Tests

- No automated tests for device connection flow
- No tests for register query sequence
- No validation of response handling

### 3. No Mock BLE Device

- Testing requires real hardware
- No simulator for development

---

## Documentation Gaps

### 1. Multi-Part Responses Undocumented in Code

- SE+S1 and SW+S2 behavior not documented in docstrings
- No comments explaining the timing requirements

### 2. No BLE State Machine Diagram

- Connection flow is complex but not diagrammed
- Difficult to understand hello → query → time sync sequence

### 3. Missing Protocol Version

- Code doesn't track which firmware version's protocol it implements
- No version negotiation with device

---

## Security Concerns

### 1. No BLE Pairing PIN Validation

**Documentation (page 710):** `--btcode` command sets 6-digit PIN

**Current Implementation:**
- Pairing uses BlueZ "KeyboardDisplay" capability
- No validation of PIN format or security

**Impact:** Low - Relies on BlueZ security

### 2. WiFi Credentials Sent in Plain Text

**Documentation:** `0x55` message sends SSID and password

**Problem:**
- BLE is encrypted at link layer, but no additional encryption
- WiFi password visible in BLE packet captures

**Impact:** Low - Standard BLE practice, but worth noting

---

## Performance Issues

### 1. Sequential Register Queries

**Current:** Commands sent one-by-one with delays

**Improvement:** Could batch non-dependent queries

### 2. No Response Caching

**Problem:** Every frontend connection re-queries all registers

**Improvement:** Cache responses and only refresh on user request or timeout

---

## Recommendations Summary

### Immediate Fixes (Critical) 🔴

1. **Fix `--pos info` to `--pos`** (main.py:494)
2. **Add delay after hello handshake** before querying registers
3. **Verify hello bytes format** (`\x03` vs `\x04`)

### High Priority 🟠

4. Implement 0xF0 save & reboot message
5. Add automatic time sync on connection
6. Implement retry logic for failed commands
7. Add MTU size validation

### Medium Priority 🟡

8. Implement missing message types (0x50, 0x55, 0x70, 0x80, 0x90, 0x95)
9. Add FCS validation for binary messages
10. Extend register queries to include SE/S1, SW/S2, W, AN
11. Add unit tests for protocol encoding/decoding

### Low Priority ⚪

12. Translate German comments to English
13. Add type hints to ble_handler.py
14. Implement spectrum analysis commands
15. Create BLE state machine diagram

---

## Testing Checklist

Before deploying to production:

- [ ] Verify hello handshake with real device
- [ ] Test `--pos` command returns TYP: G correctly
- [ ] Confirm SE+S1 arrive as separate notifications
- [ ] Confirm SW+S2 arrive as separate notifications
- [ ] Test MTU overflow behavior (send 300-byte message)
- [ ] Test time sync accuracy
- [ ] Test connection recovery after BLE disconnect
- [ ] Test register queries with poor BLE signal
- [ ] Verify settings persistence after `--save` / 0xF0

---

## Conclusion

The BLE implementation is **functional but incomplete**. The most critical issue is the incorrect `--pos info` command that will prevent GPS data from loading. The lack of hello handshake delay and missing save functionality are also significant gaps.

**Recommended Action Plan:**

1. Apply immediate fixes (Issues #1-3) - **1 hour**
2. Implement high-priority features (Issues #4-7) - **1 day**
3. Add comprehensive testing - **2 days**
4. Address medium-priority items as needed - **ongoing**

**Risk Assessment:** Medium - System works for basic use cases but has reliability and completeness issues that could cause problems in production.

---

**Document Version:** 1.0
**Author:** Gap analysis by Claude Sonnet 4.5
**Next Review:** After immediate fixes are applied
