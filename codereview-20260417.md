# Code Review — MCProxy

**Date:** 2026-04-17
**Scope:** `src/mcapp/` full tree + `ble_service/` + dependencies
**Reviewers:** 4 parallel audit agents (DRY, bugs, efficiency, CVE) + human verification of cited lines

---

## TL;DR

- **No known CVEs** in the resolved dependency tree (28 packages, verified against PyPI advisory DB + OSV). `uv lock --upgrade` applied — minor version bumps only.
- **No critical bugs.** A handful of medium bugs (control-flow, unbounded caches, blocking I/O on the event loop) worth fixing on the next cleanup pass.
- **Real duplication hotspots** are all in `main.py` (MessageRouter command handlers) and in the BLE client variants — both are extraction candidates.
- **Pi-specific efficiency concerns**: `TimezoneFinder` re-instantiation per request, unbounded dedup caches, per-client JSON serialization on SSE broadcast.

The codebase is in good shape overall. The Priority 1/2/3 refactor commits have paid off — `print()`→logger migration is mostly complete, typed SSE events landed, BLE disconnect debounce and suppression extraction tightened the router. Remaining issues are second-order.

---

## 1. Dependencies & CVEs

### Result
**Zero known vulnerabilities** across direct and transitive deps, verified via `pip-audit` against both PyPI advisory DB and Google OSV.

### Updates applied
`uv lock --upgrade` refreshed the lockfile:

| package | from | to |
|---|---|---|
| fastapi | 0.135.3 | 0.136.0 |
| pydantic | 2.12.5 | 2.13.2 |
| pydantic-core | 2.41.5 | 2.46.2 |
| packaging | 26.0 | 26.1 |
| ruff (dev) | 0.15.9 | 0.15.11 |

All others already at latest (httpx 0.28.1, uvicorn 0.44.0, starlette 1.0.0, anyio 4.13.0, h11 0.16.0, certifi 2026.2.25, idna 3.11, sse-starlette 3.3.4, timezonefinder 8.2.2).

### Notes on the working tree
The pre-existing uncommitted `pyproject.toml` diff (dropping `aiohttp`/`aiohttp-sse-client`/`requests` in favor of `httpx`) was already present when this review started — not part of this audit. It reduces `uv.lock` from 1147 → 222 lines (~81% smaller). **Before committing that change, verify no code still imports `aiohttp` or `requests`** (`grep -r "import aiohttp\|import requests\|from aiohttp\|from requests" src/`).

### Verification
- `uvx ruff check` → **pass** ("All checks passed!")
- `uv sync` → clean
- `pip-audit` (PyPI + OSV) → clean

**Working tree left dirty as requested.** No commits made.

---

## 2. Bugs & Correctness

### 2.1 — `continue` nested inside `if has_console` in response handler

- **Severity:** medium
- **File:** `src/mcapp/commands/response.py:102-105`
- **Bug:** The `continue` after a failed BLE send is inside the `if has_console:` branch. When console is disabled (production Pi without TTY attached), exception is swallowed but loop falls through to `asyncio.sleep(12)` for a chunk that failed. Behavior silently differs between dev and prod.
- **Fix:** Move `continue` out of the `if has_console` block:
  ```python
  except Exception as ble_error:
      if has_console:
          print(f"⚠️  CommandHandler: send failed to {recipient}: {ble_error}")
      logger.warning("CommandHandler: send failed to %s: %s", recipient, ble_error)
      continue
  ```

### 2.2 — Unbounded dedup/throttle dicts

- **Severity:** medium
- **File:** `src/mcapp/commands/dedup.py:15-28` (module-level dicts)
- **Bug:** `processed_msg_ids`, `command_throttle`, `failed_attempts`, `blocked_users`, `block_notifications_sent` only grow. Cleanup is lazy (runs per check) and stops running during quiet periods, leaving stale entries until the next message.
- **Impact on Pi Zero 2W (512 MB RAM):** 50k stale entries × ~100 bytes each = ~5 MB per namespace leaked over long uptime windows.
- **Fix:** Add a periodic background task (e.g., every hour) that calls the cleanup functions regardless of incoming traffic. Or use `cachetools.TTLCache` to eliminate manual cleanup entirely.

### 2.3 — Unbounded `cached_ble_registers`

- **Severity:** medium
- **File:** `src/mcapp/main.py` (around register caching — verify current line; agent cited 1162/1169)
- **Bug:** BLE registers dict only clears on disconnect. A long-lived connection with many register-type updates (TYP=`I`/`SN`/`G`/…) grows without bound.
- **Fix:** Cap size (e.g., 50 entries with `OrderedDict.popitem(last=False)` when exceeded) or key the dict on the finite set of known register types so updates are replacements, not appends.

### 2.4 — f-string SQL with hardcoded values (style, not injection)

- **Severity:** low
- **Files:** `src/mcapp/sqlite_storage.py:1561-1563` (size-based pruning), `1613`, `1623` (bucket aggregation)
- **Finding:** The bug agent flagged these as SQL injection. **They are not** — table names come from a hardcoded tuple (`line 1550-1556`), and `bucket_5min_ms = BUCKET_SECONDS * 1000` is a constant. But: SQLite does not parameterize table names, so f-strings are the only option there. For `bucket_5min_ms` (line 1613, 1623), switch to `?` parameters — cheap and removes the pattern.
- **Fix:** Parameterize `bucket_5min_ms`. Leave table-name f-strings with a comment noting the allowlist.

### 2.5 — Blocking file I/O on the event loop

- **Severity:** low-medium
- **Files:**
  - `src/mcapp/main.py` — startup `Path.exists()`/`Path.rename()` for dump handling (~line 1145)
  - `src/mcapp/sse_handler.py:729-730, 766` — `.write_text()` / `.read_text()` in async route handlers (update endpoints)
- **Bug:** Sync file I/O called from async context. On µSD storage the block can be 10–100 ms.
- **Fix:** Wrap in `await asyncio.to_thread(...)`.

### 2.6 — SSE generator cleanup under shutdown

- **Severity:** low
- **File:** `src/mcapp/sse_handler.py:305-340`
- **Bug:** `event_generator()`'s `while client.connected:` has no secondary shutdown guard; if `stop_server()` is called mid-generator, clients may leak through the teardown path.
- **Fix:** Add a shared shutdown `asyncio.Event` that the generator also checks. `queue.get()` already has a 30 s timeout, so worst-case leak is bounded — this is a polish fix.

---

## 3. DRY Violations & Duplication

### 3.1 — Three near-identical `progress_callback` closures for mheard dumps (HIGH)

- **Files:** `src/mcapp/main.py:465-481`, `502-518`, `537-553`
- **What:** `_handle_mheard_dump_command`, `_handle_mheard_dump_monthly_command`, `_handle_mheard_dump_yearly_command` each define a nested `progress_callback` that builds the same `progress_msg` dict.
- **Risk:** Schema drift. Adding a field to the progress payload means touching three locations; forgetting one is a silent bug.
- **Fix:** Extract `MessageRouter._build_progress_message(stage, detail, callsign=None)` once and pass it in.

### 3.2 — Four copies of the "websocket? direct : broadcast" branch (HIGH)

- **Files:** `src/mcapp/main.py:366-389`, `400-406`, `417-424`, and in six handler methods at `366`, `433`, `455`, `491`, `527`, `562`
- **What:** Every command handler ends with:
  ```python
  if websocket:
      await self.publish('router', 'websocket_direct', {'websocket': websocket, 'data': payload})
  else:
      await self.publish('router', 'websocket_message', payload)
  ```
- **Risk:** Any change to routing (rate limiting, logging, priority queue) needs six edits.
- **Fix:** `_send_payload(payload, websocket=None)` on `MessageRouter`. Every handler becomes one call.

### 3.3 — `_publish_status` duplicated between BLE client variants (MEDIUM)

- **Files:** `src/mcapp/ble_client_remote.py:148-158`, `src/mcapp/ble_client_disabled.py:37-47`
- **What:** Both define the same method; only the `TYP` string differs (`blueZ` vs `disabled`).
- **Fix:** Concrete method on `BLEClientBase`; subclasses override a `TYP` class attribute.

### 3.4 — BLE operation try/except/publish boilerplate (MEDIUM)

- **File:** `src/mcapp/ble_client_remote.py` — `scan`, `pair`, `unpair`, `connect`, etc. (lines 313, 319, 335, 341, …)
- **What:** Each op repeats:
  ```python
  try:
      await self._publish_status('<op>', 'info', ...)
      response = await self._request(...)
      ...
      await self._publish_status('<op> result', 'ok'|'error', ...)
      return success
  except Exception as e:
      await self._publish_status('<op> result', 'error', str(e))
      return False
  ```
- **Fix:** `_execute_ble_operation(op_name, request_coro)` wraps the try/publish/error pattern. Each op shrinks to 3 lines.

### Not duplication (don't extract)

- **`publish_ble_status` / `publish_system_message` / `publish_error`** in `main.py` look parallel but are semantically distinct (different `type` field, different routing). Leave them.
- **`_request` retry logic** in `ble_client_remote.py` is already centralized — individual ops correctly delegate. This is the *right* pattern.
- **Normalization called from both UDP and BLE handlers** — both genuinely need it; don't merge the handlers.

---

## 4. Efficiency (Pi Zero 2W)

### 4.1 — `TimezoneFinder()` instantiated per request (HIGH)

- **File:** `src/mcapp/sse_handler.py:656` (inside `/api/timezone`)
- **Problem:** `TimezoneFinder()` loads a ~100 KB geo dataset on every request. ~100–200 ms latency on Pi storage per call + GC churn.
- **Fix:** Module-level singleton: `_TF = TimezoneFinder()` at import; reuse. Also used by `meteo.py` — share the same singleton.

### 4.2 — SSE broadcast serializes the same payload per client (MEDIUM)

- **File:** `src/mcapp/sse_handler.py:313, 316, 851`
- **Problem:** Each client's `event_generator` calls `_format_sse_event(data, ...)` → `json.dumps(data)`. With N subscribers, payload is JSON-encoded N times.
- **Fix:** Format once in `broadcast_message` and push the string into client queues, or cache the serialized form on the routed message and let generators reuse it.

### 4.3 — `print()` and `has_console` debug in dedup hot path (MEDIUM)

- **File:** `src/mcapp/commands/dedup.py:54-55`, `135-164`
- **Problem:** Per-message debug prints during throttle checks and cleanup. Conditional on `has_console`, which is truthy in production on the Pi.
- **Fix:** Switch to `logger.debug()` — respects log level, zero cost when disabled. Matches the Priority 1 migration already started.

### 4.4 — SELECT-then-UPDATE pattern in message storage (LOW-MEDIUM)

- **File:** `src/mcapp/sqlite_storage.py:1051-1057`, `1102-1109`, `1176-1183`, `1196-1202`
- **Problem:** Every message stored triggers 2–3 separate `_execute()` calls. At peak mesh traffic, context-switch and thread-pool overhead add up on a single-core Pi.
- **Fix:** Combine into `INSERT ... ON CONFLICT DO UPDATE` or `UPDATE ... WHERE id = (SELECT ...)` where applicable.

### 4.5 — Sequential fan-out in `broadcast_message` (LOW)

- **File:** `src/mcapp/sse_handler.py:883-887`
- **Problem:** Clients iterated sequentially; a slow `client.send()` delays all others.
- **Fix:** `await asyncio.gather(*[c.send(m) for c in clients], return_exceptions=True)` once per broadcast.

---

## 5. Recommended Action Order

Rough priority (high value, low risk first):

1. **Ship CVE/lockfile refresh** — `uv.lock` already regenerated, ruff clean. Verify no dangling `aiohttp`/`requests` imports (see §1), then commit.
2. **§2.1** — `response.py` `continue` placement. One-line fix, silent prod bug.
3. **§4.1** — `TimezoneFinder` singleton. One-line fix, big latency win.
4. **§4.3** — Dedup `print()` → `logger.debug`. Continues the Priority 1 migration.
5. **§3.1 + §3.2** — Extract `_build_progress_message` and `_send_payload` in `MessageRouter`. ~30-minute refactor, kills six duplications.
6. **§2.2** — Periodic dedup cleanup task. Protects long-uptime deployments.
7. **§3.3 + §3.4** — BLE client consolidation. Bigger refactor; pairs well with §2.3 cache cap.
8. **§4.2** — SSE serialize-once. Worth doing if client counts grow.
9. **§2.5, §4.4, §4.5** — Polish pass.

All fixes are compatible with the current architecture; none require schema or API changes.
