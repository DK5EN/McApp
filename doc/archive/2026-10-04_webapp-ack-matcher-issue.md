# webapp: inline `:ack` matcher marks the wrong DM as delivered; internet-only acks are erased on reconnect

Repos as reviewed: webapp `bae53c4`, MCProxy `ef87299` (2026-10-04). All findings below were
verified by executing the real code: the webapp store in a scratch vitest, and MCProxy's
`store_message` path against a temporary SQLite DB. File/line references are to those commits.

Context: a user report ("already acknowledged ticks disappear again") was traced to the MeshCom
**firmware** web GUI, not to MCApp. While checking MCApp, the review found the bugs below. They
are real, but in MCApp they mostly do not produce a visible flicker, because of the replay
described in finding 1.

## Summary

| #   | Finding                                                                                           | Severity | Repo    |
| --- | ------------------------------------------------------------------------------------------------- | -------- | ------- |
| 1   | Client `findAckMessage` accepts acks MCProxy rejects (SSID stripped, ack addressee never checked) | high     | webapp  |
| 2   | A true ack seen only on the internet WebSocket is erased by the next `proxy:initial` snapshot     | medium   | webapp  |
| 3   | No shared test vectors pin ack-to-message matching between webapp and MCProxy                     | medium   | both    |
| 4   | Node/gateway `sent` `msg_status` is published even when no DB row matched                         | low      | MCProxy |

## Finding 1: client matcher is looser than MCProxy's `_inline_ack_original`

Files: `src/services/messageProcessor/ackMatch.ts` (`findAckMessage`), caller
`src/services/messageProcessor.ts` ~350-372, apply `src/stores/messages.ts` ~1241-1246.
Server reference: MCProxy `src/mcapp/storage/ingest.py` `_inline_ack_original` (~203-238) and the
inline path (~1976-2060).

| Rule                 | webapp `findAckMessage`                   | MCProxy                                     |
| -------------------- | ----------------------------------------- | ------------------------------------------- |
| Ack sender vs target | `normalizeCallsign` — **SSID stripped**   | full call, exact (`strip().upper()`)        |
| Ack addressee        | **not checked** (function never gets dst) | must equal the original's sender            |
| Original's sender    | not checked (any resident message)        | must equal the ack's addressee              |
| Counter location     | `/\{NNN(?!\d)/` anywhere in the text      | `echo_id` = `\{([0-9]+)$`, end of text only |
| Window               | `abs(orig - ack) <= 1 h`                  | `orig > ack - 1 h`; `held` rows get 168 h   |

Executed failure cases:

- **Wrong addressee (most realistic):** own DM `DK5EN-99 → DK1TCP-77 "hello{201"`, then an
  overheard `DK1TCP-12 → DH6MAV ":ack201"`. Client: own DM gets ✓✓ Delivered. Server: no match,
  no event. If our DM was never delivered, the UI claims it was.
- **Other SSID:** own DM to `DL1ABC-1 {123`, ack from `DL1ABC-2` → client ✓✓, server rejects.
  On its own this needs a counter wrap; its real effect is widening the addressee case to every
  SSID of the base call.
- **Reverse steal:** a real ack `DB0B-1 → me :ack123` while a newer third-party DM
  `DL9XX-5 → DB0B-1 "yo{123"` is resident. The client marks the third-party DM, our own stays
  unmarked until the next snapshot.
- **Counter mid-text:** `"treffpunkt {123 raum"` matches on the client only. Negligible; the
  firmware always appends `{NNN` at the end.

Why it does not flicker today: `proxy:initial` first reconciles `messages[]` (downgrades the
false ✓✓ to MCProxy's `acked=0`, `messages.ts` ~1110-1116), then replays `acks[]` (newest 200
inline ack rows, MCProxy `storage/query.py` ~750-760, no time window, all peers) through the
same lax matcher, which sets the false ✓✓ again inside the same synchronous handler. A false ✓✓
therefore persists until that ack falls out of the newest 200. A matched ack never records a
dedup key, so every replay re-matches it.

Fix:

1. Pass the ack frame's `dst` into `findAckMessage` and require: original target == ack sender
   AND original sender == ack addressee, both compared on the **full** callsign (SSID kept,
   case-insensitive, `effectiveDst` for via-routed dst). Mirror `_inline_ack_original` exactly.
2. Window: one-sided `orig.timestamp > ack.timestamp - TIME_WINDOW_MS`, plus the `held`
   carve-out (`delivery_status === 'held'` → 168 h, MCProxy `HELD_ACK_WINDOW_MS`).
3. Counter: match only a trailing `{NNN` (same as MCProxy `ACK_SUFFIX_RE`).
4. Keep the existing newest-first scan; it is still needed for counter reuse.

Regression tests (must fail before, pass after) in `messages.ack.spec.ts`:

- wrong addressee: the overheard `DK1TCP-12 → DH6MAV ":ack201"` must NOT mark own `→ DK1TCP-77 {201`
- other SSID: ack from `DL1ABC-2` must NOT mark own DM to `DL1ABC-1`
- reverse steal: the real ack must mark our DM, not the newer third-party DM
- positive control: ack from `DK1TCP-77` to us marks our DM

## Finding 2: internet-only acks are erased on reconnect

Files: `src/stores/messages.ts` ~1110-1116 (`source === 'initial'` reconcile, "server wins on
acks"), internet WebSocket `src/services/internetWebSocket.ts` (`wss://mcmap.oevsv.at/ws`,
`messages.ts:235`), setting `internetEnabled` (default `false`, `userSettings.ts:111`).

Scenario: `internetEnabled` on. The addressee's ack reaches the browser only through the mcmap
WebSocket (our node never hears it over RF). The client matcher sets ✓✓. MCProxy never ingests
that feed, so DB `acked` stays 0. The next SSE reconnect (frequent: heartbeat timeout, mobile
visibility resume, `useConnectionManager.ts` ~480) sends `proxy:initial` with `acked` absent, and
the reconcile sets `msg_ack = false`. The ack is not in `acks[]` (MCProxy never stored it), so
the replay does not restore it. `_patchMessage` re-puts the downgraded row to IndexedDB, so the
loss survives a reload.

Fix (only AFTER finding 1 is fixed, otherwise false ✓✓ become permanent):

- Store provenance on the row: `ack_origin: 'server' | 'local' | 'www'`. `server` = set by
  `msg:status` or by a snapshot with `acked=1`; `local` = client matcher on a MCProxy-delivered
  frame; `www` = client matcher on the internet WebSocket.
- Snapshot reconcile: never downgrade `msg_ack` for `ack_origin === 'www'`; keep the downgrade
  for `local` (MCProxy saw the same frame and rejected it) and for rows without `ack_origin`
  (legacy cache rows — the original reason for "server wins", the 2026-08 ctcping bug).
- Persist `ack_origin` through the offline cache.

Regression test (`messagesHydrate.spec.ts` or `messages.ack.spec.ts`): own DM via `'local'`,
inline `:ack` via `'websocket'` → `msg_ack` true; then `proxy:initial` with that row and no
`acked` → `msg_ack` must stay true. Keep the existing ctcping test
(`messagesHydrate.spec.ts` ~333, "REPORTED BUG (ctcping, cached rows)") green.

## Finding 3: no shared vectors for ack-to-message matching

`ack_predicate_vectors.json` (replayed in `predicates.spec.ts`) covers only the text predicate
(`is_ack`, `ack_number`, sto). Nothing pins which message an ack belongs to, so the client and
server rules drifted silently (finding 1). The comment at `messages.ack.spec.ts` ~712 is stale.

Fix: add `ack_match_vectors.json` (frames + resident messages + expected match id or null),
replayed by both the webapp spec and MCProxy's `ack_status_tests`. Include the four cases from
finding 1, the held 168 h case, the one-sided window boundary, and a trailing vs mid-text
counter. Follow the existing cross-repo contract pattern (`dedup_contract.json`), including
the mc-chat copy if that is where the contracts are mastered.

## Finding 4 (MCProxy, hygiene): `sent` published without a DB match

MCProxy `ingest.py` ~1466-1481 publishes `{msg_id, sent: true, ack_kind}` for 0x00/0x01 even
when `_resolve_ack_target` found no row ("publishes even when target is None"). Not user-visible:
the client cannot hold that msg_id before the row exists (the router awaits storage before the SSE
broadcast, `main.py` ~408), and `msg_echo` keeps the single ✓ anyway. Optional: publish only on
a match, keep a debug log for the unmatched case. Do not change the published payload shape
(byte-pinned by `ack_status_tests`, shared with mc-chat).

## Verified non-issues — do not re-investigate

- **Two DB rows per DM (PN retry / BLE+UDP twin) letting an unacked row win the snapshot:**
  refuted. MCProxy dedups direct DMs on `msg_core` (`ingest.py:372`, `:396-403`); retries and
  twins collapse into one row, and the firmware never forwards its own retry copies to the
  client.
- **Stale snapshot overtaking a fresh live ack:** refuted. `/events` registers the client before
  the snapshot read and queues live events behind it; the webapp processes `proxy:initial`
  synchronously.
- **Unmatched node/gateway `sent` clearing the ✓:** refuted (see finding 4).
- **Node RTC skew making the client accept what the server rejects:** refuted. The asymmetry
  runs the other way: the server accepts (no upper bound, 168 h for held), the client rejects, so
  a ✓✓ appears only after reconnect.
- **"Server wins" downgrade is dead code since the 14-day hydrate horizon:** refuted. Pre-fix
  ctcping rows are effectively gone, but the downgrade still clears finding 1's false positives.
- **An ack MCProxy matched can be lost:** refuted. MCProxy writes the DB before it publishes
  (`ingest.py` ~1310-1339, ~2019-2023), and the snapshot ships it.

## Suggested order

1. Tests that fail today (finding 1 cases, finding 2 case).
2. Finding 1 + finding 3 (shared vectors) together.
3. Finding 2 (`ack_origin`).
4. Finding 4 in MCProxy, optional, separate commit.
