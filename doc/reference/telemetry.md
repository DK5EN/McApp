# MeshCom Telemetry Packets

> **Verifiziert 2026-11-10** gegen den Code-Stand: Pipeline-Stellen, QFE-Regel, Dedup-Fenster und
> Tabellenschema geprüft und korrigiert (`src/mcapp/udp_handler.py`, `src/mcapp/storage/ingest.py`,
> `src/mcapp/storage/telemetry_reconcile.py`, `src/mcapp/storage/constants.py`,
> `src/mcapp/storage/migrations.py`). Firmware-Aussagen (Versionsnummern, Wire-Feldsemantik) sind im
> Repo nicht prüfbar und als solche markiert.

## Current Behavior

Telemetry packets (`type: "tele"`) are received via UDP (`udp_handler.py`, tele branch), routed to
`store_telemetry()` (`storage/ingest.py`, early exit in `store_message`) and stored in the dedicated
`telemetry` table. Zero values on the Extern-UDP path mean "no sensor fitted" and are not stored as
readings; a frame without any reading is dropped. A second observation of the same callsign within
±60 seconds is merged by the pure `telemetry_reconcile.reconcile()`. Sensor values are also upserted
into `station_positions` for quick lookups.

## Packet Format

Since firmware v4.35k.02.19 (firmware claim, not checkable in this repo), telemetry packets come in
two variants:

### Node Telemetry (own node)

```json
{
  "src_type": "node",
  "type": "tele",
  "src": "DK5EN-99",
  "temp1": 0,
  "temp2": 0,
  "hum": 0,
  "qfe": 0,
  "qnh": 0,
  "gas": 0,
  "co2": 0
}
```

### LoRa Telemetry (remote nodes, new in v4.35k.02.19)

```json
{
  "src_type": "lora",
  "type": "tele",
  "src": "DL2JA-2,DL2UD-01",
  "batt": 70,
  "temp1": 16.5,
  "temp2": 0,
  "hum": 33.6,
  "qfe": 412,
  "qnh": 0,
  "gas": 81.8,
  "co2": 0
}
```

The `qfe` value in the LoRa example is the wire value only. For `src_type: "lora"` it is the
BME680 barometric altitude in metres, not a pressure (see the `qfe` row below).

### Fields

| Field      | Type   | Description                                       | Availability                                                                                                                                                                                                                                  |
| ---------- | ------ | ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src_type` | string | `"node"` (own) or `"lora"` (remote)               | Always                                                                                                                                                                                                                                        |
| `type`     | string | Always `"tele"`                                   | Always                                                                                                                                                                                                                                        |
| `src`      | string | Callsign + relay path (e.g. `"DL2JA-1,DB0ED-99"`) | Always (new FW); missing in old FW. Stored callsign is the part before the first comma.                                                                                                                                                       |
| `batt`     | int    | Battery level (%)                                 | Stored since schema v14 (`telemetry.batt`)                                                                                                                                                                                                    |
| `temp1`    | float  | Temperature sensor 1 (C)                          | Optional                                                                                                                                                                                                                                      |
| `temp2`    | float  | Temperature sensor 2 (C)                          | Optional                                                                                                                                                                                                                                      |
| `hum`      | float  | Relative humidity (%)                             | Optional                                                                                                                                                                                                                                      |
| `qfe`      | float  | Station pressure (hPa), only for `src_type` `node` and BLE `/P=` | Optional. **`src_type: "lora"`: the wire value is the BME680 altitude (m) and never reaches the `qfe` column.** Stored `qfe` must lie in 300-1100 hPa (`_QFE_PLAUSIBLE_HPA_RANGE`), otherwise it is discarded. |
| `qnh`      | float  | Sea-level pressure (hPa)                          | Optional; stored when plausible (850-1100 hPa, `_QNH_PLAUSIBLE_HPA_RANGE`) and used to calculate QFE if QFE is missing                                                                                                                       |
| `gas`      | int    | Gas sensor reading                                | Optional                                                                                                                                                                                                                                      |
| `co2`      | int    | CO2 concentration (ppm)                           | Optional                                                                                                                                                                                                                                      |

Unknown sensor keys are kept in the `extras` JSON column (`ingest.py`, `_TELEMETRY_KNOWN_KEYS`).

## Processing Pipeline

1. **UDP handler** (`udp_handler.py`, `_process_received_message`, tele branch around line 605):
   stamps the timestamp, normalizes altitude to metres (`_normalize_altitude_to_meters`), and
   generates a pseudo-callsign `NODE-<last octet>` from the sender IP if `src` is missing (legacy
   firmware fallback; IPv6 or malformed addresses are skipped). Logs at DEBUG level.
2. **Storage entry** (`storage/ingest.py`, `store_message`, around line 1968): the early exit routes
   `type="tele"` to `store_telemetry()` and returns before the message INSERT.
3. **`store_telemetry()`** (`storage/ingest.py`, around line 2506):
   - Callsign is `src.split(",")[0]` (relay path goes to `via`).
   - Sentinel decoding depends on transport: zero is "no sensor" on the Extern-UDP path, but a real
     value on BLE (`src_type` `ble`, `ble_remote`, `BLE`).
   - QFE origin is decided by `src_type`, not by magnitude: `lora` never feeds `qfe`, `node` feeds
     it from the real pressure reading, BLE from `/P=`. The 300-1100 hPa range is only a garbage check.
   - If QFE is missing but a plausible QNH and an altitude are available, QFE is derived with the
     barometric formula as a DERIVED reading. A measured value always outranks a derived one.
   - Altitude falls back to `station_positions.alt` when the frame carries none.
   - A frame with no reading at all (all absent) returns without writing.
   - Dedup: rows of the same callsign within `TELEMETRY_DEDUP_WINDOW_MS` (60 s, symmetric) are
     handled by `telemetry_reconcile.reconcile()`, which decides INSERT, UPDATE, REPLACE or SKIP from
     stored timestamps and provenance (never from arrival order).
   - Inserts or updates the `telemetry` row; then upserts `station_positions` with the reconciled
     values. There is no `NULLIF(…, 0)` guard: sentinels were already decoded, so a merged 0 is a
     real reading.

## SQLite Table

```sql
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    temp1 REAL,
    temp2 REAL,
    hum REAL,
    hum2 REAL,
    qfe REAL,
    qnh REAL,
    gas INTEGER,
    co2 INTEGER,
    alt REAL,
    batt INTEGER,
    extras TEXT,
    origin TEXT
);
```

Columns were added in steps: the table itself at schema v4, `alt` at v6, `batt` at v14,
`hum2` and `extras` at v15, `origin` at v41 (`NULL` = live RF sample, non-NULL = backfilled stand-in
from `telemetry_fill`). Index `idx_telemetry_cs_ts`; the bucket indexes for downsampled reads came
at v42. `LATEST_SCHEMA_VERSION` is in `storage/constants.py`.

## Dual-Path Telemetry

Some stations send telemetry via two paths simultaneously:

1. **BLE/APRS position beacons** — parsed in `ble_protocol.py`, `/P=` carries the station pressure
2. **UDP LoRa telemetry** (new since FW v4.35k.02.19) — raw sensor values; `/F=` in the same data is
   altitude, not pressure

The dedup logic handles most duplicates. Because `src_type` decides where `qfe` comes from, the
altitude of a LoRa frame can never be mistaken for a pressure, so the two paths agree on the
pressure column and a measured BLE value outranks a derived UDP estimate.

## Known Limitations

- `qnh` is stored again as of 2026-09-11 (firmware item 174 gave the node's barometric QNH reference a plausibility gate and a re-latch once the altitude filter converges, shipped in 4.35s/4.35t — see `../archive/2026-09-11_1200-aprs-key-contract-adoption.md`). A plausible value (850-1100 hPa) is written to both `telemetry.qnh` and `station_positions.qnh`; an implausible one (e.g. wrong-unit mmHg junk) stores NULL. If QFE is missing/invalid and a plausible QNH + altitude are available, QFE is still additionally calculated via the barometric formula (a DERIVED reading — a real `/P=` sensor reading always outranks it).
- Relay path information is lost during callsign normalization (no `via` field in telemetry table)
- Some stations send only zeros (no sensors) — correctly filtered by all-zero check
