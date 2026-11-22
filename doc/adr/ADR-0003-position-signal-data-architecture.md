# ADR-0003: Positions, signal and MHeard data architecture

| Field | Value |
|---|---|
| Status | Accepted (implemented). Amended 2026-07-05: the "disjoint packet" invariant no longer holds for Extern-UDP. |
| Date | 2026-02-11 |
| Deciders | Martin Werner |
| Affects | `storage/ingest.py`, `storage/query.py`, `sqlite_storage.py`, `main.py` |
| Supersedes | the single-table position model |
| Superseded by | - |

Converted 2026-11-10 to the ADR format. Original full text, with the packet shape matrix,
the aggregation strategy and the frontend impact: [archive/2026-02-11_1400-position-signal-architecture-ADR.md](../archive/2026-02-11_1400-position-signal-architecture-ADR.md).

## Context

MHeard beacons carry RSSI and SNR but no coordinates. Position beacons carry coordinates but no
signal. The first design mixed both in one row, so a signal update overwrote a position and the
reverse. Charts for signal history need time buckets, and the position table needs one row per station.

## Decision

Split the data by what it measures:

- `station_positions`: one row per station (UPSERT). Position and signal are **independent field
  groups**: a signal write never touches location columns, and the reverse.
- `signal_log`: raw RSSI/SNR per beacon, with a `source` column (`mheard` or `lora`).
- `signal_buckets`: pre-aggregated time buckets. 5-minute for 8 days, 1-hour for 365 days, rolled
  up nightly at 04:00.
- `messages`: chat and ACKs, dual-write for the legacy position type.

Amendment 2026-07-05: since firmware `c4ad78bb`, an Extern-UDP `pos` packet with `src_type == "lora"`
carries both a position and a signal. One `store_message()` call then updates both field groups.
The invariant is now "field groups stay independent", not "packet types are disjoint".

## Consequences

- Charts and maps read different tables and never block each other.
- A `pos` frame with `src_type` other than `lora` carries a `0/0` sentinel. It must be excluded by an
  explicit `src_type` check, not by a range check (see `../policy/POL-007`).
- Retention differs per table. The current values are in `../reference/database-reference.md`.
- Schema migrations for these tables are listed in the database reference, not here.

## Alternatives considered

- One wide table for all station data: rejected, because signal and position overwrite each other.
- Storing signal only in `messages`: rejected, because MHeard beacons are not chat and would
  pollute the message history.

## Evidence

- `archive/2026-02-11_1400-position-signal-architecture-ADR.md`
- `reference/dataflow.md` (signal data path)
