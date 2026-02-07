# McApp BLE Service

Standalone BLE service that exposes MeshCom BLE device functionality via HTTP REST API and Server-Sent Events (SSE).

## Purpose

This service enables the McApp "brain" to run on hardware without Bluetooth support (e.g., Mac, cloud server, OrbStack VM) while the BLE service runs on a Raspberry Pi with Bluetooth hardware.

## Quick Start

### On Raspberry Pi (with Bluetooth)

```bash
# Install dependencies
cd ~/mcapp/ble_service
pip install -e .

# Set environment variables
export BLE_SERVICE_API_KEY=your-secret-key
export BLE_SERVICE_PORT=8081

# Run service
uvicorn src.main:app --host 0.0.0.0 --port 8081
```

### As systemd service

```bash
# Copy service file
sudo cp mcapp-ble.service /etc/systemd/system/

# Edit API key
sudo systemctl edit mcapp-ble
# Add: Environment=BLE_SERVICE_API_KEY=your-secret-key

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable mcapp-ble
sudo systemctl start mcapp-ble
```

## API Endpoints

### Status & Discovery

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/ble/status` | Get connection status |
| GET | `/api/ble/devices` | Scan for BLE devices |
| GET | `/health` | Health check |

### Connection Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/ble/connect` | Connect to device |
| POST | `/api/ble/disconnect` | Disconnect |
| POST | `/api/ble/pair` | Pair with device |
| POST | `/api/ble/unpair` | Remove pairing |

### Communication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/ble/send` | Send data to device |
| POST | `/api/ble/settime` | Set device time |
| GET | `/api/ble/notifications` | SSE notification stream |

## Authentication

All endpoints require the `X-API-Key` header:

```bash
curl -H "X-API-Key: your-secret-key" http://pi.local:8081/api/ble/status
```

## Examples

### Scan for devices

```bash
curl -H "X-API-Key: secret" \
  "http://pi.local:8081/api/ble/devices?timeout=10&prefix=MC-"
```

### Connect to device

```bash
curl -X POST -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d '{"device_address": "AA:BB:CC:DD:EE:FF"}' \
  http://pi.local:8081/api/ble/connect
```

### Send message

```bash
curl -X POST -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello Mesh!", "group": "20"}' \
  http://pi.local:8081/api/ble/send
```

### Stream notifications (SSE)

```bash
curl -N -H "X-API-Key: secret" \
  http://pi.local:8081/api/ble/notifications
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BLE_SERVICE_PORT` | 8081 | HTTP server port |
| `BLE_SERVICE_API_KEY` | mcapp-ble-secret | API authentication key |
| `BLE_SERVICE_CORS_ORIGINS` | * | Allowed CORS origins |

## Notification Format (SSE)

```json
{
  "timestamp": 1706100000000,
  "raw_base64": "RHsiVFlQIjoiTUgiLCJDQUxMIjoiT0U1SFdOLTEyIn0=",
  "raw_hex": "447b22545950223a224d48222c2243414c4c223a224f45354857..",
  "format": "json",
  "parsed": {
    "TYP": "MH",
    "CALL": "OE5HWN-12"
  }
}
```

## Security Notes

- Always use a strong, unique API key in production
- Consider using HTTPS via a reverse proxy
- Limit CORS origins in production
- The service requires D-Bus access to BlueZ
