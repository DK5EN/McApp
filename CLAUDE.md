# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCProxy is a message proxy service for MeshCom (LoRa mesh network for ham radio operators). It bridges MeshCom nodes with web clients via WebSocket, supporting both UDP and Bluetooth Low Energy (BLE) connections. The system runs on Raspberry Pi and serves a Vue.js web application.

## Architecture

```
WebSocket Clients (Vue.js SPA)
         │
         │ WSS:2981 (via Caddy proxy)
         ▼
┌─────────────────────────────────────────┐
│        MESSAGE ROUTER (C2-mc-ws.py)     │
│                                         │
│  ┌───────────┐ ┌───────────┐ ┌────────┐ │
│  │UDP Handler│ │BLE Handler│ │WS Mgr  │ │
│  │ :1799     │ │ D-Bus/BlueZ│ │ :2980  │ │
│  └───────────┘ └───────────┘ └────────┘ │
│                                         │
│  ┌─────────────────────────────────────┐│
│  │ MessageStorageHandler               ││
│  │ (In-memory deque + JSON persistence)││
│  └─────────────────────────────────────┘│
└─────────────────────────────────────────┘
         │                    │
         ▼ UDP:1799           ▼ Bluetooth GATT
    MeshCom Node          ESP32 LoRa Node
    (192.168.68.xxx)      (MC-xxxxxx)
```

### Core Components

- **C2-mc-ws.py**: Main entry point. Initializes MessageRouter and all protocol handlers
- **message_storage.py**: In-memory message store with JSON persistence, pruning, and parallel mheard statistics processing
- **udp_handler.py**: UDP listener/sender for MeshCom node communication (port 1799)
- **websocket_handler.py**: WebSocket server for web clients (port 2980)
- **ble_handler.py**: BlueZ D-Bus based BLE handler for direct ESP32 connection via GATT
- **command_handler.py**: Chat command processor (`!wx`, `!mheard`, `!stats`, `!dice`, etc.)

### Message Flow

1. Messages arrive via UDP (from MeshCom node) or BLE (from ESP32)
2. MessageRouter publishes to subscribers based on message type
3. Messages are stored, broadcast to WebSocket clients, and processed for commands
4. Outbound messages from clients go through suppression logic before mesh transmission

### Key Classes

- `MessageRouter`: Central pub/sub hub connecting all protocols
- `MessageValidator`: Handles message normalization and outbound suppression logic
- `MessageStorageHandler`: Deque-based storage with size limits and parallel processing
- `BLEClient`: D-Bus based BLE connection with keep-alive and auto-reconnect
- `CommandHandler`: Extensible command system with throttling and abuse protection

### Module Integration

The `MessageRouter` (defined in `C2-mc-ws.py:56-539`) is the central pub/sub hub that connects all protocol handlers:

```
┌─────────────────────────────────────────────────────────────────┐
│                    C2-mc-ws.py (main entry point)               │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    MessageRouter                          │  │
│  │                 (Central Pub/Sub Hub)                     │  │
│  │                                                           │  │
│  │  _subscribers: {message_type → [handler_functions]}       │  │
│  │  _protocols:   {protocol_name → handler_instance}         │  │
│  └───────────────────────────────────────────────────────────┘  │
│         │              │              │              │          │
│         ▼              ▼              ▼              ▼          │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌────────────┐   │
│  │UDPHandler │  │WSManager  │  │BLE funcs  │  │CommandHdlr │   │
│  │(udp_      │  │(websocket_│  │(ble_      │  │(command_   │   │
│  │handler.py)│  │handler.py)│  │handler.py)│  │handler.py) │   │
│  └───────────┘  └───────────┘  └───────────┘  └────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**Initialization Flow** (`main()` at line 904):
```python
storage_handler = MessageStorageHandler(message_store, MAX_STORE_SIZE_MB)
message_router = MessageRouter(storage_handler)

command_handler = create_command_handler(...)
message_router.register_protocol('commands', command_handler)

udp_handler = UDPHandler(..., message_router=message_router)
message_router.register_protocol('udp', udp_handler)

websocket_manager = WebSocketManager(WS_HOST, WS_PORT, message_router)
message_router.register_protocol('websocket', websocket_manager)
```

**Message Types & Subscriptions:**

| Message Type | Subscribers | Purpose |
|--------------|-------------|---------|
| `mesh_message` | WSManager, StorageHandler | Messages from LoRa mesh |
| `ble_notification` | WSManager, StorageHandler, CommandHandler | BLE device notifications |
| `ble_status` | WSManager | BLE connection status updates |
| `websocket_message` | WSManager | Messages to broadcast to clients |
| `ble_message` | BLE handler | Outbound messages via BLE |
| `udp_message` | UDP handler | Outbound messages via UDP |

**Incoming Message Flow (BLE → WebSocket clients):**
1. BLE device sends GATT notification
2. `BLEClient._on_props_changed()` receives raw bytes
3. `notification_handler()` parses JSON or binary format
4. `message_router.publish('ble', 'ble_notification', data)`
5. `WebSocketManager._broadcast_handler()` receives via subscription
6. Broadcasts JSON to all connected WebSocket clients

**Outgoing Message Flow (WebSocket client → Mesh):**
1. Client sends message via WebSocket
2. `WebSocketManager._process_client_message()` routes by type
3. `message_router.publish('websocket', 'udp_message', data)`
4. `MessageRouter._udp_message_handler()` applies suppression logic
5. `UDPHandler.send_message()` sends JSON to MeshCom node

## Development Commands

```bash
# Run in development mode (enables verbose logging)
./dev.sh

# Run directly with venv
source ~/venv/bin/activate
export MCADVCHAT_ENV=dev
python C2-mc-ws.py

# View service logs
sudo journalctl -u mcproxy.service -f

# Restart production service
sudo systemctl restart mcproxy.service
```

## Configuration

Configuration lives in `/etc/mcadvchat/config.json`:
- `UDP_PORT_list/send`: Port 1799 for MeshCom node
- `WS_HOST/PORT`: WebSocket server (127.0.0.1:2980, proxied via Caddy)
- `CALL_SIGN`: Node callsign for command handling
- `LAT/LONG/STAT_NAME`: Location for weather service
- `PRUNE_HOURS`: Message retention period (default 168h = 7 days)
- `MAX_STORAGE_SIZE_MB`: In-memory store limit

Dev config: `/etc/mcadvchat/config.dev.json` (auto-selected when `MCADVCHAT_ENV=dev`)

## Dependencies

Python packages (installed in venv via `install_mcproxy.sh`):
- `websockets`: WebSocket server
- `dbus_next`: BlueZ D-Bus interface for BLE
- `timezonefinder`: Timezone detection for node time sync
- `zstandard`: Compression (unused currently)
- `requests`: HTTP client for weather API

## Protocol Details

### UDP Message Format
JSON messages with fields: `src`, `dst`, `msg`, `type` (msg/pos/ack), `timestamp`, `rssi`, `snr`

### BLE Binary Messages
- Prefix `D{`: JSON config messages (TYP: MH, SA, G, W, SN, etc.)
- Prefix `@:` or `@!`: Binary mesh messages with header (payload_type, msg_id, hop_count)
- Prefix `@A`: ACK messages

### Chat Commands
All commands start with `!` and are processed by CommandHandler:
- `!wx` / `!weather`: Current weather
- `!mheard` / `!mh`: Recently heard stations
- `!stats`: Message statistics
- `!search`: Search messages by callsign
- `!pos`: Position data lookup
- `!dice`: Roll dice (Mäxchen rules)
- `!time`: Node time
- `!topic`: Group beacon management (admin)
- `!kb`: Kick-ban management (admin)

## Testing

Built-in test suites run at startup (when `has_console` is true):
- Suppression logic tests via `message_router.test_suppression_logic()`
- Command handler tests via `command_handler.run_all_tests()`

## Deployment

Target: Raspberry Pi Zero 2W running as systemd service (`mcproxy.service`)

Installation:
```bash
curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash
```

The install script:
1. Creates Python venv in `~/venv`
2. Installs dependencies
3. Creates config template in `/etc/mcadvchat/config.json`
4. Sets up systemd service
