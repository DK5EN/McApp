# @-mention push notifications, and the ALL/TIME conversation-key asymmetry

**Status:** planned 2026-09-19, not started. Two independent pieces of work, planned together
because they were asked for together and because both are cross-repo corpus changes.

Operator decisions taken 2026-09-19 (all four as recommended): full 3-repo fix for ALL/TIME;
push contract bumped to **v11 authored in mc-chat**; mention matching is an **independent OR**
beside `dm`/`groups`/`broadcast`; **strict boundary** matching.

## Part A — @-mention push

### A1. What it is

A fourth push filter flag. With `mentions: true`, a message whose TEXT mentions the operator's
own callsign pushes **regardless of destination** — a group you do not follow, a broadcast with
`broadcast: false`, anything. Today such a message reaches you only if you already subscribed to
its whole destination.

### A2. The grammar, validated against live traffic before implementation

```
(?<![A-Za-z0-9])@<BASE>(?:-[0-9]{1,2})?(?![A-Za-z0-9])        case-insensitive
```

`<BASE>` is the operator's own callsign with the SSID stripped and uppercased (`DK5EN-98` →
`DK5EN`), so `@DK5EN`, `@DK5EN-12` and `@dk5en-98` all fire — the SSID is matched but not
compared, exactly as the unread-cursor scheme already treats own traffic by base callsign.

**Replayed against mcapp.local's 285 messages containing `@`**: 16 fire for `DK5EN`, all of them
genuine mentions from other stations, none from the operator (own-src is excluded by
`is_eligible` anyway). Three of the sixteen are the cases that justify the feature — a group
message (`DG0OPK-11 → 20`) and via-routed broadcasts (`DM3KS-12 → DB0ND-22,*`) that the
destination filters alone would never have delivered.

Negative controls, all silent: `sam@dk5en.de`, `SP sam@iam.com Subject` (a Winlink body — this
is why the leading boundary is not optional), `foo@DK5EN`, `@DK5ENX`, `@DK5EN2`,
`Hallo Om s@ Welche ant`, `back @ +6 dBm`. Positive controls that must keep firing:
`(@DK5EN)`, `@DK5EN.`, `@DK5EN...Smarthome`, `moin @DK5EN, 73`, `@DK5EN-12: danke`.

### A3. Where it sits in the gate order — load-bearing

The mention test is a **gate**, so it runs on `_build_gate_view`'s **unstripped** text, beside
eligibility and the destination match, and BEFORE `build_push_payload` strips the firmware ack
suffix. This is the ordering contract v7 already fixed in place; do not reorder it. Everything
downstream is unchanged: eligibility (non-chat frames and own-src excluded), the blocklist,
`PushDedup`, `PushCoalescer`, payload build.

Match semantics become:

```
push IFF is_eligible(msg) AND (
      ( target == own AND filter.dm )
   OR ( target == '*'  AND filter.broadcast )
   OR ( target in filter.groups )
   OR ( filter.mentions AND mentions_own_callsign(text, own) )   <- v11
)
```

`filter.mentions` defaults to **false**, so an existing subscription's behaviour is byte-identical
until the operator ticks the box. Note the contract's standing rule that a subscribe POST replaces
the filter wholesale: a client that POSTs without the new key clears it, which is correct and is
the client's obligation to get right (contract v6 `endpoints.subscribe.semantics`).

### A4. Three implementations, three vendored corpora

The contract governs three implementations — MCProxy `push_delivery.py`, mc-chat `push.py`, and
the webapp `pwa/pushFilter.ts` — and `push_contract.json` is vendored in three places, each with
its own sha pin. All three move together or the parity suites fail.

## Part B — the ALL/TIME conversation-key asymmetry

`compute_conversation_key` (`storage/constants.py`) applies no plausibility test on its DM
branch, so it keys `ALL<>DK6GC` and `DK6GC<>TIME` as ordinary DM pairs. The webapp refuses both
(`NON_PERSON_PAIR_MEMBERS`, added by Wave 2 of the digit-less-callsign plan), so such a
conversation would be advertised in the server summary and silently dropped by the client — the
same defect class as the WLNK bug, pointed at a different pair of strings.

**Never observed.** Zero rows in mcapp.local's 5488: `ALL` and `TIME` do not occur as wire
destinations at all, `*` is the broadcast (2873 rows), and the webapp's `Time` bucket is
client-side. This is a latent trap, not a live bug, and it is being closed on that basis.

**The fix:** treat `ALL`/`TIME` like `*` — return the target **verbatim** as the key, so the
conversation is a broadcast-style bucket rather than a DM pair. Case-insensitive test,
string-preserving key, exactly like the group and hashtag branches above it. The webapp then
needs `ALL` in the same pass-through set that already carries `*` and `Time`, or the key comes
back through `serverKeyForSidebarKey`'s own-DM fallback and gets paired again — which would
re-create the bug at one remove.

`conversation_key_vectors.json` goes to **v5** with vectors for `ALL`, `TIME`, `Time`, `all` and
the `X<>ALL` shape that must no longer be produced. Canonical HERE; hand-copied to mc-chat
(`tests/fixtures/`) and the webapp (`src/utils/__tests__/`), and the webapp's `EXPECTED_SHA256`
re-captured in the same commit.

### A5. The mention test must see the FULL text — not the payload, not the gate view

`_payload_fields` (`push_delivery.py:125`) truncates to `MAX_TEXT_LEN = 120`, and **both**
`build_push_payload` and `_build_gate_view` go through it. So neither of the two views the
delivery path already has carries the whole message. A MeshCom DM runs to ~149 characters
(`:{dst}msg` in a 160-byte buffer) and mcapp.local holds **162 rows longer than 120 chars**,
longest 150.

Today that costs nothing — of 264 callsign-shaped mentions in the live DB, **0** sit past
character 120, because a mention is an addressing token and addressing tokens come first. But
"the message mentioned me and no push arrived" is a silent failure with no diagnosable trace, so
the mention test takes the RAW text (`raw_message['msg']`), not a truncated view. The contract
clause must say this explicitly and name the 120-char cap as the trap, or the next implementation
will reach for `payload['text']` because it is the obvious thing to reach for.

Stripping, by contrast, is provably irrelevant here: `strip_ack_suffix` only removes a trailing
`\{[0-9]+`, `{` is already a boundary character, and a run of digits cannot contain a callsign —
so it can neither create nor destroy a match. Documented rather than guarded.

### A6. mc-chat's matcher has the wrong shape and must change

MCProxy's `matches(payload, own_callsign, filt)` (`push_delivery.py:331`) receives a dict and can
reach the text; mc-chat's `matches(msg_dst, own_callsign, filt)` (`meshcom_mock/push.py:143`)
receives **only the destination string**. The contract cannot be satisfied there without a
signature change. The webapp's `matchesPushFilter(msg, filter, ownCall)`
(`pwa/pushFilter.ts:208`) already takes the whole message.

## Wave map

Ownership tables and the dispatch plan follow once recon lands. Fixed ordering constraints:

1. **Part B is independent of Part A** — different files in all three repos, so it can run in
   parallel with the contract work.
2. **The contract must be authored in mc-chat first.** It reaches MCProxy only by
   `git subtree split` + `git subtree pull --squash`, which is an orchestrator-only git
   operation, and no implementation wave here or in the webapp can start before it lands.
3. MCProxy and webapp implementation waves are disjoint by repo and can run in parallel once the
   contract exists.

### Ownership

| Wave | Repo    | Exclusive files                                                                                                                                                                                            |
| ---- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1a  | mc-chat | `contract/push_contract.json`, `meshcom_mock/push.py`, `tests/test_push.py`                                                                                                                                |
| W1b  | MCProxy | `src/mcapp/storage/constants.py`, `src/mcapp/storage/conversation_key_vectors.json`, its replaying suite                                                                                                   |
| W2a  | MCProxy | `src/mcapp/push_delivery.py`, `src/mcapp/sse_routes/push.py`, `src/mcapp/push_tests.py`                                                                                                                    |
| W2b  | webapp  | `src/pwa/pushFilter.ts`, `src/composables/usePushNotifications.ts`, `src/components/settings/PushNotificationsCard.vue`, `src/stores/userSettings.ts`, their specs, `src/pwa/__tests__/push_contract.json` |
| W2c  | webapp  | `src/utils/callsignUtils.ts`, `src/utils/__tests__/callsignUtils.spec.ts`, `src/utils/__tests__/conversation_key_vectors.json`                                                                             |
| W2d  | mc-chat | `meshcom_mock/storage.py`, `tests/fixtures/conversation_key_vectors.json`, `tests/test_storage.py`                                                                                                         |

W2b and W2c share the webapp's vitest runner but no files: each writer runs only its own spec,
the whole-suite run belongs to the gate (`orchestrate-waves` §1.1).

### Orchestrator-owned, never delegated

- `git subtree split` in mc-chat + `git subtree pull --prefix=src/mcapp/contract` here, at gate 1.
  No agent runs git.
- Copying `conversation_key_vectors.json` v5 from the canonical MCProxy copy to mc-chat and the
  webapp, and re-capturing the webapp's `EXPECTED_SHA256` — the corpus is the hotspot, so it is
  carved out of every brief.
- Re-capturing the push-contract sha in `push_tests.py` and `pushFilter.spec.ts` after the pull.

### Gate order

Gate 1 after W1a+W1b: subtree pull, corpus fan-out, both repos' full gates. Gate 2 after
W2a-W2d: all three repos' full gates, then ONE advisor pass over the whole campaign diff, then
commits — per repo, explicit paths.

## Outcome — implemented 2026-09-19

Both parts shipped, all three repos, advisor-gated. Unreleased.

| Wave | Repo    | Commit                                       |
| ---- | ------- | -------------------------------------------- |
| B    | MCProxy | `c52dd1a` non-person alias branch, corpus v5 |
| W1a  | mc-chat | `0c1dc7e` contract v11                       |
| W2d  | mc-chat | `227333e` conversation-key mirror            |
| W2a  | MCProxy | `57c85b3` mentions disjunct                  |
| —    | MCProxy | `e2665fc` subtree pull of v11 + sha re-pin   |
| W2b  | webapp  | `7c14727` mention predicate, filter, toggle  |
| W2c  | webapp  | `198fd5f` ALL/TIME pass-through, both gates  |

**Deviations from the plan as written.**

- The normative matcher is **lookaround-free** and uppercases both sides, rather than the
  case-insensitive lookbehind form §A2 specified. Two reasons, both found at the advisor gate:
  `(?<!…)` is Safari 16.4+ only and throws at regex CONSTRUCTION on older iOS, which would take
  the webapp's whole `pushFilter` module down rather than just the mention branch; and under
  case-insensitive matching the ASCII boundary classes stop being ASCII, where Python and JS
  disagree. Verified equivalent to the original form on every case before adopting it.
- **`mentions` defaulting to false was unpinned** by the first 24 vectors — an implementation
  defaulting a missing key to `true` passed all of them, because every mention vector spelled the
  key explicitly. That is exactly the shape of a stored pre-v11 subscription row. A vector with the
  key ABSENT now pins it; 27 vectors in total.
- **The `isGroupOrBroadcastKey` half of Part B was not in the plan at all.** `serverKeyForSidebarKey`
  is only one of the two client-side places that decide "is this key a person"; the other gates
  which server summary keys are surfaced with no local traffic yet and knew only the exact-case
  `Time`. Found by the wave-2 writer as an escalation and fixed by the orchestrator.
- **§A5's trap was stated but unenforced.** Both backends' tests exercised `matches()` directly, so
  the dispatcher call site was free to pass the truncated payload text: mutating it left both
  suites fully green. Each backend now has a dispatcher-level test driving a >120-char message
  whose mention starts past the cap, and both mutants are confirmed dead.

**Left open, deliberately.**

- `sidebarKeyFor` (`stores/messages/sidebar.ts`) still uses the older group/broadcast test without
  the alias check, so a LIVE third-party `→ ALL` message keys to null while the server summary now
  surfaces `ALL`. Pre-existing, out of this campaign's scope, but the two predicates no longer
  mirror each other — worth a follow-up.
- A legacy `ALL<>X` row written before `c52dd1a` would not round-trip. Zero such rows exist on
  mcapp.local, which is why this is a note and not a migration.
- `dst_kind("TIME")` (`commands/parsing.py`) returns `"direct"` while the conversation-key branch
  treats it as non-person. Pre-dates this work; its own change if it ever matters.
- `@mentions` are matched against the operator's OWN base callsign only. A user-editable watch list
  was never in scope for v1.
