# MeshCom ACK System Documentation

## Overview

The MeshCom ACK (Acknowledgment) system uses special messages to confirm receipt. ACK messages have a fixed structure and use various status values.

## ACK Message Format

An ACK message has the following structure:

```
[0x41] [MSG_ID - 4 Bytes] [FLAGS] [ACK_MSG_ID - 4 Bytes] [ACK_TYPE] [0x00]
```

### Byte Breakdown:

1. **Byte 0: Message Type (0x41)**
   - Identifies the message as an ACK message

2. **Bytes 1-4: MSG_ID**
   - 32-bit message ID of the ACK message itself
   - Byte 1: LSB (Least Significant Byte)
   - Byte 2:
   - Byte 3:
   - Byte 4: MSB (Most Significant Byte)
   - Format: Little-Endian

3. **Byte 5: FLAGS**
   - Bit 7 (0x80): Server Flag (`msg_server`)
     - 1 = frame has passed a gateway/server
     - 0 = normal peer-to-peer message
   - Bit 6 (0x40): Track Flag (`msg_track`)
   - Bit 5 (0x20): App-Offline Flag (`msg_app_offline`)
     - Set by a gateway when re-emitting a server frame
   - Bit 4 (0x10): Mesh Flag (`msg_mesh`)
     - Mesh (relay) enabled
   - Bits 0-3 (0x0F): Max Hop Count
     - Number of remaining hops for mesh forwarding
     - Decremented with each forwarding step

4. **Bytes 6-9: ACK_MSG_ID**
   - 32-bit message ID of the original message being acknowledged
   - Byte 6: LSB
   - Byte 7:
   - Byte 8:
   - Byte 9: MSB
   - Format: Little-Endian

5. **Byte 10: ACK_TYPE**
   - 0x00: Node ACK (acknowledgment from a regular node)
   - 0x01: Gateway ACK (acknowledgment from a gateway)
   - 0x02: Peer ACK (the addressee's own matched `:ack`/`:rej` reply to a DM this
     node originated, confirmed over the mesh or via the server path — the
     strongest ack the firmware emits; MCProxy's `ACK_KIND_BY_TYPE` in
     `src/mcapp/ble_protocol.py` maps 0/1/2 to `node`/`gateway`/`peer`)

6. **Byte 11: Terminator (0x00)**
   - Marks the end of the ACK message

## ACK Status Codes

The following status values are used in the code for message tracking:

```cpp
own_msg_id[index][4] = status
```

- **0x00**: Message not yet heard
- **0x01**: Message was heard (HEARD)
- **0x02**: ACK was received

## Message ID Structure

The message ID is a 32-bit value that can be structured as follows:

### Standard Message ID

- Based on `millis()` (milliseconds since start)
- Unique per node during a session

### Gateway Message ID Format

```cpp
msg_counter = ((_GW_ID & 0x3FFFFF) << 10) | (iAckId & 0x3FF);
```

- Bits 31-10: Gateway ID (22 Bits)
- Bits 9-0: ACK ID (10 Bits)

## ACK Processing Logic

### 1. Receiving a Regular Message

- System checks whether it matches one of its own message IDs
- If yes and status = 0x00, status is set to 0x01 (HEARD)
- A HEARD notification is sent to the phone/BLE

### 2. Receiving an ACK Message

- System checks the ACK_MSG_ID against its own sent messages
- If found and status < 0x02:
  - Status is set to 0x02 (ACK received)
  - ACK is forwarded to phone/BLE

### 3. Gateway ACK Generation

Gateways automatically send ACKs for:

- Messages to "*" (broadcast)
- Messages to "WLNK-1"
- Messages to "APRS2SOTA"
- Group messages

### 4. ACK Forwarding in the Mesh

- ACKs are only forwarded when:
  - Max hop count > 0
  - Mesh functionality is enabled
  - It is a new message ID
  - The message does not already come from the server

## Special ACK Cases

### Direct Message ACK

For direct messages with "{" at the end:

```
:messagetext{123
```

The number after "{" is the ACK ID referenced in the reply.

### ACK/REJ Messages

Payload format:

- `:ack123` - Positive acknowledgment for message 123
- `:rej123` - Rejection for message 123

## Example ACK Sequence

1. **Original message sent:**

   ```
   MSG_ID: 0x12345678
   ```

2. **HEARD status (when message is heard on the network):**

   ```
   [0x41] [0x78,0x56,0x34,0x12] [0x00] [0x00,0x00]
   Status → 0x01
   ```

3. **ACK received:**
   ```
   [0x41] [new_msg_id] [0x83] [0x78,0x56,0x34,0x12] [0x01] [0x00]
   Status → 0x02
   ```

## Implementation Notes

- ACKs are not generated for telemetry messages (destination: "100001")
- Broadcast messages with special prefixes ({MCP}, {SET}, {CET}) do not generate gateway ACKs
- The retransmission logic uses the ACK status to decide on retries
- ACK messages themselves are marked with status 0xFF (no retransmission)

### BLE vs. LoRa ACK Formats

**Important:** The firmware sends two different ACK formats:

**7-byte ACK (sent to BLE phone):**

```
[0x41] [ORIG_MSG_ID - 4 Bytes] [ACK_TYPE] [0x00]
```

- Only contains the original message ID and ack type
- No separate ACK sender ID or FLAGS byte
- `addBLEOutBuffer` appends a 4-byte unix timestamp
- `sendToPhone` prepends `0x40`, making the GATT notification:
  `[0x40][0x41][orig_msg_id×4][ack_type][0x00][timestamp×4][pad]` (**13 bytes on
  the wire**: 1 + 1 + 4 + 1 + 1 + 4 + 1; see firmware
  `docs/architecture/11-wire-format.md` §4.3)
- The fork's ACK-attribution appendix extends this further: byte 7 (normally
  the trailing pad) becomes the length `n` of an acknowledging-station
  callsign, followed by `n` callsign bytes at `[8..8+n]`, then the timestamp.
  `n == 0` is byte-identical to the legacy 13-byte form above, so old
  firmware and the legacy path both parse unchanged.

**12-byte ACK (sent over LoRa mesh):**

```
[0x41] [ACK_MSG_ID - 4 Bytes] [FLAGS] [ORIG_MSG_ID - 4 Bytes] [ACK_TYPE] [0x00]
```

- Contains both the ACK sender's counter and the original message ID
- This format is described in the main section above but is **never sent to BLE**

McApp parses the 7-byte BLE format: `msg_id` (bytes 2-5 after `@` prefix) is the original message ID used for DB lookup. `ack_type` is byte 6 (the position that would be FLAGS in non-ACK messages).
