# ADR-0007: Stall capture: pure ASGI middleware and an own writer thread

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-15 |
| Deciders | Martin Werner |
| Affects | `src/mcapp/stall_middleware.py`, `src/mcapp/stalls.py`, `src/mcapp/sse_routes/stalls.py`, `sse_handler.py` (`_create_app`) |
| Supersedes | - |
| Superseded by | - |

## Context

The webapp showed stalls that the backend could not explain: slow HTTP calls, SSE round trips that
never answered, event-loop drift and handlers that held the loop. Logs did not carry enough to
reproduce them. The first data (2026-09-16) showed handler stalls caused by per-call write
connections, and the stall logger itself must not stall when the thread pool is starved.

## Decision

1. **Pure ASGI middleware.** `StallMiddleware` taps `receive` and `send` chunk by chunk. It is not
   `BaseHTTPMiddleware`, which buffers the response and would turn `/events` and `/update-stream` into one
   slow request. `/events*`, `/update-stream` and `/health` pass through untouched.
2. **Outermost layer.** It is added after `CORSMiddleware`, so it times the whole stack.
3. **Own writer thread.** `record()` and `ingest_client()` push onto a bounded queue (1000) and
   return. A dedicated thread with its own SQLite connection writes. A full queue drops the row and
   counts the drop. The recorder never uses the shared executor.
4. **Counting executor installed first.** `install_executor()` runs before any `to_thread` call, so
   `pool_wait` sees every job.
5. **Thresholds.** Stall at 0.5 s, critical at 2 s, plus a 1-in-50 sample of every other call.
   `loop_lag` and `pool_wait` use 100 ms and 500 ms, because they measure infrastructure.
6. **Redaction replaces values, not keys**, for push endpoints and `*api_key*` fields. Message text is
   kept on purpose. The body of the credentials route is withheld entirely.

## Consequences

- Stall reporting keeps working when the pool or the loop is busy, which is when it is needed.
- `stall_events` grows. Rows are capped by `max_rows` (default 5000) and pruned every 200 inserts,
  not by the nightly job.
- The lag sampler (a daemon thread reading the loop thread's frame) gives the blocker's stack
  without running on the loop.
- Use the tool through `../sop/SOP-006-stall-investigation.md`.

## Alternatives considered

- `BaseHTTPMiddleware`: rejected, buffers SSE.
- Logging to the journal only: rejected, no correlation id and no replay.
- Writing from the shared executor: rejected, a starved pool would stop the logger.

## Evidence

- `../archive/2026-09-15_1530-stall-tracking-plan.md`
- `../archive/2026-09-16_0900-stall-popover-campaign.md`
- `../archive/2026-09-18_2200-stall-followup-plan.md` (checkpoint-per-write finding)
- `../reference/database-reference.md` (table `stall_events`)
