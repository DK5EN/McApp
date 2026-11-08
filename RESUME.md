# Resume: soak v2.1.26-dev.3, then release v2.1.26

**Next action (2026-11-08, from about 15:00):** run the soak checks below on mcapp.local. If all pass, cut production `v2.1.26` with the `prod-release` skill.

## What is under soak

- **Release:** `v2.1.26-dev.3`, deployed 2026-11-07, service restart **14:57:18 CET**, slot-2, schema v42.
- **Content:** meshmap WX gap fill (doc/2026-11-07_1000-telemetry-gap-fill-plan.md) and the downsampled columnar telemetry reads (doc/2026-11-07_1400-telemetry-downsampling-plan.md, Waves 5-10).
- **Commits:** MCProxy `6339106` (SQLite JSON arrays, migration 42) + `f619204` (docs); webapp `6ba3e92`.

## Baseline at deploy (2026-11-07, about 15:20)

| Measure                                    | Value                                   |
| ------------------------------------------ | --------------------------------------- |
| `mcapp` RSS, 9 min after the restart       | 122 MB                                  |
| `loop_lag` rows, previous 24 h             | 96 (includes the dev.1/dev.2 deploys)   |
| `handler` rows, previous 24 h (non-sample) | 59                                      |
| Cold 744 h (stats_hours=720) / yearly      | 3.5 s / 4.1 s; cached 0.006 s / 0.008 s |
| Cold 48 h / 192 h                          | 0.13 s / 0.45 s                         |

## Soak checks (all must pass)

1. **Telemetry reads do not lag the loop.** No `loop_lag` row (`/api/stalls?kind=loop_lag`) whose `detail.stack` points into `query.py` `_tm_*` / `_telemetry_payload`. Total `loop_lag` and `handler` counts since the restart at or below the baseline.
2. **The nightly prune ran** with the two new indexes: journal line for the prune or retention job after midnight, no error, and its duration does not show up as a stall.
3. **No warnings in the journal** since the restart: `sudo journalctl -u mcapp.service --since "2026-11-07 14:57:18" -p warning --no-pager`.
4. **Gap fill is healthy:** `GET /api/telemetry/fill` state looks sane, requests per 30 min cycle are about the WX station count (45), no fetch errors in the journal, and no RF row has a meshmap row within +-15 min (python query on the DB, read-only).
5. **WX page live check** at https://mcapp.local, ideally on the iPhone too: 24 h, 7 d, 30 d and 1 y load; hollow points for meshmap samples; lines break at real gaps; the sidebar time on 30 d is the real last sample; the stats table fills on 30 d.
6. **`mcapp` RSS** is close to the baseline (no steady growth): `ps -o rss= -p $(systemctl show mcapp -p MainPID --value)`.
7. **Cold timings unchanged:** re-time the four requests from the table above on the Pi (`curl -w %{time_total}` against `http://127.0.0.1:2981/api/...`). The first 744 h and yearly calls are cold only if nobody opened those views in the last 60 min.

The `ai-ops` skill covers 3 and 6 and most of 1. 2, 4 and 7 need the extra queries.

## Then

- All pass: `prod-release` (release notes about gap fill + faster WX charts, `./scripts/release.sh 2`, deploy from the webapp Update page).
- Any fail: note it in the downsampling plan doc, fix, and cut a new dev release. The soak restarts.
- Delete this file once v2.1.26 is out.
