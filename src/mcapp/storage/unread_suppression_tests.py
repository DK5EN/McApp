"""Regression suite for the `unread` half of the suppression wiring in
`get_conversation_summary` (`storage/query.py`).

Backend half of doc/2026-09-19_2140-unread-suppression-plan.md §D1's wave 2 —
the predicate itself (`storage/suppression.py`) is pinned in isolation by
`storage.suppression_tests.run_suppression_predicate_tests`; this suite pins
only the wiring: that `unread` subtracts exactly the candidate rows
`is_suppressed` calls hidden, that `count`/`last_ts` never move, and that the
no-op fast path reproduces today's numbers byte-for-byte.

Mirrors `storage.read_cursor_tests`'s house pattern: a `(label, ok)` results
list, a PASS/FAIL line per case, a bool return, an ephemeral tempfile SQLite
DB per scenario via `create_sqlite_storage`, and real production entry points
(`store_message`, `get_conversation_summary`, `set_read_cursor`,
`set_filter_prefs`, `set_blocked_texts`) rather than a reimplementation of
their logic. No TTY, no network, no `/etc/mcapp`.

Fixture note: `create_sqlite_storage` leaves `_classifier` unset (`None`, see
`sqlite_storage.py`), so `store_message` always writes NULL classifier
columns here — there is no rule-based classifier wired into this ephemeral
DB. `_classify()` below stamps `category`/`tags`/`info_score`/`template_hash`
onto an already-inserted row directly (white-box `UPDATE`, the same pattern
`read_cursor_tests.py`'s `_test_transport_duplicates` uses for its second
transport copy), which is the only way to get a deterministic classification
into a fixture row without standing up the full classifier.

Coverage (per the wave brief):
  1. The group-20 regression, end to end: a group conversation whose NEWEST
     message is `category='node_advert'` with `filter_prefs` hiding
     `node_advert`, cursor sitting on the previous (visible) message ->
     `unread == 0`. Proven to FAIL before the query.py fix (see the module
     docstring note in the wave's report; this suite is written against the
     failing baseline first).
  2. Same fixture with `hiddenCategories: []` -> `unread == 1`.
  3. `count`/`last_ts` are IDENTICAL between cases 1 and 2 — only `unread`
     differs.
  4. A blocked-text message as the newest row is not counted, including with
     `filter_prefs.enabled = false` (the blocklist half is unconditional).
  5. A no-op policy (no `filter_prefs` row, no blocked texts) reproduces the
     pre-suppression counts exactly, across several conversations, own and
     foreign messages alike.
  6. Own messages stay excluded by BASE callsign (`DK5EN-98` vs `DK5EN-14`);
     suppression does not change that rule.
  7. Transport duplicates: the same `msg_id` stored twice ~100 ms apart still
     counts as ONE message, and when suppressed subtracts ONE unit for the
     pair — proven against a second, VISIBLE foreign message in the same
     key, which is the only thing that tells a correct single subtraction
     (unread 2 -> 1) apart from a buggy per-physical-row subtraction (unread
     2 -> 0, floored by the clamp) — a fixture with only the duplicated
     message cannot distinguish the two (both read `unread == 0`).
  8. SPAM_GROUP rebucketing: a suppressed row the blocklist rebuckets to 9999
     subtracts from 9999's unread, not from its original key's.
  9. The `key=` narrowing path returns the same `unread` for that key as the
     full scan does.
  10. The own-sender early return in `_subtract_suppressed_row` is load-
      bearing: an own message stamped with a hidden category (e.g. an
      operator's own `!wx`) must never cancel a foreign, visible message's
      unread just because both are candidates in the same conversation.
  11. SPAM_GROUP subtraction judges a rebucketed row against `newer_spam`
      (MAX of both cursors), not the original key's `newer` — an older
      rebucketed row already read under the SPAM_GROUP cursor must not be
      subtracted even though it would still read "newer" under its
      original, untouched key's cursor.
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from ..commands.parsing import SPAM_GROUP
from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import compute_conversation_key
from .query import HistoryFilter

logger = get_logger(__name__)

MY_CALLSIGN = "DK5EN-98"


async def _store_msg(storage: Any, src: str, dst: str, msg: str, ts: int) -> None:
    """Drive a chat message through the REAL ingest path so conversation_key
    is computed by production code (compute_conversation_key), matching
    read_cursor_tests.py's own fixture helper.
    """
    await storage.store_message(
        {
            "src": src,
            "dst": dst,
            "msg": msg,
            "type": "msg",
            "timestamp": ts,
            "src_type": "lora",
        },
        raw="",
    )


async def _classify(storage: Any, src: str, ts: int, fields: dict[str, Any]) -> None:
    """White-box: stamp classifier columns onto the row `_store_msg` just
    inserted for (src, ts). See the module docstring's fixture note for why
    this is necessary rather than incidental. `fields` accepts any of
    `category`/`tags`/`info_score`/`template_hash`; anything omitted is
    written as NULL, matching `view_from_row`'s tolerant defaults.
    """
    tags = fields.get("tags")
    await storage._mutate(
        "UPDATE messages SET category = ?, tags = ?, info_score = ?, template_hash = ?"
        " WHERE src = ? AND timestamp = ?",
        (
            fields.get("category"),
            json.dumps(tags) if tags is not None else None,
            fields.get("info_score"),
            fields.get("template_hash"),
            src,
            ts,
        ),
    )


async def _test_group20_regression(results: list[tuple[str, bool]]) -> None:
    """Case 1/2/3: the field regression itself, plus the hiddenCategories: []
    control and the count/last_ts invariance across both runs.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "group20_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "20"
            t0 = now_ms() - 100_000

            # The previous, VISIBLE message — the cursor sits here, exactly
            # like the live mcapp.local row (read_cursors['20'] == the ts of
            # the last message actually rendered).
            await _store_msg(storage, "OE1REAL-1", group_key, "real chat", t0)

            # The newest message: a bare URL classified as node_advert,
            # exactly the live row (HB9VQQ-1 -> 20, category=node_advert,
            # info_score=0.3, tags=["has_url"]).
            advert_ts = t0 + 10
            await _store_msg(
                storage,
                "HB9VQQ-1",
                group_key,
                "https://www.varac-hamradio.com/group/x",
                advert_ts,
            )
            await _classify(
                storage,
                "HB9VQQ-1",
                advert_ts,
                {"category": "node_advert", "tags": ["has_url"], "info_score": 0.3},
            )

            await storage.set_read_cursor(group_key, t0)  # cursor on the visible message

            await storage.set_filter_prefs(
                {
                    "enabled": True,
                    "hiddenCategories": [
                        "timestamp_beacon",
                        "bot_command",
                        "node_advert",
                        "sw_advert",
                    ],
                }
            )

            summary_hidden = await storage.get_conversation_summary(MY_CALLSIGN)
            entry_hidden = summary_hidden.get(group_key, {})
            results.append(
                (
                    (
                        "group-20 regression: node_advert hidden -> unread == 0"
                        " (the badge the client can never clear before this fix)"
                    ),
                    entry_hidden.get("unread") == 0,
                )
            )
            results.append(
                (
                    (
                        "group-20 regression: count still counts BOTH rows"
                        " (a hidden message still belongs to the conversation)"
                    ),
                    entry_hidden.get("count") == 2,
                )
            )

            # Case 2: hiddenCategories: [] -> the advert is visible again,
            # unread reverts to 1.
            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": []})
            summary_visible = await storage.get_conversation_summary(MY_CALLSIGN)
            entry_visible = summary_visible.get(group_key, {})
            results.append(
                (
                    "group-20 control: hiddenCategories: [] -> unread == 1",
                    entry_visible.get("unread") == 1,
                )
            )

            # Case 3: count/last_ts identical between the two runs — only
            # unread differs.
            results.append(
                (
                    "group-20: count is identical whether the advert is hidden or not",
                    entry_hidden.get("count") == entry_visible.get("count"),
                )
            )
            results.append(
                (
                    "group-20: last_ts is identical whether the advert is hidden or not",
                    entry_hidden.get("last_ts") == entry_visible.get("last_ts") == advert_ts,
                )
            )
        finally:
            await storage.close()


async def _test_blocked_text(results: list[tuple[str, bool]]) -> None:
    """Case 4: a blocked-text newest message is not counted as unread,
    including when the spam-filter half is disabled (the blocklist half is
    unconditional — is_text_blocked never checks policy.enabled).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "blocked_text_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            dm_dst = "DK3PB"
            key = compute_conversation_key(MY_CALLSIGN, dm_dst) or ""
            t0 = now_ms() - 100_000

            await _store_msg(storage, dm_dst, MY_CALLSIGN, "hi there", t0)
            await _store_msg(storage, dm_dst, MY_CALLSIGN, "buy crypto now!!!", t0 + 10)

            await storage.set_blocked_texts(["buy crypto"])
            await storage.set_filter_prefs({"enabled": False})

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(key, {})
            results.append(
                (
                    "blocked text: unconditional even with filter_prefs.enabled=False",
                    entry.get("unread") == 1,
                )
            )
            results.append(
                ("blocked text: count still includes the blocked row", entry.get("count") == 2)
            )
        finally:
            await storage.close()


async def _test_noop_policy_reproduces_baseline(results: list[tuple[str, bool]]) -> None:
    """Case 5: with no filter_prefs row and no blocked texts (the common,
    untouched-install case), the full summary is byte-identical to what
    get_conversation_summary computed before the suppression wave — several
    conversations, own and foreign traffic mixed.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "noop_baseline_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 200_000

            dm_dst = "DK3PB"
            dm_key = compute_conversation_key(MY_CALLSIGN, dm_dst) or ""
            await _store_msg(storage, MY_CALLSIGN, dm_dst, "hello from me", t0)
            await _store_msg(storage, dm_dst, MY_CALLSIGN, "reply", t0 + 10)

            group_key = "232"
            await _store_msg(storage, "OE1ABC-1", group_key, "g1", t0 + 20)
            await _store_msg(storage, "OE1XYZ-1", group_key, "g2", t0 + 30)

            broadcast_key = "*"
            await _store_msg(storage, "OE1QQQ-1", broadcast_key, "broadcast", t0 + 40)

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            expected = {
                dm_key: {"count": 2, "last_ts": t0 + 10, "unread": 1},
                group_key: {"count": 2, "last_ts": t0 + 30, "unread": 2},
                broadcast_key: {"count": 1, "last_ts": t0 + 40, "unread": 1},
            }
            results.append(
                (
                    "no-op policy: full summary reproduces the pre-suppression baseline exactly",
                    summary == expected,
                )
            )
        finally:
            await storage.close()


async def _test_own_message_exclusion_unaffected(results: list[tuple[str, bool]]) -> None:
    """Case 6: own-message exclusion by BASE callsign still holds with an
    active (non-noop) suppression policy in play — suppression subtracts
    from candidates that were already excluded from unread the same way it
    was before.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "own_message_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            dm_dst = "DK3PB"
            key = compute_conversation_key(MY_CALLSIGN, dm_dst) or ""
            t0 = now_ms() - 100_000

            await _store_msg(storage, "DK5EN-98", dm_dst, "hello from -98", t0)
            await _store_msg(storage, "DK5EN-14", dm_dst, "hello from -14", t0 + 10)
            reply_ts = t0 + 20
            await _store_msg(storage, dm_dst, "DK5EN-98", "reply from partner", reply_ts)
            await _classify(storage, dm_dst, reply_ts, {"category": "node_advert"})

            # Active, non-noop policy: hides node_advert, which is the
            # partner's reply — but the own-message rule is evaluated first
            # for count/unread purposes and must not be disturbed by it.
            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": ["node_advert"]})

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(key, {})
            results.append(
                (
                    (
                        "own-message exclusion holds under an active suppression policy:"
                        " count includes all three rows"
                    ),
                    entry.get("count") == 3,
                )
            )
            results.append(
                (
                    (
                        "own-message exclusion holds under an active suppression policy:"
                        " the partner's reply is suppressed -> unread == 0, not 1"
                    ),
                    entry.get("unread") == 0,
                )
            )
        finally:
            await storage.close()


async def _test_transport_duplicates_suppressed(results: list[tuple[str, bool]]) -> None:
    """Case 7: the same msg_id stored twice (~100 ms apart, mirroring the
    real UDP+BLE transport-duplicate pattern) still counts as ONE message,
    and suppressing it subtracts ONE unit for the pair, never one per
    physical row.

    A fixture holding ONLY the duplicated message cannot tell a correct
    single subtraction (unread 1 -> 0) apart from a buggy per-physical-row
    subtraction (unread 1 -1 -1, floored by `_subtract_suppressed_row`'s
    clamp at 0) — both read `unread == 0`. A second, VISIBLE foreign message
    in the same conversation key, newer than the (implicit, unset -> 0)
    cursor, is what separates them: a correct single subtraction leaves it
    at `unread == 1` (the visible message only), while a per-physical-row
    subtraction over-subtracts to `unread == 0`.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "dup_suppression_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            t0 = now_ms() - 100_000
            base = {"src": "DK1TCP-77", "dst": "*", "msg": "spammy text", "type": "msg"}
            await storage.store_message(
                {**base, "msg_id": "E686400E", "timestamp": t0, "src_type": "udp"}, raw=""
            )
            await storage._mutate(  # white-box: mirror the production row pair
                "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type,"
                " conversation_key)"
                " SELECT msg_id, src, dst, msg, type, ?, 'ble_remote', conversation_key"
                " FROM messages WHERE msg_id = 'E686400E'",
                (t0 + 107,),
            )
            # Stamp classification on BOTH rows so the fixture does not
            # depend on which physical row the earliest-copy aggregation
            # happens to read from.
            await storage._mutate(
                "UPDATE messages SET category = 'node_advert' WHERE msg_id = 'E686400E'"
            )

            # A second, DISTINCT, VISIBLE foreign message in the same key,
            # newer than the cursor (unset -> 0) — see the docstring above
            # for why this is the only thing that makes the two subtraction
            # bugs distinguishable.
            visible_ts = t0 + 1_000
            await _store_msg(storage, "OE1REAL-1", "*", "real chat", visible_ts)

            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": ["node_advert"]})

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get("*", {})
            results.append(
                (
                    (
                        "transport duplicates + suppression: count is per distinct message"
                        " (the duplicate pair collapses to one, plus the visible message = 2,"
                        " not 3)"
                    ),
                    entry.get("count") == 2,
                )
            )
            results.append(
                (
                    (
                        "transport duplicates + suppression: the duplicate pair subtracts as ONE"
                        " unit, leaving the visible message's unread untouched (unread == 1, not"
                        " 0 from a per-physical-row double subtraction)"
                    ),
                    entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_spam_group_rebucket_subtraction(results: list[tuple[str, bool]]) -> None:
    """Case 8: a suppressed row that the blocklist rebuckets to SPAM_GROUP
    subtracts from SPAM_GROUP's unread, not from its original key's — the
    subtraction loop must apply the SAME rebucketing decision as the
    aggregation loop, not a second convention.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "spam_rebucket_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "555"
            spam_src = "SPAMMER-1"
            t0 = now_ms() - 100_000

            await _store_msg(storage, "OE1REAL-1", group_key, "real chat", t0)
            spam_ts = t0 + 10
            await _store_msg(storage, spam_src, group_key, "buy now", spam_ts)
            await _classify(storage, spam_src, spam_ts, {"category": "node_advert"})

            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": ["node_advert"]})

            def _filter(data: dict[str, Any]) -> dict[str, Any] | None:
                if data.get("src") == spam_src:
                    return {"dst": SPAM_GROUP}
                return data

            filter_fn: HistoryFilter = _filter
            summary = await storage.get_conversation_summary(MY_CALLSIGN, filter_fn)

            spam_entry = summary.get(SPAM_GROUP, {})
            group_entry = summary.get(group_key, {})
            results.append(
                (
                    "SPAM_GROUP rebucket: the suppressed row's count lands under SPAM_GROUP",
                    spam_entry.get("count") == 1,
                )
            )
            results.append(
                (
                    (
                        "SPAM_GROUP rebucket: suppression subtracts from SPAM_GROUP's unread,"
                        " landing it at 0, not 1"
                    ),
                    spam_entry.get("unread") == 0,
                )
            )
            results.append(
                (
                    (
                        "SPAM_GROUP rebucket: the original group key's own unread is untouched"
                        " by the rebucketed row's suppression (real chat still unread)"
                    ),
                    group_entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_key_narrowing_matches_full_scan(results: list[tuple[str, bool]]) -> None:
    """Case 9: the `key=` narrowing path returns the same `unread` for that
    key as a full, unnarrowed scan — with an active suppression policy in
    play, not just in the no-op case.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "key_narrow_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "777"
            t0 = now_ms() - 100_000

            await _store_msg(storage, "OE1ABC-1", group_key, "g1", t0)
            advert_ts = t0 + 10
            await _store_msg(storage, "OE1ABC-1", group_key, "advert", advert_ts)
            await _classify(storage, "OE1ABC-1", advert_ts, {"category": "node_advert"})

            # Unrelated second conversation, so the full scan has more than
            # one key to prove the narrowing actually narrows.
            await _store_msg(storage, "OE1XYZ-1", "888", "other convo", t0 + 20)

            # Cursor on the first (non-suppressed) message, so the only
            # candidate newer than it is the advert — making the suppressed
            # unread deterministically 0, the same shape as the group-20
            # regression above.
            await storage.set_read_cursor(group_key, t0)
            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": ["node_advert"]})

            full_summary = await storage.get_conversation_summary(MY_CALLSIGN)
            narrowed_summary = await storage.get_conversation_summary(MY_CALLSIGN, key=group_key)

            results.append(
                (
                    "key= narrowing: scopes the result to exactly the requested key",
                    list(narrowed_summary) == [group_key],
                )
            )
            results.append(
                (
                    (
                        "key= narrowing: unread for that key matches the full scan's value"
                        " under an active suppression policy"
                    ),
                    narrowed_summary[group_key]["unread"] == full_summary[group_key]["unread"] == 0,
                )
            )
        finally:
            await storage.close()


async def _test_own_suppressed_message_does_not_cancel_foreign_unread(
    results: list[tuple[str, bool]],
) -> None:
    """Case 10: the own-sender early return in `_subtract_suppressed_row`
    (`if sender_base == my_base: return`) must fire BEFORE the suppression
    check — an own message that happens to carry a hidden category (the real
    shape: the operator's own `!wx` is classified `bot_command`, a hidden
    category) was never added to `unread` by `_apply_conversation_row` in
    the first place (own traffic is excluded by sender there), so it must
    never be subtracted either. Without the guard, a suppressed own message
    wrongly cancels a foreign, visible message's unread in the SAME
    conversation just because both are candidates.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "own_suppressed_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            dm_dst = "DK3PB"
            key = compute_conversation_key(MY_CALLSIGN, dm_dst) or ""
            t0 = now_ms() - 100_000

            # The operator's own weather-bot command: hidden, and a
            # candidate row (newer than the unset -> 0 cursor).
            await _store_msg(storage, MY_CALLSIGN, dm_dst, "!wx", t0)
            await _classify(storage, MY_CALLSIGN, t0, {"category": "bot_command"})

            # The partner's reply: foreign, visible, newer still.
            reply_ts = t0 + 10
            await _store_msg(storage, dm_dst, MY_CALLSIGN, "sunny today", reply_ts)

            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": ["bot_command"]})

            summary = await storage.get_conversation_summary(MY_CALLSIGN)
            entry = summary.get(key, {})
            results.append(
                (
                    (
                        "own-message suppression guard: a hidden own message never cancels a"
                        " foreign visible message's unread (unread == 1, not 0)"
                    ),
                    entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def _test_spam_group_subtract_uses_rebucketed_cursor(
    results: list[tuple[str, bool]],
) -> None:
    """Case 11: `_subtract_suppressed_row` must judge a rebucketed row
    against `newer_spam` (MAX of the original key's cursor and the
    SPAM_GROUP cursor), exactly like `_apply_conversation_row` does — not
    against the original key's bare `newer`, which ignores the SPAM_GROUP
    cursor entirely.

    Two rebucketed rows from the same blocklisted src, both suppressible
    (hidden category), with the SPAM_GROUP cursor sitting BETWEEN their
    timestamps: the OLDER row is already read under SPAM_GROUP
    (`newer_spam == False`, even though it still reads `newer == True`
    under its untouched original key) and must not be subtracted at all —
    it was never added to `unread` in the first place. The newer row IS
    unread but is left unclassified (visible), so only the older row's
    mis-handling can move the final number. Judging by `newer` instead
    wrongly subtracts the older row anyway, landing `unread` at 0.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "spam_rebucket_cursor_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            group_key = "666"
            spam_src = "SPAMMER-2"
            t0 = now_ms() - 100_000

            older_ts = t0
            spam_cursor_ts = t0 + 50
            newer_ts = t0 + 100

            await _store_msg(storage, spam_src, group_key, "buy now (old)", older_ts)
            await _classify(storage, spam_src, older_ts, {"category": "node_advert"})
            # The newer row is left unclassified (visible) on purpose — see
            # the docstring above: only the OLDER row's mis-handling can
            # move `unread` here.
            await _store_msg(storage, spam_src, group_key, "buy now (new)", newer_ts)

            # The SPAM_GROUP cursor sits strictly between the two rows'
            # timestamps; the original group_key never gets a cursor at all
            # (unset -> 0), so `newer` reads True for BOTH rows regardless
            # of the SPAM_GROUP cursor.
            await storage.set_read_cursor(SPAM_GROUP, spam_cursor_ts)

            await storage.set_filter_prefs({"enabled": True, "hiddenCategories": ["node_advert"]})

            def _filter(data: dict[str, Any]) -> dict[str, Any] | None:
                if data.get("src") == spam_src:
                    return {"dst": SPAM_GROUP}
                return data

            filter_fn: HistoryFilter = _filter
            summary = await storage.get_conversation_summary(MY_CALLSIGN, filter_fn)

            spam_entry = summary.get(SPAM_GROUP, {})
            results.append(
                (
                    (
                        "SPAM_GROUP subtraction: judges a rebucketed row against newer_spam,"
                        " not the original key's newer -- the older, already-read-under-9999 row"
                        " is skipped and unread stays at 1 (not 0)"
                    ),
                    spam_entry.get("unread") == 1,
                )
            )
        finally:
            await storage.close()


async def run_unread_suppression_tests() -> bool:
    """Run the unread-suppression regression suite. Returns True iff every
    case passes."""
    results: list[tuple[str, bool]] = []

    await _test_group20_regression(results)
    await _test_blocked_text(results)
    await _test_noop_policy_reproduces_baseline(results)
    await _test_own_message_exclusion_unaffected(results)
    await _test_transport_duplicates_suppressed(results)
    await _test_spam_group_rebucket_subtraction(results)
    await _test_key_narrowing_matches_full_scan(results)
    await _test_own_suppressed_message_does_not_cancel_foreign_unread(results)
    await _test_spam_group_subtract_uses_rebucketed_cursor(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  unread_suppression: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(0 if asyncio.run(run_unread_suppression_tests()) else 1)
