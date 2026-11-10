# RB-008 Standalone BLE service

| Field | Value |
|---|---|
| ID | RB-008 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 5-25, 161-193; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` (BLE service on the same Pi), and any Linux or macOS host with a BLE adapter |
| Applies to (variants) | `debian-13-trixie`; macOS backend per `plans/2026-10-31_1230-ble-service-macos-backend-design.md` |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("BLE Testing Modes", "BLE Service Deployment"); `ble_service/README.md` |

## 1. Purpose and scope

Running the BLE service on its own (remote mode), for testing against a live node, and for the systemd unit on the Pi. For the API, see `ble_service/README.md`.

| Scenario                      | BLE Mode   | Setup                                     |
| ----------------------------- | ---------- | ----------------------------------------- |
| Non-BLE features              | `disabled` | `export MCAPP_BLE_MODE=disabled`          |
| Production / Testing with BLE | `remote`   | Deploy BLE service on Pi, point URL to it |

Remote BLE testing:

```bash
# On Pi: Start BLE service (from the repo root; the unit file runs the same app)
uv run uvicorn ble_service.src.main:app --host 127.0.0.1 --port 8081
# A non-loopback bind is refused unless BLE_SERVICE_API_KEY is set (ble_service/src/main.py)

# On Mac/OrbStack/other: Connect to remote BLE
export MCAPP_BLE_MODE=remote
export MCAPP_BLE_URL=http://pi.local:8081
export MCAPP_BLE_API_KEY=your-secret-key
uv run mcapp
```


For distributed setups where McApp runs on a different machine than the Bluetooth hardware:

```bash
# On Pi with Bluetooth hardware
cd ~/mcapp/ble_service
uv sync

# Configure API key
export BLE_SERVICE_API_KEY=your-secret-key

# Run directly (non-loopback bind: the key is required)
uv run uvicorn ble_service.src.main:app --host 0.0.0.0 --port 8081

# Or install as systemd service
sudo cp mcapp-ble.service /etc/systemd/system/
sudo systemctl edit mcapp-ble  # Add: Environment=BLE_SERVICE_API_KEY=...
sudo systemctl enable --now mcapp-ble
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

