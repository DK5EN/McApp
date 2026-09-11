# APRS `/X=` position-comment key contract adoption

**Date:** 2026-09-11
**From:** DK5EN (MCProxy)
**To:** MCProxy maintainers
**Status:** adopted

## What changed on the firmware side

The firmware doc now publishes the full 17-key `/X=` position-comment contract
(`MeshCom-Firmware-DEV-Main/docs/architecture/11-wire-format.md` §1.8): fixed
concatenation order, no separator between keys (the leading `/` is the
separator), and a shared 100-byte budget with `atxt`/`#name`. This note
records what MCProxy does with that contract; it is a decision record, not a
change log for this wave.

## Keys MCProxy types

`A` (alt), `B` (batt), `R` (group list), `T` (temp1), `O` (temp2), `H` (hum),
`P` (qfe), `Q` (qnh — parsed but not stored, see below), `G` (gas), `C` (co2)
— already typed before this wave — plus four newly typed keys:

- `N` → `mh_ncnt` (neighbour count; the no-`=` `/N%i` form gets its own
  pattern, and shares the field name `transform_mh()` already uses for the
  MH register's `NCNT`)
- `D` → `din` (MCP23017 port-A input bits, fork-only, `b179fdff`; kept as the
  raw 8-char string, never coerced to a number)
- `U` → `vbus` (INA226 bus voltage)
- `I` → `vcurrent` (INA226 current)

## Kept in `extras`, deliberately

- `F` — the firmware's `qfe` _variable name_ but not a pressure: it carries
  `node_press_alt`, a barometric altitude in metres. Routing it into `qfe`
  would silently corrupt every BME680 station's pressure reading.
- `V` — sensor-block version marker (`2`/`3`/`5`), not a sensor reading.
- `Y` — telemetry-beacon flag, not a sensor reading.

None of the three has a plausible numeric home in the typed fields above;
`extras` is where a value with no dedicated column belongs.

## The `T#` routing fix

APRS `T#` telemetry (`:T#seq,v1..v5,bits` to group `100001`) is detected
after the padded originator callsign on `PAYLOAD_TYPE_MSG` and handed to
`parse_aprs_telemetry()` instead of falling through to chat storage — closes
the gap where a telemetry frame was previously stored as a chat message in a
phantom group.

## `/Q=` stays unstored by design

`qnh` is parsed off the wire (needed as an intermediate for the QFE
derivation) but the `telemetry`/`station_positions` `qnh` column is never
written — see `src/mcapp/storage/ingest.py:1815-1823`: node-reported QNH is
unreliable, so the frontend derives QNH from QFE + altitude instead. This was
flagged as a gap in the firmware-side drift analysis; it is not a gap, it is
this decision, and this note is where it is now recorded.
