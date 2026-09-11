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
`P` (qfe), `Q` (qnh — stored again, see below), `G` (gas), `C` (co2) —
already typed before this wave — plus four newly typed keys:

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

## `/Q=` is stored again

`qnh` is parsed off the wire (still feeding the QFE derivation when QFE is
missing) and, as of 2026-09-11, is also written to the `telemetry` and
`station_positions` `qnh` columns — see `src/mcapp/storage/ingest.py`'s
`qnh_reading` note (`store_telemetry`, near the QFE derivation) and
`telemetry_reconcile.ALL_FIELDS`.

This reverses the original "node QNH is unreliable" policy that dropped the
column (the frontend derived QNH from QFE + altitude instead). The reversal
is a firmware-side fix, not a policy change on general principle:
MeshCom-Firmware item 174 (GPS-01..04, `docs/CHANGELOG-stability.md`
~line 755, shipped in 4.35s/4.35t) gave the node's barometric QNH reference a
plausibility gate and a re-latch once the altitude filter converges, so a
plausible reading is now trustworthy enough to store under the same
measured/derived precedence as every other telemetry sensor column.

`qnh` still passes through `_QNH_PLAUSIBLE_HPA_RANGE` (850-1100 hPa) before
either use — that range guards against wrong-unit junk (an mmHg feeder
reporting ~760 for hPa), a distinct failure mode from the firmware-side
reference drift that item 174 fixed, and keeps applying independently of it.
