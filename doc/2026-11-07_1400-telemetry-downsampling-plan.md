# Telemetry downsampling for the WX charts

**Goal:** the 30-day and yearly WX views load in about a second on the Pi Zero and never block the event loop, now that the gap fill roughly doubled the stored telemetry.

**Status (2026-11-07):** Waves 5 and 6 done and committed (both advisors approved after small fixes). Next: Wave 7 (measure on a Pi-sized DB copy, dev release, measure on mcapp.local). Open advisor nice-to-haves for Wave 7 or later: sidebar "latest" shows a bucket start (up to 2 h old) on 30 d; a ~10 h old station qualifies by raw rows but has under 6 buckets to chart; the Kalman gate's `stddev > 2` sees 2 h means (smaller spread), so a jittery station may stabilize on 7 d but not on 30 d; a station whose stats window holds only temp2/hum2/gas gets no `stats` object. Follows `doc/2026-11-07_1000-telemetry-gap-fill-plan.md` (Waves 0-4 done, `v2.1.26-dev.1` live on mcapp.local).

## Measurements (mcapp.local, 2026-11-07 13:10, after the first fill cycle)

| View                  | Time  | Size    | Rows   |
| --------------------- | ----- | ------- | ------ |
| 24 h (fetches 48 h)   | 0.2 s | 0.4 MB  | 1,783  |
| 7 d (fetches 192 h)   | 1.2 s | 2.8 MB  | 12,483 |
| 30 d (fetches 744 h)  | 4.9 s | 12.3 MB | 57,398 |
| 1 y (4 h min/max SQL) | 8.2 s | 6.8 MB  | -      |

- **Python row handling dominates, not the scan.** Counting the 30-day rows takes 44 ms. Fetching them as dicts takes 4.1 s, `json.dumps` another 1.5 s, and that 1.5 s runs on the event loop.
- **Time buckets only help from 1 h up.** WX stations report every 30 min (median; p90 91 min). 15- or 30-min buckets remove almost nothing; 1 h halves the rows (33k), 2 h quarters them (19k).
- **SQL bucketing alone is not enough.** A 1 h `AVG` query still took 3.7 s plus 0.9 s encoding; 2 h took 2.6 s plus 0.5 s. The rows have to leave SQLite already reduced and the JSON has to be built off the loop.
- **89 of 139 stations in the 30-day response have no WX sensor** (altitude and battery only), about 16k of the 57k rows.
- **Columnar JSON is 3.6 times smaller** (14.3 MB to 4.0 MB).

## Constraints

- **No backward compatibility (operator, 2026-11-07).** Backend and webapp ship in one release, so the response shape may change. mc-chat's mock also serves `/api/telemetry` (`meshcom_mock/api.py:756`); the WX page breaks against it until it gets the same shape.
- **The webapp has cadence-dependent logic:** the Kalman altitude gate (24 h stddev, per-row steps), the "N datapoints (X/h)" header, the min/median/max table, `MIN_WX_POINTS`, and the gap threshold (`max(3 x median interval, 90 min)`). Each must be told when it gets buckets.
- **Zero means "sensor absent"** for temp2, hum2 and gas, so averages must ignore zeros, as RF ingest does.

## Decisions for the operator

1. **What the 30-day chart draws.**
   - A. 1 h mean line. Looks like today; halves the rows.
   - B. 2 h mean line. Smoother; a quarter of the rows. Outages under 6 h no longer break the line. **Recommended.**
   - C. 2 h min/max band like the yearly view. Shows extremes; the 30-day view changes style.
2. **The stats table (min/median/max, "N datapoints").**
   - a. Exact, computed by the server from the raw rows in a small separate response. **Recommended.**
   - b. From the buckets: min and max narrow, the count becomes buckets.
3. **Stations without a WX sensor** (altitude and battery only) in the WX views.
   - i. Drop them from the WX views, as the fill already does. The sidebar loses about 89 altitude-only stations. **Recommended.**
   - ii. Keep them.
4. **Hollow points on a bucket:** hollow only if every sample in it came from the central server. **Recommended**; the alternative is "hollow if any".
5. **7-day view:** stays raw (it is already near native cadence) but gets the off-loop encoding. **Recommended.**
6. **Yearly view:** keep the 4 h min/max shape, add the WX-sensor filter and off-loop encoding, and build it the same reduced way. **Recommended.**

## Waves

| Wave | Content                                                                                                                                                                          | Owner                     |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| 5    | Backend: new columnar response for all telemetry routes, server-side bucketing per range, exact stats in the same response, station filter, JSON built in a worker thread, tests | Implementer, then advisor |
| 6    | Webapp: read the columnar response, tell the Kalman gate, stats, gap threshold and point count about buckets                                                                     | Implementer, then advisor |
| 7    | Gate both repos, measure on a Pi-sized DB copy, dev release, measure on mcapp.local                                                                                              | Orchestrator              |

Waves 5 and 6 run in parallel once the wire contract below is fixed: separate repos, disjoint files.

### File ownership

| Wave | Files                                                                                                                                                                                                                                                                                                             |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 5    | `src/mcapp/storage/query.py` (telemetry readers only), `src/mcapp/sse_routes/weather.py`, NEW `src/mcapp/telemetry_read_tests.py`, `scripts/run_startup_tests.py` (orchestrator)                                                                                                                                  |
| 6    | `src/stores/WxDataStore.ts`, `src/components/stats/WxData.vue`, `src/components/stats/WxListPanel.vue`, `src/views/WxView.vue`, `src/utils/wxGaps.ts`, `src/utils/wxPhysics.ts`, `src/utils/wxChartConfig.ts`, `src/utils/wxDataHygiene.ts`, their specs, NEW `src/stores/__tests__/WxDataStore.columnar.spec.ts` |

### Wire contract (fixed 2026-11-07, at dispatch)

Decisions taken as recommended: 1B, 2a, 3i, 4 (hollow only if all), 5, 6. mc-chat's mock is deferred.

- **One shape for every range:** `GET /api/telemetry?hours=48|192|744[&stats_hours=N]` and `GET /api/telemetry/yearly` return `{"bucket_s": int, "stations": {<callsign>: {...}}}`. `bucket_s` is `0` (raw) for `hours <= 192`, `7200` for `hours > 192`, `14400` for yearly. The webapp never chooses a bucket.
- **Fields on the wire are exactly the charted ones:** `temp1 temp2 hum hum2 qfe qnh gas alt`. `co2`, `batt` and `extras` are dropped (the webapp never charts them).
- **Raw (`bucket_s == 0`), per station:** `ts` (ms), one array per field (entries may be `null`), `origin` (`1` = meshmap, `0` = RF). Rows where all eight fields are NULL are not sent. No `stats`, no `n`; the webapp keeps computing stats itself from raw rows.
- **Mean buckets (`bucket_s == 7200`), per station:** `ts` (bucket start, `timestamp / 7200000 * 7200000`), one array per field = `ROUND(AVG(...), 2)`, with zeros ignored (`NULLIF(x, 0)`) for `temp2 hum2 gas`, `n` (raw rows in the bucket), `origin` (`1` only if every row in the bucket is meshmap), and `stats` (below).
- **Yearly (`bucket_s == 14400`), per station:** `ts`, `<field>_min` / `<field>_max` for `temp1 temp2 hum hum2 qfe gas alt` (zeros ignored for `temp2 hum2 gas`), `n`, `origin`. No `stats`: the yearly view has no stats table, and an 8760 h median pass is the most expensive read on the Pi (advisor, Wave 5).
- **`stats`** (mean buckets only): `{"count": int, "first_ms": int, "last_ms": int, "<field>": [min, median, max]}` from the RAW rows in the last `stats_hours` (default: `hours`). The webapp ignores `stats.qnh` and derives the QNH row from its local qfe + alt series, so the table matches the chart (advisor, Wave 6). `count` = distinct raw timestamps where any of `temp1 hum qfe qnh alt` is non-NULL (mirrors the webapp's datapoint union). Median = lower-middle value for an even count. Zeros ignored for `temp2 hum2 gas`. A field with no value is omitted from `stats`.
- **A field with no value at all for a station is omitted** from that station's object (arrays and stats).
- **Station filter on every read:** a station is sent iff some row in the window has a non-NULL, non-zero value in one of `temp1 temp2 hum hum2 qfe qnh gas` (any origin). Altitude/battery-only stations disappear. Webapp-hidden stations are unaffected (the server does not know them).
- **Built without per-row Python dicts:** reduction and grouping in SQL, the JSON encoded in the same worker thread and returned as `bytes` (`Response(content=..., media_type="application/json")`).

## Cost and time

- **Waves 5 and 6:** one Sonnet implementer each at medium, about 25 min and $4-6 each; one Opus advisor each, about $2-3.
- **Rework round, if needed:** about $5 and 20 min per repo.
- **Wave 7:** orchestrator, about 30 min plus the deploy.
- **Total:** about $20-30 and 1.5-2 h of wall-clock to a dev release.

## Open questions

- Decisions 1-6 above.
- mc-chat's mock: port the new shape there too (a third, small brief in that repo), or leave its WX page broken for now?
- Is a 30-day load of about 1 s on the Pi the right target, or is 2 s acceptable?
- Release: does this go into v2.1.26 together with the gap fill (recommended, since the fill makes the problem acute), or separately?
