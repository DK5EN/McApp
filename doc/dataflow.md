# McApp Data Flow

> **Migration Note (2026-02-15):** This document describes the legacy local BLE architecture. As of v1.01.1, local mode was removed. BLE access is now provided by the standalone BLE service (`ble_service/`) accessed via remote mode. See CLAUDE.md for current architecture.

## Standard Deployment (Pi with local Bluetooth)

```mermaid
flowchart TD
    WC["Web Clients<br/>(Vue.js SPA Frontend)"]
    SSE["SSE :2981<br/>(GET /events + POST /api/send<br/>via lighttpd reverse proxy)"]

    WC --> SSE
    SSE --> MR

    subgraph MR["MESSAGE ROUTER (Central Hub - C2-mc-ws.py)"]
        UDP["UDP Handler<br/>Port :1799"]
        BLE["BLE Client (local mode)<br/>D-Bus/BlueZ"]
        SSEH["SSE Handler (FastAPI)<br/>Port :2981"]
        MSH["MessageStorageHandler<br/>(In-memory deque + JSON/SQLite persistence)"]
    end

    subgraph MCN["MeshCom Node (192.168.68.xxx)"]
        LR1["LoRa Mesh Radio<br/>APRS Decoder<br/>Message Router"]
    end

    subgraph ESP["ESP32 LoRa Node (MC-xxxxxx)"]
        LR2["LoRa Mesh Radio<br/>APRS Generator<br/>GPS Module"]
    end

    UDP -- "UDP:1799" --> MCN
    BLE -- "Bluetooth GATT<br/>(BLE characteristics)" --> ESP
    MCN <-. "433MHz LoRa Mesh<br/>(Ham Radio Frequencies)" .-> ESP
```

## Distributed Deployment (Remote BLE Service)

McApp runs on a server without Bluetooth hardware.
A separate BLE service on a Pi exposes BLE via HTTP/SSE.

```mermaid
flowchart TD
    WC2["Web Clients<br/>(Vue.js SPA Frontend)"]

    WC2 -- "SSE :2981<br/>(via lighttpd)" --> Brain

    subgraph Brain["McApp Brain (Mac, OrbStack, or any server)"]
        UDP2["UDP Handler<br/>:1799"]
        BLE2["BLE Client<br/>(remote mode)"]
        SSEH2["SSE Handler (FastAPI)<br/>:2981"]
        MSH2["MessageStorageHandler<br/>(In-memory deque + JSON/SQLite)"]
    end

    subgraph BLES["BLE Service (Raspberry Pi)"]
        FA["FastAPI (REST + SSE) :8081<br/>D-Bus/BlueZ interface"]
        EP["POST /api/ble/connect<br/>POST /api/ble/send<br/>GET /api/ble/notifications SSE<br/>GET /api/ble/status"]
    end

    subgraph MCN2["MeshCom Node (192.168.68.xxx)"]
        LR3["LoRa Mesh Radio"]
    end

    subgraph ESP2["ESP32 LoRa Node (MC-xxxxxx)"]
        LR4["LoRa Mesh Radio<br/>APRS Generator<br/>GPS Module"]
    end

    UDP2 -- "UDP:1799" --> MCN2
    BLE2 -- "HTTP/SSE :8081" --> BLES
    BLES -- "Bluetooth GATT" --> ESP2
    MCN2 <-. "433MHz LoRa Mesh<br/>(Ham Radio Frequencies)" .-> ESP2
```

## BLE Mode Selection

| Mode | BLE Client | Description |
|------|------------|-------------|
| `local` | `ble_client_local.py` | Direct D-Bus/BlueZ on the same Pi |
| `remote` | `ble_client_remote.py` | HTTP/SSE to BLE service on another Pi |
| `disabled` | `ble_client_disabled.py` | No-op stub (for testing without BLE) |

Configured via `BLE_MODE` in config or `MCAPP_BLE_MODE` environment variable.
