# MeshCom Telemetry Packets

## Current Behavior

Telemetry packets (`type: "tele"`) are received via UDP, filtered (all-zero readings discarded), deduplicated (60-second window per callsign), and stored in the dedicated `telemetry` table. Sensor values are also propagated to `station_positions` for quick lookups.

## Packet Format

Since firmware v4.35k.02.19, telemetry packets come in two variants:

### Node Telemetry (own node)

```json
{
  "src_type": "node",
  "type": "tele",
  "src": "DK5EN-99",
  "temp1": 0, "temp2": 0, "hum": 0,
  "qfe": 0, "qnh": 0, "gas": 0, "co2": 0
}
```

### LoRa Telemetry (remote nodes, new in v4.35k.02.19)

```json
{
  "src_type": "lora",
  "type": "tele",
  "src": "DL2JA-2,DL2UD-01",
  "batt": 70,
  "temp1": 16.5, "temp2": 0, "hum": 33.6,
  "qfe": 412, "qnh": 0, "gas": 81.8, "co2": 0
}
```

### Fields

| Field | Type | Description | Availability |
|-------|------|-------------|-------------|
| `src_type` | string | `"node"` (own) or `"lora"` (remote) | Always |
| `type` | string | Always `"tele"` | Always |
| `src` | string | Callsign + relay path (e.g. `"DL2JA-1,DB0ED-99"`) | Always (new FW); missing in old FW |
| `batt` | int | Battery level (%) | LoRa only; stored since schema v14 |
| `temp1` | float | Temperature sensor 1 (C) | Optional |
| `temp2` | float | Temperature sensor 2 (C) | Optional |
| `hum` | float | Relative humidity (%) | Optional |
| `qfe` | float | Station pressure (hPa) | Optional |
| `qnh` | float | Sea-level pressure (hPa) | Optional; used to calculate QFE if QFE missing |
| `gas` | int | Gas sensor reading | Optional |
| `co2` | int | CO2 concentration (ppm) | Optional |

## Processing Pipeline

1. **UDP handler** (`udp_handler.py:163-212`): Receives telemetry, adds timestamp, normalizes altitude, generates pseudo-callsign from IP if `src` is missing (legacy firmware fallback). Logs at INFO level.
2. **Storage** (`sqlite_storage.py:965-969`): Early exit routes `type="tele"` to `store_telemetry()`.
3. **`store_telemetry()`** (`sqlite_storage.py:1149-1228`):
   - Extracts callsign from relay path (`src.split(",")[0]`)
   - Filters all-zero readings (no sensors attached)
   - Validates QFE: values < 850 hPa are discarded (firmware mapping error in UDP LoRa telemetry)
   - If QFE missing but QNH + altitude available, calculates QFE via barometric formula
   - Deduplicates within 60-second window, keeping the record with better QFE
   - Looks up altitude from `station_positions` if missing
   - Inserts into `telemetry` table
   - Upserts `station_positions` with latest sensor values (NULLIF protects against zero-overwrites)

## SQLite Table

```sql
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    temp1 REAL,
    temp2 REAL,
    hum REAL,
    qfe REAL,
    qnh REAL,
    gas INTEGER,
    co2 INTEGER,
    alt REAL,
    batt INTEGER
);
```

## Dual-Path Telemetry

Some stations send telemetry via two paths simultaneously:

1. **BLE/APRS position beacons** — parsed in `ble_protocol.py`, contain normalized QFE
2. **UDP LoRa telemetry** (new since FW v4.35k.02.19) — raw sensor values

The dedup logic handles most duplicates. QFE validation (< 850 hPa → None) ensures that raw firmware values (~420 hPa) from the UDP path don't conflict with normalized BLE values (~963 hPa), which also improves dedup accuracy.

## Known Limitations

- `qnh` intentionally not stored (node-reported QNH is unreliable; frontend calculates from QFE + altitude). However, if QFE is missing/invalid and QNH + altitude are available, QFE is calculated via barometric formula before discarding QNH.
- Relay path information is lost during callsign normalization (no `via` field in telemetry table)
- Some stations send only zeros (no sensors) — correctly filtered by all-zero check
