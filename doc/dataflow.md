# McApp Data Flow

## Standard Deployment (Pi with local Bluetooth)

```
                    ┌─────────────────────────────┐
                    │     Web Clients              │
                    │   (Vue.js SPA Frontend)      │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────┴───────────────┐
                    │ SSE :2981                    │
                    │ (GET /events + POST /api/send│
                    │  via lighttpd reverse proxy) │
                    └─────────────┬───────────────┘
                                  │
    ┌─────────────────────────────▼─────────────────────────────┐
    │                MESSAGE ROUTER                             │
    │           (Central Hub - C2-mc-ws.py)                     │
    │                                                           │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐   │
    │  │ UDP Handler │  │ BLE Client  │  │ SSE Handler     │   │
    │  │             │  │ (local mode)│  │ (FastAPI)       │   │
    │  │ Port :1799  │  │ D-Bus/BlueZ │  │ Port :2981      │   │
    │  └─────────────┘  └─────────────┘  └─────────────────┘   │
    │                                                           │
    │  ┌─────────────────────────────────────────────────────┐  │
    │  │         MessageStorageHandler                       │  │
    │  │    (In-memory deque + JSON/SQLite persistence)      │  │
    │  └─────────────────────────────────────────────────────┘  │
    └─────────────┬───────────────┬─────────────────────────────┘
                  │               │
                  │ UDP:1799      │ Bluetooth GATT
                  │               │ (BLE characteristics)
                  ▼               ▼
    ┌─────────────────────┐   ┌─────────────────────┐
    │    MeshCom Node     │   │   ESP32 LoRa Node   │
    │  (192.168.68.xxx)   │   │    (MC-xxxxxx)      │
    │                     │   │                     │
    │ ┌─────────────────┐ │   │ ┌─────────────────┐ │
    │ │ LoRa Mesh Radio │ │   │ │ LoRa Mesh Radio │ │
    │ │ APRS Decoder    │ │   │ │ APRS Generator  │ │
    │ │ Message Router  │ │   │ │ GPS Module      │ │
    │ └─────────────────┘ │   │ └─────────────────┘ │
    └─────────────────────┘   └─────────────────────┘
                  │                       │
                  └───────────────────────┘
                     433MHz LoRa Mesh
                   (Ham Radio Frequencies)
```

## Distributed Deployment (Remote BLE Service)

McApp runs on a server without Bluetooth hardware.
A separate BLE service on a Pi exposes BLE via HTTP/SSE.

```
    ┌─────────────────────────────┐
    │     Web Clients              │
    │   (Vue.js SPA Frontend)      │
    └─────────────┬───────────────┘
                  │ SSE :2981
                  │ (via lighttpd)
                  ▼
    ┌─────────────────────────────────────────────────────┐
    │  McApp Brain (Mac, OrbStack, or any server)         │
    │                                                     │
    │  ┌───────────┐  ┌────────────┐  ┌───────────────┐   │
    │  │UDP Handler│  │ BLE Client │  │ SSE Handler   │   │
    │  │ :1799     │  │(remote mode)│  │ (FastAPI)     │   │
    │  └─────┬─────┘  └──────┬─────┘  │ :2981         │   │
    │        │               │        └───────────────┘   │
    │        │               │ HTTP/SSE                    │
    │  ┌─────────────────────────────────────────────┐     │
    │  │    MessageStorageHandler                    │     │
    │  │  (In-memory deque + JSON/SQLite)            │     │
    │  └─────────────────────────────────────────────┘     │
    └────────┬───────────────┼────────────────────────────┘
             │               │
             │ UDP:1799      │ HTTP :8081
             ▼               ▼
    ┌─────────────────┐   ┌───────────────────────────────────┐
    │  MeshCom Node   │   │  BLE Service (Raspberry Pi)       │
    │ (192.168.68.xxx)│   │                                   │
    │                 │   │  FastAPI (REST + SSE)  :8081      │
    │ ┌─────────────┐ │   │  D-Bus/BlueZ interface            │
    │ │ LoRa Mesh   │ │   │                                   │
    │ │ Radio       │ │   │  POST /api/ble/connect            │
    │ └─────────────┘ │   │  POST /api/ble/send               │
    └─────────────────┘   │  GET  /api/ble/notifications (SSE)│
             │            │  GET  /api/ble/status              │
             │            └──────────────┬────────────────────┘
             │                           │ Bluetooth GATT
             │                           ▼
             │            ┌─────────────────────┐
             │            │   ESP32 LoRa Node   │
             │            │    (MC-xxxxxx)      │
             │            │                     │
             │            │ ┌─────────────────┐ │
             │            │ │ LoRa Mesh Radio │ │
             │            │ │ APRS Generator  │ │
             │            │ │ GPS Module      │ │
             │            │ └─────────────────┘ │
             │            └─────────────────────┘
             │                       │
             └───────────────────────┘
                433MHz LoRa Mesh
              (Ham Radio Frequencies)
```

## BLE Mode Selection

| Mode | BLE Client | Description |
|------|------------|-------------|
| `local` | `ble_client_local.py` | Direct D-Bus/BlueZ on the same Pi |
| `remote` | `ble_client_remote.py` | HTTP/SSE to BLE service on another Pi |
| `disabled` | `ble_client_disabled.py` | No-op stub (for testing without BLE) |

Configured via `BLE_MODE` in config or `MCAPP_BLE_MODE` environment variable.
