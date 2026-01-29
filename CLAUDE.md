# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCProxy is a message proxy service for MeshCom (LoRa mesh network for ham radio operators). It bridges MeshCom nodes with web clients via WebSocket, supporting both UDP and Bluetooth Low Energy (BLE) connections. The system runs on Raspberry Pi and serves a Vue.js web application.

## Architecture

### Standard Deployment (Pi with Bluetooth)
```
WebSocket Clients (Vue.js SPA)
         │
         │ WSS:2981 (via Caddy proxy)
         ▼
┌─────────────────────────────────────────┐
│        MESSAGE ROUTER (C2-mc-ws.py)     │
│                                         │
│  ┌───────────┐ ┌───────────┐ ┌────────┐ │
│  │UDP Handler│ │BLE Client │ │WS Mgr  │ │
│  │ :1799     │ │(local mode)│ │ :2980  │ │
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

### Distributed Deployment (Remote BLE Service)
```
┌─────────────────────────────────────────────────────────┐
│  MCProxy Brain (Mac, OrbStack, or any server)           │
│  ├── UDP Handler ──────► MeshCom Node (UDP:1799)        │
│  ├── WebSocket Manager ► Web Clients (WS:2980)          │
│  ├── BLE Client ─────► HTTP/SSE ──┐                     │
│  └── Command Handler              │                     │
└───────────────────────────────────│─────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────┐
│  BLE Service (Raspberry Pi with Bluetooth hardware)     │
│  ├── FastAPI server (REST + SSE)  :8081                 │
│  ├── D-Bus/BlueZ interface                              │
│  └── Endpoints:                                         │
│      POST /api/ble/connect     - Connect to device      │
│      POST /api/ble/disconnect  - Disconnect             │
│      POST /api/ble/send        - Send message           │
│      GET  /api/ble/status      - Connection status      │
│      GET  /api/ble/notifications - SSE stream           │
│      GET  /api/ble/devices     - Scan for devices       │
└─────────────────────────────────────────────────────────┘
```

### Core Components

- **C2-mc-ws.py**: Main entry point. Initializes MessageRouter and all protocol handlers
- **message_storage.py**: In-memory message store with JSON persistence, pruning, and parallel mheard statistics processing
- **udp_handler.py**: UDP listener/sender for MeshCom node communication (port 1799)
- **websocket_handler.py**: WebSocket server for web clients (port 2980)
- **command_handler.py**: Chat command processor (`!wx`, `!mheard`, `!stats`, `!dice`, etc.)
- **config_loader.py**: Dataclass-based configuration with environment variable overrides

### BLE Abstraction Layer

The BLE subsystem supports three modes via a unified client interface:

| Mode | File | Description |
|------|------|-------------|
| `local` | `ble_client_local.py` | Direct D-Bus/BlueZ (wraps `ble_handler.py`) |
| `remote` | `ble_client_remote.py` | HTTP/SSE client to remote BLE service |
| `disabled` | `ble_client_disabled.py` | No-op stub for testing |

- **ble_client.py**: Abstract interface + `create_ble_client()` factory function
- **ble_handler.py**: Legacy BlueZ D-Bus implementation (used by local mode)

### BLE Service (Standalone)

Located in `ble_service/` - a FastAPI service that exposes BLE hardware via HTTP:

- **ble_service/src/main.py**: FastAPI REST API + SSE endpoints
- **ble_service/src/ble_adapter.py**: Clean D-Bus/BlueZ wrapper class
- **ble_service/mcproxy-ble.service**: Systemd service file for Pi

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

## Repository Structure

This project consists of **two separate Git repositories**:

| Repo | Path (local) | Content |
|------|-------------|---------|
| **MCProxy** | `/Users/martinwerner/WebDev/MCProxy` | Python backend (this repo) |
| **webapp** | `/Users/martinwerner/WebDev/webapp` | Vue 3 frontend (separate repo) |

Each repo has its own Git history, branches, and CLAUDE.md.

## Dev Backend Server

The development backend runs on a Raspberry Pi accessible via:

```bash
ssh mcapp.local    # No username needed
```

The MCProxy code is deployed to `~/mcproxy-test/` on that machine.

## Package Management

- **Python**: `uv` only — NEVER use `pip` or `venv`
  - `uv sync` to install dependencies
  - `uv run mcproxy` to run
- **Frontend (webapp repo)**: `npm`

## Code Quality

- **Python**: `uvx ruff check` is mandatory — zero tolerance for errors and warnings
- **Frontend**: `npx eslint` is mandatory — zero tolerance for errors and warnings
- All issues must be resolved before committing

## Development Commands

```bash
# Run in development mode (enables verbose logging)
./dev.sh

# Run with uv
export MCADVCHAT_ENV=dev
uv run mcproxy

# View service logs
sudo journalctl -u mcproxy.service -f

# Restart production service
sudo systemctl restart mcproxy.service
```

## OrbStack Development (macOS)

OrbStack provides fast ARM64 Linux VMs on Apple Silicon, enabling MCProxy development without physical Pi hardware. See `orb-testing.md` for full documentation.

### Quick Start

```bash
# Create Debian VM (matches Pi target)
orb create debian:trixie mcproxy-dev
orb shell mcproxy-dev

# Run with BLE disabled (no Bluetooth in VM)
export MCPROXY_BLE_MODE=disabled
python C2-mc-ws.py
```

### Testing Scenarios

| Scenario | BLE Mode | Setup |
|----------|----------|-------|
| Non-BLE features | `disabled` | Just set env var |
| Real BLE via Pi | `remote` | Run BLE service on Pi, point URL to it |
| Production on Pi | `local` | Default, uses D-Bus/BlueZ directly |

### Remote BLE Testing

```bash
# On Pi: Start BLE service
cd ble_service
uvicorn src.main:app --host 0.0.0.0 --port 8081

# On Mac/OrbStack: Connect to remote BLE
export MCPROXY_BLE_MODE=remote
export MCPROXY_BLE_URL=http://pi.local:8081
export MCPROXY_BLE_API_KEY=your-secret-key
python C2-mc-ws.py
```

### Snapshots

```bash
orb snapshot create mcproxy-dev pre-test   # Save state
orb snapshot restore mcproxy-dev pre-test  # Restore
orb snapshot list mcproxy-dev              # List snapshots
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

### BLE Configuration

```json
{
  "BLE_MODE": "local",           // "local" | "remote" | "disabled"
  "BLE_REMOTE_URL": "",          // URL for remote BLE service (remote mode only)
  "BLE_API_KEY": "",             // API key for remote service authentication
  "BLE_DEVICE_NAME": "",         // Auto-connect device name (e.g., "MC-XXXXXX")
  "BLE_DEVICE_ADDRESS": ""       // Auto-connect device MAC address
}
```

**Environment variable overrides** (useful for testing):
- `MCPROXY_BLE_MODE` - Override BLE mode without editing config
- `MCPROXY_BLE_URL` - Override remote BLE service URL
- `MCPROXY_BLE_API_KEY` - Override API key

## Dependencies

Python packages (installed via uv in `~/mcproxy-venv`):
- `websockets>=14.0`: WebSocket server
- `dbus-next>=0.2.3`: BlueZ D-Bus interface for BLE (local mode)
- `timezonefinder>=6.5.0`: Timezone detection for node time sync
- `httpx>=0.28.0`: Async HTTP client for weather API
- `aiohttp>=3.9.0`: Async HTTP client for remote BLE
- `aiohttp-sse-client>=0.2.1`: SSE client for remote BLE notifications
- `fastapi>=0.115.0`: REST API framework (SSE transport, BLE service)
- `uvicorn>=0.34.0`: ASGI server for FastAPI
- `sse-starlette>=2.0.0`: SSE support for FastAPI
- `pydantic>=2.0`: Data validation

System packages (installed via apt):
- `caddy`: HTTPS reverse proxy with auto-TLS
- `lighttpd`: Static file server for Vue.js SPA
- `bluez`: Bluetooth stack for BLE (local mode only)
- `jq`: JSON processing in shell scripts

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

### New Bootstrap System (Recommended)

```bash
# Single command for install, update, or repair
curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/bootstrap/mcproxy.sh | sudo bash
```

The bootstrap script:
1. Auto-detects state (fresh/migrate/upgrade)
2. Prompts for configuration on first install
3. Creates Python venv in `~/mcproxy-venv` using uv
4. Configures SD card protection (tmpfs, volatile journal)
5. Sets up firewall (nftables on Trixie, iptables on Bookworm)
6. Enables and starts systemd service

See `bootstrap/README.md` for full documentation.

### Pi SD-Card Hardening (`pi-harden.sh`)

Idempotent script for Raspberry Pi (Debian Trixie) that optimizes for SD card longevity, security, and fast SSH login. Safe to run repeatedly — each section checks current state before modifying.

```bash
scp pi-harden.sh mcapp.local:~/
ssh mcapp.local "sudo bash ~/pi-harden.sh"
```

**What it does:**

| Step | Action | Benefit |
|------|--------|---------|
| Disable services | Stops ModemManager, cloud-init, udisks2, e2scrub, serial-getty | Fewer writes, faster boot |
| Unattended upgrades | Security-only auto-updates, no auto-reboot | Patched without intervention |
| tmpfs /var/log | 30M RAM-backed log directory | Eliminates log writes to SD |
| Journald volatile | `Storage=volatile`, 20M max | No persistent journal on disk |
| Fast SSH login | Disables MOTD scripts, pam_motd, DNS lookup | Sub-second login |
| SSHD hardening | ed25519-only, modern ciphers, no root login, MaxAuthTries 3 | Reduced attack surface |
| Logrotate | Reduces retention to 2 rotations | Less disk churn |

**Idempotency:** Uses marker comments in `/etc/fstab`, `grep -q` checks, and state detection so re-runs skip already-applied changes. A reboot is recommended after first run.

### Legacy Installation (Deprecated)

The old scripts remain for reference but are deprecated:
- `install_caddy.sh` - Caddy + lighttpd setup
- `mc-install.sh` - Webapp deployment
- `install_mcproxy.sh` - Python venv and service setup

## Project Directory Structure

```
MCProxy/
├── C2-mc-ws.py              # Main entry point
├── config_loader.py         # Configuration management
├── message_storage.py       # Message persistence
├── udp_handler.py           # UDP protocol handler
├── websocket_handler.py     # WebSocket server
├── command_handler.py       # Chat command processor
├── ble_handler.py           # Legacy D-Bus/BlueZ implementation
├── ble_client.py            # BLE abstraction interface
├── ble_client_local.py      # Local BLE (wraps ble_handler)
├── ble_client_remote.py     # Remote BLE (HTTP/SSE)
├── ble_client_disabled.py   # No-op stub
├── sse_handler.py           # SSE transport (optional)
├── sqlite_storage.py        # SQLite backend (optional)
├── pi-harden.sh             # SD-card longevity & security hardening
├── config.sample.json       # Configuration template
├── requirements.txt         # Python dependencies
├── orb-testing.md           # OrbStack development guide
│
├── ble_service/             # Standalone BLE service
│   ├── src/
│   │   ├── __init__.py
│   │   ├── main.py          # FastAPI REST + SSE
│   │   └── ble_adapter.py   # Clean D-Bus wrapper
│   ├── pyproject.toml
│   ├── mcproxy-ble.service  # Systemd service
│   └── README.md            # API documentation
│
└── bootstrap/               # Installation scripts
    ├── mcproxy.sh           # Main entry point
    ├── lib/                 # Script modules
    └── templates/           # Config templates
```

## Bootstrap Directory Structure

```
bootstrap/
├── mcproxy.sh           # Main entry point (run with sudo)
├── lib/
│   ├── detect.sh        # State detection (fresh/migrate/upgrade)
│   ├── config.sh        # Interactive configuration prompts
│   ├── system.sh        # tmpfs, firewall, journald, hardening
│   ├── packages.sh      # apt + uv package management
│   ├── deploy.sh        # Webapp + Python script deployment
│   └── health.sh        # Health checks and diagnostics
├── templates/
│   ├── config.json.tmpl # Configuration template
│   ├── mcproxy.service  # systemd unit file
│   ├── Caddyfile.tmpl   # Caddy reverse proxy config
│   ├── nftables.conf    # Firewall rules
│   └── journald.conf    # Volatile journal config
├── requirements.txt     # Python dependencies (minimum versions)
└── README.md            # Installation documentation
```

### Bootstrap CLI Options

```bash
sudo ./mcproxy.sh --check       # Dry-run, show what would change
sudo ./mcproxy.sh --force       # Force reinstall everything
sudo ./mcproxy.sh --fix         # Repair broken installation
sudo ./mcproxy.sh --reconfigure # Re-prompt for config values
sudo ./mcproxy.sh --quiet       # Minimal output (for cron)
```

### BLE Service Deployment (Optional)

For distributed setups where MCProxy runs on a different machine than the Bluetooth hardware:

```bash
# On Pi with Bluetooth hardware
cd ~/MCProxy/ble_service
pip install -e .

# Configure API key
export BLE_SERVICE_API_KEY=your-secret-key

# Run directly
uvicorn src.main:app --host 0.0.0.0 --port 8081

# Or install as systemd service
sudo cp mcproxy-ble.service /etc/systemd/system/
sudo systemctl edit mcproxy-ble  # Add: Environment=BLE_SERVICE_API_KEY=...
sudo systemctl enable --now mcproxy-ble
```

The BLE service exposes:
- `GET /api/ble/status` - Connection status
- `GET /api/ble/devices` - Scan for devices
- `POST /api/ble/connect` - Connect to device
- `POST /api/ble/disconnect` - Disconnect
- `POST /api/ble/send` - Send message/command
- `GET /api/ble/notifications` - SSE notification stream
- `GET /health` - Health check

See `ble_service/README.md` for full API documentation.

## Troubleshooting

### Bluetooth Blocked by rfkill

**Symptom:** `bluetoothctl power on` fails with "Failed to set power on: org.bluez.Error.Failed"

**Diagnosis:**
```bash
rfkill list bluetooth
# Shows: Soft blocked: yes
```

**Root cause:** Some Raspberry Pi images ship with `/etc/modprobe.d/rfkill_default.conf` containing `options rfkill default_state=0`, which blocks all radios (Bluetooth, WiFi) at boot.

**Solution:** The bootstrap script automatically installs `unblock-bluetooth.service` which runs `rfkill unblock bluetooth` after the Bluetooth service starts.

**Manual fix (if not using bootstrap):**
```bash
# Create systemd service
cat <<'EOF' | sudo tee /etc/systemd/system/unblock-bluetooth.service
[Unit]
Description=Unblock Bluetooth for MCProxy BLE
After=bluetooth.service
Before=mcproxy.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/rfkill unblock bluetooth
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now unblock-bluetooth

# Verify
rfkill list bluetooth  # Should show: Soft blocked: no
```
