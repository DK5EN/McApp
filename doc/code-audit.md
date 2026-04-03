# Code Audit — McApp Backend + Webapp Frontend

**Date:** 2026-04-03
**Last verified:** 2026-04-03
**Scope:** `/Users/martinwerner/WebDev/MCProxy` (backend) + `/Users/martinwerner/WebDev/webapp` (frontend)

---

## Table of Contents

1. [Feature Drift — Frontend vs Backend](#1-feature-drift)
2. [Dead & Unused Code](#2-dead--unused-code)
3. [Inconsistencies & Mismatches](#3-inconsistencies--mismatches)
4. [Solution Pattern Problems](#4-solution-pattern-problems)
5. [BLE Feature Audit](#5-ble-feature-audit)
6. [UI Polish & Visual Consistency](#6-ui-polish--visual-consistency)
7. [Dependencies & Bundle](#7-dependencies--bundle)
8. [Remaining Open Items](#8-remaining-open-items)

---

## 1. Feature Drift

### 1.1 Backend Endpoints Without Frontend Consumer

| Endpoint | Status | Resolution |
|----------|--------|------------|
| `GET /api/status` | DONE | Kept as observability endpoint, documented |
| `GET /api/update/check` | DONE | Removed — frontend uses GitHub API directly |
| `GET /api/blocked_texts` | DONE | Kept as SSE-delivered; REST GET available as fallback |
| `GET /api/read_counts` | DONE | Same pattern as above |

### 1.2 Dual-Mode POST Endpoints

DONE — Each endpoint consolidated to one mode: `hidden_destinations` accepts bulk only, `blocked_texts` accepts single only.

### 1.3 Topic Beacon — Backend Only, No Frontend UI

Left as-is (intentional — misuse risk). Comment in `topic_beacon.py` explains why no UI exists.

### 1.4 MHeard Beacons — Backend Parses, Frontend Ignores

Noted for future evaluation. Backend `ble_protocol.py` `transform_mh()` parses MHeard beacon data from BLE, not yet surfaced in frontend BLE components.

---

## 2. Dead & Unused Code

| Item | Status | Resolution |
|------|--------|------------|
| `composables/useMessageRouter.ts` | DONE | Deleted |
| `system:connected` / `system:ping` events | **OPEN** | Backend sends these SSE events, frontend doesn't type them in EventMap. Events arrive and are silently ignored. See [Section 8](#8-remaining-open-items). |
| `receivedData` property | DONE | Removed from `UserAttributes` type and all write sites |
| V2 component naming | DONE | All 9 components renamed to drop V2 suffix, imports updated |
| `.old/` directory | DONE | Deleted — `magicword.py`, `supervisor.py`, `daily_sqlite_dumper.py` |
| Deprecated lat/lon config fields | Acceptable | GPS overrides at runtime; config values serve as fallback until first GPS fix |

---

## 3. Inconsistencies & Mismatches

| Item | Status | Resolution |
|------|--------|------------|
| SSE legacy vs typed events | DONE | All SSE events use explicit `event:` headers. `routeLegacyMessage()` removed. |
| Timestamp dual-format guards | DONE | Guards removed — backend confirmed ms-only |
| `call_sign` vs `callsign` naming | Low priority | Cosmetic inconsistency remains; address when touching config_loader |
| Duplicate version-check mechanism | DONE | Backend `GET /api/update/check` removed |

---

## 4. Solution Pattern Problems

### 4.1 Message Routing Complexity

| Item | Status | Resolution |
|------|--------|------------|
| `print()` in main.py | DONE | Zero `print()` calls remain; all replaced with logger |
| Suppression logic entangled | DONE | Extracted to `suppression.py` with pure functions: `should_suppress_outbound()`, `get_suppression_reason()`, `is_command()`, `is_valid_destination()` |
| Message flow documentation | Refer to `doc/dataflow.md` |

### 4.2 Frontend API Patterns

| Item | Status | Resolution |
|------|--------|------------|
| No shared API wrapper | DONE | `useProxyAPI()` composable created with `get<T>()`/`post<T>()`, consistent error handling via `ProxyAPIError` |

### 4.3 Backend Error Handling

| Item | Status | Resolution |
|------|--------|------------|
| Inconsistent error handling | DONE | Conventions established: structured error responses in routing.py, no silent `pass`, proper try-except with logging |

### 4.4 Other Pattern Items

| Item | Status | Resolution |
|------|--------|------------|
| Event bus undocumented | DONE | `eventTypes.ts` now has producer/consumer mapping table |
| f-string SQL for RSSI range | DONE | Replaced with parameterized `WHERE rssi BETWEEN ? AND ?` |
| Config mutation at runtime | Acceptable | GPS override lifecycle is by-design; config values are fallback |
| Pydantic used sparingly | Low priority | Use Pydantic for new endpoints; no need to retrofit |

---

## 5. BLE Feature Audit

BLE is the main feature. Current state: single-device, solid. Multi-node is upcoming.

### 5.1 Connection Robustness (Completed)

| Item | Status | Resolution |
|------|--------|------------|
| SSE loss = immediate disconnect | DONE | 2.0s debounce buffer in `ble_client_remote.py` |
| 409 Conflict not distinguished | DONE | Frontend now shows "busy, retrying" toast instead of generic error |
| No savereboot confirmation | DONE | `BaseConfirmModal` shown before reboot-triggering operations |

### 5.2 Single-Device Architecture (Multi-Node Blockers)

These remain as **Priority 4 — Future** items. See [Section 8](#8-remaining-open-items).

| Location | Single-Device Assumption |
|----------|--------------------------|
| `ble_client.py` | `is_connected` is a single boolean |
| `ble_client_remote.py` | Single `_status` object, single `device_address` |
| `bleStore.ts` registers | Scalar values (I, G, SN, etc.), not per-device maps |
| `userSettings.usrAttr.MAC` | Single scalar string |
| SSE stream | One BLE notification stream, no device identifier framing |

---

## 6. UI Polish & Visual Consistency

### 6.1 Hardcoded Colors

**DONE** — `--color-destructive` (#e53935 light / #ef5350 dark) defined in `base.css`. All `#e53935` instances replaced with `var(--color-destructive)` across 9 BLE and chat components. All `#dc3545` instances replaced with `var(--status-error)`. `.btn--danger` in base.css also updated.

### 6.2 Other UI Items (All Completed)

| Item | Status | Resolution |
|------|--------|------------|
| Inconsistent button styles | DONE | `BaseButton.vue` with 5 variants (default, primary, secondary, danger, ghost), 62 imports across codebase |
| No shared loading/empty state | DONE | `BaseEmptyState.vue`, `BaseLoadingState.vue`, `BaseSpinner.vue` created, 20+ usages |
| Mixed German/English UI | DONE | All UI text is now English |
| Missing aria-labels | DONE | Improved from ~6 to 9 aria-label attributes across key interactive elements |
| Inconsistent breakpoints | DONE | Standardized on 640px / 900px |

---

## 7. Dependencies & Bundle

| Item | Status | Resolution |
|------|--------|------------|
| Unused `fflate` | DONE | Removed from `package.json` and `vite.config.js` |
| `globals` in wrong section | DONE | Moved to `devDependencies` |
| Large libs not lazy-loaded | DONE | All routes use `() => import()` pattern |
| Backend dependencies | OK | All current, no concerns |

---

## 8. Remaining Open Items

### Still Open (2 items)

#### TODO-B2: `system:connected` / `system:ping` SSE Events

Backend sends these typed SSE events on connection and as keepalive. Frontend has no consumers — events arrive and are silently discarded. Not in frontend `EventMap`.

**Action:** Either (a) add frontend consumers (connection ID display, latency indicator in status bar) or (b) stop sending them from backend if the frontend SSE heartbeat detection is sufficient.

---

### Priority 4 — Multi-Node BLE Preparation (Future)

| ID | Description | Files |
|----|-------------|-------|
| TODO-E1 | Redesign BleStore for multi-device: `Map<mac, DeviceState>` | `bleStore.ts` |
| TODO-E2 | Add device MAC parameter to backend BLE endpoints | `ble_client.py`, `ble_client_remote.py` |
| TODO-E3 | Add device identifier to SSE BLE events | `sse_handler.py`, `main.py` |
| TODO-E5 | Add explicit BLE keep-alive ping or use SSE 30s keepalive as health signal | `ble_client_remote.py` |
| TODO-E7 | Show CONFFIN "config saved" feedback in UI | `bleStore.ts`, BLE components |
| TODO-E9 | Replace hardcoded `hci0` with dynamic adapter detection | `useBtConnectionState.ts` |

---

## Notes

- **No security vulnerabilities found.** SQL uses parameterized queries throughout. No XSS vectors identified. CORS is permissive (all origins) but appropriate for the deployment model (same-network Raspberry Pi).
- **BLE is solid for single-device use.** Multi-node will require structural changes (Priority 4), but nothing is blocking current usage.
- **The frontend design system exists and is now mostly adopted.** The remaining `#e53935` instances are the last holdout.
