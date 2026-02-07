# MeshCom Telemetry Packets

## Current Behavior

Telemetry packets (`type: "tele"`) are accepted gracefully and logged at DEBUG level.
They are not stored or forwarded to WebSocket clients.

## Packet Format

Telemetry packets arrive via UDP from MeshCom nodes. They contain sensor data
but no `msg` field (unlike chat messages).

```json
{
  "src_type": "node",
  "type": "tele",
  "src": "OE1XXX-1",
  "temp1": 22.5,
  "temp2": 21.8,
  "hum": 45.2,
  "qfe": 1013.25,
  "qnh": 1015.0,
  "gas": 0,
  "co2": 420
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `src_type` | string | Always `"node"` |
| `type` | string | Always `"tele"` |
| `src` | string | Source callsign |
| `temp1` | float | Temperature sensor 1 (C) |
| `temp2` | float | Temperature sensor 2 (C) |
| `hum` | float | Relative humidity (%) |
| `qfe` | float | Station pressure (hPa) |
| `qnh` | float | Sea-level pressure (hPa) |
| `gas` | int | Gas sensor reading |
| `co2` | int | CO2 concentration (ppm) |

Not all fields are present in every packet. Only `src_type`, `type`, and `src` are guaranteed.

## Future: Storage and Display

### SQLite Table

```sql
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    temp1 REAL,
    temp2 REAL,
    hum REAL,
    qfe REAL,
    qnh REAL,
    gas INTEGER,
    co2 INTEGER
);

CREATE INDEX idx_telemetry_src ON telemetry(src);
CREATE INDEX idx_telemetry_ts ON telemetry(timestamp DESC);
```

### Integration Points

1. **UDP handler** - Store telemetry in SQLite after accepting
2. **SSE/WebSocket** - Broadcast new telemetry to connected clients
3. **Command handler** - Add `!tele [CALLSIGN]` to query latest readings
4. **Webapp** - Display sensor charts per station
