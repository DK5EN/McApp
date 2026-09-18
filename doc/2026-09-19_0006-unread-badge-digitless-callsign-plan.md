# Unread badge stuck for digit-less DM partners (WLNK-1) — RCA + fix plan

Issue: DK5EN/McApp#11 (HB9VQQ) — "Nachrichten von WLNK-1 werden bei mir als ungelesen
angezeigt, obwohl sie bereits gelesen wurden."

## BLUF

Not all DMs — only DM partners whose SSID-stripped base callsign fails the webapp's
`isValidPairMember` plausibility test (3-7 alphanumerics containing **at least one digit**).
`WLNK-1` (a Winlink gateway alias, base `WLNK`) has no digit, so the webapp writes its read
cursor to the server under the key `WLNK` instead of `HB9VQQ<>WLNK`. The server stores the
bogus row, the conversation's real cursor is never set, and every `proxy:conversations`
snapshot re-lights the badge. The state is self-locking: `markRead` early-returns because the
local cursor already advanced, so the badge can never be cleared again.

Two fixes, both needed:

1. **Webapp** — restore the documented round-trip invariant in `serverKeyForSidebarKey`
   (stops it recurring).
2. **Backend** — normalise a bare-callsign cursor key on `POST /api/read_cursor`, plus a
   one-shot repair pass for rows already written (heals existing installs, including nodes
   running a stale cached PWA).

## Evidence

Round-trip, run against the current webapp sources (own call `HB9VQQ`):

| server key       | → sidebar key | → back to server key | correct? |
| ---------------- | ------------- | -------------------- | -------- |
| `HB9VQQ<>OE1XYZ` | `OE1XYZ`      | `HB9VQQ<>OE1XYZ`     | yes      |
| `HB9VQQ<>WLNK`   | `WLNK`        | `WLNK`               | **no**   |

`makePairKey('APRS2SOTA', 'DL8FMA')` → `null` (same root heuristic, different symptom — see
§ Secondary).

On mcapp.local (production DB, 2026-09-19): one affected conversation key exists,
`APRS2SOTA<>DL8FMA` (11 rows, third-party), and `read_cursors` holds **no** bogus bare-callsign
row — DK5EN has never DM'd a digit-less partner, which is why this never showed up here.

## Root cause

`webapp/src/utils/callsignUtils.ts`:

```ts
export function isValidPairMember(callsign: string): boolean {
  const c = normalizeCallsign((callsign || "").split(",")[0]);
  return /^[A-Z0-9]{3,7}$/.test(c) && /[A-Z]/.test(c) && /[0-9]/.test(c);
}
```

The digit requirement is correct for its original job — rejecting garbage pair members
(`TEST`, `ALL`, `WX`, `Time`) in `makePairKey`. It is wrong in `serverKeyForSidebarKey`:

```ts
if (!isValidPairMember(sidebarKey)) return sidebarKey; // ← returns 'WLNK' unchanged
const partner = normalizeCallsign(sidebarKey);
return ownBase < partner ? `${ownBase}<>${partner}` : `${partner}<>${ownBase}`;
```

The **forward** direction applies no such test: `translateServerSummaryKey` returns the other
side verbatim as soon as one side equals `ownBase`, whatever it looks like. So the function's
own documented invariant —

```
serverKeyForSidebarKey(translateServerSummaryKey(k, own), own) === k
```

— is violated for exactly the partners `isValidPairMember` rejects. The backend's
`compute_conversation_key` has no plausibility notion at all (`storage/constants.py`), and the
existing server-side translation in `seed_read_cursors_from_counts` (`storage/prefs.py`) does
not validate either. The webapp guard is the single outlier.

### Why it locks itself

1. `markRead('WLNK', ts)` sets `readCursor['WLNK'] = ts` locally, persists it to IndexedDB,
   and POSTs `{key: 'WLNK', ts}`.
2. `POST /api/read_cursor` stores a `read_cursors` row keyed `WLNK`, then looks up
   `summary['WLNK']` — which does not exist (the summary is keyed `HB9VQQ<>WLNK`) — and echoes
   `unread: 0`. The badge clears optimistically.
3. The next SSE burst sends `proxy:conversations` with `HB9VQQ<>WLNK` → sidebar key `WLNK`,
   `unread > 0` (no cursor matches it server-side). Badge returns.
4. `applyReadCursors` translates the bogus server row `WLNK` → sidebar key `WLNK`, sees the
   local cursor is _not_ ahead of it, and skips the lost-POST re-send.
5. `markRead` early-returns on every future render (`ts <= current`). Nothing can clear it.

Step 4/5 is why the webapp fix alone does **not** heal an already-affected box: after the fix,
`markRead` still refuses to re-POST, and the correct key is never written.

## Fix

### Wave 1a — backend (MCProxy)

Files (exclusive): `src/mcapp/storage/prefs.py`, `src/mcapp/storage/_base.py`,
`src/mcapp/sse_routes/prefs.py`, `src/mcapp/main.py`,
`src/mcapp/storage/read_cursor_tests.py`.

1. **Extract the translation.** Lift the sidebar-key → `conversation_key` mapping already
   inlined in `seed_read_cursors_from_counts` into a module-level
   `conversation_key_for_sidebar_key(sidebar_key: str, my_base: str) -> str`, with the same
   three-shape order (`A~B` pair → sorted `<>`; group / hashtag / `*` / `Time` → verbatim;
   anything else → sorted `[my_base, base(key)]`). Call it from the seed so there is one
   implementation, not two.

2. **Normalise on write.** In `build_prefs_router`'s `POST /api/read_cursor`, map `body.key`
   through that function _before_ `set_read_cursor`, and use the normalised key for the
   `set_read_cursor` call, the `get_conversation_summary` lookup, the `SPAM_GROUP` comparison
   and the `proxy:read_cursor` broadcast payload. A key that is already a `<>` pair is
   returned unchanged — add that early exit to the helper. `my_base` comes from
   `manager.message_router.my_callsign`, which the route already reads; with an empty callsign,
   skip normalisation entirely and store the key as-is rather than producing `<>WLNK`.

   This is deliberate defence in depth, not redundancy: the webapp ships as a PWA behind a
   service worker, and a stale cached bundle keeps POSTing the bad key long after the webapp
   fix is released (cf. `project_webapp_sw_stale_verify`).

3. **Repair existing rows.** Add
   `PrefsMixin.repair_read_cursor_dm_keys(my_callsign: str) -> int`, one-shot and idempotent,
   guarded by the `classifier_meta` marker `read_cursors_dm_repaired` — same shape as
   `read_cursors_seeded`, including the "empty callsign → skip _without_ setting the marker,
   retry next boot" rule. For every `read_cursors` row whose key needs translating
   (i.e. `conversation_key_for_sidebar_key` returns something different), write the translated
   key through `set_read_cursor` (MAX semantics protect an already-correct row) and delete the
   old row. Returns the number repaired. Declare it on `StorageBase` next to
   `seed_read_cursors_from_counts`.

4. **Wire it.** Call it from `main.py` immediately after the `seed_read_cursors_from_counts`
   call (same try/except shape, failure is non-fatal), so the order is seed-then-repair.

5. **Tests** (`storage/read_cursor_tests.py`, already registered in `run_startup_tests.py`):
   - `conversation_key_for_sidebar_key` maps `WLNK` → `HB9VQQ<>WLNK`, leaves `232`, `#OE-SOTA`,
     `*`, `Time` and an existing `A<>B` untouched, and maps `A~B` → `A<>B`.
   - `POST`-level: setting a cursor under `WLNK` makes `get_conversation_summary` report
     `unread == 0` for `HB9VQQ<>WLNK` (the end-to-end claim; fails before the fix).
   - `repair_read_cursor_dm_keys` moves a pre-existing `WLNK` row to `HB9VQQ<>WLNK`, MAX-merges
     against an existing correct row, deletes the stale one, is a no-op on the second call, and
     leaves the marker unset for an empty callsign.

### Wave 1b — webapp (parallel with 1a, disjoint repo)

Files (exclusive): `src/utils/callsignUtils.ts`, `src/utils/__tests__/callsignUtils.spec.ts`.

Replace the `isValidPairMember` gate in `serverKeyForSidebarKey` with the weakest condition that
still refuses to guess — the key is non-empty and is not one of the shapes handled above it.
Everything reaching that line is an own-DM partner base callsign by construction, exactly as the
forward direction and the backend both already assume. Update the docstring: the invariant is
now total for every server key `translateServerSummaryKey` does not collapse to `null`, and the
reason the plausibility test must not appear here gets a sentence, because the next reader will
want to put it back.

Tests: the `WLNK` round-trip both ways, `APRS2SOTA`, and a loop asserting the invariant over
every DM `server_key` in `conversation_key_vectors.json` plus the two new ones.

### Wave 2 (optional, sequential — same file as 1b) — secondary symptom

`makePairKey` returns `null` for a third-party DM with a digit-less or over-long member, so
`APRS2SOTA<>DL8FMA` has no sidebar bucket at all and `translateServerSummaryKey` drops it from
the server summary. Real but not what was reported, and the digit test is load-bearing where it
sits (`conversation_key_vectors.json` calls the asymmetry "deliberate and unpinned").

Recommendation: **defer**. If taken, the shape is to replace the digit requirement with an
explicit non-person denylist (`NON_PERSON_DST_ALIASES` already exists) and widen the length
bound, then re-run the sidebar and push-notification suites. On mcapp.local exactly one
conversation and zero other callsigns are affected, so the payoff is small and the blast radius
(every pair-key derivation, incl. `pushNotification.ts`) is not.

## Verification

- MCProxy: `uvx ruff check`, `uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src`,
  `uv run python scripts/run_startup_tests.py`.
- webapp: `npm run lint`, `npm run type-check`, `npm run test:unit`.
- Both new backend tests must be confirmed red before the fix and green after (regression-test
  rule), and the drift checks against the vendored vector copies must stay green.
- Field check after deploy on mcapp.local: `read_cursors` holds no key that lacks `<>` and is
  not a group / `*` / `Time` / hashtag.

## Ship

Both repos change, so this needs a dev release (`dev-release`) and a deploy to mcapp.local.
HB9VQQ runs his own node: the repair pass is what actually clears his badge, and it only runs
once his box takes the update — worth saying so when answering the issue.

## Assumptions

- HB9VQQ runs v2.0.4 or later (the read-cursor scheme). If his box predates it, he is on the
  legacy `read_counts` path and the diagnosis above does not apply — worth confirming in the
  issue thread before shipping.

## Status — implemented 2026-09-19

Waves 1a and 1b are **done and gated**. Wave 2 remains **deferred** as recommended above.

| Wave | Scope                                | State                 |
| ---- | ------------------------------------ | --------------------- |
| 1a   | Backend normalise + one-shot repair  | done                  |
| 1b   | Webapp round-trip invariant          | done                  |
| 2    | `makePairKey` third-party digit-less | deferred, not started |

Shipped as specified, with one addition forced by the advisor gate.

**Advisor finding (fixed before commit).** Weakening the webapp gate to a bare empty-string
check — and mirroring that in the new backend helper — mis-keyed the shapes
`compute_conversation_key` deliberately **refuses**: an all-ASCII-digit key outside the
1..99999 group range (`0`, `100000`) and a malformed `#` tag (`#OE_SOTA`, bare `#`). Those rows
are read back under `COALESCE(conversation_key, dst)` (`storage/query.py`), so the raw dst
_already is_ the server key. Pairing it produced `0<>HB9VQQ`, a conversation that does not
exist — the reported bug again, pointed at a different set of keys — and
`repair_read_cursor_dm_keys` would then have moved a **correct** cursor row onto that phantom
key and set its marker, making the damage one-shot and unrepeatable. The old
`isValidPairMember` gate had been covering these by accident (no digit, no letter, bad
charset).

Both translators now carry an explicit refusal branch mirroring `compute_conversation_key`'s
own, directly after the group/hashtag/`*`/`Time` verbatim branch, and the empty key is returned
unchanged on both sides (it previously degenerated to `<>HB9VQQ` on the backend). Pinned by
`conversation_key_for_sidebar_key` cases for `0` / `100000` / `#OE_SOTA` / `#` / `''`, a
`repair_read_cursor_dm_keys` case asserting a `'0'` row is left alone, and the webapp's
"leaves keys the server refuses to give a conversation key unchanged" case. Each was confirmed
red with the guard removed and green with it restored.

Everything else the advisor examined came back clean: the repair loop's write-then-delete
ordering is crash-safe and the marker is only set after a complete pass; the route uses the
normalised key at all five downstream sites; no webapp consumer keys off a verbatim echo of its
own POSTed key (`applyReadCursorEcho` translates, `scheduleCursorPost`/`cancelCursorPost` share
the client-side key); and the two translators agree on every non-garbage shape.

**Gate.** MCProxy: `ruff check`, `ruff format --check .`, `mypy src/mcapp ble_service/src`
("no issues found in 106 source files"), `scripts/run_startup_tests.py` exit 0 with
`read_cursor: PASS`. webapp: `eslint src`, `vue-tsc --noEmit`, 3366 unit tests, `prettier
--check .` — all clean, `conversation_key_vectors.json` unmodified.

**Field replay.** `conversation_key_for_sidebar_key` run against mcapp.local's 144 real
`read_cursors` rows rewrites **0** of them: the repair pass is inert on a healthy box and fires
only where the bug actually deposited a bare key.

**Still open:** a dev release and deploy, and the reply to issue #11 — including the
confirmation that HB9VQQ is on v2.0.4 or later (§ Assumptions).
