# Telemetry gap fill from meshmap

**Goal:** fill the gaps in the WX charts with samples from the central server and persist them, so the charts stay complete even when RF reception is not.

**Status (2026-11-07):** Waves 0 and 1 done. Next: Wave 2.

## Decisions (operator, 2026-11-07)

- **Method:** fill and persist in `telemetry`, marked by a new `origin` column.
- **Switch:** none of its own. While the Internet feed is enabled, data is ingested. While it is off, nothing new goes into the charts and stored rows stay.
- **Stations:** visible stations only, meaning not in `wx_sidebar.hidden`.
- **First fill:** the full history. Local retention is 365 days, so the fill stops there: older rows would be pruned the same night.
- **Chart:** hollow points for central samples, and the line breaks at real gaps. No separate RF statistic.
- **Load on meshmap:** accepted. The operator checks the impact later on SmokePing (baseline 2026-11-07: 0% loss, about 550 ms).

## Measurements

Local backup of 2026-10-20 against `GET https://meshmap.oevsv.at/api/v2/telemetry`, window 2026-10-13 to 10-20. The live DB was not read.

| Station   | Samples, local | Samples, meshmap | Max gap, local | Max gap, combined |
| --------- | -------------- | ---------------- | -------------- | ----------------- |
| DB0MMR-12 | 52             | 415              | 41.7 h         | 2.3 h             |
| DF2KX-12  | 94             | 216              | 27.8 h         | 22.3 h            |
| DM6CS-12  | 84             | 381              | 11.6 h         | 0.6 h             |
| DL1RHS-24 | 113            | 419              | 7.1 h          | 0.6 h             |
| DG7RJ-11  | 152            | 277              | 5.7 h          | 2.0 h             |
| DG7RJ-8   | 271            | 369              | 3.4 h          | 0.5 h             |
| DB0ISM-1  | 300            | 383              | 2.6 h          | 1.6 h             |
| DL2UD-1   | 306            | 422              | 1.0 h          | 1.0 h             |

- **Meshmap counts are after collapsing twin rows** closer than 60 s. Raw counts are up to 1.8 times higher.
- **Central is not complete.** DF2KX-12 keeps a 22 h gap, and 2-13% of local samples had no central twin in 3 of the 5 stations checked.
- **Row identity is estimated.** It was matched on identical temperature and humidity within 20 min, which is weak for slow sensors.

## Why this is not a plain merge

- **Local timestamps run late.** They are arrival times, a median of 16-305 s per station behind the server's, up to about 14 min at p90. A 60 s match would duplicate.
- **There is no shared identity.** The API returns no `msg_id`, and meshmap's `timestamp` is its own receive time, in seconds.
- **The API caps a response at 5000 rows, keeping the newest.** A busy station has about 85 raw rows a day, so 90-day chunks would silently lose their oldest rows.

## The pressure offset is ours

QFE differed from meshmap by a station-specific median of -1.3 to +0.5 hPa. The cause is on the local side:

- **Rows with a real `/P=` match meshmap exactly** (median, p10 and p90 all 0.00) on all four stations checked.
- **The others hold a derived QFE.** Without `/P=`, `store_telemetry` computes it from QNH and altitude with the standard atmosphere. Worked example, DF2KX-12: `qnh=1026.4`, `alt=425` gives 975.7, the stored value, while meshmap has the measured 977.1.
- **Consequence:** central `press` is a measured value and is safe to fill. The local QFE chart already mixes measured and derived values, up to about 1.3 hPa apart on a 70 hPa axis. Filling makes that no worse in kind, so local rows are left alone.

## Rules for the fill

| Rule                                                                           | Why                                                                                              |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Collapse central rows closer than 60 s, keeping the first                      | The upstream hub re-emits frames seconds apart                                                   |
| Fill only if no RF row sits within ±15 min                                     | Local arrival runs up to about 14 min late                                                       |
| A filled row blocks only within 60 s, and counts as a kept twin                | Otherwise the result depends on chunk boundaries and poll phase                                  |
| An exact 0 is absent for every field except `alt`                              | Stations without a second probe send `temp2: 0`; the RF path drops zeros the same way            |
| QFE and QNH outside the RF plausibility ranges are absent                      | Same junk filter as RF ingest                                                                    |
| Only samples at least 30 min old                                               | A late RF copy gets the chance to arrive first                                                   |
| Write directly to `telemetry`, never through `store_telemetry`                 | That path upserts `station_positions`, so an old sample would regress the sidebar's latest value |
| Skip blocked callsigns                                                         | The blocklist applies to every source                                                            |
| Keep `origin = NULL` for RF rows, `'meshmap'` for filled rows                  | No rewrite of existing rows; one `DELETE` undoes the whole feature                               |
| A live RF frame matching a filled row within 60 s clears its `origin`          | RF wins: its values replace the filled ones, and only its own values reach `station_positions`   |
| Chunk 30 days per request, and halve the chunk when a response reaches the cap | A capped response drops its oldest rows without an error                                         |

## Field mapping

| Meshmap      | `telemetry` | Note                                                                    |
| ------------ | ----------- | ----------------------------------------------------------------------- |
| `temp`       | `temp1`     |                                                                         |
| `temp2`      | `temp2`     |                                                                         |
| `hum`        | `hum`       |                                                                         |
| `press`      | `qfe`       | Measured                                                                |
| `qnh`        | `qnh`       | Only a stored value                                                     |
| `qnhReduced` | not stored  | Depends on the request window's median temperature, so it is not stable |
| `alt`        | `alt`       |                                                                         |
| `batt`       | `batt`      |                                                                         |
| `gas_res`    | `gas`       |                                                                         |
| `eco2`       | `co2`       |                                                                         |
| `wlevel`     | `extras`    | Only when present                                                       |
| `wtemp`      | `extras`    | Only when present                                                       |

## Behaviour

- **Polling:** every 30 min per visible station, while the Internet feed is enabled.
- **After the feed was off:** each station's cursor catches up when it is enabled again, so that period is filled too.
- **Newly visible station:** it has no cursor yet, so it starts its first fill on the next cycle.
- **Newly hidden station:** polling stops and its rows stay.
- **First fill:** background, resumable after a restart, one request every 10 s, with a random start delay per box.
- **Line break:** at more than 3 times the station's median interval, at least 90 min. The yearly view keeps its 4 h buckets.

## Waves

| Wave | Content                                                                                            | Owner                     | Status |
| ---- | -------------------------------------------------------------------------------------------------- | ------------------------- | ------ |
| 0    | Pressure cause, field mapping, this plan                                                           | Orchestrator              | Done   |
| 1    | Migration v41, pure merge logic, write and cursor storage, `origin` handling in ingest, test suite | Implementer, then advisor | Done   |
| 2    | Fetch job, status route, `origin` in `/api/telemetry`                                              | Implementer, then advisor | Open   |
| 3    | Webapp: hollow points, line breaks, fill status in the Internet card                               | Implementer, then advisor | Open   |
| 4    | Full gate in both repos, backfill test on a DB copy, read-time check                               | Orchestrator              | Open   |

A release or deploy to mcapp.local is not part of this plan and needs a separate go.

## Open risks

- **Read time on the Pi Zero.** `/api/telemetry` has no row limit, so filled data can slow the 30-day chart. Wave 4 measures it. The fix would be server-side downsampling, which is not planned yet.
- **Row growth.** At most about 17,500 rows per visible station per year.
- **Data quality upstream.** The hub re-emits one station's frame under another station's call. mcmap filters this, but a filled row can inherit a miss. Marked rows make that traceable.
