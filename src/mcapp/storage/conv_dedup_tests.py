"""Regression suite for `_conv_dedup_subquery`'s sender/key/time-fenced
distinct-message grouping (`storage/query.py`).

doc/2026-09-20_1000-live-classifier-and-dedup-plan.md §F2: the pre-fix
subquery grouped every row sharing a `msg_id` (or, absent one, its own
rowid) into ONE "message", full stop. A firmware `msg_id` is a node-local
32-bit counter that gets reused across stations, conversations and days (118
reuse groups on the live snapshot, spans >= 3.33 h, 71 of them crossing more
than one conversation key), so that bare grouping silently collapsed
genuinely distinct messages together. The fix mirrors the INGEST dedup rule
(`_find_duplicate_row_id`, storage/ingest.py) exactly: same `msg_id` group
AND the same resolved sender base (`sender_base_sql`, storage/constants.py —
shared text, so the two boundaries cannot drift) AND the same conversation
key, with a data-anchored time fence (never a modulo/division bucket,
which would split a real <=172 ms transport pair and resurrect the
unclearable "+1" badge doc/2026-09-19_2140-unread-suppression-plan.md fixed).

Mirrors `storage.unread_suppression_tests`'s house pattern: a `(label, ok)`
results list, a PASS/FAIL line per case, a bool return, an ephemeral tempfile
SQLite DB per scenario via `create_sqlite_storage`, and real production entry
points (`store_message`, `get_conversation_summary`) rather than a
reimplementation of their logic.

Fixture note on "two physical rows, one msg_id": storage/ingest.py's dedup
gate (2026-09-06) now ENRICHES a genuine in-window, same-sender duplicate
frame into the existing row instead of inserting a second one — real
transport-duplicate PAIRS (the shape cases 1/2/7 need) therefore no longer
land as two physical rows going forward, only in DB rows written before that
fix. `_duplicate_row` below writes the second physical row directly with
`storage._mutate`, exactly the pattern
`unread_suppression_tests._test_transport_duplicates_suppressed` uses, so
these fixtures exercise the query-side fence against the shape historical
data still has, not against what today's ingest path produces.

Coverage (per the wave brief):
  1. A same-msg_id, same-sender, same-key transport pair 68 ms apart -> ONE
     message.
  2. A pair straddling a `DEDUP_WINDOW_MS` multiple in absolute timestamp
     terms (t = k*W - 60, t+120) still collapses to ONE message. This is the
     case that discriminates the data-anchored fence (correct) from a
     modulo/division bucket (would split this exact pair).
  3. Same `msg_id` reused 3.5 h later in a DIFFERENT conversation key -> two
     distinct messages, one under each key.
  4. Same `msg_id` reused DAYS later in the SAME conversation key -> two
     distinct messages.
  5. Same `msg_id` from two different sender bases (one via a relayed src) ->
     two distinct messages, `unread` still excludes only the caller's own.
  6. Narrowed (`key=`) vs full-scan (`key=None`) parity across every shape
     above in one combined fixture — the wave's own acceptance criterion (10
     keys disagreed on the live snapshot before this change, 0 after).
  7. Same-row-picked-under-a-tie: a same-millisecond TIE between two
     physical rows that differ in `msg`, `category` AND `info_score`. Probes
     `_conv_dedup_subquery` directly to see which physical row it exposes,
     then asserts `get_conversation_summary`'s `unread` (which runs the
     aggregate query AND, once anything might be suppressed, the candidate
     query) agrees with that SAME row's category — proving the two internal
     queries cannot have picked different rows for the tie. NOTE: SQLite's
     bare-column rule explicitly does NOT promise a deterministic pick on a
     tie ("the choice is arbitrary", https://www.sqlite.org/lang_select.html
     §2.5) — this case pins that the aggregate and candidate query agree
     with EACH OTHER on whichever row they pick, not that the pick itself
     is guaranteed stable.
  8. Non-regression: an ordinary conversation with no `msg_id` reuse and no
     duplicates gets the literal count/unread/last_ts it always would have.
  9. Query plan: `EXPLAIN QUERY PLAN` on the aggregate query still shows
     BOTH `idx_messages_type_timestamp` (the outer scan) AND
     `idx_messages_msgid_timestamp` (the anchor's own correlated-subquery
     seek, `_CONV_ANCHOR_SQL` — the entire basis of choosing this form over
     the equivalent, and slower, window-function one). Asserting only the
     first would not detect a regression in the anchor's own index usage,
     which is the half this fix was actually about.
  10. The anchor's OWN sender-base/conversation-key equality legs
      (`_CONV_ANCHOR_SQL`'s `p`-side `AND` clauses) are load-bearing:
      deleting them lets a same-msg_id, same-key, DIFFERENT-sender row drag
      a real transport pair's anchor backwards, splitting the pair into two
      messages. Three rows, one msg_id, one key: a foreign sender just
      inside the window, then a same-sender pair 68 ms apart — shipped
      code reports 2 distinct messages (the foreign row, plus the collapsed
      pair); with the equality legs removed it reports 3 (the pair splits).
  11. The time fence's WIDTH (`DEDUP_WINDOW_MS` inside `_CONV_ANCHOR_SQL`) is
      pinned by a same-msg_id/sender/key pair ~5 minutes apart -> ONE
      message. A narrowed window (e.g. 1000 ms) would report two.
  12. The time fence's STRICTNESS is pinned by a pair exactly
      `DEDUP_WINDOW_MS` apart -> TWO distinct messages, because
      `_CONV_ANCHOR_SQL` uses strict `>` (matching `_find_duplicate_row_id`'s
      own `timestamp > ?`) — an inclusive `>=` at this exact boundary would
      report one. This is the one place a `RANGE ... PRECEDING` window
      function (inclusive by definition) would have disagreed with the
      correlated-subquery form actually shipped.
  13. The GROUP BY's own sender-base and conversation-key LEGS are not made
      redundant by the anchor's `p`-side equality clauses: a same-millisecond
      collision (two rows, same msg_id, same timestamp, differing only in
      sender OR only in conversation key) makes each row's anchor resolve to
      the SAME value (its own timestamp) independently of the other, which
      is exactly the case that separates "the anchor legs already imply
      distinctness" (false) from "the GROUP BY legs are load-bearing in
      their own right" (true). Dropping either leg from the GROUP BY
      collapses that pair into one message and drops one station's/key's
      message from `count` entirely.

Every case that a bare `msg_id`-only grouping would get wrong (3, 4, 5, 6) is
run against the OLD `_conv_dedup_subquery` first and confirmed to fail there;
cases 10-13 are each run against a targeted mutation of `_CONV_ANCHOR_SQL` /
the GROUP BY clause and confirmed to fail there too -- see the wave report
for both "before" runs. This file only pins the fixed behaviour going
forward.
"""

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from ..commands.parsing import SPAM_GROUP
from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import DEDUP_WINDOW_MS, LONG_RETENTION_DAYS, SECONDS_PER_DAY, db_read
from .query import (
    _CONV_CURSOR_JOINS,
    _CONV_NEWER_EXPR,
    _CONV_NEWER_SPAM_EXPR,
    _conv_dedup_subquery,
)

logger = get_logger(__name__)

MY_CALLSIGN = "DK5EN-98"


def _window_cutoff_ms() -> int:
    """Same cutoff formula `get_conversation_summary` uses, reproduced so
    the direct-SQL probes in this file scan the identical window.
    """
    return now_ms() - LONG_RETENTION_DAYS * SECONDS_PER_DAY * 1000


async def _store_msg(  # noqa: PLR0913 - one arg per message field, mirrors unread_suppression_tests._store_msg
    storage: Any,
    src: str,
    dst: str,
    msg: str,
    ts: int,
    *,
    msg_id: str | None = None,
    src_type: str = "lora",
) -> None:
    """Drive a chat message through the REAL ingest path (`store_message`),
    matching `unread_suppression_tests._store_msg`.
    """
    payload: dict[str, Any] = {
        "src": src,
        "dst": dst,
        "msg": msg,
        "type": "msg",
        "timestamp": ts,
        "src_type": src_type,
    }
    if msg_id is not None:
        payload["msg_id"] = msg_id
    await storage.store_message(payload, raw="")


async def _duplicate_row(  # noqa: PLR0913 - one arg per overridable column, mirrors _store_msg
    storage: Any,
    msg_id: str,
    ts: int,
    *,
    src_type: str = "ble_remote",
    dst: str | None = None,
    conversation_key: str | None = None,
    msg: str | None = None,
    category: str | None = None,
    info_score: float | None = None,
) -> None:
    """White-box: insert a SECOND physical row for `msg_id`, copying
    src/dst/conversation_key from the existing row and overriding
    `dst`/`conversation_key`/`msg`/`category`/`info_score` when given. See
    the module docstring's fixture note for why this, not a second
    `store_message` call, is how a fixture gets two physical rows for one
    transport-duplicate pair. `dst`/`conversation_key` exist for case 13's
    cross-key half: a same-sender, same-msg_id, same-timestamp row under a
    DIFFERENT key would otherwise be merged by ingest's own dedup gate
    (`_find_duplicate_row_id` matches on msg_id + sender + time only, not
    dst), which is exactly why that fixture needs the white-box path too.
    """
    rows = await storage._query(
        "SELECT src, dst, conversation_key, msg FROM messages"
        " WHERE msg_id = ? ORDER BY id ASC LIMIT 1",
        (msg_id,),
    )
    if not rows:
        raise AssertionError(f"no existing row for msg_id={msg_id!r} to duplicate")
    base = rows[0]
    await storage._mutate(
        "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type,"
        " conversation_key, category, info_score)"
        " VALUES (?, ?, ?, ?, 'msg', ?, ?, ?, ?, ?)",
        (
            msg_id,
            base["src"],
            dst if dst is not None else base["dst"],
            msg if msg is not None else base["msg"],
            ts,
            src_type,
            conversation_key if conversation_key is not None else base["conversation_key"],
            category,
            info_score,
        ),
    )


async def _test_transport_pair_collapses(results: list[tuple[str, bool]]) -> None:
    """Case 1: an ordinary same-msg_id/sender/key pair 68 ms apart is ONE
    message, count and unread alike.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case1.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 1_000_000
            await _store_msg(
                storage, "OE1ABC-1", "42", "hello mesh", t0, msg_id="AABBCCDD", src_type="udp"
            )
            await _duplicate_row(storage, "AABBCCDD", t0 + 68)

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get("42", {})
            results.append(
                (
                    (
                        "case 1: two transport copies of one frame (68 ms apart)"
                        " collapse to ONE message (count=1, unread=1)"
                    ),
                    entry.get("count") == 1 and entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_hour_boundary_pair_collapses(results: list[tuple[str, bool]]) -> None:
    """Case 2: the anchor-vs-modulo discriminator. `t0` is picked so it sits
    just BELOW a `DEDUP_WINDOW_MS` multiple in absolute timestamp terms
    (`t0 = k*W - 60`) and the duplicate lands just ABOVE it (`t0 + 120`) --
    a `timestamp // DEDUP_WINDOW_MS` bucket would put them in different
    buckets and report two messages; the `_CONV_ANCHOR_SQL` fence this
    subquery actually uses is anchored on the data, not on absolute time,
    so it never splits
    a pair this close.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case2.db"
        storage = await create_sqlite_storage(db_path)
        try:
            now = now_ms()
            k = now // DEDUP_WINDOW_MS
            t0 = k * DEDUP_WINDOW_MS - 60
            if t0 // DEDUP_WINDOW_MS == (t0 + 120) // DEDUP_WINDOW_MS:
                raise AssertionError(
                    "fixture bug: t0/t0+120 must straddle a DEDUP_WINDOW_MS multiple"
                )
            await _store_msg(
                storage, "OE1ABC-1", "42", "hello mesh", t0, msg_id="10203040", src_type="udp"
            )
            await _duplicate_row(storage, "10203040", t0 + 120)

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get("42", {})
            results.append(
                (
                    (
                        "case 2: a pair straddling a DEDUP_WINDOW_MS boundary (t=k*W-60,"
                        " t+120) still collapses to ONE message -- data anchor, not a"
                        " modulo bucket"
                    ),
                    entry.get("count") == 1 and entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_reuse_across_conversation_keys(results: list[tuple[str, bool]]) -> None:
    """Case 3: the same msg_id, same sender, reused 3.5 h later (> the 60-
    minute DEDUP_WINDOW_MS) in a DIFFERENT conversation key -> two distinct
    messages, one counted under each key. A bare msg_id grouping (the old
    rule) would collapse these into ONE, dropping the second key's message
    from its own conversation's count entirely.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case3.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 500_000
            t1 = t0 + int(3.5 * 3600 * 1000)
            await _store_msg(
                storage, "IV3OEP-10", "55", "first send", t0, msg_id="7788AA00", src_type="udp"
            )
            await _store_msg(
                storage,
                "IV3OEP-10",
                "77",
                "reused msg_id, different group",
                t1,
                msg_id="7788AA00",
                src_type="udp",
            )

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            e55 = summary.get("55", {})
            e77 = summary.get("77", {})
            results.append(
                (
                    (
                        "case 3: msg_id reused 3.5h later under a different key ->"
                        " two distinct messages, one per key (55: count=1/unread=1,"
                        " 77: count=1/unread=1)"
                    ),
                    e55.get("count") == 1
                    and e55.get("unread") == 1
                    and e77.get("count") == 1
                    and e77.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_reuse_same_key_days_later(results: list[tuple[str, bool]]) -> None:
    """Case 4: the same msg_id, same sender, same key, reused DAYS later ->
    two distinct messages. A bare msg_id grouping would collapse these into
    ONE and leave the newer copy invisible to the read cursor forever
    (exactly the "+1 forever" shape the suppression wave fixed for the
    classifier-hidden case, here from firmware msg_id reuse instead).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case4.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 8 * 24 * 3600 * 1000
            t1 = t0 + 3 * 24 * 3600 * 1000
            await _store_msg(
                storage, "IV3OEP-10", "55", "first", t0, msg_id="ABCDEF12", src_type="udp"
            )
            await _store_msg(
                storage,
                "IV3OEP-10",
                "55",
                "reused msg_id, same group, days later",
                t1,
                msg_id="ABCDEF12",
                src_type="udp",
            )

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get("55", {})
            results.append(
                (
                    (
                        "case 4: msg_id reused days later in the SAME key ->"
                        " two distinct messages (count=2, unread=2)"
                    ),
                    entry.get("count") == 2 and entry.get("unread") == 2,
                )
            )
        finally:
            await storage.close()


async def _test_reuse_across_sender_bases(results: list[tuple[str, bool]]) -> None:
    """Case 5: the same msg_id from two different sender bases -- the second
    src carries a relay path ('DK5EN-10,DK5EN-98'), which `sender_base_sql`
    strips down to its FRONT component ('DK5EN-10'), still distinct from
    'IV3OEP-10'. Two distinct messages: count=2. `unread` stays 1 because the
    second row's sender base collapses to the caller's own BASE callsign
    ('DK5EN') and the own-traffic exclusion still applies per row, unaffected
    by the dedup change. A bare msg_id grouping (the old rule) would report
    count=1 for this key, silently dropping one sender's message.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case5.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 600_000
            t1 = t0 + 30_000
            await _store_msg(
                storage, "IV3OEP-10", "88", "first sender", t0, msg_id="11223344", src_type="udp"
            )
            await _store_msg(
                storage,
                "DK5EN-10,DK5EN-98",
                "88",
                "relayed frame, same msg_id",
                t1,
                msg_id="11223344",
                src_type="lora",
            )

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get("88", {})
            results.append(
                (
                    (
                        "case 5: msg_id reused across two sender bases -> two distinct"
                        " messages (count=2), unread=1 (the second row's base is mine)"
                    ),
                    entry.get("count") == 2 and entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_narrowed_matches_full_scan(results: list[tuple[str, bool]]) -> None:
    """Case 6: the wave's acceptance criterion. One fixture combining every
    shape above; for EVERY key it produces, the `key=`-narrowed scan must
    return the identical count/unread/last_ts the full scan (`key=None`)
    does. 10 keys disagreed on the live snapshot under the old rule; this
    asserts 0 do under the new one.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case6.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 9 * 24 * 3600 * 1000

            # Shape: ordinary transport pair.
            await _store_msg(
                storage, "OE1ABC-1", "142", "hello mesh", t0, msg_id="AA000001", src_type="udp"
            )
            await _duplicate_row(storage, "AA000001", t0 + 90)

            # Shape: msg_id reused across two conversation keys, hours apart.
            await _store_msg(
                storage, "IV3OEP-10", "155", "first", t0 + 1_000, msg_id="AA110011", src_type="udp"
            )
            await _store_msg(
                storage,
                "IV3OEP-10",
                "177",
                "reused, different key",
                t0 + 1_000 + 4 * 3600 * 1000,
                msg_id="AA110011",
                src_type="udp",
            )

            # Shape: msg_id reused in the SAME key, days apart.
            await _store_msg(
                storage,
                "IV3OEP-10",
                "188",
                "first",
                t0 + 2_000,
                msg_id="BB220022",
                src_type="udp",
            )
            await _store_msg(
                storage,
                "IV3OEP-10",
                "188",
                "reused, same key, days later",
                t0 + 2_000 + 3 * 24 * 3600 * 1000,
                msg_id="BB220022",
                src_type="udp",
            )

            # Shape: msg_id reused across two sender bases, same key.
            await _store_msg(
                storage,
                "IV3OEP-10",
                "199",
                "first sender",
                t0 + 3_000,
                msg_id="CC330033",
                src_type="udp",
            )
            await _store_msg(
                storage,
                "DK5EN-10,DK5EN-98",
                "199",
                "relayed, second sender",
                t0 + 33_000,
                msg_id="CC330033",
                src_type="lora",
            )

            # Also give a couple of these keys read cursors, so `newer`/
            # `newer_spam` are not trivially all-True -- the disagreement
            # this wave measured lived in `count`, but the same subquery
            # backs `unread` too.
            await storage.set_read_cursor("155", t0 + 500)
            await storage.set_read_cursor("188", t0 + 2_500)

            full_scan = await storage.get_conversation_summary(MY_CALLSIGN)
            keys = ["142", "155", "177", "188", "199"]
            all_match = True
            mismatches: list[str] = []
            for key in keys:
                narrowed = await storage.get_conversation_summary(MY_CALLSIGN, key=key)
                if narrowed.get(key) != full_scan.get(key):
                    all_match = False
                    mismatches.append(
                        f"{key}: narrowed={narrowed.get(key)} full={full_scan.get(key)}"
                    )

            label = "case 6: key=-narrowed scan matches the full scan for every key"
            if mismatches:
                label += " -- mismatches: " + "; ".join(mismatches)
            results.append((label, all_match))
        finally:
            await storage.close()


async def _test_bare_column_tie(results: list[tuple[str, bool]]) -> None:
    """Case 7: pins CURRENT observed behaviour under a same-millisecond tie
    between two physical rows of one group, differing in `msg`, `category`
    AND `info_score` -- NOT a guarantee SQLite documents. Probes
    `_conv_dedup_subquery` directly to see which row it exposes for the
    group, then asserts `get_conversation_summary`'s own `unread` (which
    runs the aggregate query and, since a filter can suppress this
    category, also the per-row candidate query) agrees with that SAME row's
    category. If the aggregate and candidate queries ever picked different
    physical rows for the tie, `unread` would disagree with the probe here.
    SQLite's own bare-column rule explicitly does NOT promise which row wins
    a tie ("the choice is arbitrary",
    https://www.sqlite.org/lang_select.html §2.5) -- this case does not
    contradict that; it pins that both queries make the SAME arbitrary
    choice, which is the property the suppression subtraction actually
    depends on.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case7.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "23"
            t0 = now_ms() - 700_000
            await _store_msg(
                storage,
                "OE1REAL-1",
                group_key,
                "visible text",
                t0,
                msg_id="DEAD0001",
                src_type="udp",
            )
            # Exact same timestamp -- the tie.
            await _duplicate_row(
                storage,
                "DEAD0001",
                t0,
                msg="hidden text",
                category="node_advert",
                info_score=0.3,
            )
            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": ["node_advert"]})

            # Independent probe of the SAME subquery both internal queries
            # share, at the identical window cutoff.
            dedup_sql = _conv_dedup_subquery("")
            with db_read(db_path) as conn:
                conn.row_factory = sqlite3.Row
                probed = conn.execute(
                    f"SELECT category FROM ({dedup_sql}) w WHERE w.dst = ?",  # noqa: S608 - dedup_sql is a fixed literal built by _conv_dedup_subquery; values parameterized
                    (_window_cutoff_ms(), group_key),
                ).fetchall()
            if len(probed) != 1:
                raise AssertionError(f"expected exactly one deduped row, got {len(probed)}")
            probed_category = probed[0]["category"]
            expected_unread = 0 if probed_category == "node_advert" else 1

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(group_key, {})
            results.append(
                (
                    (
                        "case 7: current observed behaviour under a same-millisecond tie"
                        f" (not an SQLite guarantee) -- get_conversation_summary's unread"
                        f" ({entry.get('unread')}) agrees with the SAME row"
                        " _conv_dedup_subquery itself exposes"
                        f" (category={probed_category!r} -> expected unread={expected_unread})"
                    ),
                    entry.get("count") == 1 and entry.get("unread") == expected_unread,
                )
            )
        finally:
            await storage.close()


async def _test_non_regression_no_reuse(results: list[tuple[str, bool]]) -> None:
    """Case 8: an ordinary conversation with no msg_id reuse and no
    duplicates gets the same literal numbers the old bare-msg_id grouping
    would have given it -- two distinct foreign messages, no dedup collapse
    of any kind: count=2, unread=2, last_ts=the later message's timestamp.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case8.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 900_000
            t1 = t0 + 10_000
            await _store_msg(
                storage, "OE1REAL-1", "33", "first message", t0, msg_id="F0000001", src_type="udp"
            )
            await _store_msg(
                storage, "OE1REAL-1", "33", "second message", t1, msg_id="F0000002", src_type="udp"
            )

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get("33", {})
            results.append(
                (
                    (
                        "case 8: non-regression, no reuse/no duplicates ->"
                        f" count=2, unread=2, last_ts={t1} (got {entry})"
                    ),
                    entry.get("count") == 2
                    and entry.get("unread") == 2
                    and entry.get("last_ts") == t1,
                )
            )
        finally:
            await storage.close()


async def _test_query_plan_stays_indexed(results: list[tuple[str, bool]]) -> None:
    """Case 9: EXPLAIN QUERY PLAN on the aggregate query (the exact SQL
    `get_conversation_summary` builds for `key=None`) still shows BOTH
    `idx_messages_type_timestamp` on the base `messages m` scan AND
    `idx_messages_msgid_timestamp` on `_CONV_ANCHOR_SQL`'s correlated
    subquery (`messages p`) -- asserting only the first would not catch a
    regression in the anchor's own index usage, which is the entire basis
    of choosing this correlated-subquery form over the equivalent, slower
    `RANGE ... PRECEDING` window function (699 ms vs 517 ms on the live DB,
    2026-09-20). A fixture DB carries no `sqlite_stat1`, so this asserts
    against the plan text actually produced here, not an assumption about
    what the planner does with real-world statistics.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case9.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 1_000_000
            await _store_msg(storage, "OE1REAL-1", "12", "msg", t0, msg_id="12340001")

            dedup_sql = _conv_dedup_subquery("")
            params = (_window_cutoff_ms(), SPAM_GROUP)
            query = (
                "SELECT COALESCE(d.conversation_key, d.dst) AS key, d.src, d.dst,"  # noqa: S608 - built from fixed literals imported from query.py; values parameterized
                " COUNT(*) AS cnt, MAX(d.ts) AS last_ts,"
                f" SUM(CASE WHEN {_CONV_NEWER_EXPR} THEN 1 ELSE 0 END) AS newer,"
                f" SUM(CASE WHEN {_CONV_NEWER_SPAM_EXPR} THEN 1 ELSE 0 END) AS newer_spam"
                f" FROM ({dedup_sql}) d" + _CONV_CURSOR_JOINS + " GROUP BY 1, d.src, d.dst"
            )

            with db_read(db_path) as conn:
                plan_rows = conn.execute(f"EXPLAIN QUERY PLAN {query}", params).fetchall()
            plan_text = " | ".join(str(row[3]) for row in plan_rows)
            results.append(
                (
                    (
                        "case 9: aggregate query plan uses idx_messages_type_timestamp"
                        " (outer scan) AND idx_messages_msgid_timestamp (anchor seek)"
                        f" (plan: {plan_text})"
                    ),
                    "idx_messages_type_timestamp" in plan_text
                    and "idx_messages_msgid_timestamp" in plan_text,
                )
            )
        finally:
            await storage.close()


async def _test_anchor_equality_legs_pin(results: list[tuple[str, bool]]) -> None:
    """Case 10: `_CONV_ANCHOR_SQL`'s own `p`-side sender-base and
    conversation-key equality legs are load-bearing, not redundant with the
    outer GROUP BY. Three rows, one msg_id, one conversation key: a foreign
    sender's row lands just inside the window before a same-sender pair 68
    ms apart. Shipped code anchors each sender's rows only against ITS OWN
    prior rows (the `p.sbase = m.sbase` / `p.ckey = m.ckey` legs), so the
    foreign row cannot drag the pair's anchor -- 2 distinct messages (the
    foreign row, plus the collapsed pair). Deleting those two legs from
    `_CONV_ANCHOR_SQL` lets the foreign row (msg_id-equal, timestamp inside
    the window) drag the FIRST same-sender row's anchor back to the
    foreign row's own timestamp, while the SECOND same-sender row's anchor
    stays put (the foreign row has fallen outside ITS window by then) --
    the pair's two rows now disagree on anchor and SPLIT: 3 distinct
    messages, the exact unclearable "+1" shape this whole fix exists to
    prevent.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case10.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "60"
            t = now_ms() - 2_000_000
            # Same-sender pair first, so `_duplicate_row`'s "first row for
            # this msg_id" lookup finds the DK5EN-98 row, not the foreign one.
            await _store_msg(
                storage, "DK5EN-98", group_key, "from DK5EN", t, msg_id="AA", src_type="udp"
            )
            await _duplicate_row(storage, "AA", t + 68)
            # Foreign sender, same msg_id/key, just inside the window before t.
            await _store_msg(
                storage,
                "OTHER-1",
                group_key,
                "from OTHER",
                t - DEDUP_WINDOW_MS + 10,
                msg_id="AA",
                src_type="udp",
            )

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(group_key, {})
            results.append(
                (
                    (
                        "case 10: _CONV_ANCHOR_SQL's p-side sender/key equality legs pin"
                        " the anchor per-sender -- a foreign sender's row just inside the"
                        " window must not drag a same-sender pair's anchor and split it"
                        f" (count={entry.get('count')}, expected 2)"
                    ),
                    entry.get("count") == 2,
                )
            )
        finally:
            await storage.close()


async def _test_anchor_width_pin(results: list[tuple[str, bool]]) -> None:
    """Case 11: the time fence's WIDTH (`DEDUP_WINDOW_MS` inside
    `_CONV_ANCHOR_SQL`) is pinned by a same-msg_id/sender/key pair ~5
    minutes apart -> ONE message. A narrowed window (e.g. 1000 ms, well
    under 5 minutes) would report two, since no earlier fixture in this
    file has a same-sender/same-key pair between the 172 ms real-pair
    ceiling and the 60-minute window.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case11.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "61"
            t = now_ms() - 2_100_000
            await _store_msg(
                storage, "OE1ABC-1", group_key, "first copy", t, msg_id="BB", src_type="udp"
            )
            await _duplicate_row(storage, "BB", t + 5 * 60 * 1000)

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(group_key, {})
            results.append(
                (
                    (
                        "case 11: the anchor's DEDUP_WINDOW_MS width pin -- a pair ~5 min"
                        f" apart is still ONE message (count={entry.get('count')}, expected 1)"
                    ),
                    entry.get("count") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_anchor_strict_boundary_pin(results: list[tuple[str, bool]]) -> None:
    """Case 12: the time fence's STRICTNESS is pinned by a pair exactly
    `DEDUP_WINDOW_MS` apart -> TWO distinct messages. `_CONV_ANCHOR_SQL`
    uses strict `p.timestamp > m.timestamp - DEDUP_WINDOW_MS`, matching
    `_find_duplicate_row_id`'s own strict `timestamp > ?` -- an inclusive
    `>=` at this exact boundary would pull the earlier row into the later
    row's anchor and report ONE message instead. This is the one place the
    correlated-subquery form actually shipped disagrees with the equivalent
    `RANGE BETWEEN ... PRECEDING` window function, which is inclusive of
    the boundary by definition.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case12.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "62"
            t1 = now_ms() - 2_200_000
            t2 = t1 + DEDUP_WINDOW_MS
            await _store_msg(
                storage, "OE1ABC-1", group_key, "first copy", t1, msg_id="CC", src_type="udp"
            )
            await _duplicate_row(storage, "CC", t2)

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(group_key, {})
            results.append(
                (
                    (
                        "case 12: the anchor's strict '>' boundary pin -- a pair EXACTLY"
                        " DEDUP_WINDOW_MS apart is TWO distinct messages, not one"
                        f" (count={entry.get('count')}, expected 2)"
                    ),
                    entry.get("count") == 2,
                )
            )
        finally:
            await storage.close()


async def _test_group_by_identity_legs_pin(results: list[tuple[str, bool]]) -> None:
    """Case 13: the outer GROUP BY's own sender-base and conversation-key
    legs are NOT made redundant by `_CONV_ANCHOR_SQL`'s `p`-side equality
    clauses, and a same-MILLISECOND collision is the construction that
    proves it. Each row's anchor subquery only ever sees rows matching its
    OWN sender/key (that is what the equality legs do), so on a tie two
    rows both resolve their anchor to their OWN timestamp independently --
    the SAME anchor VALUE by coincidence, not because they were grouped
    together. Only the outer GROUP BY's bare sbase/ckey columns keep them
    apart.

    Two halves:
      A. Same msg_id, same key, same millisecond, DIFFERENT sender bases ->
         both survive under that one key (count == 2). Dropping `sbase`
         from the GROUP BY (leaving the anchor's `p.sbase = m.sbase` leg
         untouched) collapses them to count == 1 -- one station's message
         vanishes.
      B. Same msg_id, same sender, same millisecond, DIFFERENT conversation
         keys -> each key gets its OWN message (count == 1 under each).
         Dropping `ckey` from the GROUP BY collapses them into one row
         under a single, arbitrarily-chosen key -- the other key's message
         vanishes from the summary entirely.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "case13.db"
        storage = await create_sqlite_storage(db_path)
        try:
            # A: sender collision, same key, same millisecond.
            key_a = "70"
            t_a = now_ms() - 2_300_000
            await _store_msg(
                storage, "DK5EN-98", key_a, "from DK5EN", t_a, msg_id="DD", src_type="udp"
            )
            await _store_msg(
                storage, "OE1ABC-7", key_a, "from OE1ABC", t_a, msg_id="DD", src_type="udp"
            )

            # B: conversation-key collision, same sender, same millisecond.
            # White-box for the second row: ingest's own dedup gate matches
            # on msg_id + sender + time only (not dst), so a second REAL
            # store_message call at the identical timestamp would be merged
            # into the first row instead of creating a second one -- see
            # `_duplicate_row`'s docstring.
            key_b1, key_b2 = "71", "72"
            t_b = now_ms() - 2_300_000 + 1  # distinct base ts, still its own tie internally
            await _store_msg(
                storage, "DK5EN-98", key_b1, "first key", t_b, msg_id="EE", src_type="udp"
            )
            await _duplicate_row(storage, "EE", t_b, dst=key_b2, conversation_key=key_b2)

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry_a = summary.get(key_a, {})
            entry_b1 = summary.get(key_b1, {})
            entry_b2 = summary.get(key_b2, {})
            results.append(
                (
                    (
                        "case 13a: GROUP BY's sender-base leg is not redundant with the"
                        " anchor's own sbase equality -- a same-millisecond collision"
                        " across two senders under one key survives as count=="
                        f"{entry_a.get('count')} (expected 2)"
                    ),
                    entry_a.get("count") == 2,
                )
            )
            results.append(
                (
                    (
                        "case 13b: GROUP BY's conversation-key leg is not redundant with"
                        " the anchor's own ckey equality -- a same-millisecond collision"
                        " across two keys for one sender survives as count=="
                        f"{entry_b1.get('count')} under {key_b1!r} and"
                        f" count=={entry_b2.get('count')} under {key_b2!r} (expected 1/1)"
                    ),
                    entry_b1.get("count") == 1 and entry_b2.get("count") == 1,
                )
            )
        finally:
            await storage.close()


async def run_conv_dedup_tests() -> bool:
    """Run the conversation-dedup regression suite. Returns True iff every
    case passes."""
    results: list[tuple[str, bool]] = []

    await _test_transport_pair_collapses(results)
    await _test_hour_boundary_pair_collapses(results)
    await _test_reuse_across_conversation_keys(results)
    await _test_reuse_same_key_days_later(results)
    await _test_reuse_across_sender_bases(results)
    await _test_narrowed_matches_full_scan(results)
    await _test_bare_column_tie(results)
    await _test_non_regression_no_reuse(results)
    await _test_query_plan_stays_indexed(results)
    await _test_anchor_equality_legs_pin(results)
    await _test_anchor_width_pin(results)
    await _test_anchor_strict_boundary_pin(results)
    await _test_group_by_identity_legs_pin(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  conv_dedup: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(0 if asyncio.run(run_conv_dedup_tests()) else 1)
