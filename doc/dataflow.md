# McApp Data Flow

## Standard Deployment (Pi with Bluetooth)

All components run on the same Raspberry Pi. McApp uses remote mode to communicate with the BLE service via HTTP/SSE on localhost.

```mermaid
flowchart TD
    WC["Web Clients<br/>(Vue.js SPA Frontend)"]

    WC -- "HTTP :80" --> LH

    subgraph Pi["Raspberry Pi Zero 2W"]
        LH["lighttpd :80<br/>(static files + proxy)"]
        FA["FastAPI :2981<br/>(SSE + REST API)"]
        BLES["BLE Service :8081<br/>(D-Bus/BlueZ interface)"]

        subgraph MR["MESSAGE ROUTER (src/mcapp/main.py)"]
            UDP["UDP Handler<br/>:1799"]
            BLE["BLE Client (remote mode)"]
            MSH["MessageStorageHandler<br/>(SQLite or in-memory deque)"]
        end

        LH -- "/webapp/ → static files" --> LH
        LH -- "/events, /api/ → proxy" --> FA
        FA --> MR
        BLE -- "HTTP/SSE :8081" --> BLES
    end

    subgraph MCN["MeshCom Node (192.168.68.xxx)"]
        LR1["LoRa Mesh Radio<br/>APRS Decoder"]
    end

    subgraph ESP["ESP32 LoRa Node (MC-xxxxxx)"]
        LR2["LoRa Mesh Radio<br/>APRS Generator<br/>GPS Module"]
    end

    UDP -- "UDP:1799" --> MCN
    BLES -- "Bluetooth GATT" --> ESP
    MCN <-. "433MHz LoRa Mesh" .-> ESP
```

## Distributed Deployment (Remote BLE Service)

McApp runs on a server without Bluetooth hardware.
A separate BLE service on a Pi exposes BLE via HTTP/SSE.

```mermaid
flowchart TD
    WC2["Web Clients<br/>(Vue.js SPA Frontend)"]

    WC2 -- "SSE :2981<br/>(via lighttpd)" --> Brain

    subgraph Brain["McApp Brain (Mac, OrbStack, or any server - src/mcapp/main.py)"]
        UDP2["UDP Handler<br/>:1799"]
        BLE2["BLE Client<br/>(remote mode)"]
        SSEH2["SSE Handler (FastAPI)<br/>:2981"]
        MSH2["MessageStorageHandler<br/>(SQLite or in-memory deque)"]
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
| `remote` | `ble_client_remote.py` | HTTP/SSE to BLE service (default for production) |
| `disabled` | `ble_client_disabled.py` | No-op stub (for testing without BLE hardware) |

**Note:** Local mode (`ble_client_local.py`) was removed in v1.01.1. For local BLE hardware access, deploy the standalone BLE service (`ble_service/`) and use `remote` mode pointing to `http://localhost:8081`.

Configured via `BLE_MODE` in config or `MCAPP_BLE_MODE` environment variable.
