# Gateway Availability card fix (2026-11-06)

Resume point and contract for the wave campaign.

## Root causes

- **Last Year shows only yellow: a rendering bug.** `buildRenderSegments` (webapp
  `utils/linkUptime.ts`) floors every non-`up` segment to 2 px of an assumed 280 px bar and takes the
  width from `up`. 131 gaps claim 93.6% of the bar while `up` truly has 19.7%, so `up` reaches zero
  and renormalising squeezes `dark` from 79.2% to 45.8%. The stats were right.
- **The card never passes its real width**, so an 830 px bar is computed as 280 px.
- **Backend `_downsample` would bring it back:** above 500 segments the worst kind wins per chunk.
  At the current rate a year crosses that around January 2027.
- **`dark` is missing from the legend.**
- **Gateway mode is ignored.** `storage/uptime.py` only sees `{CET}` arrivals. A node with gateway off
  gets no hop-0 beacon (`udp_functions.cpp:256`, harvest-only path), so it records one endless outage.
  The node reports the mode as `GW` in the BLE `SN` register, which the ledger never used.

## Decisions

- Bucket colours by uptime share: green at 99% or more, amber from 80% to under 99%, red under 80%.
- Bucket sizes: 1 day for `1y`, 6 hours for `30d`. The webapp merges further when narrow.
- `disabled` lowers COVERAGE and never touches UPTIME.
- One lost beacon still counts as a gap: out of scope, it would not repair stored history.
- Old rows cannot be reclassified: gateway state was never recorded.
- A UDP-only box has no `SN` register, so its state stays unknown and it measures as before.

## Contract: `GET /api/uptime?range=`

Additive; existing fields keep their meaning.

```text
state:           adds "disabled"
gateway_enabled: true | false | null        null = never reported
segments[].kind: adds "disabled"
buckets:         list for 30d and 1y, null for 24h and 7d
  { start_ms, end_ms, up_ms, gap_ms, dark_ms, disabled_ms, longest_gap_ms }
```

- `buckets` are built from the exact pre-downsample segments, contiguous from `start_ms`. The last one
  may be short. Their `*_ms` sum to the bucket's width.
- `longest_gap_ms` is the full length of the longest gap touching the bucket, not its clipped part.
- `segments` stays in the response for every range.
- `uptime_pct` is `up / (up + gap)`; `coverage_pct` is `(up + gap) / window`; `disabled` is in neither.

## Ledger semantics

- `link_uptime_state` gains `gateway_enabled` and `disabled_since_ms` (migration 40).
- `GW=false` while not disabled: close any silence beyond `GAP_TOLERANCE_MS` as a `gap` ending now, set
  `disabled_since_ms`.
- `GW=true` while disabled: store `disabled` from `disabled_since_ms` to now, clear it, reset
  `last_beacon_ms` and `open_up_start_ms` to now so no gap spans the disabled time.
- Repeats of the same value are no-ops: registers replay on every reconnect.
- A counted beacon while disabled proves the uplink is alive and overrides a stale `GW=false`: close
  the `disabled` row, set `gateway_enabled` to true.
- The reader renders an open `disabled` tail instead of a `gap` tail; `state` is `disabled` only while
  disabled.
- A startup `dark` reconcile while disabled: close `disabled` at `last_tick_ms`, store `dark` to now,
  reopen `disabled` at now.
- A startup `dark` reconcile while not disabled: first store the silence the proxy watched before it
  went down as a `gap`, or it reads as `up` once the anchors reset.
- A `SN` replayed from ble_service's age-less cache only seeds a NULL ledger, and cannot disable while
  beacons are fresh. Firmware pushes `SN` only on a BLE-driven change, so a console, web or RM1 toggle
  stays invisible until the next hello.
- `GW=true` writes `last_beacon_ms = now`, like reconcile: the badge reads active for up to 12 min.

## Waves

| Wave | Content                              | Owner        | Status |
| ---- | ------------------------------------ | ------------ | ------ |
| 0    | Contract, firmware and SN facts      | orchestrator | done   |
| 1A   | Backend buckets and gateway ledger   | implementer  | done   |
| 1B   | Webapp bar, legend and gateway state | implementer  | done   |
| 2    | Gates, advisor, commit in each repo  | orchestrator | done   |

The operator asked for commits only. Push and dev release are a separate decision.
