# ADR-0015: Gateway uptime as a ledger of gap and dark runs

| Field | Value |
|---|---|
| Status | Accepted. Amended 2026-08-28 (cadence) and 2026-11-06 (`disabled` claim). |
| Date | 2026-08-21 |
| Deciders | Martin Werner |
| Affects | `storage/uptime.py`, `storage/constants.py`, `storage/ingest.py` (hook before the filter), `sse_routes/uptime.py`, webapp `WATCHDOG_TIMEOUT_MS` |
| Supersedes | - |
| Superseded by | - |

## Context

The `{CET}` time beacon shows that the node's uplink to the MeshCom server is alive. The operator
wants an availability figure for it. The beacon is dropped at ingest, so nothing was stored, and a
per-minute row per minute would grow with wall-clock time.

## Decision

- **Ledger of transitions.** Store only closed `gap` and `dark` runs. `up` runs are derived by the
  reader from the holes. Row count follows transitions (tens per month), not time.
- **`gap`** (proxy running, no beacon) counts against UPTIME. **`dark`** (proxy not running) counts
  against COVERAGE only. They are different claims and must never be merged.
- **`disabled`** (from the `SN` register's `GW`) also counts against COVERAGE only. A counted beacon
  overrides a stale `GW=false`.
- **Hook before `_should_filter_message`.** That guard drops `{CET}` before any insert, so a recorder
  placed after it never fires.
- **Gate is hop count 0**, not `not via`. The beacon arrives as UDP, as BLE with `via == src`, and as a
  foreign multi-hop relay that must not count.
- **`GAP_TOLERANCE_MS` is above the beacon cadence, which is set upstream and changed once.** It was
  303 s until 2026-08-22 and 606.5 s since. The tolerance is 12 min.
- Startup reconciliation writes the `dark` row before the heartbeat starts, so a deploy restart is never
  read as a link outage.

## Consequences

- A tolerance at or below the cadence records a gap every cycle. Symptom: uptime near zero while beacons
  arrive. The first step is to re-measure the cadence.
- Retune four thresholds together: `GAP_TOLERANCE_MS`, `SILENT_MS` (12 min), `OFF_MS` (30 min) in
  `storage/constants.py`, and the webapp's `WATCHDOG_TIMEOUT_MS`. Only the tolerance is baked into stored
  history.
- Resolution is one beacon cadence. A 20-minute outage reads as about 20 minutes.

## Alternatives considered

- One row per minute: rejected, grows with time and makes long ranges slow on the Pi.
- Gate on `not via`: rejected, drops the BLE copy of the beacon.

## Evidence

- `../archive/2026-08-21_2350-gateway-uptime-plan.md`
- `../archive/2026-11-06_0020-gateway-availability-fix.md`
- Migrations 25, 28 and 40 in `../reference/database-reference.md`
