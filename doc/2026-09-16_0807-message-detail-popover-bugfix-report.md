# Bug fix report: message detail popover (Hardware / Signal rows)

**Status:** RCA complete, nothing implemented. Written 2026-09-16 for a later coding agent to pick up.
**Reported by:** DK5EN, from live screenshots of mcapp.local (v2.0.8-dev.3), 2026-09-15 18:16-18:42.
**Blast radius:** mostly `webapp`; one optional MCProxy change; one upstream firmware change.

The four reports are about the detail popover that opens from a chat bubble's meta line
(`webapp/src/components/chat/ChatBubble.vue`, `BasePopover` at `:470`). Three of them are render
bugs in that one component. The fourth (`Hardware` missing) is **not** backend data loss — the
merged row in `/var/lib/mcapp/messages.db` is correct; the webapp keeps whichever transport copy
of the frame arrived first and never merges the other half.

---

## Evidence base

All figures from mcapp.local, `/var/lib/mcapp/messages.db`, window `timestamp > now-7d`
(read-only query, 2026-09-16). Firmware line numbers from
`/Users/martinwerner/WebDev/MeshCom-Firmware-DEV-Main` (fork-main as checked out 2026-09-16).

| Fact                                                        | Value                                                                   |
| ----------------------------------------------------------- | ----------------------------------------------------------------------- |
| message rows in window                                      | 14,388                                                                  |
| rows with `rssi = 0 AND snr = 0`                            | 1,565 (10.9%)                                                           |
| `src_type` of those rows                                    | `udp` 972, `ble_remote` 400, `node` 193 — **never** `lora`              |
| `lora_mod` distinct values                                  | `8` on 12,425 rows, `NULL` on 1,963 — no other value exists             |
| `src_type='lora'` + DM destination (foreign DM heard on RF) | 324 rows, **3** carry the BLE half (0.9%)                               |
| MHeard rows (`transformer='mh'`)                            | 4,552, zero `rssi=0 AND snr=0` — real measurements, must stay untouched |

Three of the screenshotted messages, tooltip vs. stored row:

| `msg_id`   | popover showed                | DB row holds                                          |
| ---------- | ----------------------------- | ----------------------------------------------------- |
| `11B75166` | `Hardware 4.35p`              | `hw_id=4, lora_mod=8, max_hop=4, mesh_info=9, fw 35p` |
| `71A49035` | `Hardware 4.35t`              | `hw_id=43, lora_mod=8, max_hop=4, fw 35t`             |
| `134EF3B2` | `Heltec V3, MOD 8`, no Signal | also `firmware=35`, `fw_sub=s`, `rssi=0`, `snr=0`     |

`134EF3B2` is the mirror image of the other two: there the client kept the **BLE** copy (hardware,
no firmware, no signal) instead of the **UDP** copy (firmware + signal, no hardware). Same defect,
opposite half.

---

## BUG-1 — `Signal 0 dBm, 0 dB` is a sentinel, not a measurement

### Root cause

`rssi = 0` together with `snr = 0` is the firmware's "no RF reception" sentinel. The Extern-UDP
builder writes `cJson["rssi"]`/`cJson["snr"]` unconditionally on both the position (`0x21`) and
text (`0x3A`) branch (`src/extudp_functions.cpp:566-567`, `:661-662`), including for
`src_type` `node` (our own transmission) and `udp` (relayed to us by the MeshCom server), where
no radio measured anything.

MCProxy already knows this — `_ingest_signal` (`src/mcapp/storage/ingest.py:714-731`) excludes
`node`/`udp` **by `src_type`, not by a range check**, so `signal_log` and `station_positions` are
clean. What is missing is the same exclusion on the `messages` row itself: `store_message` stores
the raw `0`/`0` (`ingest.py:1485-1486`) and the SSE broadcast passes it to the client verbatim.

The webapp then renders it, correctly by its own rules: `signalLabel`
(`ChatBubble.vue:165-171`) tests `typeof rssi === 'number'`, and the shared
`formatSignalMeasurement` (`webapp/src/utils/positionHelpers.ts:432`) documents explicitly that
"a measured 0 must render". That rule is right for the position/map render sites — it is wrong for
the `(0, 0)` pair on a chat message.

A LoRa receiver cannot measure exactly `0 dBm`, so the **pair** `(0, 0)` is a safe sentinel test.
A lone `snr = 0` with a real `rssi` is a legitimate reading and must still render.

### Fix

1. **webapp (required, fixes history retroactively).** Add an exported predicate next to
   `formatSignalMeasurement` in `src/utils/positionHelpers.ts`, e.g.
   `isSignalSentinel(rssi, snr)` — true only when both coerce to numeric `0`. Consume it in
   `ChatBubble.vue`'s `signalLabel`: return `null` for the sentinel so the whole `Signal` row is
   omitted (same convention the other rows already use).
   Do **not** change `formatSignalMeasurement`'s own behaviour: its callers
   (`formatViaChipText`, `formatExplicitSignalChipText`, `buildPopupHtml`,
   `PositionListPanel`, `ChatPositionsPanel`) read `station_positions`-derived data that is
   already `src_type`-gated backend-side, and a measured `0` there is meaningful.
2. **MCProxy (optional, keeps new rows honest).** In `store_message`, normalize
   `rssi`/`snr` to `None` when both are `0` and `src_type != "lora"`. Placement: with the other
   field extraction around `ingest.py:1485`, **before** the `params` tuple and before
   `_enrich_duplicate_row` can COALESCE a `0` into a row that a later copy could have filled with
   a real value. Gate on `src_type`, matching `_ingest_signal`'s existing rule — never on a bare
   range check, and never in a way that touches the MHeard path.
   No migration: the render guard from step 1 already covers the 1,565 historical rows, and a
   scrub would destroy the only record that the sentinel was received.

### Tests

- `webapp/src/components/chat/__tests__/ChatBubble.spec.ts` — new case: a message with
  `rssi: 0, snr: 0` renders **no** `Signal` row; a message with `rssi: -118, snr: 0` renders
  `-118 dBm, 0 dB`. The second case is the mutation that discriminates a pair test from a
  truthiness test — without it, `if (rssi && snr)` passes the suite while being wrong.
- MCProxy (only if step 2 is done): a case in the ingest suite pinning that a
  `src_type='udp'` frame with `rssi=0, snr=0` stores `NULL`, and that a `src_type='lora'` frame
  with a real `rssi` and `snr=0` stores both unchanged.

---

## BUG-2 — `Hardware` present on some messages, absent on others

Two independent causes. Cause A explains every screenshot; cause B is a smaller, genuine data gap
that only a firmware change can close.

### Cause A (client): the complementary transport copy is never merged

One frame reaches the proxy twice, 40-170 ms apart, carrying **complementary halves**:

| transport                             | carries                                                                                                                                   |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Extern-UDP (`src_type` lora/udp/node) | `firmware`, `fw_sub`, `rssi`, `snr` — and on `pos` frames only, `hw_id`                                                                   |
| BLE GATT (`transformer='msg'`)        | `hw_id`, `lora_mod`, `max_hop`, `mesh_info`, `fcs_ok`, `last_hw_id`, `firmware`/`fw_sub` when the frame carried them — never `rssi`/`snr` |

The backend merges them into one row: `_enrich_duplicate_row`
(`src/mcapp/storage/ingest.py:283`, column set `_ENRICH_COLUMNS` at `:116`) fills NULL-only
columns on the already-stored row. But it is **DB-only** — the call site at `ingest.py:1736`
enriches and then `return`s at `:1789` without publishing anything. There is no `msg:enrich` SSE
event.

Meanwhile `_broadcast_handler` (`src/mcapp/sse_handler.py:760`) has **no dedup**, so the client
genuinely receives both copies. The webapp's store treats the second as a duplicate
(`webapp/src/stores/messages.ts:921-927`) and, in the patch block at `:967-991`, sets only
`patch.msg_www` — plus `msg_ack`/`msg_sent` reconciliation restricted to `source === 'initial'`.
The frame-detail fields are dropped on the floor. First copy wins for the lifetime of that
message object.

`processDataElement` already documents the split it produces
(`webapp/src/services/messageProcessor.ts:330-338`: "a BLE-won frame has no signal, a UDP-won
frame has no hardware/flag fields, and `undefined` is the popover's cue to omit the row") — the
merge is simply the missing counterpart.

This is why a **page reload repairs it**: the history/initial snapshot is built from the merged DB
row. Live rendering is the only broken path, which is exactly the "sometimes yes, sometimes no"
the report describes.

### Cause B (wire): foreign DMs heard on RF carry no hardware at all

`src_type='lora'` + DM destination: 324 rows in 7 days, 3 with the BLE half. Two things compound:

- the node does not push a DM addressed to a third party over BLE, so there is no BLE copy to
  enrich from;
- the firmware's Extern-UDP **text** branch never serializes `hw_id`. `cJson["hw_id"]` appears
  exactly once in the whole firmware — `src/extudp_functions.cpp:548`, inside the `0x21`
  position branch. The `0x3A` text branch (`:607-667`) emits only
  `src_type/type/src/dst/msg/msg_id/firmware/fw_sub/rssi/snr`.

The value **is available** on a text frame: `aprsmsg.msg_source_hw` is set by the originator at
message creation (`src/aprs_functions.cpp:112`, `= BOARD_HARDWARE`) and parsed back out of the
epilogue of every received frame (`:421`). It is carried end-to-end and describes the
**originator**, not the last hop — same provenance as `msg_source_fw_version`, which the text
branch already sends.

### Fix

1. **webapp (required).** In `stores/messages.ts`, inside the existing live-duplicate patch block
   (`:964-991`, the `if (element.msg_id)` / `existingMsg` branch), extend `patch` with the frame
   detail fields the resident message is missing. Mirror the backend rule exactly:
   - fill a field **only** when the resident value is `undefined`/`null` and the incoming copy
     has one — never overwrite, same NULL-only semantics as `COALESCE(col, ?)`;
   - field set mirrors MCProxy's `_ENRICH_COLUMNS`: `rssi`, `snr`, `hw_id`, `lora_mod`,
     `max_hop`, `mesh_info`, `firmware`, `fw_sub`, `last_hw` (the webapp's name for
     `last_hw_id`). Reference `_ENRICH_COLUMNS` by name in the code comment so the two sets can
     be kept in step;
   - apply through `this._patchMessage(existing, patch)` (`:332`) — already the single choke
     point for store slot + `msgByIdIndex` + offline-cache re-put;
   - keep it inside the existing `source !== 'history' && source !== 'hydrate'` guard. History
     and hydrate rows are already merged server-side, and hydrate deliberately trusts the
     persisted row as-is.

   Apply the BUG-1 sentinel rule when merging signal: a duplicate carrying `rssi=0, snr=0` must
   not fill a message that has no signal yet, or the merge re-introduces the fake measurement the
   render guard just removed. (If BUG-1 step 2 is implemented backend-side, the sentinel never
   reaches the client and this is belt-and-braces.)

2. **Firmware (separate change, upstream, only thing that closes cause B).** Add
   `cJson["hw_id"] = aprsmsg.msg_source_hw;` to the `0x3A` text branch in
   `src/extudp_functions.cpp`, alongside the existing `firmware`/`fw_sub` keys (~`:651-660`).
   `lora_mod` and `max_hop` are worth adding in the same change for parity with the BLE half.
   MCProxy already reads all three straight out of the UDP dict
   (`ingest.py:1491-1493`) — **no backend change is needed to consume them**. Mind
   `BLE_JSON_PAYLOAD_MAX`-class budget concerns on the receiving side and the `c_json` buffer
   bound noted at `:664-666`.

### Do not

- **Do not** fall back to `station_positions.hw_id` for the sender when a message carries no
  `hw_id`. That row is populated from position beacons; presenting it as this frame's hardware
  invents provenance. If a future change wants it, it has to be labelled as "last known for this
  station", not as a property of the message.
- **Do not** add a `msg:enrich` SSE event as the fix for cause A unless the client-side merge
  turns out to be insufficient. It is a new wire event plus a contract change plus a client
  handler, for a case the client can already resolve from data it receives.
- **Do not** dedup in `_broadcast_handler`. The client depends on receiving both copies —
  `msg_www` marking (`stores/messages.ts:969-974`) already does, and after this fix the merge
  does too.

### Tests

- New spec `webapp/src/stores/__tests__/messages.enrich.spec.ts`:
  1. UDP copy first (`firmware`, `rssi`), BLE copy second (`hw_id`, `lora_mod`, `max_hop`,
     `mesh_info`) → resident message ends up with **all** of them, one entry in `msgData`.
  2. BLE copy first, UDP copy second → same result. This is the `134EF3B2` direction and is a
     separate assertion, not a symmetry assumption.
  3. Never-overwrite: a second copy with a **different** `hw_id` does not change the resident
     value.
  4. Sentinel: a second copy with `rssi: 0, snr: 0` leaves a signal-less resident message
     signal-less.
- `ChatBubble.spec.ts:418-476` currently pins the split shapes ("a UDP-shaped message renders
  Signal, and none of the frame rows"). Those cases stay valid as _component_ tests — a component
  still renders only what its props carry — but their comments should point at the store-level
  merge so the next reader does not read them as "this split is intended end to end".

---

## BUG-3 — `MOD 8` is noise

### Root cause

Not a defect: `hardwareLabel` (`ChatBubble.vue:154-163`) appends `MOD ${mod}` whenever
`lora_mod` is a number. The fleet is uniformly modulation 8 — 12,425 of 12,425 non-NULL values in
a week — so the token carries zero information in practice.

Note `lora_mod` is already masked with `& 0x0F` in `ble_protocol.py`; the high nibble is the
country index and is deliberately not persisted (see CLAUDE.md, MHeard `MOD` bullet). So the value
rendered here is the modulation only, 3..8.

### Fix

Render `MOD n` **only when `n !== 8`**, rather than deleting the code. A node running a different
modulation stays diagnosable, and today the token disappears from every bubble in the field —
which is what the report asks for. Put the `8` in a named constant with a one-line comment stating
it is the fleet default, not a protocol constant.

Reporter's literal ask was "MOD 8 can be removed". If the reviewer prefers the token gone
unconditionally, that is a one-line variant of the same change; it just gives up the non-8 signal.

### Tests

`ChatBubble.spec.ts` — `lora_mod: 8` produces no `MOD` token; `lora_mod: 5` still produces
`MOD 5`.

---

## BUG-4 — `4.35t` is labelled `Hardware`

### Root cause

`hardwareLabel` (`ChatBubble.vue:154-163`) concatenates three unrelated facts into one row that
the template (`:494-497`) labels `Hardware`:

```
hwIdMap[hw_id]  +  MOD <lora_mod>  +  formatFirmware(firmware, fw_sub)
```

`formatFirmware` (`positionHelpers.ts:87-94`) returns the firmware version string (`4.35t`).
When `hw_id` is absent — every text message that never got a BLE copy, see BUG-2 — the row
collapses to the firmware version alone under the label `Hardware`, which is what the screenshots
show.

### Fix

Split into two `<dt>/<dd>` pairs in the popover:

- `Hardware` → `hwIdMap[hw_id]` (plus `MOD n` per BUG-3, when shown);
- `Firmware` → `formatFirmware(firmware, fw_sub)`.

Each row keeps the existing "render only when the datum is present" convention, so a message with
firmware but no hardware now shows a `Firmware` row and no `Hardware` row — honest, and it makes
BUG-2's remaining cause-B gap visible as a gap instead of as a mislabel.

Leave the station card / map popup alone: it lists the same values as unlabelled chips
(`0 km · 480m · 4.35t · Heltec V3 · …`, `positionHelpers.ts` `buildPopupHtml` ~`:832`), so there
is no wrong label to fix there.

### Tests

`ChatBubble.spec.ts:418-476` — update the existing cases for the new two-row layout, and add one
where `hw_id` is absent but firmware is present: asserts a `Firmware` row and **no** `Hardware`
row.

---

## Suggested execution order

| #   | Change                                                                                       | Repo     | Ships in                           |
| --- | -------------------------------------------------------------------------------------------- | -------- | ---------------------------------- |
| 1   | BUG-1 render guard, BUG-3 MOD gate, BUG-4 row split (`ChatBubble.vue`, `positionHelpers.ts`) | webapp   | webapp release                     |
| 2   | BUG-2 cause A: live-duplicate merge (`stores/messages.ts`)                                   | webapp   | same release                       |
| 3   | BUG-1 backend normalization (`storage/ingest.py`), optional                                  | MCProxy  | pulls MCProxy into the dev release |
| 4   | BUG-2 cause B: `hw_id` on Extern-UDP text frames                                             | firmware | own firmware release               |

1 and 2 are separate commits; 2 is the one with real regression risk (it touches the shared
duplicate path that also carries `msg_www` and the ack reconciliation), so it wants its own spec
file and its own review pass.

## Gate before committing (both repos)

webapp:

```bash
cd /Users/martinwerner/WebDev/webapp
npm run lint && npm run type-check && npm run test:unit && npm run format:check
```

MCProxy (only if step 3 is taken):

```bash
cd /Users/martinwerner/WebDev/MCProxy
uvx ruff check && uvx ruff format --check . && uv run mypy src/mcapp ble_service/src
uv run python scripts/run_startup_tests.py
```

`ruff format --check .` covers `.md` files too — run prettier on any doc first, then the ruff
check, per CLAUDE.md.

## Live verification after deploy

The four fixes are only provable against real dual-transport traffic, not against fixtures:

1. Open a group conversation on mcapp.local **without reloading**, wait for a fresh message, open
   its popover: `Hardware`, `Firmware` and `Max hops` must all be there on first render
   (BUG-2 cause A). Before the fix, one half is missing until reload.
2. Own outbound message → popover shows no `Signal` row (BUG-1), a `Firmware` row (BUG-4), and no
   `MOD 8` (BUG-3).
3. A foreign DM heard on RF still shows no `Hardware` row until the firmware change lands — that
   is cause B, expected, and not a regression.
