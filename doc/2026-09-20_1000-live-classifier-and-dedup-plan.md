# Live classifier fields + distinct-message dedup fences

Status: DONE — both waves implemented and gated on `development`. Not yet released.
The two follow-ups left open by `doc/2026-09-19_2140-unread-suppression-plan.md` §"Open follow-ups",
items 1 and 2. Both are MCProxy-only — the webapp already reads every field involved.

## Wave status log

| Wave | Scope                                                    | Status |
| ---- | -------------------------------------------------------- | ------ |
| 1A   | Classifier fields on the live broadcast payload          | done   |
| 1B   | Dedup gains sender / conversation-key / time fences      | done   |
| 2    | Suite registration, `sender_base_sql` switch, docs, gate | done   |

## F1 — The live broadcast carries no classifier fields

`MessageRouter.publish` hands **one** `data` dict to every subscriber in subscription order.
`_storage_handler` (subscribed in `MessageRouter.__init__`, therefore first) classifies inside
`store_message` and uses the result as INSERT parameters only; `SSEManager._broadcast_handler`
(subscribed later, when the manager is built) broadcasts that same dict. So a `node_advert`
reaches the client UNclassified live and classified after a reload:

- `isSpamByClassifier` cannot fire live, so the webapp renders a message it would hide,
- the read marker advances over it, and the badge resolves itself — self-healing, but the live
  and reloaded views of the same message disagree.

### Fix

`store_message` annotates the shared `message` dict with the classification it just computed,
using the **same shaping `_build_message_dict` applies to a stored row**, so live and history
agree by construction:

| key              | value                                                                | emitted when |
| ---------------- | -------------------------------------------------------------------- | ------------ |
| `category`       | `cls.category`                                                       | not None     |
| `tags`           | `list(cls.tags)` — a LIST, never the JSON string that goes to SQLite | **not None** |
| `info_score`     | `cls.info_score`                                                     | not None     |
| `template_hash`  | `cls.template_hash`                                                  | truthy       |
| `classifier_ver` | `cls.classifier_version`                                             | not None     |

`raw_json` is serialized by `_storage_handler` **before** `store_message` runs, so the stored
`raw` column is untouched. `_get_event_type` reads only `type`/`msg`/`src_type`/`TYP`/`command`,
so the SSE event name cannot change. Push (`_payload_fields`) and the command handler read named
keys only.

### Deliberately NOT covered (document, do not fix here)

- **A classifier failure annotates nothing.** The presence rules above leave the dict as it was,
  which is exactly today's behaviour for an unclassified message.
- **The transport-duplicate second copy is broadcast unannotated.** `store_message` returns at
  the dedup gate before classification; the webapp dedups to the first copy, which is annotated.
- **A blocked-but-rebucketed message is broadcast unannotated.** `_storage_handler` returns
  before `store_message` for any non-`pass` blocklist decision, so SPAM_GROUP live traffic has no
  classification. It is already quarantined.
- **Frames that are never stored** (`{CET}`, other `_should_filter_message` drops) stay
  unclassified live, as they are absent from history.

### The ordering this depends on

`_storage_handler` must precede `_broadcast_handler` in `_subscribers["mesh_message"]`. It does,
because the router subscribes storage in its own `__init__` and the SSE manager cannot be built
before the router exists. That is an invariant, not a coincidence, and gets a test.

## F2 — `msg_id` groups span conversation keys, senders and days

`_conv_dedup_subquery` groups by `COALESCE(NULLIF(m.msg_id,''), 'row:'||m.rowid)` alone. The
INGEST dedup rule (`_find_duplicate_row_id`) is strictly narrower: same `msg_id` **and** same
resolved sender base **and** within `DEDUP_WINDOW_MS` (60 min). The query-side rule therefore
over-collapses genuinely distinct messages that reuse a firmware `msg_id`.

### Evidence (live snapshot, 2026-09-19, whole `type='msg'` table, 5744 rows)

572 multi-row `msg_id` groups, and they separate perfectly:

| shape                 | groups | span                    | spans >1 key | >1 sender base | >1 dst |
| --------------------- | -----: | ----------------------- | -----------: | -------------: | -----: |
| real transport pairs  |    454 | <= 172 ms, max size 2   |            0 |              0 |      0 |
| firmware msg_id reuse |    118 | >= 3.33 h (the minimum) |           71 |              1 |      — |

Nothing lands between 172 ms and 3.33 h. A 60-minute fence separates the two populations with a
3.3x margin and cannot split a real pair.

### Fix

Mirror the ingest rule. A message is identified by FOUR legs, not one:

| leg              | expression                                                                      |
| ---------------- | ------------------------------------------------------------------------------- |
| msg_id group     | `COALESCE(NULLIF(m.msg_id, ''), 'row:' \|\| m.rowid)`                           |
| sender base      | `sender_base_sql("m.src")` — the SAME helper `_find_duplicate_row_id` now calls |
| conversation key | `COALESCE(m.conversation_key, m.dst)`                                           |
| time fence       | `_CONV_ANCHOR_SQL` — see below                                                  |

The time fence is the timestamp of the **earliest copy within `DEDUP_WINDOW_MS`
before this row**, computed by a correlated subquery carrying the same universe
filters (`type = 'msg'`, non-ack) as the query it fences. Two rows group together
only when they resolve to the same anchor, so a group closes as soon as a gap
wider than the window opens.

It is deliberately **not** a modulo bucket (`m.timestamp / W`). A fixed boundary
can fall between the two copies of one real transport pair (≤ 172 ms apart) and
split them into two "messages" — which is exactly the unclearable `+1` badge
`doc/2026-09-19_2140-unread-suppression-plan.md` exists to fix. An anchor derived
from the data can never do that. `conv_dedup_tests.py` case 2 pins it with a pair
placed at `k*W - 60` and `k*W + 60`.

`MIN(m.timestamp) OVER (… RANGE BETWEEN W PRECEDING AND CURRENT ROW)` was
implemented first and replaced because it costs a second sort where the
correlated form is an index seek (numbers below). The two return a
**byte-identical result set on the live DB** — but they are NOT identical in
general, and the difference matters at exactly one point: a `RANGE` frame is
**inclusive** of the row exactly `W` back, while `_CONV_ANCHOR_SQL` is
**strict** (`p.timestamp > m.timestamp - W`). Two copies exactly
`DEDUP_WINDOW_MS` apart are one message under the window form and two under the
shipped one. The shipped, strict form is the correct one: it is what
`_find_duplicate_row_id` uses (`timestamp > ?`), so the read-time fence and the
ingest fence agree at the boundary. There are no such pairs on the live DB, which
is why the result sets matched. `conv_dedup_tests.py` pins it.

The subquery keeps **exactly one aggregate** (`MIN(m.timestamp)`), so the SQLite
bare-column guarantee that `src`/`dst`/`msg`/`category`/`tags`/`info_score`/
`template_hash` come from the earliest copy is unchanged — it is the property the
suppression subtraction rests on. A correlated subquery in the `GROUP BY` is not
an aggregate of that query, so it does not disturb it.

One limit of that guarantee, worth stating because the suite brushes against it:
SQLite does **not** extend it to ties. "If the same minimum or maximum value
occurs on two or more rows, then bare values might be selected from any of those
rows. The choice is arbitrary" (sqlite.org/lang_select.html §2.5). The live DB
holds 10 same-millisecond transport pairs; in all 10 both copies carry identical
text, so nothing observable depends on the choice. `conv_dedup_tests.py` case 7
pins the behaviour actually observed, not a promise.

### Measured

Correctness, against the live snapshot through the real `get_conversation_summary`:

| metric                                   |    before |     after |
| ---------------------------------------- | --------: | --------: |
| keys where narrowed `count` != full scan |        10 |     **0** |
| distinct messages, whole table           |      4823 |      4955 |
| key `20` count (narrowed / full scan)    | 838 / 794 | 842 / 842 |
| `count` changed                          |         — |   11 keys |
| `unread` changed                         |         — |     1 key |
| `last_ts` changed                        |         — |    3 keys |

Cost, median of 9 runs on mcapp.local's Pi Zero 2W against the live database
(SQLite 3.46.1), 90-day window:

| form                        | full scan | narrowed `key=20` |
| --------------------------- | --------: | ----------------: |
| before                      |    275 ms |             87 ms |
| `RANGE` window function     |    699 ms |            156 ms |
| correlated anchor (shipped) |    517 ms |            126 ms |

**Those are the dedup subquery in isolation and they understate the real cost by about
half.** Measured end-to-end through `get_conversation_summary` on the deployed box
(slot-0 = old, slot-2 = shipped, same live snapshot, 2026-09-20):

| slot            | full scan | narrowed `key=20` | narrowed != full | total `count` |
| --------------- | --------: | ----------------: | ---------------: | ------------: |
| slot-0 (before) |    460 ms |            178 ms |               10 |          4876 |
| slot-2 (after)  |    990 ms |            266 ms |            **0** |          5014 |

The gap is structural: the dedup subquery costs 313 ms and **both** the aggregate and the
candidate query execute it, so it is paid twice per call (480 ms + 427 ms measured
separately). Materialising it once — a temp table, or one query instead of two — is the
lever if this needs to come down. Not done here: it is a separate change with its own
blast radius, and the two queries sharing one `dedup_sql` string is exactly what keeps
their predicates from drifting.

The full scan runs once per client connect (the SSE burst, which `StallMiddleware`
deliberately does not time) and off-loop in a worker thread. The narrowed form is
the per-`POST /api/read_cursor` refresh. Watch `pool_wait` after deploy.

### The one `unread` change is a recovery, not a regression

Key `26386`, `unread` 0 → 1. `msg_id 55A8F2B4` from `DK5DM-0` appears twice in that
same conversation, **11 days apart** — `":-)"` on 09-08 and `"Schönen Abend in die
Südost Bayern Gruppe.73"` on 09-19 21:29. The old rule collapsed them into one
message carrying the OLDEST timestamp, which sits **before** the read cursor
(09-19 09:25), so the 21:29 message was invisible to the badge and `last_ts`
reported 09:25. This is the mirror image of the group-20 bug: that one showed a
phantom unread, this one **hid a real** one.
