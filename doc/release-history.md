# McApp v1.5.1 Release Notes

**Date**: February 20, 2026

---

### Bug Fixes - new MeshCom Firmware v4.35k.02.19

- **Fix telemetry chart corruption from duplicate UDP/BLE paths** — When the MeshCom node forwarded other stations' telemetry via UDP, pressure charts oscillated between real values and 0. Three fixes: filter all-zero sensor packets before altitude lookup, use `NULLIF(x, 0)` in the station_positions UPSERT so zero values from the UDP path don't overwrite real BLE values, and add a 60-second dedup window keeping the record with better data.
