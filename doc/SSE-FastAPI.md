# SSE API Specification for MeshCom Proxy

This document specifies the Server-Sent Events (SSE) API that replaces the existing WebSocket implementation for the MeshCom Web App. SSE is needed for local Raspberry Pi connections over unencrypted HTTP (WebSockets require HTTPS for `wss://`).

## Overview

The frontend will connect to the backend using two endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/events` | GET | SSE stream for receiving messages from the backend |
| `/api/send` | POST | Send commands/messages to the backend |

**Base URL:** `http://{host}:{port}` (e.g., `http://rpizero.local:2981`)

---

## GET `/events` - SSE Event Stream

### Description
Opens a Server-Sent Events connection that streams JSON messages to the client. The connection should remain open indefinitely, with the server pushing events as they occur.

### Request
```http
GET /events HTTP/1.1
Host: rpizero.local:2981
Accept: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

### Response Headers
```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

### SSE Event Format
Each event follows the standard SSE format. The `data` field contains a JSON string.

```
data: {"type":"response","msg":"message dump","data":[...]}

data: {"msg_id":12345,"src":"OE1ABC","dst":"*","msg":"Hello World","timestamp":1706200000}

```

**Important:** Each event is terminated by two newlines (`\n\n`).

### Event Types (JSON Payloads)

The server sends different types of JSON messages. The client parses these based on the `type` or `src_type` fields.

#### 1. Message Data
Regular chat/text messages from the mesh network.

```json
{
  "msg_id": 12345,
  "src": "OE1ABC-12",
  "src_type": "node",
  "dst": "OE2XYZ",
  "via": "OE3GW",
  "gateway": "OE3GW",
  "timestamp": 1706200000,
  "msg": "Hello from the mesh!",
  "msg_ack": true,
  "duplicate": false,
  "max_hop": 3,
  "mesh_info": 1,
  "lora_mod": 0,
  "last_hw": 2,
  "hw_id": 42
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `msg_id` | number | Yes | Unique message identifier |
| `src` | string | Yes | Source callsign (may include `,via` suffix) |
| `src_type` | string | Yes | Source type: `node`, `www`, `udp`, `ble`, `lora` |
| `dst` | string | Yes | Destination callsign or group (`*` for broadcast) |
| `via` | string | No | Relay station callsign |
| `gateway` | string | No | Gateway station |
| `timestamp` | number | Yes | Unix timestamp (seconds) |
| `msg` | string | Yes | Message content |
| `msg_ack` | boolean | No | Message acknowledged |
| `msg_www` | boolean | No | Message from web |
| `duplicate` | boolean | No | Duplicate message flag |
| `max_hop` | number | No | Maximum hop count |
| `mesh_info` | number | No | Mesh information flags |
| `lora_mod` | number | No | LoRa modulation mode |
| `last_hw` | number | No | Last hardware ID |
| `hw_id` | number | No | Hardware ID |

#### 2. Position Data
APRS position reports with optional telemetry.

```json
{
  "msg_id": 12346,
  "src": "OE1ABC-9",
  "src_type": "node",
  "dst": "*",
  "timestamp": 1706200100,
  "lat": 48.2082,
  "lat_dir": "N",
  "long": 16.3738,
  "long_dir": "E",
  "aprs_sym": "k",
  "aprs_sym_grp": "/",
  "alt": 180,
  "batt": 85,
  "fw": "4.30",
  "rssi": -95,
  "snr": 8.5,
  "temp": 22.5,
  "hum": 65,
  "press": 1013
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `lat` | number | Yes | Latitude in decimal degrees |
| `lat_dir` | string | No | Latitude direction (N/S) |
| `long` | number | Yes | Longitude in decimal degrees |
| `long_dir` | string | No | Longitude direction (E/W) |
| `aprs_sym` | string | Yes | APRS symbol character |
| `aprs_sym_grp` | string | Yes | APRS symbol group (/ or \) |
| `alt` | number | No | Altitude in meters |
| `batt` | number | No | Battery percentage |
| `fw` | string | No | Firmware version |
| `fw_sub` | string | No | Firmware sub-version |
| `rssi` | number | No | Signal strength (dBm) |
| `snr` | number | No | Signal-to-noise ratio |
| `temp` | number | No | Temperature (°C) |
| `temp2` | number | No | Secondary temperature |
| `hum` | number | No | Humidity (%) |
| `press` | number | No | Pressure (hPa) |
| `qnh` | number | No | QNH pressure |
| `gas_res` | number | No | Gas resistance |
| `eco2` | number | No | eCO2 level |
| `wlevel` | number | No | Water level |
| `wtemp` | number | No | Water temperature |
| `gw` | number | No | Gateway flag |

#### 3. Response Data (Command Results)
Responses to commands sent by the client.

```json
{
  "type": "response",
  "msg": "message dump",
  "data": [
    {"msg_id": 1, "src": "OE1ABC", "dst": "*", "msg": "Test", "timestamp": 1706200000},
    {"msg_id": 2, "src": "OE2XYZ", "dst": "*", "msg": "Hello", "timestamp": 1706200100}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | `"response"` | Indicates this is a command response |
| `msg` | string | Command that was executed |
| `data` | array/object | Response data (varies by command) |

**Known `msg` values:**
- `"message dump"` - Returns array of recent messages
- `"mheard stats"` - Returns station statistics data

#### 4. MHeard Stats Response
Statistics for heard stations (used for RSSI/SNR charts).

```json
{
  "type": "response",
  "msg": "mheard stats",
  "data": [
    {"callsign": "OE1ABC", "timestamp": 1706200000, "rssi": -95, "snr": 8.5, "count": 15},
    {"callsign": "OE2XYZ", "timestamp": 1706200100, "rssi": -102, "snr": 5.2, "count": 8}
  ]
}
```

#### 5. ACK Data
Message acknowledgment.

```json
{
  "type": "ack",
  "msg_id": 12345
}
```

#### 6. BLE Control Data
Bluetooth LE device responses.

```json
{
  "src_type": "BLE",
  "command": "info",
  "data": {...}
}
```

| Field | Type | Description |
|-------|------|-------------|
| `src_type` | `"BLE"` | Indicates BLE-related data |
| `command` | string | BLE command that was executed |
| `data` | object | Command-specific response data |

#### 7. IP Resolution Response
Response to `resolve-ip` command.

```json
{
  "command": "resolve-ip",
  "msg": "192.168.1.50"
}
```

### FastAPI Implementation Example

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse
import asyncio
import json

app = FastAPI()

# Message queue for SSE clients
message_queue: asyncio.Queue = asyncio.Queue()

async def event_generator():
    """Generate SSE events from the message queue."""
    while True:
        try:
            # Wait for messages from the queue
            message = await message_queue.get()
            yield {
                "event": "message",
                "data": json.dumps(message)
            }
        except asyncio.CancelledError:
            break

@app.get("/events")
async def sse_endpoint():
    """SSE endpoint for streaming messages to clients."""
    return EventSourceResponse(event_generator())

# Alternative without sse-starlette:
@app.get("/events")
async def sse_endpoint_manual():
    async def generate():
        while True:
            message = await message_queue.get()
            yield f"data: {json.dumps(message)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
```

### Connection Lifecycle

1. **Client connects** → Server sends initial data (if available)
2. **Server pushes events** → As mesh messages arrive
3. **Keep-alive** → Send comment lines (`: keepalive\n\n`) every 30s to prevent timeout
4. **Client disconnects** → Clean up resources
5. **Reconnection** → EventSource auto-reconnects; server should handle gracefully

### Keep-Alive (Heartbeat)
Send periodic keep-alive comments to prevent connection timeout:

```
: keepalive

```

**Recommended interval:** Every 15-30 seconds.

---

## POST `/api/send` - Send Commands/Messages

### Description
Receives JSON commands from the client and processes them (sends to mesh, executes BLE commands, etc.).

### Request
```http
POST /api/send HTTP/1.1
Host: rpizero.local:2981
Content-Type: application/json

{"type":"command","dst":"999","msg":"send message dump"}
```

### Request Body Schema

```typescript
interface SendRequest {
  type?: string       // "command" for system commands, undefined for regular messages
  dst: string         // Destination callsign or group
  msg: string         // Message content or command string
  MAC?: string        // BLE device MAC address (for BLE commands)
  BLE_Pin?: string    // BLE PIN code (for pairing)
}
```

### Common Request Examples

#### 1. Request Message Dump (Initial Load)
```json
{
  "type": "command",
  "dst": "999",
  "msg": "send message dump"
}
```

#### 2. Send Chat Message
```json
{
  "dst": "OE2XYZ",
  "msg": "Hello, how are you?"
}
```

#### 3. Broadcast Message
```json
{
  "dst": "*",
  "msg": "CQ CQ CQ de OE1ABC"
}
```

#### 4. BLE Connect Command
```json
{
  "type": "command",
  "dst": "TEST",
  "msg": "connect BLE",
  "MAC": "D4:D4:DA:9E:B5:62"
}
```

#### 5. BLE Info Commands
```json
{"type": "command", "dst": "TEST", "msg": "--info", "MAC": "D4:D4:DA:9E:B5:62"}
{"type": "command", "dst": "TEST", "msg": "--nodeset", "MAC": "D4:D4:DA:9E:B5:62"}
{"type": "command", "dst": "TEST", "msg": "--pos info", "MAC": "D4:D4:DA:9E:B5:62"}
{"type": "command", "dst": "TEST", "msg": "--aprsset", "MAC": "D4:D4:DA:9E:B5:62"}
```

#### 6. Resolve IP Command
```json
{
  "type": "command",
  "dst": "TEST",
  "msg": "resolve-ip",
  "MAC": "rpizero.local"
}
```

### Response

#### Success
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok"}
```

#### Success with Data
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok", "msg_id": 12345}
```

#### Error
```http
HTTP/1.1 400 Bad Request
Content-Type: application/json

{"status": "error", "message": "Invalid destination"}
```

```http
HTTP/1.1 500 Internal Server Error
Content-Type: application/json

{"status": "error", "message": "Failed to send to mesh"}
```

### FastAPI Implementation Example

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

class SendRequest(BaseModel):
    type: Optional[str] = None
    dst: str
    msg: str
    MAC: Optional[str] = None
    BLE_Pin: Optional[str] = None

class SendResponse(BaseModel):
    status: str
    message: Optional[str] = None
    msg_id: Optional[int] = None

@app.post("/api/send", response_model=SendResponse)
async def send_message(request: SendRequest):
    """Handle incoming commands and messages from the web client."""
    try:
        if request.type == "command":
            # Handle system commands
            if request.msg == "send message dump":
                # Trigger message dump response via SSE
                await send_message_dump_via_sse()
                return SendResponse(status="ok")

            elif request.msg.startswith("--"):
                # BLE commands
                if not request.MAC:
                    raise HTTPException(400, "MAC address required for BLE commands")
                await handle_ble_command(request.msg, request.MAC)
                return SendResponse(status="ok")

            elif request.msg == "connect BLE":
                await connect_ble_device(request.MAC, request.BLE_Pin)
                return SendResponse(status="ok")

            elif request.msg == "resolve-ip":
                # IP resolution
                resolved = await resolve_ip(request.MAC)
                # Send response via SSE
                return SendResponse(status="ok")

            else:
                raise HTTPException(400, f"Unknown command: {request.msg}")

        else:
            # Regular message - send to mesh
            msg_id = await send_to_mesh(request.dst, request.msg)
            return SendResponse(status="ok", msg_id=msg_id)

    except Exception as e:
        raise HTTPException(500, str(e))
```

---

## CORS Configuration

For local development, configure CORS to allow requests from the web app:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or specific origins like ["http://localhost:5173"]
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

---

## Complete FastAPI Example

```python
"""
MeshCom SSE Proxy Server
Replaces WebSocket with SSE for unencrypted HTTP connections.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import json
from datetime import datetime

app = FastAPI(title="MeshCom SSE Proxy")

# CORS for web app access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Connected SSE clients
clients: List[asyncio.Queue] = []

# Request/Response models
class SendRequest(BaseModel):
    type: Optional[str] = None
    dst: str
    msg: str
    MAC: Optional[str] = None
    BLE_Pin: Optional[str] = None

class SendResponse(BaseModel):
    status: str
    message: Optional[str] = None
    msg_id: Optional[int] = None


async def broadcast_to_clients(message: dict):
    """Send a message to all connected SSE clients."""
    for queue in clients:
        try:
            await queue.put(message)
        except:
            pass  # Client disconnected


async def sse_generator(queue: asyncio.Queue):
    """Generate SSE events for a client."""
    try:
        while True:
            # Send keepalive every 30 seconds
            try:
                message = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(message)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass


@app.get("/events")
async def sse_endpoint():
    """SSE endpoint for streaming messages to clients."""
    queue = asyncio.Queue()
    clients.append(queue)

    async def cleanup_generator():
        try:
            async for event in sse_generator(queue):
                yield event
        finally:
            clients.remove(queue)

    return StreamingResponse(
        cleanup_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/api/send", response_model=SendResponse)
async def send_message(request: SendRequest):
    """Handle incoming commands and messages from the web client."""
    try:
        if request.type == "command":
            return await handle_command(request)
        else:
            return await handle_chat_message(request)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


async def handle_command(request: SendRequest) -> SendResponse:
    """Process system commands."""

    if request.msg == "send message dump":
        # TODO: Fetch recent messages from your data source
        messages = await get_recent_messages()
        response = {
            "type": "response",
            "msg": "message dump",
            "data": messages
        }
        await broadcast_to_clients(response)
        return SendResponse(status="ok")

    elif request.msg.startswith("--") or request.msg == "connect BLE":
        # BLE commands - forward to BLE handler
        if not request.MAC:
            raise HTTPException(400, "MAC address required for BLE commands")
        # TODO: Implement BLE command handling
        return SendResponse(status="ok")

    elif request.msg == "resolve-ip":
        # IP resolution
        # TODO: Implement IP resolution
        response = {
            "command": "resolve-ip",
            "msg": "192.168.1.50"  # Replace with actual resolution
        }
        await broadcast_to_clients(response)
        return SendResponse(status="ok")

    else:
        raise HTTPException(400, f"Unknown command: {request.msg}")


async def handle_chat_message(request: SendRequest) -> SendResponse:
    """Process regular chat messages."""
    # TODO: Send to mesh network
    msg_id = int(datetime.now().timestamp() * 1000)

    # Echo back to all clients (or wait for mesh confirmation)
    message = {
        "msg_id": msg_id,
        "src": "LOCAL",  # Replace with actual callsign
        "src_type": "www",
        "dst": request.dst,
        "timestamp": int(datetime.now().timestamp()),
        "msg": request.msg,
    }
    await broadcast_to_clients(message)

    return SendResponse(status="ok", msg_id=msg_id)


async def get_recent_messages() -> list:
    """Fetch recent messages from data source."""
    # TODO: Implement actual message retrieval
    return []


# Integration point for mesh network messages
async def on_mesh_message_received(message: dict):
    """Called when a message is received from the mesh network."""
    await broadcast_to_clients(message)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=2981)
```

---

## Migration Checklist

- [ ] Install dependencies: `pip install fastapi uvicorn sse-starlette`
- [ ] Implement `/events` SSE endpoint
- [ ] Implement `/api/send` POST endpoint
- [ ] Configure CORS for web app origin
- [ ] Migrate existing WebSocket message handlers to SSE broadcast
- [ ] Add keep-alive heartbeat (every 15-30s)
- [ ] Test with frontend SSE mode toggle
- [ ] Verify message format compatibility

---

## Testing

### Test SSE Connection
```bash
curl -N http://rpizero.local:2981/events
```

### Test Send Endpoint
```bash
# Request message dump
curl -X POST http://rpizero.local:2981/api/send \
  -H "Content-Type: application/json" \
  -d '{"type":"command","dst":"999","msg":"send message dump"}'

# Send chat message
curl -X POST http://rpizero.local:2981/api/send \
  -H "Content-Type: application/json" \
  -d '{"dst":"*","msg":"Test message from curl"}'
```

---

## Frontend Client Reference

The frontend SSE client (`src/composables/useSSEClient.ts`) expects:

1. **EventSource connection** to `http://{host}:{port}/events`
2. **Messages as `data:` events** with JSON payload
3. **POST requests** to `http://{host}:{port}/api/send` with JSON body
4. **HTTP 200 response** from POST with `{"status": "ok"}` for success
