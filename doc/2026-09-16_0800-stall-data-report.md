# Stall data report — first 14 h after v2.0.8-dev.3 (2026-09-15 17:17 → 2026-09-16 07:50)

Handover for a coding agent. Goal of the follow-up: turn the findings below into an improvement
plan with verified root causes, then implement. Everything here was read from
`GET /api/stalls` on mcapp.local; the recording mechanism is documented in `CLAUDE.md` §"Stall
Tracking" and `doc/2026-09-15_1530-stall-tracking-plan.md` (record schema §2, kinds and
thresholds §1, endpoints §4, replay §5).

## 0. How to get at the data

- Live rows: `curl -s 'http://mcapp.local/api/stalls?limit=2000'` (newest first; filters
  `kind=`, `severity=`, `since=<ms>`). Per-path baseline: `GET /api/stalls/summary`.
- Replay one row against the running box: `uv run python scripts/replay_stall.py --base
http://mcapp.local --id <row id> --repeat 20`.
- Database on the box: `/var/lib/mcapp/messages.db` (SQLite, WAL). **No `sqlite3` CLI on the
  Pi** — write a python3 script, `scp` it, run it, delete it (`ssh martin@mcapp.local`). All
  timestamps are **milliseconds**. Table `stall_events`: `id, ts_ms, origin, kind, severity,
request_id, session_id, method, path, query, body, status, duration_ms, context (JSON),
detail (JSON)`.
- A `client_*` row and the matching server `http` row share `request_id` (`X-Request-Id`);
  join on it to split "time in the backend" from "time in the Caddy/lighttpd hop".
- Thresholds: `http`/`client_http`/`handler`/`sse_answer` 500 ms stall, 2000 ms critical;
  `loop_lag`/`pool_wait` 100 ms stall, 500 ms critical; 1-in-50 sub-threshold sampling.
  Configurable under `stalls` in `/etc/mcapp/config.json` (`StallsConfig`,
  `src/mcapp/config_loader.py`).

## 1. Totals (175 rows)

| origin | kind          | severity | rows |
| ------ | ------------- | -------- | ---- |
| server | handler       | stall    | 58   |
| server | loop_lag      | stall    | 30   |
| server | loop_lag      | critical | 8    |
| server | http          | stall    | 16   |
| server | http          | critical | 3    |
| server | http          | sample   | 3    |
| client | client_http   | stall    | 36   |
| client | client_http   | critical | 4    |
| client | client_http   | sample   | 1    |
| client | sse_heartbeat | critical | 16   |

Not seen at all: `pool_wait`, `client_timeout`, `client_error`, `sse_answer`,
`sse_answer_missing`. The thread pool was never the bottleneck in this window.

## 2. Findings

### F1 — Message ingest takes 0.5–1.25 s per message (HIGH, the main target)

- **Evidence:** 53 `handler` rows since the reboot, every one of them
  `detail.handler = "MessageRouter._storage_handler"` (`src/mcapp/main.py`, the subscriber that
  calls `store_message`). Durations 543–1251 ms. Triggers are ordinary traffic: position beacons,
  telemetry, `ble_notification` and `mesh_message` alike (`detail.message` carries the full routed
  message for each row — replayable input).
- **What the context rules out:** at every one of these moments `pool_queued=0`,
  `pool_running≤2`, `loop_lag_ms≈1`. Nothing was queued behind anything; the time is spent
  inside the ingest path itself.
- **Hypothesis to verify:** `store_message` (`src/mcapp/storage/ingest.py:1446`) performs ~69
  sequential awaited DB calls (`_mutate` / query helpers, `grep -c "await self\._" ingest.py`),
  and each goes through `db_write`/`db_read` (`storage/constants.py:242-270`) which opens a NEW
  `sqlite3.connect(...)` per call, plus `PRAGMA journal_mode=WAL` on the read paths. On a Pi Zero
  2 W the per-connection open + WAL handshake is on the order of 5–15 ms, so 69 of them lands
  exactly in the observed range. Classifier work (`classify()` inline in `store_message`) is the
  second candidate.
- **How to verify:** wrap each awaited call in `store_message` with `time.perf_counter()` in a
  scratch copy, or replay one `detail.message` through `store_message` against a copy of the DB
  with `python -X importtime`-style timing; compare "N connections × open cost" with the row's
  `duration_ms`. Also check `EXPLAIN QUERY PLAN` for the dedup/ack lookups inside the path.
- **Fix shape:** one connection and one transaction per ingested message (a per-message
  `db_write` context passed down, or a small write-batch object), keeping the invariants in
  `CLAUDE.md` (the `_claim_recent_ingest` in-memory claim before the first await; hook order for
  `{CET}` and link-check; `_store_mheard` early-return). Acceptance: `handler` rows for
  `_storage_handler` disappear under normal traffic; `ingest_dedup_tests`, `linkcheck_ingest_tests`,
  `uptime_tests`, `ack_status_tests` stay green.

### F2 — `/api/weather` is ~750 ms on every call (MEDIUM)

- **Evidence:** 25 `client_http` rows for `GET /api/weather` (24 stall, 1 critical, p50 768 ms,
  p95 1849 ms, max 2110 ms) vs only 5 server `http` rows (p50 748 ms, max 1502 ms). The
  server-side rows are the ones over 500 ms; the other 20 calls were 400–500 ms server-side and
  crossed 500 ms only with the proxy hop. So the route is consistently 400–750 ms.
- **Where:** `src/mcapp/sse_routes/weather.py:63/74` → `asyncio.to_thread(weather_service.get_weather_data)`;
  the service is `src/mcapp/meteo.py` (cache at `self._cache`, TTL check at `meteo.py:271`).
  The journal shows upstream fetches to `api.brightsky.dev` and `api.open-meteo.com` at
  03:23:23 and 05:10:52, i.e. on client reconnects — check whether the TTL is being honoured
  when two callers (sidebar + main view) hit it back to back, and whether the sidebar variant
  (`/api/wx/sidebar`, 330 ms) shares the cache.
- **Fix shape:** serve from cache within TTL unconditionally and refresh in the background;
  acceptance: p50 under 100 ms for a warm cache.

### F3 — Two startup one-offs (LOW, document rather than fix)

- `GET /api/timezone` 3876 ms at 18:35:39: first use lazily imports `timezonefinder`
  (`sse_routes/weather.py:105`, comment says so). Options: warm it in a background task after
  startup, or accept and mark.
- `GET /api/telemetry?hours=744` 2812 ms at 18:35:26: a 31-day series. Check the query plan in
  `storage/query.py` (telemetry range query) and whether an index on `(station, ts)` or a coarser
  bucket for ranges > 7 d is warranted.

### F4 — Event-loop lag, 28 events up to 895 ms, cause unknown (MEDIUM, needs instrumentation)

- **Evidence:** `loop_lag` rows at 17–23 h and 01–07 h, max 895 ms (07:28:21), p95 887 ms for
  the critical ones. The journal has no entries at those seconds. Not aligned with the nightly
  prune (04:xx) or with any recorded HTTP request.
- **What it means:** something ran synchronously on the event loop for up to 0.9 s. Candidates:
  a synchronous SQLite call outside `to_thread` (grep `sqlite3.connect` / `db_read(` /
  `db_write(` in non-thread code paths under `src/mcapp/`, e.g. `commands/`, `push_delivery.py`,
  `classifier/`), large `json.dumps` of SSE bursts, `classify()` regex work inline.
- **Fix shape:** first make it attributable — in `StallRecorder._loop_lag_loop`
  (`src/mcapp/stalls.py`) capture the main thread's stack via `sys._current_frames()` from the
  lag monitor (it runs on the loop, so instead: a helper thread that samples the main thread's
  frame when the loop's heartbeat is overdue) and store it in `detail`. Then fix what it shows.

### F5 — `sse_heartbeat` rows are iOS background suspension, NOT a stall (FALSE ALARM, fix the reporter)

- **Evidence:** all 16 rows: `context.ua` = Safari 27 (iOS PWA), `visibility=hidden`,
  `sse_age_ms` 109 s – 40 min, `online=true`, and `context.server.sse_clients=0` — the server had
  no connected client at that moment. Roughly hourly (18:12, 18:31, 18:59, 19:14, 20:22, 22:29,
  23:56, 01:24, 02:27, 03:23, 04:15, 05:10, 06:07, 07:00, 07:32, 07:49).
- **Reading:** iOS suspends the background PWA; the EventSource is torn down server-side; iOS
  wakes the app briefly about once an hour, the 90 s watchdog (`useSSEClient.ts`,
  `HEARTBEAT_TIMEOUT_MS`) fires, reconnect follows. Correct behaviour, noise in the data.
- **Fix (webapp):** in `recordSseHeartbeatTimeout` (`src/services/stallReporter.ts`) record
  severity `sample` (or skip) when `document.visibilityState === 'hidden'`, and keep `critical`
  only for a visible tab. Consider reconnecting immediately on `visibilitychange → visible`
  instead of waiting for the watchdog (check `useConnectionManager.ts:632`).

## 3. Not a finding (checked)

- `pool_wait` never fired: the 8-thread default executor was never saturated in this window.
- No `client_timeout` / `client_error`: no request exceeded the 10 s abort, no non-2xx.
- No `sse_answer` rows: every `page_request` was answered under 500 ms.
- Memory after 14 h: used 259 of 462 MB; mcapp 78 MB PSS + 21 MB zram, BLE service 13 + 27 MB,
  Caddy 27 MB. Stable; the reboot delivered MemTotal 415 → 473 MB and CMA 256 → 64 MB.

## 4. Suggested order

1. F1 (ingest batching) — largest user-visible effect, replayable inputs already recorded.
2. F5 (reporter noise) — five-line webapp change, keeps the dataset honest.
3. F4 (lag attribution) — small recorder change, unblocks the next finding.
4. F2 (weather cache), then F3.

Re-collect after each step with the script pattern in this session's scratchpad
(`collect.py`: counts by kind, per-path p50/p95/max since a boot timestamp, worst rows with
context) — or simply `GET /api/stalls/summary` before and after.
