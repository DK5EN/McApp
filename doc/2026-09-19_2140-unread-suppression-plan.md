# Unread badge counts messages the webapp hides (group 20 `+1`)

Status: DONE — all waves committed on `development` in both repos. Not yet released or deployed.
Reported 2026-09-19 against v2.0.11-dev.3 on mcapp.local.

## BLUF

The sidebar badge never clears for a conversation whose **newest** message is one the webapp is
configured to hide. The server counts it as unread, the chat pane never renders it, and the read
cursor only advances over rendered bubbles — so the badge is unclearable by any client action.

Fix: apply the same suppression predicate server-side, where `unread` is minted. Plus: stop the
classifier claiming that a bare URL is a node advert.

## Root cause (evidence)

Live row on mcapp.local, group 20:

| field          | value                                                                             |
| -------------- | --------------------------------------------------------------------------------- |
| rowid / msg_id | 393160 / `5E71BD07`                                                               |
| ts             | 2026-09-19 20:28:31.724                                                           |
| src → dst      | HB9VQQ-1 → 20                                                                     |
| msg            | `https://www.varac-hamradio.com/group/varac-hf-discussion-forum/discussion/f20f…` |
| classifier     | `category=node_advert`, `info_score=0.3`, `tags=["has_url"]`                      |

`read_cursors['20'] = 1789842483985` — exactly the timestamp of row 393159, the last message
visible in the chat pane. `filter_prefs` on the box:
`{"enabled": true, "hiddenCategories": ["timestamp_beacon","bot_command","node_advert","sw_advert"], "minInfoScore": null, "hideAutoBeacons": false}`.

Chain:

1. `filterMessagesByDst`'s group branch drops the row via `isSpamByClassifier`
   (webapp `src/stores/messages/predicates.ts:170,180`).
2. No bubble → `useReadMarker` only marks elements carrying `data-conv-key`/`data-ts`
   (webapp `src/composables/useReadMarker.ts:41`) → `markRead` is never called with that ts.
3. `get_conversation_summary` counts every non-ack `type='msg'` row newer than the cursor
   (`src/mcapp/storage/query.py:632`) → `unread = 1`, forever.

Reproduced read-only against the live DB with the production summary SQL: `unread('20') = 1`,
contributed solely by HB9VQQ-1.

Scope: only a **trailing** suppressed message sticks — one mid-history is swept up when a later
visible bubble renders. Group 20 was the only stuck conversation in the last 7 days.

The same divergence exists for `blocked_texts`: the webapp hides those in chat **and** in its own
sidebar counter (`passesBaseGuards`), the server's `unread` does not apply them at all.

## Design

### D1 — `unread` excludes what the client will not render

One Python predicate, `src/mcapp/storage/suppression.py`, mirroring the webapp's
`isSpamByClassifier` decision order exactly, plus the `isTextBlocked` half of `passesBaseGuards`:

```
suppressed(view, policy) :=
      is_text_blocked(view.msg, policy.blocked_texts)          # case-insensitive substring
   or is_spam_by_classifier(view, policy)                      # webapp order, verbatim:
        #   prefs disabled            -> False
        #   template_hash in promoted -> True
        #   template_hash in demoted  -> False
        #   category in hidden        -> True
        #   hide_auto_beacons and 'auto_beacon' in tags -> True
        #   min_info_score is not None and info_score < it     -> True
```

`policy` is loaded inside `get_conversation_summary`'s existing `_run()` from the same `db_read`
connection: `filter_prefs` (one row), `blocked_texts`, and `beacon_templates WHERE user_action IS
NOT NULL` (14 858 template rows on mcapp.local, **0** with a user action — the promoted/demoted
sets are tiny by construction, never the whole table).

`unread` moves out of the aggregate SQL and is computed in Python over **candidate rows only**
(distinct message, `ts > cursor`, sender base ≠ mine). Bound: the retained message set, 5 741
`type='msg'` rows on mcapp.local — affordable even for a client with no cursors at all. This keeps
ONE implementation of the predicate; no SQL mirror to drift.

Preserved unchanged: distinct-message grouping by `msg_id` (transport copies, v2.0.4-dev.3), the
blocklist rebucketing to `SPAM_GROUP`, and the `MAX(original cursor, SPAM_GROUP cursor)` rule.

**`count` is deliberately NOT filtered.** It answers "how many messages does this conversation
hold", drives "N unread · M total", and matches the client's own total. Only `unread` changes
meaning: "unread **and** visible to you".

Backward compatible: `filter_prefs.enabled = false` and an empty blocklist yield today's numbers.

### D2 — a bare URL is not a node advert

Seed rule "URL advert" (priority 42, `^\s*(https?://|www\.)[^\s]+\s*$`) claims
`category: node_advert`. Shape alone is not evidence of an advert — HB9VQQ-1's plain link inside a
human thread is the report's own trigger, and his URL-plus-text message in the same thread scored
1.0.

Change: `category` → `other`, `extra_tags: ["has_url"]` kept, rule name kept (the seeding key).
Repetition, not shape, is what identifies a real beacon, and that path survives:

- `other` is **not** in `_HUMAN_CATEGORIES` (`classifier/template.py:49`), so template
  fingerprinting can still auto-promote a repeating URL to `auto_beacon`…
- …except that a bare URL normalises to the single token `URL` and
  `is_exempt` treats `<= _AUTO_BEACON_MIN_TOKENS (2)` as too short
  (`classifier/template.py:116`). So in practice the remaining signal is the score:
  `_MINIMAL_CONTENT_CAP` pins a bare URL at **0.30** (`classifier/score.py:52`), which the
  operator governs with `minInfoScore` — an information claim, not a false advert claim.
- The decorated-advert rules stay: "HTML advert" (36) and "Emoji URL advert" (37).

Requires the documented rule-mutation flow: `bump_classifier_version()` + `classifier.load()`, with
the startup backfill re-running once per version via `backfill_done:v{N}`.

## Wave plan

Ownership is by repo and file set; `scripts/run_startup_tests.py` and `src/mcapp/main.py` are
carved out to the orchestrator (both are integration hotspots that two waves would otherwise
share). No writer runs git.

| Wave   | Task                                                                        | Repo                           | Exclusive files                                                                                            | Status     |
| ------ | --------------------------------------------------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------- | ---------- |
| 1      | A1 predicate + corpus + unit suite                                          | MCProxy                        | `storage/suppression.py`, `storage/suppression_vectors.json`, `storage/suppression_tests.py`               | dispatched |
| 1      | B "URL advert" rule + regression test                                       | **mc-chat** (subtree upstream) | `meshcom_mock/classifier/seed.py`, `meshcom_mock/classifier/tests.py`                                      | dispatched |
| gate 1 | register suite; wire version bump; commit; `subtree split` + `subtree pull` | both                           | `scripts/run_startup_tests.py`, `src/mcapp/main.py`                                                        | —          |
| 2      | A2 `unread` integration + e2e regression                                    | MCProxy                        | `storage/query.py`, `storage/unread_suppression_tests.py`                                                  | pending A1 |
| 2      | C corpus copy, parity spec, local counters                                  | **webapp**                     | `src/stores/messages/{predicates,sidebar}.ts`, `src/stores/messages.ts`, `src/stores/messages/__tests__/*` | pending A1 |
| gate 2 | full gate both repos, advisor pass, commit                                  | all                            | —                                                                                                          | —          |

B must land upstream first because `src/mcapp/classifier/` is a vendored subtree; and the
`subtree pull` merge needs a clean MCProxy tree, which is why it sits in a gate and not in a wave.

### Orchestrator carve-out: the version bump

`seed_defaults()` matches a rule **by name** and updates pattern/category/priority/scope/extra_tags
in place, but it does **not** bump `classifier_version` — so a changed rule would reach new
messages only, and every stored row would keep its stale `node_advert`. `main.py`'s startup gains
the same postlude `sse_handler.after_rule_mutation` already uses for UI rule edits:

```
inserted, updated = await seed_defaults(storage_handler)
if inserted or updated:
    await storage_handler.bump_classifier_version()   # -> _maybe_backfill_classifier reclassifies
await classifier.load()
```

`_maybe_backfill_classifier` then finds no `backfill_done:v{N}` marker for the new version and
reclassifies every row below it as a batched background job (`force=False` — the version is
already bumped, which is exactly the case `reclassify`'s docstring reserves for rule-change flows).
Idempotent: the next start sees `updated = 0` and no bump.

## Wave 1 outcome (2026-09-19)

Closed and committed. What landed and what it cost:

- MCProxy `8d68929` — `storage/suppression.py`, the 21-vector sha256-pinned corpus and its
  46-case suite, registered in `scripts/run_startup_tests.py` as `suppression_predicate`
  (deliberately NOT `suppression`, which is already taken by `router.test_suppression_logic`).
- mc-chat `7f9bd45` — rule 42 now assigns `other`. Pulled into MCProxy as `955bc3b` /
  `ed51d7b`, verified byte-identical to upstream afterwards.
- MCProxy `5b52fc6` — the startup `bump_classifier_version()` carve-out.
- MCProxy `7666fd3` — `doc/spam-filter-BE.md`'s rule table row, which was doubly stale.

Two things the gate caught that the writers' own verification did not:

1. **mc-chat's canonical gate is pytest, not the classifier package's `run_all_tests()` helper.**
   Three pre-existing tests pinned bare-URL -> `node_advert`
   (`tests/test_classifier_rules.py::test_url_advert` and two `test_classifier_rules_v2.py`
   parametrised cases). They were correct failures and were updated to the new expectation.
2. **An empty `blocked_texts` pattern would diverge**: the webapp's `isTextBlocked` uses
   `String.includes`, and `''.includes('')` is true, so an empty pattern hides EVERYTHING there,
   while `policy_from_parts` drops empty patterns. Unreachable in practice —
   `BlockedTextRequest.text` carries `Field(min_length=1)` — so the corpus deliberately contains
   no empty-pattern vector and the server keeps the stricter reading. Revisit only if another
   write path to `blocked_texts` ever appears.

Verified at the gate, not taken from a report: full `scripts/run_startup_tests.py` exit 0,
`uvx ruff check` clean, `uvx ruff format --check .` clean (209 files, `.md` included),
`uv run mypy src/mcapp ble_service/src` clean (108 files); mc-chat `1904 passed`, mypy 145 files,
ruff clean. The corpus tripwire was mutation-checked: flipping one vector fails both the sha256
case and that vector's own assertion.

## Advisor pass (2026-09-19) — APPROVED, with test-only rework

An independent advisor mutation-tested the wave rather than reading it. No correctness finding
against `query.py`, `suppression.py` or the webapp counters survived; all six acceptance criteria
were confirmed against the live-data snapshot. Three findings were gaps in what the SUITE pins —
mutations M1 (own-sender guard removed), M2 (`applicable` hardcoded to `newer`) and M4 (candidate
query per physical row instead of per distinct message) all survived green. Closed in wave 3.

Confirmed rather than assumed, and worth keeping:

- **The SQLite bare-column guarantee holds for this query shape.** `_conv_dedup_subquery` has
  exactly one aggregate (`MIN(m.timestamp)`), so the five classifier columns come from the same
  input row as the min. Verified on SQLite 3.50.4 including the TIE case (two copies with an
  identical millisecond): both the aggregate-shaped and candidate-shaped queries pick the SAME
  row, and the live DB's 10 same-millisecond transport pairs agreed in all 10.
- **The two queries cannot disagree.** Same `dedup_sql` string object, same `params`, the
  candidate `WHERE` adds no placeholders, `read_cursors.key` is a PRIMARY KEY so the joins cannot
  fan out, and `newer_spam` implies `newer`.

### Known, accepted: the live broadcast carries no classifier fields

`store_message` uses `cls_cols` as INSERT parameters only (`storage/ingest.py:1846/1858/1892`) and
never writes `category`/`tags`/`info_score`/`template_hash` back into the dict
`sse_handler._broadcast_handler` broadcasts. So a live `node_advert` reaches the client
UNclassified: `isSpamByClassifier` returns false, the webapp's new `liveUnread` gate does not
fire, and the chat pane renders the message — which means the read marker advances over it and the
badge resolves on its own. Self-healing, not a stuck badge, and the server's `unread` is correct
throughout. The client gate is therefore inert against today's live payload and load-bearing only
after a reload, when history rows carry the classifier columns.

**Symptom to cause:** a transient `+1` on a live hidden message that clears by itself is this, not
a regression of the cursor fix. Do not chase it on the client.

The real repair is to put the classifier fields on the broadcast payload, which would also end the
inconsistency where a message looks unclassified live and classified after reload. That is a wire-
payload change with its own blast radius and is NOT part of this campaign — recorded here as the
follow-up.

### Out of scope, pre-existing (do not attribute to this wave)

Narrowed (`key=`) and full-scan `count` disagree for 10 keys on the live snapshot (key 20: 838 vs
794), identically at HEAD and after this change. Cause: 71 `msg_id` groups span more than one
conversation key, with spans of DAYS (firmware msg_id reuse — `690F6284` appears in `20`, `262`
and `*` over 9 days), while the dedup subquery groups by `msg_id` across the whole window with no
key or time fence. `unread` agreed for all 400 keys probed. Its own wave.

## Open follow-ups (not part of this campaign)

1. **Put the classifier fields on the live broadcast payload.** See the advisor note above: a
   message currently looks unclassified live and classified after a reload. Wire-payload change,
   own blast radius.
2. **`msg_id` groups that span conversation keys.** 71 groups on the live DB, spans of days, from
   firmware msg_id reuse. Makes narrowed and full-scan `count` disagree for 10 keys. Pre-existing,
   identical at HEAD, `unread` unaffected.
3. **Deploy.** Nothing is released. Reaching mcapp.local needs a dev release in both repos; the
   startup bump then backfills the stored `node_advert` rows once on first start.
