# Unread badge counts messages the webapp hides (group 20 `+1`)

Status: IN PROGRESS — wave plan below is the resume point.
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
