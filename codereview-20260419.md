# Code Review — MCProxy

**Date:** 2026-04-19
**Scope:** `src/mcapp/` full tree, focused on the five dimensions below
**Method:** 4 parallel audit agents (DRY+magic-numbers, complexity, race conditions, type safety) + human verification of cited lines against current code
**Prior review:** `codereview-20260417.md` — deferred items (§3.1–3.4 DRY, §4.4 SELECT-then-UPDATE) are still open and not re-covered here

---

## TL;DR

- **One real concurrency bug** worth fixing: `update_stats` srcs-list clobber (§3.1). Rest of the race-condition candidates resolve to "narrow window" or "not a race."
- **Classifier module** (new since 04-17) is tightly written. Biggest gap is **untyped dict payloads** crossing module boundaries, especially for SSE events and router messages.
- **`store_message` in `sqlite_storage.py`** is 273 lines and branches by message type — largest remaining complexity hotspot.
- **Magic numbers are mostly named** in the classifier (good). The exceptions: the 60 s stats broadcast interval, 12-char template-hash length, 500-row reclassify batch.
- **Prior review's §3.1–3.4 and §4.4 remain open** — DRY duplication in `main.py` router handlers and in BLE client variants, plus SELECT-then-UPDATE in storage.

---

## 1. DRY Violations

### 1.1 — Classifier stats time windows, inlined (MEDIUM)
`classifier/classify.py:279–281` computes `cutoff_30d`, `cutoff_24h`, `cutoff_7d` with inline `30 * 24 * 60 * 60 * 1000` arithmetic. The same 24 h window is also hard-coded as `AUTO_BEACON_WINDOW_SEC = 24 * 60 * 60` in `template.py:18`. Extract shared `STATS_WINDOW_*_MS` constants (module-level in `classify.py`) so tuning has one home.

### 1.2 — Classifier-meta table access duplicated (LOW-MEDIUM)
`classify.py:339–349` (`_read_version`) and the one-shot backfill gate in `main.py:1440–1450` both SELECT from `classifier_meta` with fallback handling but no shared helper. A helper like `storage.get_meta(key, default)` would collapse them.

### 1.3 — Background-task shutdown boilerplate (LOW)
`main.py:1402–1418` (`_nightly_prune`) and `main.py:1473–1487` (`_classifier_stats_broadcast`) repeat the same `while not stop.is_set(): asyncio.wait_for(stop.wait(), timeout=…); work()` pattern. A `_run_periodic(stop_event, interval, work)` helper would kill the duplication.

### 1.4 — Prior-review §3.1–3.4 still deferred
- §3.1 three-copy `progress_callback` closures for mheard dumps (`main.py:468–483, 505–520, 537–552`) — **unchanged**
- §3.2 six copies of the `if websocket: direct else: broadcast` branch in command handlers — **unchanged**
- §3.3 duplicated `_publish_status` between `ble_client_remote.py` and `ble_client_disabled.py` — **unchanged**
- §3.4 try/except/publish boilerplate across BLE ops — **unchanged**

---

## 2. Complexity Hotspots

### 2.1 — `sqlite_storage.store_message()` is 273 lines (HIGH)
`sqlite_storage.py:1053–1325`. Eight distinct message-type paths (`tele`, `ack`, `msg`, `pos`, `mheard`) interleave in one coroutine; classifier is called inline in the INSERT path rather than upstream of dispatch. **Sketch:** type-dispatch at the top → `_store_tele`, `_store_ack`, `_store_pos`, `_store_mheard`, `_store_msg`. Each sub-function becomes 40–60 lines with a single responsibility. This is where future bugs will surface.

### 2.2 — `_upsert_station_position()` embeds a 62-line SQL CASE tree (MEDIUM)
`sqlite_storage.py:922–1013`. The "prefer non-null / non-empty / shorter path" merge rule is re-expressed for ~10 fields in nested SQL `CASE WHEN`. Moving the merge into a Python field-rules dict (`{"speed_kmh": "prefer_new_if_nonzero", ...}`) would make semantics explicit and shrink the SQL to one simple template.

### 2.3 — `main.py:main()` is 363 lines (MEDIUM)
`main.py:1143–1506` initializes six subsystems, wires three inline subscription closures, spawns three background tasks, and has a 150-line shutdown sequence. Dependencies between subsystems are implicit in ordering. Extract `_init_classifier() / _init_ble() / _init_sse()` + move closures into a `_wiring.py` module; consider a small `ServiceManager` for shutdown. Not urgent, but the shutdown path is where race bugs hide.

### 2.4 — `ctcping.py:_monitor_test_completion()` is a polling state machine (MEDIUM)
`commands/ctcping.py:534–564` busy-polls `test_summary["completed"] + test_summary["timeouts"] >= total_pings` every second up to 300 s. An `asyncio.Event` set by the ACK handler collapses this to `await asyncio.wait_for(event.wait(), timeout=300)` and removes the shared-dict timing race.

### 2.5 — `ble_client_remote._sse_loop()` layered exception handling (MEDIUM)
`ble_client_remote.py:451–530`. Three try/except layers + implicit state in `_status`. A disconnect notification can be silently skipped if state changes during the 2 s debounce window. Extract a `_check_and_notify_disconnect(pre_drop_addr)` helper and make the SSE state explicit (`CONNECTING / CONNECTED / RECONNECTING`).

### 2.6 — `store_telemetry` dynamic SQL merge (MEDIUM)
`sqlite_storage.py:1366–1500+`. Dedup/merge logic branches on QFE presence and builds `UPDATE ... SET …` strings from lists. Adding a new telemetry field means remembering to edit two arms. A `_merge_telemetry_records(old, new)` pure function plus a single conditional INSERT/UPDATE/DELETE at the end is much safer.

---

## 3. Race Conditions

### 3.1 — `update_stats` clobbers srcs list under concurrent classify (MEDIUM, new)
`classifier/template.py:86–109`. The INSERT-ON-CONFLICT UPSERT on line 71 is atomic, but the **following SELECT (line 86) + in-Python dedupe + UPDATE srcs (line 105)** is not. Two concurrent `classify()` calls for the same `template_hash`:

1. A: UPSERT → count=N+1
2. B: UPSERT → count=N+2
3. A: SELECT srcs → [..., x_a]
4. B: SELECT srcs → [..., x_a, x_b] (B sees A's addition)
5. A: UPDATE srcs = [..., x_a] (clobbers B's x_b)
6. B: UPDATE srcs = [..., x_a, x_b]

The final-write-wins semantics of step 5→6 can drop an `src` entry if A runs after B. `_execute()` uses `asyncio.to_thread` with fresh SQLite connections per call (`sqlite_storage.py:1022–1031`), so interleaving is real, not GIL-protected. **Fix:** collapse the three statements to a single `INSERT … ON CONFLICT … SET srcs = json(...)` that computes the deduped list in SQL, or serialize via `async with self._lock:` at the classifier level.

### 3.2 — `auto_beacon_status` SELECT-then-UPDATE on the transition flip (MEDIUM)
`classifier/template.py:162–174`. SELECT `auto_beacon` → if 0, UPDATE to 1 and return `just_crossed=True`. Two concurrent callers can both see 0 and both return `just_crossed=True`, causing **duplicate `proxy:classifier_template_event` SSE events**. Not a crash; visible in the UI as duplicate auto-beacon promotion. **Fix:** `UPDATE beacon_templates SET auto_beacon = 1 WHERE template_hash = ? AND auto_beacon = 0` then check affected rowcount — only the winning writer returns `just_crossed=True`.

### 3.3 — Update-runner trigger file TOCTOU (MEDIUM)
`sse_handler.py:1010–1031`. Port-2985 check → write `args_file` → write `trigger_file` is not atomic. Two concurrent update POSTs both pass the port check and race to write the args file; the systemd runner may read whichever arrived second. **Fix:** write args to a uniquely-named temp path, then `os.rename` into place only after acquiring an `asyncio.Lock` shared by the endpoint.

### 3.4 — Stats broadcaster reads mid-flight UPSERT state (LOW)
`main.py:1473–1487` 60 s broadcaster vs `classify()`'s three-step UPSERT. Window is a few ms per message; UI sees stale `auto_beacon` at most until the next tick. Noting for completeness, not urgent.

### Ruled out (verified against current code, not reported as bugs)
- **Dedup cleanup `KeyError`** — `_cleanup_msg_id_cache` in `commands/dedup.py:164–169` has no `await` between the list comprehension and the `del` loop. The hot path is also all in the event loop. Single-threaded asyncio + GIL makes the claimed race impossible.
- **Reclassify `job.processed` torn read** — same reason: both writer and reader are in the event loop on the same thread; `x += 1` on a Python int is not torn under a single thread.
- **Classifier backfill crash-mid-way** — the `classifier_ver` marker is the version key itself, not a "done" flag; a crashed backfill just leaves the next startup to resume from the beginning. This is the intended semantics, not a bug.
- **SSE broadcast exception handling** — §4.5 fix from 04-17 still holds.
- **SQLite direct-access bypass of `_execute`** — grep for `sqlite3.connect` shows all paths route through `_execute()` or `_ensure_read_conn()`.

---

## 4. Type Safety

### 4.1 — Routed message envelope is untyped (HIGH)
`main.py:216–224` creates the pub/sub envelope `{source, type, data, timestamp}` as a bare `dict`. All subscribers (`main.py:185, 903, 989`, `sse_handler.py:1156`) access by string key with no `TypedDict`. A typo in a handler binds silently. **Fix:** define `class RoutedMessage(TypedDict)` in a shared module and annotate `publish()` + every subscriber.

### 4.2 — Message schema implicit across the ingestion boundary (HIGH)
`sqlite_storage.store_message(message: dict[str, Any], raw: str)` and `Classifier.classify(msg: dict[str, Any])` both unpack via `.get()` without a schema. A field rename (`"msg"` → `"text"`) in the UDP/BLE path would silently break both. **Fix:** a `TypedDict` for the inbound message shape used everywhere from the UDP/BLE handlers onward.

### 4.3 — Classifier callback event shapes are undocumented (HIGH)
`classify.py:31–32, 141–144, 265–270`. `on_template_event` and `on_reclassify_progress` are typed `Callable[[dict[str, Any]], Awaitable[None]]`. SSE consumers subscribe and index keys (`event["total"]`) without validation. A schema change in the classifier would crash the SSE event generator. **Fix:** two `TypedDict`s (`TemplateEvent`, `ReclassifyProgressEvent`) — even without mypy, this documents the contract.

### 4.4 — Timestamp units implicit (MEDIUM)
Functions accept `now_ms: int` by convention but nothing stops a caller passing seconds (`classify.py:107`, `template.py:123, 147`, `sqlite_storage.py:1073`). The CLAUDE.md gotcha ("all DB timestamps are ms") is project lore, not a type. **Fix:** `Milliseconds = NewType("Milliseconds", int)` in a shared module; pay the one-time annotation cost; catch year-58089 bugs at review time.

### 4.5 — `storage: Any` / `message_router: Any` in core constructors (LOW-MEDIUM)
`classify.py:67` (`Classifier.__init__`), `sse_handler.py:99–105` (`SSEManager`). Core dependencies declared as `Any` erase autocompletion and mask typos in `.storage._execute` / `.message_router.publish`. Using concrete classes (`SQLiteStorage`, `MessageRouter`) is a one-line change each.

### 4.6 — SSE event payloads are ad-hoc (MEDIUM)
`sse_handler.py:174–181, 1147–1190`. Each event has its own implicit shape; `broadcast_message` even spreads unknown `**data`. Cross-process boundary with the webapp — any refactor that drops a field is silently breaking. **Fix:** consolidate SSE event schemas under one `SSEEvent` discriminated union or a typed registry.

---

## 5. Magic Numbers

Classifier constants are mostly well-named (`AUTO_BEACON_THRESHOLD`, `AUTO_BEACON_WINDOW_SEC`, `SRCS_CAP`, score weights in `score.py:15–24`). What's missing:

| Where | Literal | Meaning | Severity |
|---|---|---|---|
| `classify.py:279–281` | `30/24/7 * 24 * 60 * 60 * 1000` | stats windows | MEDIUM |
| `main.py:1485` | `timeout=60.0` | stats broadcast interval | MEDIUM |
| `template.py:48` + `tests.py:68, 410, 429` | `[:12]` / `== 12` | template hash length | MEDIUM |
| `classify.py:215` | `500` | reclassify batch size | LOW |
| `main.py:1514, 1526, 1531, 1539, 1547` | `5.0` / `3.0` | shutdown timeouts | LOW |

Recommended: a `classifier/constants.py` (or just a section at the top of `classify.py`) for `STATS_WINDOW_*_MS`, `STATS_BROADCAST_INTERVAL_SEC`, `TEMPLATE_HASH_LENGTH`, `RECLASSIFY_BATCH_SIZE`. Shutdown timeouts probably want their own block in `main.py`.

---

## 6. Suggested Priority

If only a small chunk of time is available, do these first:

1. **§3.1 `update_stats` srcs clobber** — real bug, 20-minute fix, single atomic statement.
2. **§3.2 `auto_beacon_status` transition race** — adds `AND auto_beacon = 0` to the UPDATE and reads `rowcount`. 10 minutes.
3. **§4.1 + §4.3 TypedDicts for router messages and classifier events** — the two changes that will pay off most for future refactoring. ~1 hour.
4. **§5 extract classifier constants** — trivial, reduces the magic-numbers surface substantially.

Deferred-but-worth-doing:

5. **§2.1 split `store_message`** — biggest maintenance-burden file. Focused PR, possibly combined with prior-review §4.4.
6. **Prior-review §3.1, §3.2** — router-handler DRY. Still the cheapest way to kill six duplications.

---

## 7. Verification

- `uvx ruff check` → passes (spot-checked)
- All file:line citations verified against current `development` HEAD (`1db0b17`)
- Agents were fed `codereview-20260417.md` as context; any overlap with prior review is intentional ("still deferred")
