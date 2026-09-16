# Campaign: stall findings F1-F5 + popover BUG-1..4 (2026-09-16)

Resume point for the orchestrated fix campaign over
`doc/2026-09-16_0800-stall-data-report.md` and
`doc/2026-09-16_0807-message-detail-popover-bugfix-report.md`. Decisions taken with DK5EN
before dispatch: all of F1-F5; webapp BUG-1..4 plus the MCProxy sentinel normalization;
firmware `hw_id` on text frames deferred; BUG-3 removes the `MOD` token unconditionally.

## F1 verification (orchestrator, 2026-09-16 08:30-09:00)

The report's hypothesis ("~69 sequential DB calls, each opening a connection") is **false**.

- Replaying all 58 recorded `handler` rows through `store_message` with `_query`/`_mutate`
  counted: **2-6 DB calls per message**, never more. Locally (empty DB, macOS) 2-5 ms each.
- On mcapp.local against a copy of the live 40 MB DB on the SD card (ext4, not tmpfs):
  the MHeard path (1 SELECT + 3 writes) takes 86 ms typical, up to 913 ms; the position
  path (1 SELECT + 1 write) 14 ms.
- Split per write on the SD card (`signal_log` INSERT, `synchronous=FULL`, default): open
  0.6 ms, first statement 5 ms (schema parse on a fresh connection), **commit 15 ms typical,
  max 1710 ms**, close 5 ms. With `synchronous=NORMAL` on a persistent connection the same
  insert+commit is **0.3 ms**.
- Cause: every `_mutate` commits, and in WAL mode with `synchronous=FULL` every commit fsyncs
  the WAL on the SD card. SD cards have multi-hundred-millisecond write stalls; three writes
  per MHeard frame are three chances to hit one. The 5 ms schema parse per fresh connection
  is real but small (3-6 calls per message).
- Fix shape: `PRAGMA synchronous=NORMAL` on every write connection (`db_write`, the stall
  writer thread). WAL + NORMAL is crash-safe (no corruption); it can lose the last few
  transactions on power loss, which is acceptable for a message proxy. Per-message
  connection threading is NOT needed and is dropped from scope.

## Wave map

| Wave | Agent | Repo    | Item                              | Exclusive files                                                     | Status |
| ---- | ----- | ------- | --------------------------------- | ------------------------------------------------------------------- | ------ |
| 1    | A     | MCProxy | F1 synchronous=NORMAL             | storage/constants.py, storage/connection_lifecycle_tests.py         | done   |
| 1    | B     | MCProxy | F4 loop-lag stack + writer pragma | stalls.py, stall_tests.py                                           | done   |
| 1    | C     | MCProxy | F2 weather cache, F3 timezone     | meteo.py, sse_routes/weather.py, meteo_tests.py                     | done   |
| 1    | D     | MCProxy | BUG-1 sentinel normalization      | storage/ingest.py, storage/ingest_dedup_tests.py                    | done   |
| 1    | E     | webapp  | BUG-1/3/4 render                  | ChatBubble.vue, positionHelpers.ts, ChatBubble.spec.ts              | done   |
| 1    | F     | webapp  | BUG-2 live-duplicate merge        | stores/messages.ts, stores/**tests**/messages.enrich.spec.ts        | done   |
| 1    | G     | webapp  | F5 heartbeat reporter noise       | services/stallReporter.ts, services/**tests**/stallReporter.spec.ts | done   |

Hotspots (orchestrator only): `scripts/run_startup_tests.py`, `main.py`, `sse_handler.py`,
CLAUDE.md, this file, both reports. F3b (telemetry query): documented only, the composite
index already serves it. Orchestrator edits: `isSignalSentinel` in the webapp's
`positionHelpers.ts` (shared by E and F) and the `tz_warm_task` wiring in `main.py`.

## Log

- 08:15 docs committed and pushed (`5020c42`).
- 09:00 F1 verified on the Pi, hypothesis falsified, fix reshaped (above).
- 09:40 wave 1 landed (7 writers). Webapp gate green (213 files / 3322 tests); advisor
  APPROVED with two test-hygiene reworks (visibility stub restore under happy-dom; wire-shaped
  firmware fixtures), applied. MCProxy gate green (all suites); advisor pass pending.
- Webapp advisor's optional note, deliberately not taken: a resident message whose FIRST copy
  carried the 0/0 sentinel keeps it until reload. The backend normalization in the same release
  stops the sentinel reaching the client for new rows, so the client-side special case would
  only cover rows that predate this deploy.
- Deviation from the report: F2's stale-while-revalidate covers the good-data path only; an
  expired ERROR cache still refetches synchronously, because `commands/tests.py`
  `test_meteo_negative_cache` pins that and errors are not the stall.
- useConnectionManager.ts unchanged: `handleVisibilityResume` already reconnects immediately
  on resume to visible (`reconnectNow()`); the F5 rows fire while still hidden.
