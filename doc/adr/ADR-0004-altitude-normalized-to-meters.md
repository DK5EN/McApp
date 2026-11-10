# ADR-0004: Altitude normalized to meters at ingestion

| Field | Value |
|---|---|
| Status | Accepted (implemented) |
| Date | 2026-02-14 |
| Deciders | Martin Werner |
| Affects | `udp_handler.py`, `sqlite_storage.py`, the BLE handler |
| Supersedes | - |
| Superseded by | - |

Converted 2026-11-10 to the ADR format. Original full text (root cause and data-flow diagram):
[archive/2026-02-14_1500-altitude-normalization-ADR.md](../archive/2026-02-14_1500-altitude-normalization-ADR.md).

## Context

The positions page showed stations at about 3.28 times their real altitude. The raw APRS `/A=`
value is in **feet**. The UDP path published the raw dict unchanged to live subscribers. The storage
layer converted feet to meters, but only on its own copy, so stations with a live update showed the
feet value. The BLE path already converted, so the same station showed different units depending on
the transport.

## Decision

Convert feet to meters in `udp_handler.py`, **before** publishing. Every downstream consumer (live
SSE, storage) then receives meters.

## Consequences

- One unit everywhere after ingestion. The frontend never has to know the source's unit.
- Any new ingestion path must convert before it publishes.
- The storage layer no longer converts altitude.

## Alternatives considered

- Convert in the frontend: rejected. The frontend would have to know which source uses feet.
- Convert in `sqlite_storage.py` on the original dict: rejected. It mutates a dict other subscribers
  may already have read, which is the bug that caused this ADR.

## Evidence

- `archive/2026-02-14_1500-altitude-normalization-ADR.md`
