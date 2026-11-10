# Stall follow-up: checkpoint-per-write, SSE serialization, handler baseline

Date: 2026-09-18. Baseline: `/api/stalls` on mcapp.local, 2026-09-15 22:00 to 2026-09-18 19:43
(v2.0.8 to v2.0.9). Companion to `2026-09-15_1530-stall-tracking-plan.md`.

## Findings

| #   | Finding                                                                                                                                                                                                                                                                   | Decision                                            |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| 1   | 137 of 151 `handler` stalls are `pos` frames in `_storage_handler`, 0.5-0.75 s, ~40/day. No full-table scan: every ingest-path query is `SEARCH ... USING INDEX`, < 1 ms. Cost is per-call connection close, which checkpoints the WAL (DB-file fsync) after every write. | Option A: one persistent writer connection          |
| 2   | `/api/send` 0.8-1.05 s stalls are the mheard 7-day dump: `json.dumps` of the chart payload runs on the event loop inside `format_sse_event` (280-400 ms per `loop_lag` sample).                                                                                           | Offload serialization of large payloads to a thread |
| 3   | `/api/weather` p50 570 ms, max 1.5 s, 8 of 12 calls over threshold. Upstream fetch, already off-loop.                                                                                                                                                                     | Document and close, no code change                  |
| 4   | No `handler`-kind `sample` rows exist, so there is no healthy baseline for handler duration; `time_handler` bypasses `severity_for`.                                                                                                                                      | Wire 1-in-N sampling for `handler`                  |

## Measurements (Pi Zero 2W, ext4 root, copy of the live DB, `INSERT` + commit)

| write path                                                    | p50     | max     |
| ------------------------------------------------------------- | ------- | ------- |
| per-call connection, NORMAL, no other connection open (prod)  | 23.7 ms | 28.6 ms |
| per-call connection, NORMAL, another connection holds the WAL | 4.5 ms  | 15.5 ms |
| one persistent connection, NORMAL                             | 0.2 ms  | 10 ms   |
| open + read + close, no write                                 | 5.3 ms  | 5.6 ms  |

A position frame performs 3-6 writes; ~7.4k signal-bearing frames/day means on the order of
25k checkpoint fsyncs/day. The 0.5-2.3 s rows are the SD card's tail latency on those fsyncs.
The checkpoint-on-close behaviour was recorded as an accepted trade-off in
`connection-leak-fable-verdict.md` before these numbers existed.

## Wave plan

Single wave, three writers with disjoint file sets, orchestrator owns docs and
`scripts/run_startup_tests.py`. Status: `done` = committed after the advisor pass.

| Writer | Scope                                   | Files                                                                                                              | Status |
| ------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------ |
| A      | Finding 1, persistent writer connection | `src/mcapp/sqlite_storage.py`, `src/mcapp/storage/constants.py`, `src/mcapp/storage/connection_lifecycle_tests.py` | done   |
| B      | Finding 4, handler sampling             | `src/mcapp/stalls.py`, `src/mcapp/stall_tests.py`                                                                  | done   |
| C      | Finding 2, off-loop SSE serialization   | `src/mcapp/sse_handler.py`, `src/mcapp/main.py`, `src/mcapp/sse_format_tests.py` (new)                             | done   |
| —      | Finding 3, docs                         | `doc/backlog.md`, `CLAUDE.md`, this file                                                                           | done   |

## Acceptance

- Handler stall count per day on mcapp.local drops well below the ~40/day baseline after one
  day of soak; `handler` `sample` rows exist and give a p50.
- `/var/lib/mcapp/messages.db-wal` is non-zero between writes while mcapp runs (checkpoint no
  longer on every close).
- No `loop_lag` row whose stack is `format_sse_event` / `json.dumps` after a 7-day mheard dump.

## Advisor pass (2026-09-18, before commit)

Verdict REWORK, applied by the orchestrator, then re-gated green:

- Handler sampling gets its own rate, `handler_sample_every` (default 500). `time_handler` wraps
  every subscriber of every publish, ~80k timings/day on mcapp.local; at the http rate of 1-in-50
  the samples would have evicted the stall rows from the 5000-row ring within ~3 days.
- The persistent writer drops and reopens itself on `ProgrammingError`/`InterfaceError` or a
  failing rollback, and does not leak a half-opened handle if the pragma fails.
- `_shutdown_services` now awaits `storage_handler.close()` so the last-connection checkpoint runs
  on a graceful stop.
- The concurrent-mutate lifecycle test is documented as smoke only: CPython's sqlite3 is built in
  serialized mode, so it cannot discriminate the lock.
- The SSE thread-identity spy records only calls for its own payload.

Refuted by the advisor and not to be re-investigated: rollback semantics vs the old `db_write`,
DDL through the writer (`VACUUM`/`ANALYZE` only, both outside a transaction), migration
interaction (lazy open), WAL auto-checkpoint from the writer's own commits (verified by probe),
byte identity of the offloaded SSE path, and progress/response ordering for the mheard dump.
