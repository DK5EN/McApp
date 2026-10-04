# Inline `:ack` matcher fix — implementation plan

Issue: `doc/2026-10-04_1842-webapp-ack-matcher-issue.md` (findings 1-4). Repos at plan time:
MCProxy `ef87299`, webapp `bae53c4`, both on `development`, both clean. Executed with
`/orchestrate-waves`; this file is the campaign's resume point and is updated after every wave.

## Decisions (operator, 2026-10-04)

| #   | Decision                                                                                                                                                                                                       |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | `ack_match_vectors.json` is canonical in **MCProxy** (`src/mcapp/storage/`), hand-copied to the webapp with a sha256 pin + parsed-JSON drift check against `../MCProxy`. mc-chat is not part of this campaign. |
| D2  | Finding 2 (`ack_origin`) is implemented, in its own wave, committed only after finding 1.                                                                                                                      |
| D3  | Finding 4 is **won't fix**: `_handle_ack` publishes an unmatched node/gateway `sent` on purpose ("still reported for diagnosis", `storage/ingest.py` ~1472). Not user-visible.                                 |
| D4  | Campaign ends with push of both repos, `/dev-release`, deploy to mcapp.local and live verification.                                                                                                            |

## Reference rule (what the webapp must mirror)

MCProxy `store_message` inline path (`storage/ingest.py` ~1966-2020) + `_inline_ack_original`
(~203):

1. `echo_id` = digits of a **trailing** `{NNN` (`ACK_SUFFIX_RE`, `\{([0-9]+)$`); the ack's
   number must equal it exactly.
2. Window: `orig.timestamp > ack.timestamp - DEDUP_WINDOW_MS` (1 h), or, for a row at
   `delivery_status = 'held'`, `> ack.timestamp - HELD_ACK_WINDOW_MS` (168 h). One-sided: no
   upper bound.
3. Original sender (first comma component of `src`, `strip().upper()`) == ack frame `dst`
   resolved via `resolve_dst_target`; original target (`resolve_dst_target(dst)`) == ack frame
   sender. **Full callsign, SSID kept**, case-insensitive.
4. Candidates newest-first (`ORDER BY timestamp DESC`); first that satisfies 3 wins.
5. The padded callsign inside the ack payload is never used (truncated at 9 chars).

Out of scope, noted: mc-chat's matcher compares the own sender by BASE callsign and only over
own messages — a deliberate mock simplification, not touched here. `findStoOriginal` keeps its
symmetric window (not part of the issue).

## Corpus `ack_match_vectors.json` (v1)

```json
{
  "version": 1,
  "window_ms": 3600000,
  "held_window_ms": 604800000,
  "vectors": [
    {
      "name": "positive control",
      "resident": [
        {
          "id": "A",
          "src": "DK5EN-99",
          "dst": "DK1TCP-77",
          "msg": "hello{201",
          "timestamp": 1000000,
          "delivery_status": null
        }
      ],
      "ack": {
        "src": "DK1TCP-77",
        "dst": "DK5EN-99",
        "msg": "DK5EN-99 :ack201",
        "timestamp": 1060000
      },
      "expected": "A"
    }
  ]
}
```

`id` doubles as the webapp `msg_id`. Required cases: positive control; wrong addressee
(`DK1TCP-12 → DH6MAV :ack201`); other SSID of the target (`DL1ABC-2` acking a DM to `DL1ABC-1`);
other SSID of the addressee (ack to `DK5EN-98` for a DM from `DK5EN-99`); reverse steal (newer
third-party `DL9XX-5 → DB0B-1 "yo{123"` resident, real `DB0B-1 → me :ack123` must pick ours);
counter mid-text (null); `{1234` vs `:ack123` (null); window boundary at exactly -1 h (null) and
-1 h + 1 ms (match); original newer than the ack (match, no upper bound); held at -100 h (match)
vs the same row not held (null); held boundary at -168 h (null); via-routed dst
`RELAY-1,DK1TCP-77`; lowercase callsigns; two own DMs same target + counter (newest wins);
no-separator payload `DK1TCP-77:ack622`; truncated payload callsign from `OE1ABCD-12`. Distinct
timestamps within a vector (server order has no id tiebreak).

## Waves

| Wave | Repo    | Owner        | Files (exclusive)                                                                                                                                                                                                                                                                      | Status  |
| ---- | ------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| 0    | MCProxy | orchestrator | this plan, the issue copy                                                                                                                                                                                                                                                              | done    |
| 1a   | MCProxy | implementer  | `storage/ack_match_vectors.json` (new), `storage/ack_match_vectors_tests.py` (new)                                                                                                                                                                                                     | done    |
| 1b   | webapp  | implementer  | `services/messageProcessor/ackMatch.ts`, `services/messageProcessor.ts`, `constants/index.ts`, `services/__tests__/messageProcessor.{ackGate,core}.spec.ts`, `stores/__tests__/messages.{ack,filter}.spec.ts`, `stores/__tests__/messagesHydrate.spec.ts` (only existing ack fixtures) | done    |
| 1 G  | both    | orchestrator | register 1a in `scripts/run_startup_tests.py` `main()` (hotspot); gate both repos; advisor pass; commit per repo                                                                                                                                                                       | done    |
| 2    | webapp  | 1b, resumed  | copy corpus → `services/messageProcessor/__tests__/ack_match_vectors.json` + `ackMatchVectors.spec.ts` (sha256 pin, parsed-JSON drift vs `../MCProxy`, replay through `findAckMessage`)                                                                                                | done    |
| 3    | webapp  | implementer  | finding 2: `stores/messages.ts`, `types/message.ts`, `stores/__tests__/messagesHydrate.spec.ts` (+ the `msg:status` store method wherever it lives)                                                                                                                                    | done    |
| 3 G  | webapp  | orchestrator | gate, advisor pass, commit                                                                                                                                                                                                                                                             | done    |
| 4    | both    | orchestrator | docs: MCProxy CLAUDE.md corpus list + ACK Attribution note, webapp CLAUDE.md corpus list; push; `/dev-release`; deploy; verify on mcapp.local                                                                                                                                          | pending |

1a and 1b run in parallel: different repos, no shared build tree or runner. Wave 2 waits for the
committed corpus. Wave 3 is serialized after wave 2 because both touch `messagesHydrate.spec.ts`,
and because finding 2 must not ship ahead of finding 1 (it would make false ✓✓ permanent).

### Wave 1a — MCProxy corpus + suite

- Author the corpus above. Replay each vector through the **production** path: residents stored
  so `echo_id` is computed by production code (`store_message`, or a direct INSERT with
  `ACK_SUFFIX_RE` if `store_message` dedup gates interfere — report which), `delivery_status`
  set for held residents, then the ack frame through `store_message`; assert which row got
  `acked = 1` (or none). Temp DB, offline, mypy-strict clean.
- Pin the corpus sha256 in the suite, like `query_tests.py` `_ACK_VECTORS_EXPECTED_SHA256`.
- MCProxy is the reference: a vector that fails here is escalated, never "fixed" by editing the
  expected value or production code.
- Mutation check: break each leg of `_inline_ack_original` (sender, target), the window, and the
  held carve-out one at a time; each must turn at least one vector red. Clear `__pycache__`
  between mutation and restore.

### Wave 1b — webapp finding 1

- `findAckMessage(existing, ackNumber, src, dst, referenceTimestamp)`: rules 1-4 above, full
  callsign compare (`effectiveDst` for via-routed dst, `trim().toUpperCase()`), trailing counter
  only, one-sided window with `HELD_ACK_WINDOW_MS = 168 h` for `delivery_status === 'held'`.
  The early `break` must use the held horizon or held rows past 1 h are never reached.
- Caller in `messageProcessor.ts` passes the frame's `dst`.
- Regression tests in `messages.ack.spec.ts` (fail before, pass after): wrong addressee, other
  SSID, reverse steal, positive control; plus held 100 h and the -1 h boundary. Fix the stale
  comment near line 712.
- Existing assertions that encoded the lax rule are updated and each one is listed in the report.

### Wave 3 — webapp finding 2

- `ack_origin?: 'server' | 'local' | 'www'` on `Message`. `server`: `msg:status {acked}` or a
  snapshot with `acked = 1`. `local`: client matcher on a non-`websocket` source. `www`: client
  matcher on `source === 'websocket'`. Never downgrade `server` to anything.
- `source === 'initial'` reconcile: skip the `msg_ack` downgrade for `ack_origin === 'www'`; keep
  it for `local` and for rows without `ack_origin`.
- Persisted through the offline cache by `_patchMessage` (whole-row re-put) — test it hydrates.
- Regression test in `messagesHydrate.spec.ts`; the ctcping test ("REPORTED BUG (ctcping, cached
  rows)", ~333) stays green.

## Wave log

- **Wave 1** (2026-10-04): corpus v1 with 23 vectors (sha256 `b266e46a…`), replayed through
  production `store_message`; four mutations of `ingest.py` each turn vectors red. webapp
  `findAckMessage` mirrors the reference rule; four store-level regression tests fail on the old
  matcher. Advisor (Fable): APPROVED. Declined suggestion, recorded: the server takes the leading
  digits after `:ack` (`:ack201 ok` → 201), the webapp requires the tail to be all digits. Only a
  human-typed text produces that shape (firmware emits exactly `%-9.9s:ack%03i`), and treating it
  as an ack on the client would swallow a human message from view; not aligned.

- **Wave 2** (2026-10-04): webapp vendors the corpus (`services/messageProcessor/__tests__/`,
  sha256 identical to canonical), 23 replay tests + drift check against `../MCProxy` (ran, not
  skipped). Test-only, no advisor pass. Webapp `44f0370`.
- **Wave 3** (2026-10-04): ran in parallel with wave 2 (disjoint files). `ack_origin` on
  `Message`, `'www'` exempt from the snapshot downgrade, `_patchMessage` drops keys patched to
  `undefined`, rows built from a snapshot/history row with `acked` get `'server'`. Advisor
  (Fable): APPROVED with one test gap (the `'server'` stamp in `processMessage` was unpinned),
  closed at the gate with a discriminating test. Webapp `3922e84`. Recorded, not changed: a
  `'server'` row can still be downgraded by a snapshot without `acked` (only via pre-2026-09-06
  transport-pair rows in `smart_initial`); pre-existing and order-dependent.
- Wave 1 commits: MCProxy `2843b0d`, webapp `1dcb3be`.

## Verification

- MCProxy: `uvx ruff check`, `uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src`,
  `uv run python scripts/run_startup_tests.py`.
- webapp: `npm run lint`, `npm run typecheck`, `npm run format:check`, `npm test`.
- Live (wave 4): bundle filename on mcapp.local matches the release; an own DM acked by its
  addressee still shows ✓✓; a reconnect does not clear it.
