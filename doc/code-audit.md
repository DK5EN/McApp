# Code Audit — McApp Backend + Webapp Frontend

**Date:** 2026-04-03
**Scope:** `/Users/martinwerner/WebDev/MCProxy` (backend) + `/Users/martinwerner/WebDev/webapp` (frontend)
**Goal:** Identify feature drift, dead code, inconsistencies, pattern issues, UI polish gaps, and dependency concerns across both codebases.

---

## Table of Contents

1. [Feature Drift — Frontend vs Backend](#1-feature-drift)
2. [Dead & Unused Code](#2-dead--unused-code)
3. [Inconsistencies & Mismatches](#3-inconsistencies--mismatches)
4. [Solution Pattern Problems](#4-solution-pattern-problems)
5. [BLE Feature Audit](#5-ble-feature-audit)
6. [UI Polish & Visual Consistency](#6-ui-polish--visual-consistency)
7. [Dependencies & Bundle](#7-dependencies--bundle)
8. [Consolidated TODO List](#8-consolidated-todo-list)

---

## 1. Feature Drift

### 1.1 Backend Endpoints Without Frontend Consumer

| Endpoint | Backend Location | Issue |
|----------|-----------------|-------|
| `GET /api/status` | `sse_handler.py:399` | Returns version, client count, uptime. Never called by frontend. |
| `GET /api/update/check` | `sse_handler.py:675` | Version check with caching. Frontend uses GitHub API directly instead (`useVersionCheck.ts:68`). |
| `GET /api/blocked_texts` | `sse_handler.py:472` | REST GET exists, but frontend only receives data via SSE `proxy:blocked_texts` event. |
| `GET /api/read_counts` | `sse_handler.py:413` | Same pattern — REST GET exists, frontend only uses SSE delivery. |

**TODO-A1:** Remove `GET /api/status` or wire it into the frontend status bar.
**TODO-A2:** Remove `GET /api/update/check` — frontend already hits GitHub directly.
**TODO-A3:** Decide: keep REST GET endpoints for `blocked_texts` and `read_counts` as fallback, or remove them. If kept, document why both SSE and REST exist.

### 1.2 Dual-Mode POST Endpoints (Unused Modes)

`POST /api/hidden_destinations` and `POST /api/blocked_texts` each support both single-item and bulk modes. Frontend only uses one mode per endpoint:

| Endpoint | Backend Supports | Frontend Uses |
|----------|-----------------|---------------|
| `/api/hidden_destinations` | single `{dst, hidden}` + bulk `{destinations: [...]}` | bulk only |
| `/api/blocked_texts` | single `{text, blocked}` + bulk `{texts: [...]}` | single only |

**TODO-A4:** Consolidate to one mode per endpoint. Remove untested code paths.

### 1.3 Topic Beacon — Backend Only, No Frontend UI

`!topic` command exists in backend (`commands/topic_beacon.py`) with full beacon management (create, delete, intervals). No frontend UI for managing topics.

**TODO-A5:** Leave as-is (intentionally disabled per Martin — misuse risk). Add comment in `topic_beacon.py` explaining why no UI exists.

### 1.4 MHeard Beacons — Backend Parses, Frontend Ignores

Backend `ble_protocol.py` has `transform_mh()` that parses MHeard beacon data from BLE. This data is not surfaced in the frontend BLE components.

**TODO-A6:** Evaluate whether MHeard beacon data from BLE should appear in the BLE activity log or map.

---

## 2. Dead & Unused Code

### 2.1 Frontend — Dead Files

| File | Issue |
|------|-------|
| `composables/useMessageRouter.ts` | Defined but **never imported** anywhere. The routing it implements has been absorbed into the message store's event bus listeners. Only reference is its own export. |

**TODO-B1:** Delete `useMessageRouter.ts`.

### 2.2 Frontend — Unused Event Types

`events/eventTypes.ts:25-26` defines `system:connected` and `system:ping`. These are registered in the SSE listener array (`useSSEClient.ts:256`) so they get routed, but no store or component ever listens for them — the events arrive and are silently discarded.

**TODO-B2:** Either add consumers (e.g., display connection ID, use ping for latency indicator) or remove from `EventMap` and the listener registration.

### 2.3 Frontend — Orphaned Property `receivedData`

`userSettings.ts` type includes `receivedData`. It's written to in `useMessageRouter.ts` (which is dead code) and `useConnectionManager.ts`, but never read or displayed.

**TODO-B3:** Remove `receivedData` from `UserAttributes` type and all write sites.

### 2.4 Frontend — V2 Component Naming Without V1

Nine components carry a `V2` suffix: `ChatBubbleV2`, `ChatContainerV2`, `ChatInputV2`, `LeftItemV2`, `PositionItemV2`, `SettingsCardV2`, `SettingsInputV2`, `SettingsToggleV2`, `GroupManagerV2`. No V1 versions exist in the codebase — the migration is complete, the names are just stale.

**TODO-B4:** Rename all V2 components to drop the suffix. Update all imports.

### 2.5 Backend — `.old/` Directory

`/.old/` contains `magicword.py`, `supervisor.py`, `daily_sqlite_dumper.py`. These are disabled legacy files.

**TODO-B5:** Delete `.old/` directory. History is in git.

### 2.6 Backend — Deprecated Config Fields

`config_loader.py:60-66` marks `latitude`/`longitude` as deprecated (GPS device provides them at runtime), but they're still loaded and passed through. The code works either way — GPS overrides them.

**TODO-B6:** Low priority. Document the fallback chain clearly: config values are used until first GPS fix, then GPS takes over.

---

## 3. Inconsistencies & Mismatches

### 3.1 SSE Event Naming — Legacy vs Typed

Backend sends some events as typed SSE events (with `event:` header) and others as legacy JSON blobs (with `type`/`msg` fields in `data:`). Frontend handles both via a fallback router (`useSSEClient.ts:125-147`), but this creates two parallel paths:

- **Typed path:** `proxy:initial`, `proxy:summary`, `proxy:read_counts`, etc. — clean, matches `EventMap`
- **Legacy path:** `messages_page`, `resolve_ip`, BLE status messages — routed by `routeLegacyMessage()`, which inspects `parsed.msg` or `parsed.command`

**TODO-C1:** Migrate all backend SSE emissions to use explicit `event:` types. Then remove `routeLegacyMessage()` fallback entirely.

### 3.2 Timestamp Format — Defensive Dual-Format Handling

Backend consistently uses milliseconds (`int(time.time() * 1000)`). But frontend has defensive normalization in multiple places:

- `utils/formatters.ts` — `timestamp > 10000000000 ? Math.floor(timestamp / 1000) : timestamp`
- `PositionsLeaflet.vue` — `data.timestamp > 1e12 ? data.timestamp : data.timestamp * 1000`

This suggests an older backend version sent seconds, and the frontend still guards against it.

**TODO-C2:** Verify no remaining source sends seconds. If confirmed, remove the dual-format guards.

### 3.3 Naming — `call_sign` vs `callsign` vs `my_callsign`

- Config file and `config_loader.py`: `CALL_SIGN` / `call_sign`
- `main.py` internal: `self.my_callsign`
- Frontend everywhere: `callsign`
- `ble_protocol.py`: `callsign`

Three different conventions for the same concept.

**TODO-C3:** Low priority cosmetic. If touching config_loader, standardize to `callsign`.

### 3.4 Version Check — Two Different Mechanisms

Backend has `GET /api/update/check` (checks GitHub, caches 5 min). Frontend has `useVersionCheck.ts` that also hits GitHub API directly. Two independent implementations of the same feature.

**TODO-C4:** Pick one. Either frontend calls the backend endpoint (simpler, single source of truth) or remove the backend endpoint (already covered by TODO-A2).

---

## 4. Solution Pattern Problems

### 4.1 Message Routing Complexity — The Big One

`main.py` lines ~68-1100 contain the `MessageRouter` class, which is the pub/sub hub. The problem is conditional path complexity:

**Inbound message flow (UDP):**
```
_udp_message_handler()
├─ normalize data
├─ check suppression (should_suppress_outbound)
│  ├─ SUPPRESS → synthetic message → CommandHandler
│  └─ FORWARD → continue
├─ check self-message (_handle_outgoing_message)
│  ├─ SELF → synthetic message → CommandHandler + return
│  └─ EXTERNAL → continue
├─ store in DB
├─ publish to subscribers
└─ forward to BLE (if connected)
```

**Issues identified:**
1. **47 `print()` statements** in `main.py` — debug output mixed with production logging. These are in the hot path.
2. **Suppression logic** (lines ~1181-1226) has 6 decision points with console output at every branch. Business logic and debug UI are entangled.
3. **Subscriber registration** (lines ~1270-1313) uses inline closures that capture `message_router` — makes testing and reasoning about side effects difficult.
4. **Command routing errors** are caught generically and published as websocket messages, but not logged (line ~334-343).

**TODO-D1:** Replace all `print()` in `main.py` with `logger.debug()`. This is the single highest-impact cleanup.
**TODO-D2:** Extract suppression logic into a standalone, testable function that returns a decision enum, without side effects.
**TODO-D3:** Document the message flow with a diagram (or update `doc/dataflow.md` if it's stale).

### 4.2 Three API Call Patterns in Frontend

Frontend has three distinct ways to communicate with the backend:

1. **SSE stream** via `useSSEClient` — for real-time events
2. **Direct `fetch()`** in composables — for weather, timezone, telemetry
3. **Send queue** via `sendQueueStore.enqueueMessage()` — for chat messages and commands

Each has its own error handling (or lack thereof). No shared API wrapper.

**TODO-D4:** Create a `useProxyAPI()` composable that wraps `fetch()` with consistent error handling, base URL construction, and optional retry logic. Migrate all direct `fetch()` calls to use it.

### 4.3 Error Handling — Backend Inconsistency

| Location | Pattern |
|----------|---------|
| `main.py:238-242` | Catches subscriber exception, logs, continues |
| `main.py:334-343` | Catches command error, publishes to websocket, no log |
| `commands/routing.py:129-135` | Catches generic Exception, maps to user-facing error |
| `commands/data_commands.py` | No exception handling — relies on caller |
| `sqlite_storage.py:614` | Silent `pass` on ALTER TABLE failure |
| `ble_protocol.py` | No try/except in transform functions |

No consistent error classification. Some errors are logged, some swallowed, some re-raised.

**TODO-D5:** Establish error handling conventions: (1) log all caught exceptions at WARNING or above, (2) never silently `pass`, (3) wrap transform functions that process external data.

### 4.4 Data Validation — Pydantic Used Sparingly

Only `SendMessageRequest` in `sse_handler.py:43-54` uses Pydantic. All other endpoints parse `request.json()` manually. Config uses plain `@dataclass`.

**TODO-D6:** Low priority. If adding new endpoints, use Pydantic models. No need to retrofit existing working code.

### 4.5 Event Bus — 27+ Event Types, No Documentation

The frontend event bus (`events/eventBus.ts`) routes 27+ event types. The type definitions in `eventTypes.ts` serve as the only documentation. There's no diagram showing which stores listen to which events.

**TODO-D7:** Add a comment block or table in `eventTypes.ts` mapping each event to its producer(s) and consumer(s).

### 4.6 Config Mutation at Runtime

`main.py:1308-1311` updates `CommandHandler.lat/lon` when GPS data arrives, bypassing the config system. Weather service location is updated in two places. Restarting loses the GPS override (falls back to config values until next GPS fix).

**TODO-D8:** Acceptable design if documented. Add comment explaining the GPS override lifecycle.

### 4.7 f-string SQL Construction

`sqlite_storage.py:504-519` uses f-string to inject `VALID_RSSI_RANGE` constants into SQL. Safe today (hardcoded tuple), but the pattern is a trap for future changes.

**TODO-D9:** Replace with parameterized query: `WHERE rssi BETWEEN ? AND ?` with `VALID_RSSI_RANGE` as params.

---

## 5. BLE Feature Audit

BLE is the main feature. Current state: single-device architecture, multi-node upcoming.

### 5.1 Single-Device Assumptions (Multi-Node Blockers)

| Location | Assumption |
|----------|-----------|
| `ble_client.py:98-100` | `is_connected` is a single boolean |
| `ble_client_remote.py:43-50` | Single `_status` object, single `device_address` |
| `bleStore.ts` registers (I, G, SN, etc.) | Scalar values, not per-device maps |
| `userSettings.usrAttr.MAC` | Single scalar string |
| SSE stream | One BLE notification stream, no device identifier framing |

**TODO-E1:** For multi-node: redesign `BleStore` to `devices: Map<mac, DeviceState>`. Each device gets its own register set.
**TODO-E2:** Backend BLE endpoints need device MAC parameter (e.g., `/api/ble/{mac}/connect`).
**TODO-E3:** SSE BLE events need device identifier field so frontend can route to correct device state.

### 5.2 Connection Robustness Issues

| Issue | Location | Impact |
|-------|----------|--------|
| SSE stream loss = immediate disconnect | `ble_client_remote.py:488` | Brief network blip triggers full disconnect + user notification |
| No keep-alive ping over BLE SSE | `ble_client_remote.py` | Silent connection loss not detected until next command |
| Reconnect timeout is 120s safety net only | `useBtConnectionState.ts:349` | If backend hangs, user stares at spinner for 2 min |
| I-register arrival race | `useBtConnectionState.ts:39` | Small window where backend is connected but frontend shows "connecting" |

**TODO-E4:** Buffer SSE loss 2-3 seconds before declaring disconnect (debounce).
**TODO-E5:** Add explicit BLE keep-alive ping or use the SSE 30s keepalive as health signal.

### 5.3 UX Gaps

| Gap | Detail |
|-----|--------|
| No save/reboot confirmation | `BtNodeSettings.vue:404` sends `--savereboot` without warning modal |
| CONFFIN not shown to user | Backend detects config-finished, frontend only logs to activity log |
| 409 Conflict not distinguished | Backend returns 409 when busy, frontend shows generic "Connection failed" |
| D-Bus path hardcoded to `hci0` | `useBtConnectionState.ts:59-62` — breaks with multiple BLE adapters |
| S1/S2 fields partially displayed | Many parsed fields (OWNIP, OWNGW, OWNDNS) never shown |

**TODO-E6:** Add confirmation modal before `--savereboot` (device will go offline).
**TODO-E7:** Show explicit "Config saved" feedback when CONFFIN arrives.
**TODO-E8:** Handle 409 response specifically: "Device busy, retrying..."
**TODO-E9:** Replace hardcoded `hci0` with dynamic adapter detection.

---

## 6. UI Polish & Visual Consistency

### 6.1 Hardcoded Colors Bypassing Design System

`base.css` defines a complete set of CSS variables (`--status-success`, `--status-error`, `--chat-sender-color`, etc.). But 50+ instances across components use hardcoded hex values instead:

| Component | Hardcoded Colors |
|-----------|-----------------|
| `ToastContainer.vue` | `#16a34a`, `#dc2626`, `#ca8a04`, `#2563eb` |
| `ChatInputV2.vue` | `#e53935`, `#1a73e8` |
| `LeftItemV2.vue` | `#ff9595`, `#dc3545` |
| `GroupManagerV2.vue` | `#f5a623` (3x), `#dc3545` |
| `TheStatus.vue` | `#aab0b8`, `#f0ad4e`, `#dc3545` |
| `CommandForm.vue` | `#ff9800`, `#e53935`, `#1a73e8` |
| `SettingsInputV2.vue` | `#4caf50` |
| `ArchitectureDiagram.vue` | `#e67e22`, `#8b5cf6` |

Worse: the hardcoded values don't even match the CSS variables (e.g., `#dc3545` vs `--status-error: #dc3545` happens to match, but `#e53935` is a different red).

**TODO-F1:** Replace all hardcoded colors with CSS variable references. This is the single biggest UI consistency win.

### 6.2 Inconsistent Button Styles

At least 6 distinct button patterns:
- Chat send button (round, custom)
- Settings buttons (`.test-button`)
- Map controls (`.control-btn`)
- Delete button (inline styles)
- Base `.btn` utility (defined but often bypassed)
- Modal buttons (yet another style)

**TODO-F2:** Audit all buttons and consolidate into 2-3 `BaseButton` variants (primary, secondary, destructive). The `BaseButton.vue` component already exists — use it everywhere.

### 6.3 No Shared Loading/Error/Empty State Components

Every view implements its own:
- `ChatContainerV2.vue` — spinner overlay
- `MheardTable.vue` — `.status-msg` div
- `PositionListPanel.vue` — `FilterIcon` + text
- Empty: "Keine Nachrichten" (German), "No stations found" (English)

**TODO-F3:** Create `BaseEmptyState.vue` and `BaseLoadingState.vue` components. Use them consistently.
**TODO-F4:** Decide on UI language. Currently mixed German/English. Either go full English or add i18n.

### 6.4 Accessibility

Minimal ARIA support across the entire frontend. Only ~6 `aria-` attributes found in the whole codebase.

- Interactive elements (buttons, links, toggles) lack `aria-label`
- `ChatInputV2.vue:211` explicitly sets `tabindex="-1"` disabling keyboard navigation on the destination input
- No escape-key handling on several modals/dropdowns (inconsistent)
- Color contrast concern: `--chat-timestamp: #8b9497` on light backgrounds

**TODO-F5:** Add `aria-label` to all interactive elements (buttons, inputs, links). Start with navigation and chat input.
**TODO-F6:** Remove `tabindex="-1"` from destination input or provide alternative keyboard access.

### 6.5 Mobile Responsiveness Inconsistencies

- Breakpoints: some components use `900px`, others `640px`, no consistency
- `base.css` defines `--breakpoint-sm: 640px` and `--breakpoint-md: 900px` but these are just reference — CSS `@media` queries can't use CSS variables
- Safe-area insets applied in some views but not others
- `PositionListPanel.vue` panels use `display: none` on mobile instead of adaptive layout

**TODO-F7:** Audit all `@media` queries and standardize on the two breakpoints (640px, 900px).
**TODO-F8:** Ensure safe-area insets are applied consistently in all views.

### 6.6 Spacing and Sizing Tokens

Hardcoded `5px`, `8px`, `189px` values scattered through components instead of using spacing variables or relative units.

**TODO-F9:** Low priority. Address when touching individual components.

---

## 7. Dependencies & Bundle

### 7.1 Frontend — Unused Dependencies

| Package | Status |
|---------|--------|
| `fflate@^0.8.2` | **Not imported anywhere** in `src/`. Zero references. Also bundled into `data-utils` chunk in `vite.config.js:40`. |
| `globals@^17.4.0` | Not imported in any source file. Likely an ESLint config dependency that should be in `devDependencies`. |

**TODO-G1:** Remove `fflate` from `package.json` and from `vite.config.js` manual chunks.
**TODO-G2:** Move `globals` to `devDependencies` if needed by ESLint, or remove entirely.

### 7.2 Frontend — Duplicate Compression Libraries

`pako` (52.5 KB gzipped, used in `websocket.ts`) and `fflate` (24 KB, unused) both do gzip. Only `pako` is actually used.

Covered by TODO-G1 above.

### 7.3 Frontend — Large Libraries Not Lazy-Loaded

| Library | Size (uncompressed) | Used In | Issue |
|---------|---------------------|---------|-------|
| `maplibre-gl` | ~1.2 MB | `PositionsLeaflet.vue` only | Bundled in "maps" chunk, loaded upfront |
| `chart.js` + `vue-chartjs` | ~500 KB | `MheardTable.vue`, `WxData.vue` only | Bundled in "charts" chunk, loaded upfront |

`PositionsLeaflet.vue` does `await import('maplibre-gl')` dynamically, but the chunk is not route-lazy-loaded.

**TODO-G3:** Make the Positions and Stats routes lazy-loaded in `router/index.ts`:
```ts
component: () => import('@/views/PositionsView.vue')
```

### 7.4 Backend Dependencies

All backend dependencies are current and appropriate. No concerns.

### 7.5 `receivedData` in `useConnectionManager.ts`

Writes to `usrAttr.receivedData` on every SSE message — a property that is never read. This is dead work on every incoming message.

Covered by TODO-B3 above.

---

## 8. Consolidated TODO List

### Priority 1 — High Impact, Low Risk

| ID | Category | Description | Files |
|----|----------|-------------|-------|
| TODO-D1 | Patterns | Replace 47 `print()` in main.py with `logger.debug()` | `main.py` |
| TODO-F1 | UI | Replace 50+ hardcoded colors with CSS variables | Multiple components |
| TODO-G1 | Deps | Remove unused `fflate` from package.json and vite.config.js | `package.json`, `vite.config.js` |
| TODO-B1 | Dead Code | Delete `useMessageRouter.ts` | `composables/useMessageRouter.ts` |
| TODO-B3 | Dead Code | Remove `receivedData` from UserAttributes and all write sites | `types/userSettings.ts`, `useConnectionManager.ts` |
| TODO-C1 | Consistency | Migrate all SSE events to typed `event:` headers, remove legacy fallback | `sse_handler.py`, `useSSEClient.ts` |

### Priority 2 — Important, Medium Effort

| ID | Category | Description | Files |
|----|----------|-------------|-------|
| TODO-B4 | Dead Code | Rename V2 components to drop suffix | 9 component files + all imports |
| TODO-D2 | Patterns | Extract suppression logic into testable pure function | `main.py` |
| TODO-D4 | Patterns | Create `useProxyAPI()` composable for consistent fetch handling | New composable + migrate existing calls |
| TODO-D5 | Patterns | Establish backend error handling conventions | `main.py`, commands/* |
| TODO-F2 | UI | Consolidate button styles into BaseButton variants | Multiple components |
| TODO-F3 | UI | Create shared Loading/Empty state components | New components |
| TODO-G3 | Bundle | Lazy-load Positions and Stats routes | `router/index.ts` |
| TODO-E4 | BLE | Buffer SSE loss 2-3s before disconnect (debounce) | `ble_client_remote.py` |
| TODO-E6 | BLE | Add confirmation modal before --savereboot | `BtNodeSettings.vue` |
| TODO-E8 | BLE | Handle 409 Conflict with "busy, retrying" message | `useBtConnectionState.ts` |

### Priority 3 — Cleanup & Polish

| ID | Category | Description | Files |
|----|----------|-------------|-------|
| TODO-A1 | Drift | Remove or wire up `GET /api/status` | `sse_handler.py` |
| TODO-A2 | Drift | Remove `GET /api/update/check` (frontend uses GitHub directly) | `sse_handler.py` |
| TODO-A4 | Drift | Consolidate single/bulk POST modes | `sse_handler.py` |
| TODO-B2 | Dead Code | Handle or remove `system:connected`/`system:ping` events | `eventTypes.ts`, `useSSEClient.ts` |
| TODO-B5 | Dead Code | Delete `.old/` directory | `.old/` |
| TODO-C2 | Consistency | Remove dual-format timestamp guards if backend confirmed ms-only | `formatters.ts`, `PositionsLeaflet.vue` |
| TODO-D7 | Patterns | Document event bus producer/consumer mapping | `eventTypes.ts` |
| TODO-D9 | Patterns | Replace f-string SQL with parameterized query for RSSI range | `sqlite_storage.py` |
| TODO-F4 | UI | Decide UI language (German/English) | Multiple components |
| TODO-F5 | UI | Add aria-labels to interactive elements | Multiple components |
| TODO-F7 | UI | Standardize media query breakpoints | Multiple components |
| TODO-G2 | Deps | Move `globals` to devDependencies or remove | `package.json` |

### Priority 4 — Multi-Node Preparation (Future)

| ID | Category | Description | Files |
|----|----------|-------------|-------|
| TODO-E1 | BLE | Redesign BleStore for multi-device: `Map<mac, DeviceState>` | `bleStore.ts` |
| TODO-E2 | BLE | Add device MAC parameter to backend BLE endpoints | `ble_client.py`, `ble_client_remote.py` |
| TODO-E3 | BLE | Add device identifier to SSE BLE events | `sse_handler.py`, `main.py` |
| TODO-E7 | BLE | Show CONFFIN "config saved" feedback | `bleStore.ts`, BLE components |
| TODO-E9 | BLE | Replace hardcoded `hci0` with dynamic adapter detection | `useBtConnectionState.ts` |

---

## Notes

- **Message routing pub/sub** (Section 4.1) is the most complex part of the codebase. Martin flagged this as a known pain point. The 47 print statements and entangled conditional paths make debugging difficult. Recommend incremental cleanup rather than a rewrite.
- **BLE is solid for single-device use.** Multi-node will require structural changes (Priority 4 items), but the current architecture is not blocking — it just can't scale to multiple devices without refactoring.
- **The frontend design system exists** (`base.css` has well-organized CSS variables) but is not consistently used. The gap is adoption, not design. TODO-F1 is the biggest visual consistency win.
- **No security vulnerabilities found.** SQL uses parameterized queries throughout (one f-string exception in TODO-D9 is safe but fragile). No XSS vectors identified. CORS is permissive (all origins) but appropriate for the deployment model (same-network Raspberry Pi).
