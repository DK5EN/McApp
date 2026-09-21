"""QueryMixin: read-heavy reporting/chart/dump methods for SQLiteStorage.

Moved out of sqlite_storage.py (ST-04) — mheard/signal chart building
(process_mheard_yearly/_monthly → _build_chart_series), paged message
retrieval, stats, pruning, and dump import/export.
"""

import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, cast

from ..commands.parsing import SPAM_GROUP, is_group, is_hashtag, resolve_dst_target
from ..logging_setup import get_logger
from ..util import now_ms
from ._base import StorageBase
from .constants import (
    _MSG_SELECT,
    ACK_MSG_ID_WINDOW_MS,
    BUCKET_SECONDS,
    CORE_DUMP_FILTER_TEXT,
    DEDUP_WINDOW_MS,
    DEFAULT_PAGE_SIZE,
    DEFAULT_POS_RETENTION_HOURS,
    EIGHT_DAYS_MS,
    EST_BYTES_PER_ROW,
    GAP_THRESHOLD_MULTIPLIER,
    HELD_ACK_WINDOW_MS,
    HOURLY_BUCKET_MS,
    HOURLY_BUCKET_S,
    HOURLY_GAP_THRESHOLD,
    HOURS_PER_YEAR,
    INITIAL_ACK_LIMIT,
    INVALID_CHARACTER_MSG,
    LINK_UPTIME_RETENTION_DAYS,
    LONG_RETENTION_DAYS,
    MHEARD_STATION_SCAN_LIMIT,
    MIN_DATAPOINTS_FOR_STATS,
    MIN_PRUNE_ROWS,
    ONE_MONTH_MS,
    ONE_YEAR_MS,
    PRUNE_TARGET_FRACTION,
    SECONDS_PER_DAY,
    SEVEN_DAYS_MS,
    SPARSE_MIN_DATAPOINTS,
    STATION_RETENTION_DAYS,
    TELEMETRY_BUCKET_MS,
    VALID_RSSI_RANGE,
    VALID_SNR_RANGE,
    compute_conversation_key,
    db_read,
    escape_like,
    sender_base_sql,
)
from .suppression import (
    SuppressionPolicy,
    is_suppressed,
    load_policy,
    policy_is_noop,
    view_from_row,
)

logger = get_logger(__name__)

# Read-path blocklist hook. Takes an already-built message/position dict and
# returns the dict to emit (possibly a dst-rewritten copy) or None to drop it.
# Supplied by MessageRouter.filter_history_row — storage deliberately holds no
# blocklist of its own, so the same shared decision governs ingest, live
# broadcast and history alike.
HistoryFilter = Callable[[dict[str, Any]], dict[str, Any] | None]

# Progress throttle for _build_chart_series: one "gaps" SSE progress event per
# this many qualified stations, not one per station. Measured on mcapp.local
# (2026-09-19, thread pool idle throughout): the yearly mheard dump qualifies
# 65 stations -> 65 on-loop SSE sends, 1422 ms http duration / 584 ms
# loop_lag; chunking to 10 cuts that to 7 progress events (monthly: 13 -> 2,
# 7day: 11 -> 2).
MHEARD_PROGRESS_CHUNK = 10

# --- ack payload shapes on the read path ---------------------------------
# The firmware emits TWO ack payloads and only one carries a callsign prefix.
#
#   '%-9.9s:ack%03i'  the ordinary peer ack. The cross-repo predicate, defined
#                     by ack_predicate_vectors.json v2 and replayed by MCProxy,
#                     mc-chat and the webapp alike.
#   'ack%04i'         no prefix, nothing but 'ack' + the APRS message id padded
#                     to at least 4 ASCII digits. SendAckMessage()
#                     (loop_functions.cpp) switches to this shape for exactly
#                     one destination: Winlink's 'WLNK-1' gateway, which speaks
#                     APRS ack conventions rather than MeshCom's.
#
# Both are machine-to-machine protocol chatter, so both are excluded from every
# message/history query. They are NOT symmetric on the way back in: a peer ack
# feeds the client's delivery-status matching and is served by the acks query
# below, while a bare APRS ack carries no correlator a client could use (no
# callsign prefix, and the number is the correspondent's APRS message id, not
# our echo_id) and is served to nobody. The read path is therefore a THREE-way
# split — messages / peer acks / dropped noise — not the two-way complement it
# was before; `_NOT_ACK_SQL` is deliberately wider than the negation of
# `_PEER_ACK_SQL`.
#
# Deliberately NOT folded into ack_predicate_vectors.json: that corpus governs
# the ':ack<N>' predicate in three repos, and mc-chat has no RF path that can
# produce the bare shape. Kept MCProxy-local and SQL-side for that reason; the
# webapp mirrors it in its own view filter (isAprsAckMessage).
#
# GLOB, not LIKE: case-sensitive and ASCII-only, same reasoning as the peer
# shape. The bare globs are anchored (no leading or trailing '*'), so they match
# the WHOLE payload and a human message that merely starts 'ack1234 ...' stays
# visible. Four and five digits are the whole reachable range: '%04i' pads to
# four, and an APRS message id is at most five characters.
_PEER_ACK_GLOBS = ("*:ack[0-9]*",)
_APRS_ACK_GLOBS = ("ack[0-9][0-9][0-9][0-9]", "ack[0-9][0-9][0-9][0-9][0-9]")


def _ack_sql(globs: tuple[str, ...], col: str = "msg", *, negate: bool = False) -> str:
    """Render `globs` as one SQL boolean over `col`.

    `negate=True` yields the AND-of-NOT form (De Morgan), so an exclusion can
    never drift from the shape list it is built from.
    """
    if negate:
        return " AND ".join(f"{col} NOT GLOB '{g}'" for g in globs)
    return "(" + " OR ".join(f"{col} GLOB '{g}'" for g in globs) + ")"


# Every message/history query excludes both shapes; the acks query serves only
# the peer shape (see the three-way split above).
_NOT_ACK_SQL = _ack_sql(_PEER_ACK_GLOBS + _APRS_ACK_GLOBS, negate=True)
_NOT_ACK_SQL_M = _ack_sql(_PEER_ACK_GLOBS + _APRS_ACK_GLOBS, "m.msg", negate=True)
# Same predicate for the dedup anchor's correlated subquery alias, so the
# anchor is computed over exactly the rows `_conv_dedup_subquery` groups.
_NOT_ACK_SQL_P = _ack_sql(_PEER_ACK_GLOBS + _APRS_ACK_GLOBS, "p.msg", negate=True)
_PEER_ACK_SQL = _ack_sql(_PEER_ACK_GLOBS)

# --- get_conversation_summary shared SQL fragments -------------------------
# Factored so the aggregate SUM(CASE...) query and the per-row suppression
# candidate query (doc/2026-09-19_2140-unread-suppression-plan.md §D1) can
# never drift: both are built from the SAME dedup subquery text and the SAME
# two boolean "is this row unread" expressions. If the candidate predicate
# and the aggregate's counting condition ever disagreed, the suppression
# subtraction in get_conversation_summary would silently corrupt `unread`.
_CONV_DEDUP_COLS = (
    "MIN(m.timestamp) AS ts, m.src, m.dst, m.conversation_key,"
    " m.msg, m.category, m.tags, m.info_score, m.template_hash"
)

# The three identity legs of a distinct message. `_CONV_GID_SQL` is the
# msg_id-or-rowid group `_conv_dedup_subquery` always used; the other two are
# the fences added in 2026-09-20 (see the docstring below).
_CONV_GID_SQL = "COALESCE(NULLIF(m.msg_id, ''), 'row:' || m.rowid)"
_CONV_SBASE_SQL = sender_base_sql("m.src")
_CONV_CKEY_SQL = "COALESCE(m.conversation_key, m.dst)"

# The time fence: the timestamp of the EARLIEST copy of this message within
# DEDUP_WINDOW_MS before this row, or this row's own timestamp when it is the
# earliest. Two rows land in the same group only when they resolve to the same
# anchor, so a group closes as soon as a gap wider than the window opens.
#
# It is deliberately NOT a modulo/division bucket (`m.timestamp / W`): a fixed
# boundary can fall between the two copies of one real transport pair (measured
# spacing <= 172 ms) and split them into two "messages" — which is exactly the
# unclearable +1 badge doc/2026-09-19_2140-unread-suppression-plan.md exists to
# fix. An anchor derived from the data itself can never do that.
#
# A `MIN(m.timestamp) OVER (... RANGE BETWEEN W PRECEDING AND CURRENT ROW)`
# window function was the first implementation and returned a byte-identical
# result set on the live DB, but it costs a second sort (699 ms vs 517 ms full
# scan on mcapp.local's Pi Zero 2W, 2026-09-20) where this form is an index
# seek on `idx_messages_msgid_timestamp`. The two are NOT equivalent in
# general: a RANGE frame INCLUDES the row exactly `W` back, this predicate is
# STRICT. Strict is the correct one — `_find_duplicate_row_id` is also
# `timestamp > ?`, so the read-time and ingest fences agree at the boundary.
#
# `p` carries the SAME universe filters as the outer query (`type = 'msg'`,
# non-ack) so the anchor is computed over exactly the rows being grouped. That
# is the one narrowing relative to `_find_duplicate_row_id`, which asks a
# different question ("is this inbound frame a duplicate of ANY stored row").
_CONV_ANCHOR_SQL = (
    "(SELECT MIN(p.timestamp) FROM messages p"  # noqa: S608 - every interpolated fragment is a fixed literal; no value is interpolated
    f" WHERE p.msg_id = m.msg_id AND p.type = 'msg' AND {_NOT_ACK_SQL_P}"
    " AND p.timestamp <= m.timestamp"
    f" AND p.timestamp > m.timestamp - {DEDUP_WINDOW_MS}"
    f" AND {sender_base_sql('p.src')} = {_CONV_SBASE_SQL}"
    " AND COALESCE(p.conversation_key, p.dst)"
    f" = {_CONV_CKEY_SQL})"
)


def _conv_dedup_subquery(key_clause: str) -> str:
    """The distinct-message dedup subquery (aliased `d` by both callers)
    shared by get_conversation_summary's two queries.

    doc/2026-09-20_1000-live-classifier-and-dedup-plan.md F2: a bare `msg_id`
    (or rowid-fallback) group is not enough to call two rows "the same
    message" — a firmware `msg_id` is a node-local counter that gets reused
    across stations, conversations and days. Measured on the live snapshot,
    572 multi-row msg_id groups split cleanly in two: 454 real transport
    pairs <= 172 ms apart (max size 2, never spanning a sender, a dst or a
    conversation key) and 118 reuse groups >= 3.33 h apart, 71 of them across
    more than one conversation key and one across more than one sender.
    Nothing lands in between.

    A message is therefore identified by FOUR legs, mirroring the INGEST
    dedup rule (`_find_duplicate_row_id`, storage/ingest.py): the msg_id
    group, the resolved sender base (`sender_base_sql`, the single shared
    source for that expression so the ingest and read-time boundaries cannot
    drift), the conversation key, and `_CONV_ANCHOR_SQL`'s 60-minute time
    fence — which sits with a 3.3x margin between the measured real-pair
    ceiling and the measured reuse floor.

    Exactly one MIN()/MAX() aggregate (`MIN(m.timestamp)`) appears in this
    subquery, which is what makes every other, bare column in
    `_CONV_DEDUP_COLS` well-defined under SQLite's documented
    aggregate-query extension: a bare column takes its value from the SAME
    input row that produced the min()/max() result, not an arbitrary row of
    the group (https://sqlite.org/lang_select.html, "Bare columns in an
    aggregate query"). SQLite does NOT extend that guarantee to TIES — with
    two rows sharing the minimum, "bare values might be selected from any of
    those rows" — so a same-millisecond transport pair resolves arbitrarily;
    the live DB's 10 such pairs all carry identical text, and
    `conv_dedup_tests.py` case 7 pins the observed behaviour rather than a
    promise. `m.src`/`m.dst` already relied on this before the
    suppression wave (see get_conversation_summary's docstring on transport
    duplicates); `m.msg`/`m.category`/`m.tags`/`m.info_score`/
    `m.template_hash` ride the IDENTICAL guarantee — they come from the same
    earliest stored copy of a transport-duplicated message as `ts`, `src`
    and `dst`, not from an arbitrary sibling row of the same group. The
    correlated subquery in the GROUP BY is not an aggregate of THIS query, so
    it does not disturb that guarantee.
    """
    return (
        f"SELECT {_CONV_DEDUP_COLS}"  # noqa: S608 - key_clause/fragments are fixed literals; values parameterized
        " FROM messages m"
        f" WHERE m.type = 'msg' AND {_NOT_ACK_SQL_M}"
        " AND m.timestamp >= ?" + key_clause + f" GROUP BY {_CONV_GID_SQL}, {_CONV_SBASE_SQL},"
        f" {_CONV_CKEY_SQL}, {_CONV_ANCHOR_SQL}"
    )


_CONV_CURSOR_JOINS = (
    " LEFT JOIN read_cursors rc"
    "   ON rc.key = COALESCE(d.conversation_key, d.dst)"
    " LEFT JOIN read_cursors rc2"
    "   ON rc2.key = ?"
)

# The two per-row "is this message unread" conditions, shared verbatim by the
# aggregate SUM(CASE...) and the candidate query's SELECT + WHERE. `newer`
# judges against the row's own key's cursor; `newer_spam` judges against
# MAX(that cursor, the SPAM_GROUP cursor) for a row that might get
# rebucketed there (see get_conversation_summary's docstring for the
# MAX-of-both-cursors rule).
_CONV_NEWER_EXPR = "d.ts > COALESCE(rc.ts, 0)"
_CONV_NEWER_SPAM_EXPR = "d.ts > MAX(COALESCE(rc.ts, 0), COALESCE(rc2.ts, 0))"


def _apply_conversation_row(
    row: sqlite3.Row,
    summary: dict[str, dict[str, int]],
    blocklist_filter: HistoryFilter | None,
    my_base: str,
) -> None:
    """One aggregate row from get_conversation_summary's main query -> an
    update of summary[key]'s count/last_ts/unread. Split out of `_run` only
    to keep that closure under the statement/branch lint budget; the logic
    itself is unchanged from before the suppression wave.
    """
    key = row["key"]
    if not key:
        return
    src = row["src"] or ""
    dst = row["dst"] or ""
    rebucketed = False
    if blocklist_filter is not None:
        kept = blocklist_filter({"src": src, "dst": dst})
        if kept is None:
            return  # dropped outright
        if kept.get("dst") == SPAM_GROUP:
            # Rebucketed to the quarantine group, matching where the message
            # itself now shows up. Every quarantined group shares ONE cursor
            # (rc2, keyed on SPAM_GROUP itself): a row only counts as unread
            # when it is newer than BOTH the original key's cursor and the
            # SPAM_GROUP cursor (MAX semantics), so marking 9999 read
            # actually clears the badge instead of it re-lighting from the
            # untouched original cursor.
            key = SPAM_GROUP
            rebucketed = True

    entry = summary.setdefault(key, {"count": 0, "last_ts": 0, "unread": 0})
    entry["count"] += row["cnt"]
    entry["last_ts"] = max(entry["last_ts"], row["last_ts"] or 0)

    sender_base = src.split(",", maxsplit=1)[0].split("-", maxsplit=1)[0].upper()
    if sender_base != my_base:
        entry["unread"] += (row["newer_spam"] if rebucketed else row["newer"]) or 0


def _subtract_suppressed_row(
    row: sqlite3.Row,
    summary: dict[str, dict[str, int]],
    blocklist_filter: HistoryFilter | None,
    my_base: str,
    policy: SuppressionPolicy,
) -> None:
    """One per-message candidate row -> at most one unit subtracted from
    summary[key]['unread'], mirroring `_apply_conversation_row`'s blocklist/
    rebucket/own-message decisions IN THE SAME ORDER (not a second
    convention), then applying the suppression predicate as the final gate.
    """
    key = row["key"]
    if not key:
        return
    src = row["src"] or ""
    dst = row["dst"] or ""
    rebucketed = False
    if blocklist_filter is not None:
        kept = blocklist_filter({"src": src, "dst": dst})
        if kept is None:
            return  # dropped outright, same as _apply_conversation_row
        if kept.get("dst") == SPAM_GROUP:
            key = SPAM_GROUP
            rebucketed = True

    # Same "which flag applies" rule as _apply_conversation_row's
    # `entry["unread"] +=` line: a rebucketed row's unread-ness is judged
    # against newer_spam (MAX of both cursors), not newer. If the applicable
    # flag is false, this candidate was never counted as unread under this
    # (possibly rebucketed) key in the first place, so there is nothing to
    # subtract.
    applicable = row["newer_spam"] if rebucketed else row["newer"]
    if not applicable:
        return

    sender_base = src.split(",", maxsplit=1)[0].split("-", maxsplit=1)[0].upper()
    if sender_base == my_base:
        return  # own traffic was never added to unread by _apply_conversation_row

    entry = summary.get(key)
    if entry is None:
        # Unreachable in practice: the aggregate query groups the SAME
        # underlying rows by the SAME (post-rebucketing) key, so any
        # candidate that reaches here already has an entry. Guard anyway
        # rather than raise out of a read path.
        return

    view = view_from_row(dict(row))
    if not is_suppressed(view, policy):
        return

    # unread must never go below 0. This clamp should be unreachable: every
    # suppressed candidate row was already counted into `newer`/`newer_spam`
    # (and thus into entry["unread"]) by the identical predicate above, so
    # there is always at least one unit left to subtract. Kept as a hard
    # floor, not an assertion, because a future change to either query is a
    # data bug, not a crash-worthy one on a read path.
    if entry["unread"] > 0:
        entry["unread"] -= 1
    else:
        logger.warning(
            "get_conversation_summary: unread clamp hit for key=%r (should be unreachable)",
            key,
        )


def _emit_row(data: dict[str, Any], blocklist_filter: HistoryFilter | None) -> str | None:
    """Serialise one built row, or return None when the blocklist drops it."""
    if blocklist_filter is not None:
        filtered = blocklist_filter(data)
        if filtered is None:
            return None
        data = filtered
    return json.dumps(data, ensure_ascii=False)


class QueryMixin(StorageBase):
    async def get_message_count(self) -> int:
        """Get current message count."""
        result = await self._query("SELECT COUNT(*) as count FROM messages")
        return result[0]["count"] if result else 0

    async def get_storage_size_mb(self) -> float:
        """Get current database file size in MB."""

        def _get_size() -> float:
            if self.db_path.exists():
                return self.db_path.stat().st_size / (1024 * 1024)
            return 0.0

        return await asyncio.to_thread(_get_size)

    async def prune_messages(
        self,
        prune_hours: int,
        block_list: list[str],
        prune_hours_pos: int = DEFAULT_POS_RETENTION_HOURS,
        prune_hours_ack: int = DEFAULT_POS_RETENTION_HOURS,
    ) -> int:
        """Prune old messages with type-based retention.

        Args:
            prune_hours: Retention for chat messages (type='msg'), default 30 days.
            block_list: Callsigns to delete unconditionally.
            prune_hours_pos: Retention for position data (type='pos'), default 8 days.
            prune_hours_ack: Retention for ACKs (type='ack'), default 8 days.
        """
        # NOTE: datetime.now(timezone.utc), not datetime.utcnow(). The latter returns a
        # naive datetime whose .timestamp() is interpreted as LOCAL time, shifting every
        # cutoff below by the local UTC offset (2h in CEST). That used to leave only a
        # ~2h/day sliver of 5-min buckets for the rollup. See doc/charts-wrong.md §13.
        now = datetime.now(UTC)
        cutoff_msg_ms = int((now - timedelta(hours=prune_hours)).timestamp() * 1000)
        cutoff_pos_ms = int((now - timedelta(hours=prune_hours_pos)).timestamp() * 1000)
        cutoff_ack_ms = int((now - timedelta(hours=prune_hours_ack)).timestamp() * 1000)

        # Delete by type-specific retention
        await self._mutate(
            "DELETE FROM messages WHERE type = 'msg' AND timestamp < ?",
            (cutoff_msg_ms,),
        )
        await self._mutate(
            "DELETE FROM messages WHERE type = 'pos' AND timestamp < ?",
            (cutoff_pos_ms,),
        )
        await self._mutate(
            "DELETE FROM messages WHERE type = 'ack' AND timestamp < ?",
            (cutoff_ack_ms,),
        )
        # The attribution ledger follows the ACK retention, not the chat one: an
        # ack list is only meaningful next to a bubble, and 8 days covers every
        # bubble anyone opens the details popover on.
        await self._mutate(
            "DELETE FROM message_acks WHERE timestamp < ?",
            (cutoff_ack_ms,),
        )
        # Catch-all for any other types: use the shortest retention
        min_cutoff_ms = max(cutoff_pos_ms, cutoff_ack_ms)
        await self._mutate(
            "DELETE FROM messages WHERE type NOT IN ('msg', 'pos', 'ack') AND timestamp < ?",
            (min_cutoff_ms,),
        )

        # Delete blocked sources
        if block_list:
            placeholders = ",".join("?" * len(block_list))
            await self._mutate(
                f"DELETE FROM messages WHERE src IN ({placeholders})",  # noqa: S608 - identifiers from fixed set; values parameterized
                tuple(block_list),
            )

        # Delete invalid messages
        await self._mutate(
            "DELETE FROM messages WHERE msg = ? OR msg LIKE ?",
            (INVALID_CHARACTER_MSG, f"%{CORE_DUMP_FILTER_TEXT}%"),
        )

        # --- Prune new tables ---
        # telemetry: 365 days (supports "Last Year" WX view)
        cutoff_telemetry_ms = int((now - timedelta(days=LONG_RETENTION_DAYS)).timestamp() * 1000)
        await self._mutate(
            "DELETE FROM telemetry WHERE timestamp < ?",
            (cutoff_telemetry_ms,),
        )
        # signal_log: 8 days
        await self._mutate(
            "DELETE FROM signal_log WHERE timestamp < ?",
            (cutoff_pos_ms,),
        )
        # signal_buckets: 5-min buckets = 8 days, 1-hour buckets = 365 days
        await self._mutate(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (BUCKET_SECONDS * 1000, cutoff_pos_ms),
        )
        cutoff_1h_ms = int((now - timedelta(days=LONG_RETENTION_DAYS)).timestamp() * 1000)
        await self._mutate(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (HOURLY_BUCKET_MS, cutoff_1h_ms),
        )
        # station_positions: optionally prune stations not seen in STATION_RETENTION_DAYS
        cutoff_30d_ms = int((now - timedelta(days=STATION_RETENTION_DAYS)).timestamp() * 1000)
        await self._mutate(
            "DELETE FROM station_positions WHERE last_seen IS NOT NULL AND last_seen < ?",
            (cutoff_30d_ms,),
        )
        # link_uptime_segments: gateway-uptime ledger, 400 days. Deliberately NOT
        # added to the size-based emergency prune loop below — this ledger is
        # tens of transition rows per month, orders of magnitude smaller than
        # the tables already in that loop, so dropping its OLDEST rows would
        # not free meaningful space but WOULD silently truncate the "no data at
        # all before this point" boundary the reader relies on, corrupting the
        # ledger's history for no real benefit.
        cutoff_uptime_ms = int(
            (now - timedelta(days=LINK_UPTIME_RETENTION_DAYS)).timestamp() * 1000
        )
        await self._mutate(
            "DELETE FROM link_uptime_segments WHERE end_ms < ?",
            (cutoff_uptime_ms,),
        )

        # --- Size-based pruning: enforce 1 GB hard limit ---
        # SQLite doesn't shrink the file on DELETE (pages go to freelist), so we
        # estimate how many rows to delete, remove them, then VACUUM once to reclaim.
        size_mb = await self.get_storage_size_mb()
        if size_mb > self.MAX_DB_SIZE_MB:
            logger.warning(
                "DB size %.0f MB exceeds %d MB limit — pruning oldest data",
                size_mb,
                self.MAX_DB_SIZE_MB,
            )
            target_mb = self.MAX_DB_SIZE_MB * PRUNE_TARGET_FRACTION
            excess_bytes = int((size_mb - target_mb) * 1024 * 1024)
            rows_to_free = max(MIN_PRUNE_ROWS, excess_bytes // EST_BYTES_PER_ROW)

            for table, ts_col in [
                ("signal_log", "timestamp"),
                ("signal_buckets", "bucket_ts"),
                ("messages", "timestamp"),
            ]:
                result = await self._query(f"SELECT COUNT(*) as c FROM {table}")  # noqa: S608 - identifiers from fixed set; values parameterized
                table_count = result[0]["c"] if result else 0
                to_delete = min(table_count, rows_to_free)
                if to_delete > 0:
                    await self._mutate(
                        f"DELETE FROM {table} WHERE rowid IN"  # noqa: S608 - identifiers from fixed set; values parameterized
                        f" (SELECT rowid FROM {table} ORDER BY {ts_col} ASC LIMIT ?)",
                        (to_delete,),
                    )
                    logger.info("Size limit: deleted %d oldest rows from %s", to_delete, table)
                    rows_to_free -= to_delete
                if rows_to_free <= 0:
                    break

            # VACUUM rebuilds the file to reclaim disk space
            await self._mutate("VACUUM")
            new_size = await self.get_storage_size_mb()
            logger.info("Size-based pruning complete: %.0f MB → %.0f MB", size_mb, new_size)

        # Update query planner statistics after bulk deletes
        await self._mutate("ANALYZE")

        count = await self.get_message_count()
        logger.info("After pruning: %d messages remaining", count)
        return count

    async def aggregate_hourly_buckets(self) -> int:
        """Aggregate old 5-min buckets into 1-hour buckets.

        Called by the nightly prune job. Takes 5-min buckets older than 8 days
        and rolls them up into 1-hour buckets for long-term storage.
        """
        now_ts_ms = now_ms()
        cutoff_ms = now_ts_ms - EIGHT_DAYS_MS
        bucket_5min_ms = BUCKET_SECONDS * 1000

        await self._mutate(
            f"""
            INSERT OR REPLACE INTO signal_buckets
                (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,
                 snr_avg, snr_min, snr_max, count)
            SELECT
                callsign,
                (bucket_ts / {HOURLY_BUCKET_MS}) * {HOURLY_BUCKET_MS} AS hour_ts,
                {HOURLY_BUCKET_MS},
                SUM(rssi_avg * count) / SUM(count),
                MIN(rssi_min), MAX(rssi_max),
                SUM(snr_avg * count) / SUM(count),
                MIN(snr_min), MAX(snr_max),
                SUM(count)
            FROM signal_buckets
            WHERE bucket_size = ?
              AND bucket_ts < ?
            GROUP BY callsign, hour_ts
            """,  # noqa: S608 - identifiers from fixed set; values parameterized
            (bucket_5min_ms, cutoff_ms),
        )

        # Remove the aggregated 5-min buckets
        await self._mutate(
            "DELETE FROM signal_buckets WHERE bucket_size = ? AND bucket_ts < ?",
            (bucket_5min_ms, cutoff_ms),
        )

        logger.info("Aggregated old 5-min buckets into hourly buckets")
        return 0

    @staticmethod
    def _build_position_dict(row: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0912 - complex handler kept intact
        """Build a position dict from station_positions row."""
        pos_data: dict[str, Any] = {
            "type": "pos",
            "src": row["callsign"],
            "src_type": "lora" if row["source"] == "local" else "www",
            "dst": "",
            "via": row["via_shortest"] or "",
            "timestamp": row["last_seen"] or 0,
        }
        # Location fields
        if row["lat"] is not None:
            pos_data["lat"] = row["lat"]
        if row["lon"] is not None:
            pos_data["lon"] = row["lon"]
        if row["alt"] is not None:
            pos_data["alt"] = row["alt"]
        if row["lat_dir"]:
            pos_data["lat_dir"] = row["lat_dir"]
        if row["lon_dir"]:
            pos_data["lon_dir"] = row["lon_dir"]
        # Hardware/firmware
        if row["hw_id"] is not None:
            pos_data["hw_id"] = row["hw_id"]
        if row["firmware"]:
            pos_data["firmware"] = row["firmware"]
        if row["fw_sub"]:
            pos_data["fw_sub"] = row["fw_sub"]
        if row["aprs_symbol"]:
            pos_data["aprs_symbol"] = row["aprs_symbol"]
        if row["aprs_symbol_group"]:
            pos_data["aprs_symbol_group"] = row["aprs_symbol_group"]
        if row["batt"] is not None:
            pos_data["batt"] = row["batt"]
        if row["gw"] is not None:
            pos_data["gw"] = row["gw"]
        # Signal quality (from MHeard beacons)
        if row["rssi"] is not None:
            pos_data["rssi"] = row["rssi"]
            # KEY presence (not truthiness) is load-bearing here, unlike via_paths
            # above. The client uses whether "signal_via" is present at all to tell
            # a live single-frame observation (this write path — safe to derive
            # attribution from) apart from an aggregated/legacy snapshot row that
            # never got it (its via_shortest is a historical shortest path, unrelated
            # to which station actually delivered THIS rssi/snr — deriving from it
            # would silently reproduce the wrong-station bug this column exists to
            # fix). So emit the key on every rssi row even when the stored value is
            # '' (unknown, pre-migration row): omitting it would look identical to a
            # legacy row to the client and re-invite that derivation.
            pos_data["signal_via"] = row["signal_via"] or ""
        if row["snr"] is not None:
            pos_data["snr"] = row["snr"]
        # MHeard-specific fields
        if row["lora_mod"] is not None:
            pos_data["lora_mod"] = row["lora_mod"]
        if row["mesh"] is not None:
            # Wire key is "mesh_info" (not "mesh", the station_positions column
            # name) to match the live msg/pos path — ble_protocol.py's
            # transform_common_fields() emits "mesh_info" for live frames, and
            # the FE (messageProcessor.ts) only reads raw.mesh_info. Before this
            # fix, replayed positions (smart_initial/messages_page) silently lost
            # this field on initial load since the FE never looked for "mesh".
            pos_data["mesh_info"] = row["mesh"]
        # Via paths for relay line drawing
        if row["via_paths"] and row["via_paths"] != "[]":
            pos_data["via_paths"] = row["via_paths"]
        # Telemetry fields
        for tf in ("temp1", "temp2", "hum", "hum2", "qfe", "qnh", "gas", "co2"):
            if row.get(tf) is not None:
                pos_data[tf] = row[tf]
        return pos_data

    async def get_smart_initial_with_summary(
        self,
        limit_per_dst: int = DEFAULT_PAGE_SIZE,
        blocklist_filter: HistoryFilter | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Get smart initial payload + summary in a single thread call.

        Uses a ROW_NUMBER() window function partitioned by conversation_key
        to fetch the last N messages per conversation in one query, instead
        of N+1 queries (one per destination).

        `blocklist_filter` (MessageRouter.filter_history_row) is applied to
        every message, ack and position on the way out. It is what makes a
        blocklist entry retroactive: rows a station deposited before it was
        blocked are still in the table, and this is the only place that keeps
        them off every client's screen without a per-host DELETE.
        """
        build_msg = self._build_message_dict
        build_pos = self._build_position_dict
        # ST-07: bound both full-history scans by a generous window so SQLite's
        # existing idx_messages_type_timestamp(type, timestamp DESC) index can do
        # a range seek (type=? AND timestamp>=?) instead of walking every `type='msg'`
        # row regardless of age. LONG_RETENTION_DAYS is the app's own definition of
        # its outer retention horizon — actual configured prune_hours is normally far
        # shorter, so this is a safety backstop, not an effective behavior change,
        # confirmed via EXPLAIN QUERY PLAN on a fixture DB before/after this change.
        window_cutoff_ms = now_ms() - LONG_RETENTION_DAYS * SECONDS_PER_DAY * 1000

        def _run() -> tuple[dict[str, Any], dict[str, Any]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA query_only=ON")
                # 1. Messages: window function, partition by conversation_key
                msg_rows = conn.execute(
                    f"SELECT {_MSG_SELECT} FROM ("  # noqa: S608 - identifiers from fixed set; values parameterized
                    f"  SELECT *, ROW_NUMBER() OVER ("
                    f"    PARTITION BY COALESCE(conversation_key, dst)"
                    f"    ORDER BY timestamp DESC"
                    f"  ) AS rn FROM messages"
                    f"  WHERE type = 'msg' AND {_NOT_ACK_SQL} AND timestamp >= ?"
                    f") ranked WHERE rn <= ?"
                    f" ORDER BY timestamp ASC",
                    (window_cutoff_ms, limit_per_dst),
                ).fetchall()
                messages = [
                    emitted
                    for row in msg_rows
                    if (emitted := _emit_row(build_msg(dict(row)), blocklist_filter)) is not None
                ]

                # 2. Positions: station_positions table
                pos_rows = conn.execute(
                    "SELECT * FROM station_positions",
                ).fetchall()
                positions = [
                    emitted
                    for row in pos_rows
                    if (emitted := _emit_row(build_pos(dict(row)), blocklist_filter)) is not None
                ]

                # 3. ACK messages — the PEER shape only, deliberately NOT the
                # complement of the exclusion the message queries apply: a bare
                # APRS 'ack%04i' is excluded from history AND served here to
                # nobody, because it carries no correlator a client could match
                # (see _PEER_ACK_GLOBS / _APRS_ACK_GLOBS at the top of this
                # module for the three-way split). GLOB, not LIKE: SQLite GLOB
                # is case-sensitive and [0-9] is ASCII-only — that is the point:
                # the firmware emits '%-9.9s:ack%03i' (lowercase, 3 ASCII
                # digits), while LIKE's ASCII case-insensitivity silently
                # swallowed human messages containing ':ACK99' from history sync
                # (ack_predicate_vectors.json v2, strict tier).
                ack_rows = conn.execute(
                    f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                    f" WHERE type = 'msg' AND {_PEER_ACK_SQL}"
                    f" ORDER BY timestamp DESC LIMIT {INITIAL_ACK_LIMIT}",
                ).fetchall()
                acks = [
                    emitted
                    for row in ack_rows
                    if (emitted := _emit_row(build_msg(dict(row)), blocklist_filter)) is not None
                ]

                # 4. Summary counts
                # Grouped by src/dst as well as key when a blocklist filter is
                # active: the counts drive the sidebar badges, so leaving them
                # unfiltered would advertise conversations whose messages the
                # filter above has just removed. The extra group columns are only
                # paid for when there is something to filter.
                if blocklist_filter is None:
                    summary_rows = conn.execute(
                        "SELECT COALESCE(conversation_key, dst) AS key, COUNT(*) as cnt"  # noqa: S608 - ack predicate is a module constant; values parameterized
                        " FROM messages"
                        f" WHERE type = 'msg' AND {_NOT_ACK_SQL} AND timestamp >= ?"
                        " GROUP BY key",
                        (window_cutoff_ms,),
                    ).fetchall()
                    summary = {row["key"]: row["cnt"] for row in summary_rows if row["key"]}
                else:
                    summary_rows = conn.execute(
                        "SELECT COALESCE(conversation_key, dst) AS key, src, dst,"  # noqa: S608 - ack predicate is a module constant; values parameterized
                        " COUNT(*) as cnt"
                        " FROM messages"
                        f" WHERE type = 'msg' AND {_NOT_ACK_SQL} AND timestamp >= ?"
                        " GROUP BY key, src, dst",
                        (window_cutoff_ms,),
                    ).fetchall()
                    counts: dict[str, int] = defaultdict(int)
                    for row in summary_rows:
                        if not row["key"]:
                            continue
                        kept = blocklist_filter({"src": row["src"] or "", "dst": row["dst"] or ""})
                        if kept is None:
                            continue  # dropped outright
                        # A quarantined group post is counted under SPAM_GROUP,
                        # matching where the message itself now shows up.
                        key = SPAM_GROUP if kept.get("dst") == SPAM_GROUP else row["key"]
                        counts[key] += row["cnt"]
                    summary = dict(counts)

                initial = {"messages": messages, "positions": positions, "acks": acks}
                return initial, summary

        initial, summary = await asyncio.to_thread(_run)
        logger.debug(
            "smart_initial: %d msgs, %d pos, %d acks",
            len(initial["messages"]),
            len(initial["positions"]),
            len(initial["acks"]),
        )
        return initial, summary

    async def get_smart_initial(self, limit_per_dst: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        """Get smart initial payload (wrapper around get_smart_initial_with_summary)."""
        initial, _ = await self.get_smart_initial_with_summary(limit_per_dst)
        return initial

    async def get_summary(self) -> dict[str, Any]:
        """Get message count per destination (wrapper around combined method)."""
        _, summary = await self.get_smart_initial_with_summary()
        return summary

    async def get_conversation_summary(
        self,
        my_callsign: str,
        blocklist_filter: HistoryFilter | None = None,
        key: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Per-conversation count/last_ts/unread, joined against read_cursors.

        Unread-cursor rework (doc/2026-09-06_1200-unread-cursor-plan.md §3/§4):
        added BESIDE `get_smart_initial_with_summary` (which stays byte-
        identical) rather than replacing it, so the legacy `summary`/
        `read_counts` wire keeps working for one release (D6) while the new
        `conversations`/`read_cursors` events are introduced alongside it.

        `unread(key) = COUNT(rows WHERE timestamp > cursor AND sender != me)`
        — a missing cursor (LEFT JOIN, `COALESCE(rc.ts, 0)`) reads as "0",
        i.e. every row counts, never as "all read". `last_ts` is MAX(timestamp)
        over ALL rows of the key, own messages included — it answers "when did
        this conversation last see any traffic", not "when did I last hear
        from someone else".

        Same window cutoff and the same `type='msg' AND {_NOT_ACK_SQL}`
        predicate as `get_smart_initial_with_summary`, and the
        same blocklist re-bucketing (a quarantined group post is counted under
        SPAM_GROUP, matching where the message itself now shows up) — see that
        method's docstring for why this must run on the way OUT of storage to
        be retroactive. Unlike that method, `unread` for a rebucketed row is
        NOT best-effort here: a second LEFT JOIN (`rc2`) resolves the SPAM_GROUP
        cursor and the row counts as unread only past MAX(original cursor,
        SPAM_GROUP cursor), so marking 9999 read actually clears the badge.

        `key` narrows the scan to one conversation (the per-POST refresh in
        `/api/read_cursor`, which has to hand the client a fresh `unread` for
        the key it just advanced — the client's local window is capped and
        cannot recompute it). SPAM_GROUP is deliberately NOT narrowable: its
        rows live under their ORIGINAL keys and only land in the bucket via the
        blocklist rebucketing below, so a `key = SPAM_GROUP` predicate would
        match nothing — callers pass `key=None` for it and read the bucket out
        of the full scan.

        Suppression (doc/2026-09-19_2140-unread-suppression-plan.md §D1):
        `unread` additionally excludes any row the client's own spam filter
        or blocklist would hide, via the shared predicate in
        `storage.suppression`. Without this, a conversation whose NEWEST
        message is one the client never renders carries a badge no client
        action can ever clear — the read cursor only advances over rendered
        bubbles, so a hidden trailing message is unreachable by any "mark
        read". The policy (`load_policy`) is loaded once per call on the same
        connection; `policy_is_noop` short-circuits the common case (an
        install that never touched the spam-filter settings or blocklist) so
        it costs nothing beyond that one load — the second query below runs
        only when something could actually be suppressed. `count` and
        `last_ts` are DELIBERATELY NOT filtered by this predicate: they
        answer "how many messages does this conversation hold" and must
        match the client's own total (a hidden message still belongs to the
        conversation), while only `unread`'s meaning narrows to "unread AND
        visible to you".
        """
        window_cutoff_ms = now_ms() - LONG_RETENTION_DAYS * SECONDS_PER_DAY * 1000
        my_base = my_callsign.split("-", maxsplit=1)[0].upper()
        key_clause = "" if key is None else " AND COALESCE(m.conversation_key, m.dst) = ?"
        params: tuple[Any, ...] = (
            (window_cutoff_ms, SPAM_GROUP) if key is None else (window_cutoff_ms, key, SPAM_GROUP)
        )
        dedup_sql = _conv_dedup_subquery(key_clause)

        def _run() -> dict[str, dict[str, int]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA query_only=ON")

                policy = load_policy(conn)

                # One row per DISTINCT message first (subquery `d`), then per
                # conversation. The same message is stored once per transport
                # it arrived over — the UDP datagram and the BLE copy land as two
                # rows with the same msg_id ~100 ms apart (the v3 migration
                # dropped the msg_id UNIQUE constraint on purpose) — while the
                # webapp dedups to the FIRST copy and marks read with that copy's
                # timestamp. Counting rows instead of messages left the later
                # sibling "newer than the cursor" forever: on mcapp.local every
                # conversation whose newest message came in over two transports
                # sat at +1 with nothing a client could do about it
                # (v2.0.4-dev.1, 2026-09-06). A message is therefore judged by
                # its EARLIEST copy, which is the one the client holds. Rows
                # without a msg_id never collapse into each other (rowid
                # fallback). src/dst (and, since the suppression wave, the
                # classifier columns) are taken from the earliest copy too —
                # see `_conv_dedup_subquery`'s docstring for why that is
                # guaranteed, not incidental.
                rows = conn.execute(
                    "SELECT COALESCE(d.conversation_key, d.dst) AS key, d.src, d.dst,"  # noqa: S608 - dedup_sql/key_clause are fixed literals; values parameterized
                    " COUNT(*) AS cnt, MAX(d.ts) AS last_ts,"
                    f" SUM(CASE WHEN {_CONV_NEWER_EXPR} THEN 1 ELSE 0 END) AS newer,"
                    f" SUM(CASE WHEN {_CONV_NEWER_SPAM_EXPR} THEN 1 ELSE 0 END)"
                    "   AS newer_spam"
                    f" FROM ({dedup_sql}) d"
                    + _CONV_CURSOR_JOINS
                    # Positional GROUP BY, not "GROUP BY key": with the two
                    # read_cursors joins a bare "key" is ambiguous between the
                    # SELECT alias and rc.key/rc2.key and SQLite rejects the
                    # query outright with "ambiguous column name: key".
                    + " GROUP BY 1, d.src, d.dst",
                    params,
                ).fetchall()

                summary: dict[str, dict[str, int]] = {}
                for row in rows:
                    _apply_conversation_row(row, summary, blocklist_filter, my_base)

                if policy_is_noop(policy):
                    # Common case (an install that never touched the spam
                    # filter or blocklist): zero extra query cost, byte-
                    # identical to pre-suppression behaviour.
                    return summary

                # Second pass: per-row candidates only (not grouped), so each
                # can be individually judged by `is_suppressed` and, if
                # hidden, subtracted from the `unread` the loop above already
                # added it to. Built from the SAME dedup subquery and the
                # SAME two boolean expressions as the aggregate query above
                # (`_conv_dedup_subquery`, `_CONV_NEWER_EXPR`,
                # `_CONV_NEWER_SPAM_EXPR`) — see the module-level comment by
                # those definitions for why that sharing is load-bearing.
                candidate_rows = conn.execute(
                    "SELECT COALESCE(d.conversation_key, d.dst) AS key, d.src, d.dst,"  # noqa: S608 - dedup_sql/key_clause are fixed literals; values parameterized
                    " d.msg, d.category, d.tags, d.info_score, d.template_hash,"
                    f" ({_CONV_NEWER_EXPR}) AS newer,"
                    f" ({_CONV_NEWER_SPAM_EXPR}) AS newer_spam"
                    f" FROM ({dedup_sql}) d"
                    + _CONV_CURSOR_JOINS
                    + f" WHERE ({_CONV_NEWER_EXPR}) OR ({_CONV_NEWER_SPAM_EXPR})",
                    params,
                ).fetchall()

                for row in candidate_rows:
                    _subtract_suppressed_row(row, summary, blocklist_filter, my_base, policy)

                return summary

        return await asyncio.to_thread(_run)

    async def get_messages_page(
        self,
        dst: str,
        before_timestamp: int | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        src: str | None = None,
        blocklist_filter: HistoryFilter | None = None,
    ) -> dict[str, Any]:
        """Get a page of messages for a destination, cursor-based.

        For personal DMs (dst is a callsign, not a group number), pass src
        to query via conversation_key for a single-index scan. A hashtag dst
        ('#OE-SOTA') takes the same group-style conversation_key path as a
        numeric group (hashtag_dst_vectors.json).

        `blocklist_filter` is applied after the query, for the same retroactive
        reason as in `get_smart_initial_with_summary` — scrolling back must not
        re-surface what the initial burst filtered out.
        """
        if before_timestamp is None:
            before_timestamp = now_ms()

        # A hashtag dst ('#OE-SOTA') is neither digit-only nor '*', so without
        # an explicit exclusion is_dm was true whenever src was supplied and
        # the group/hashtag branches below never ran (hashtag_dst_vectors.json).
        # resolve_dst_target handles a via-routed dst param ('RELAY-1,#OE-SOTA')
        # the same way compute_conversation_key does, so the hashtag arm below
        # stays reachable for that shape too — the common case on a mesh.
        target = resolve_dst_target(dst) if dst else ""
        is_hashtag_dst = bool(target) and is_hashtag(target)
        # is_dm keeps the bare digit-shape test on purpose: its question is
        # "could dst be a personal callsign", not "is dst a group" — an
        # out-of-range digit dst like '0' is neither and must fall through to
        # the exact-dst arm below (which still serves its legacy rows), not
        # into the DM arm whose conversation_key would be NULL.
        is_dm = dst and src and not dst.isdigit() and dst != "*" and not is_hashtag_dst
        is_group_dst = bool(dst) and is_group(dst)

        params: tuple[Any, ...] = ()
        if dst == "Time":
            # Webapp's virtual Time chat: {CET}-prefixed broadcasts, split
            # out of '*' like in delete_messages_by_dst. New {CET} messages
            # are dropped by _should_filter_message, so this only serves
            # pre-filter legacy rows.
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                " WHERE type = 'msg' AND conversation_key = '*'"
                " AND msg LIKE '{CET}%' AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)
        elif dst == "*":
            # Broadcast: match via conversation_key so via-routed rows
            # (dst 'DB0FHR-12,*' → key '*') are included; {CET} rows
            # belong to the virtual Time chat
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                f" WHERE type = 'msg' AND {_NOT_ACK_SQL}"
                # (msg IS NULL OR ...) mirrors delete_messages_by_dst: in SQLite
                # `NULL NOT LIKE x` is NULL, not true, so a NULL-msg broadcast row was
                # invisible on this page while the '*' DELETE happily removed it.
                " AND conversation_key = '*' AND (msg IS NULL OR msg NOT LIKE '{CET}%')"
                " AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)
        elif is_dm:
            # DM: compute conversation_key and use idx_messages_convkey_ts
            conv_key = compute_conversation_key(src or "", dst)
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                " WHERE type = 'msg' AND conversation_key = ?"
                " AND timestamp < ? ORDER BY timestamp DESC LIMIT ?"
            )
            params = (conv_key, before_timestamp, limit + 1)
        elif is_group_dst:
            # Group: match via conversation_key so via-routed posts
            # (dst 'VIA,232' → key '232') are included in the page.
            # Unified predicate (group_dst_vectors.json): an out-of-range
            # digit dst never takes this shape — post-v2 its rows carry a
            # NULL conversation_key, which this arm could never match.
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                f" WHERE type = 'msg' AND {_NOT_ACK_SQL}"
                " AND conversation_key = ? AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (dst, before_timestamp, limit + 1)
        elif is_hashtag_dst:
            # Hashtag channel: same group-style conversation_key match, using
            # the RESOLVED tag (not the raw dst param) so a via-routed dst
            # ('RELAY-1,#OE-SOTA' → key '#OE-SOTA') is reachable too — the
            # common case on a mesh. compute_conversation_key keys a hashtag
            # on the resolved tag verbatim (conversation_key_vectors.json v4).
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                f" WHERE type = 'msg' AND {_NOT_ACK_SQL}"
                " AND conversation_key = ? AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (target, before_timestamp, limit + 1)
        elif dst:
            # Last-resort exact match. A callsign dst normally cannot reach here: the
            # route layer resolves a missing `src` to the node's own callsign (mirroring
            # delete_messages_by_dst's own_call fallback) so `is_dm` holds and the
            # conversation_key branch above runs. Relay-hopped rows are keyed by
            # conversation_key (migration v18), so this branch would miss them — it only
            # remains for rows a pre-v18 mcdump import left unkeyed.
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                f" WHERE type = 'msg' AND {_NOT_ACK_SQL}"
                " AND dst = ? AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (dst, before_timestamp, limit + 1)
        else:
            query = (
                f"SELECT {_MSG_SELECT} FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                f" WHERE type = 'msg' AND {_NOT_ACK_SQL}"
                " AND timestamp < ?"
                " ORDER BY timestamp DESC LIMIT ?"
            )
            params = (before_timestamp, limit + 1)

        rows = await self._query(query, params)

        # has_more stays keyed on the RAW row count, deliberately: a page whose
        # rows are all filtered out must still report has_more so the client keeps
        # walking backwards instead of concluding it reached the start of history.
        has_more = len(rows) > limit
        result = [
            emitted
            for row in rows[:limit]
            if (emitted := _emit_row(self._build_message_dict(row), blocklist_filter)) is not None
        ]
        result.reverse()
        return {"messages": result, "has_more": has_more}

    async def get_full_dump(self) -> list[str]:
        """Get full message dump."""
        query = f"SELECT {_MSG_SELECT} FROM messages WHERE type = 'msg' ORDER BY timestamp"  # noqa: S608 - identifiers from fixed set; values parameterized
        rows = await self._query(query)
        return [json.dumps(self._build_message_dict(row), ensure_ascii=False) for row in rows]

    async def process_mheard_store_parallel(
        self, progress_callback: Any = None
    ) -> list[dict[str, Any]]:
        """Process messages for MHeard statistics.

        Reads from pre-aggregated signal_buckets table instead of scanning
        all messages. Falls back to legacy scan if signal_buckets is empty.
        """
        cutoff_ms = now_ms() - SEVEN_DAYS_MS
        bucket_5min_ms = BUCKET_SECONDS * 1000

        if progress_callback:
            await progress_callback("start", "Querying database...")

        # Flush in-memory partial buckets BEFORE either branch below queries
        # signal_buckets. Moved here from inside the `if bucket_rows:` branch: when
        # signal_buckets is empty (the fresh-install case) the newest bucket per
        # station still lives only in RAM (_accumulate_signal only writes a bucket
        # out once the SAME callsign is heard again in a later window), so the old
        # placement meant the very first page load after ingestion started saw
        # nothing. Flush writes are INSERT OR REPLACE, so re-flushing an
        # already-persisted bucket here is idempotent.
        await self._flush_all_accumulators()

        # Try reading from pre-aggregated signal_buckets first
        bucket_rows = await self._query(
            "SELECT callsign, bucket_ts, rssi_avg, rssi_min, rssi_max,"
            "       snr_avg, snr_min, snr_max, count"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?",
            (bucket_5min_ms, cutoff_ms),
        )

        if bucket_rows:
            # Use pre-aggregated data — much faster
            logger.debug("Using %d pre-aggregated signal_buckets", len(bucket_rows))
            return await self._build_chart_series(
                bucket_rows,
                gap_threshold_s=GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS,
                gap_offset_s=BUCKET_SECONDS,
                progress_callback=progress_callback,
            )

        # --- Fallback: scan signal_log, the authoritative per-measurement table ---
        # (not `messages`: signal_log is written by every signal-bearing packet via
        # _ingest_signal, ingest.py, so it is a superset of what the `messages` scan
        # ever saw and keys off the same `callsign` column the bucket path accumulates
        # under — no comma-splitting of `src` needed here).
        logger.info("signal_buckets empty, falling back to signal_log scan")

        query = """
            SELECT callsign, timestamp, rssi, snr
            FROM signal_log
            WHERE timestamp >= ?
                AND rssi IS NOT NULL AND snr IS NOT NULL
                AND rssi BETWEEN ? AND ?
                AND snr BETWEEN ? AND ?
        """
        params = (
            cutoff_ms,
            VALID_RSSI_RANGE[0],
            VALID_RSSI_RANGE[1],
            VALID_SNR_RANGE[0],
            VALID_SNR_RANGE[1],
        )

        rows = await self._query(query, params)
        logger.info("Processing %d rows for mheard statistics (legacy)", len(rows))

        if progress_callback:
            await progress_callback("bucketing", f"Processing {len(rows)} rows...")

        buckets: dict[tuple[int, str], dict[str, list[float | int]]] = defaultdict(
            lambda: {"rssi": [], "snr": []}
        )

        for row in rows:
            call = row["callsign"]
            if not call:
                continue
            timestamp_ms = row["timestamp"]
            bucket_time = int(timestamp_ms // 1000 // BUCKET_SECONDS * BUCKET_SECONDS)
            key = (bucket_time, call)
            buckets[key]["rssi"].append(row["rssi"])
            buckets[key]["snr"].append(row["snr"])

        bucket_rows = self._legacy_buckets_to_rows(buckets)
        return await self._build_chart_series(
            bucket_rows,
            gap_threshold_s=GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS,
            gap_offset_s=BUCKET_SECONDS,
            progress_callback=progress_callback,
        )

    async def _process_mheard_window(
        self, cutoff_ms: int, label: str, progress_callback: Any = None
    ) -> list[dict[str, Any]]:
        """Shared body of the yearly/monthly mHeard reports.

        They differed only in the cutoff and three strings, while `main.py` already
        parameterizes the three *callers* through `_MHEARD_DUMP_VARIANTS` — so the gap
        parameters, the empty-result early return and the progress protocol were
        maintained in two places for one concept.
        """
        if progress_callback:
            await progress_callback("start", f"Querying {label} data...")
        bucket_rows = await self._query_rolled_up_buckets(cutoff_ms)
        if not bucket_rows:
            if progress_callback:
                await progress_callback("done", f"No {label} data available")
            return []
        logger.debug("Using %d hourly signal_buckets for %s report", len(bucket_rows), label)
        return await self._build_chart_series(
            bucket_rows,
            gap_threshold_s=HOURLY_GAP_THRESHOLD,
            gap_offset_s=HOURLY_BUCKET_S,
            progress_callback=progress_callback,
        )

    async def process_mheard_yearly(self, progress_callback: Any = None) -> list[dict[str, Any]]:
        """Process 1-hour signal buckets for yearly mHeard statistics."""
        return await self._process_mheard_window(
            now_ms() - ONE_YEAR_MS, "yearly", progress_callback
        )

    async def process_mheard_monthly(self, progress_callback: Any = None) -> list[dict[str, Any]]:
        """Process signal buckets for 30-day mHeard statistics."""
        return await self._process_mheard_window(
            now_ms() - ONE_MONTH_MS, "monthly", progress_callback
        )

    async def _query_rolled_up_buckets(self, cutoff_ms: int) -> list[dict[str, Any]]:
        """Shared query for yearly/monthly mheard stats: 5-min buckets UNION ALL'd with
        5-min buckets rolled up on the fly into hourly buckets, both filtered by cutoff.
        """
        bucket_5min_ms = BUCKET_SECONDS * 1000
        return await self._query(
            "SELECT callsign, bucket_ts, rssi_avg, rssi_min, rssi_max,"  # noqa: S608 - identifiers from fixed set; values parameterized
            "       snr_avg, snr_min, snr_max, count"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            " UNION ALL"
            " SELECT callsign,"
            f"       (bucket_ts / {HOURLY_BUCKET_MS}) * {HOURLY_BUCKET_MS} AS bucket_ts,"
            "       SUM(rssi_avg * count) / SUM(count),"
            "       MIN(rssi_min), MAX(rssi_max),"
            "       SUM(snr_avg * count) / SUM(count),"
            "       MIN(snr_min), MAX(snr_max),"
            "       SUM(count)"
            " FROM signal_buckets"
            " WHERE bucket_size = ? AND bucket_ts >= ?"
            f" GROUP BY callsign, (bucket_ts / {HOURLY_BUCKET_MS}) * {HOURLY_BUCKET_MS}",
            (HOURLY_BUCKET_MS, cutoff_ms, bucket_5min_ms, cutoff_ms),
        )

    @staticmethod
    def _legacy_buckets_to_rows(
        buckets: dict[tuple[int, str], dict[str, list[float | int]]],
    ) -> list[dict[str, Any]]:
        """Aggregate the legacy per-value-list buckets into rows shaped like a
        signal_buckets query result, so the legacy scan path can share
        _build_chart_series with the pre-aggregated-bucket paths.
        """
        rows = []
        for (bucket_time, callsign), values in buckets.items():
            rssi_values = values["rssi"]
            snr_values = values["snr"]
            count = min(len(rssi_values), len(snr_values))
            if count == 0:
                continue
            rows.append(
                {
                    "callsign": callsign,
                    "bucket_ts": bucket_time * 1000,
                    "rssi_avg": round(mean(rssi_values), 2),
                    "rssi_min": min(rssi_values),
                    "rssi_max": max(rssi_values),
                    "snr_avg": round(mean(snr_values), 2),
                    "snr_min": round(min(snr_values), 2),
                    "snr_max": round(max(snr_values), 2),
                    "count": count,
                }
            )
        return rows

    @staticmethod
    def _group_and_qualify(
        bucket_rows: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Group bucket rows by callsign and apply the qualification floor.

        Pure and synchronous (no awaits, no I/O beyond a plain logger call) —
        safe to run via asyncio.to_thread. Split out of _build_chart_series so
        the CPU-bound grouping pass over up to ~21 000 rows runs off the event
        loop.
        """
        callsign_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in bucket_rows:
            callsign_data[row["callsign"]].append(row)
        qualified = {
            cs: entries
            for cs, entries in callsign_data.items()
            if len(entries) >= MIN_DATAPOINTS_FOR_STATS
        }

        if not qualified and callsign_data:
            # Fresh-install / sparse-mesh case: nobody has 10 distinct 5-minute
            # buckets yet. Rather than show an empty chart for hours, fall back to
            # a floor of 1 so every station heard at least once is shown. Dense
            # installs never take this branch — see
            # doc/plan-mheard-fresh-install-fix.md §2.
            qualified = {
                cs: entries
                for cs, entries in callsign_data.items()
                if len(entries) >= SPARSE_MIN_DATAPOINTS
            }
            logger.info(
                "mheard: no station reached %d buckets, falling back to sparse floor"
                " %d (%d stations)",
                MIN_DATAPOINTS_FOR_STATS,
                SPARSE_MIN_DATAPOINTS,
                len(qualified),
            )

        return qualified

    @staticmethod
    def _build_series_chunk(
        items: list[tuple[str, list[dict[str, Any]]]],
        *,
        gap_threshold_s: int,
        gap_offset_s: int,
    ) -> list[dict[str, Any]]:
        """Build chart rows (data points + gap markers) for one chunk of
        already-sorted (callsign, entries) pairs.

        Pure and synchronous (no awaits) — safe to run via asyncio.to_thread.
        `items` is a slice of `sorted(qualified.items())`, so output order
        within and across chunks matches the pre-chunking single-pass loop.
        """
        chunk_result: list[dict[str, Any]] = []
        for callsign, entries in items:
            entries.sort(key=lambda x: x["bucket_ts"])
            segment_id = 0
            prev_time = None

            for entry in entries:
                # bucket_ts is in ms, convert to seconds for gap check
                bucket_time = entry["bucket_ts"] // 1000

                if prev_time and (bucket_time - prev_time) > gap_threshold_s:
                    chunk_result.append(
                        {
                            "src_type": "STATS",
                            "timestamp": bucket_time - gap_offset_s,
                            "callsign": callsign,
                            "rssi": None,
                            "snr": None,
                            "rssi_min": None,
                            "rssi_max": None,
                            "snr_min": None,
                            "snr_max": None,
                            "count": None,
                            "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                            "segment_size": 1,
                            "is_gap_marker": True,
                        }
                    )
                    segment_id += 1

                chunk_result.append(
                    {
                        "src_type": "STATS",
                        "timestamp": bucket_time,
                        "callsign": callsign,
                        "rssi": entry["rssi_avg"],
                        "snr": entry["snr_avg"],
                        "rssi_min": entry["rssi_min"],
                        "rssi_max": entry["rssi_max"],
                        "snr_min": entry["snr_min"],
                        "snr_max": entry["snr_max"],
                        "count": entry["count"],
                        "segment_id": f"{callsign}_seg_{segment_id}",
                        "segment_size": 1,
                    }
                )
                prev_time = bucket_time
        return chunk_result

    @staticmethod
    def _finalize_series(
        final_result: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Final sort for Chart.js, plus the "done" progress counters, in one pass.

        Pure and synchronous (no awaits) — safe to run via asyncio.to_thread.
        For the yearly dump this sort is over ~21 000 rows with a tuple key,
        which used to be the single largest remaining on-loop pass in
        _build_chart_series even after the grouping and per-chunk build were
        moved off it. The two counters are computed unconditionally, not only
        when a progress_callback is present — branching the thread call on
        the callback would leave two different code paths over the same data,
        which is how the returned series and the reported "done" counts could
        drift apart.
        """
        result = sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))
        stats_entries = [r for r in result if not r.get("is_gap_marker")]
        callsign_count = len({e["callsign"] for e in stats_entries}) if stats_entries else 0
        return result, len(stats_entries), callsign_count

    async def _build_chart_series(
        self,
        bucket_rows: list[dict[str, Any]],
        *,
        gap_threshold_s: int,
        gap_offset_s: int,
        progress_callback: Any = None,
    ) -> list[dict[str, Any]]:
        """Group bucket rows by callsign, insert gap markers, and sort for Chart.js.

        Shared by process_mheard_store_parallel (5-min buckets, both the pre-aggregated
        and legacy-scan paths), process_mheard_yearly, and process_mheard_monthly (both
        hourly-rolled-up) — the only differences between callers are the query that
        produces bucket_rows and the two window-specific gap parameters.

        The CPU-bound work (grouping/qualifying up to ~21 000 bucket rows,
        building chart rows + gap markers per station, and the final sort +
        "done" counters) runs off the event loop via asyncio.to_thread through
        the three pure helpers above — measured on mcapp.local, a yearly dump
        otherwise blocked the loop for ~1.4 s with the thread pool completely
        idle, and the final sort over ~21 000 rows was the single largest
        remaining on-loop pass even after the grouping and per-chunk build
        were moved off it. Progress is throttled to one "gaps" event per
        MHEARD_PROGRESS_CHUNK stations (was one per station: 65 on-loop SSE
        sends for a yearly dump).
        """
        qualified = await asyncio.to_thread(self._group_and_qualify, bucket_rows)

        if progress_callback:
            await progress_callback(
                "bucketing",
                f"Processing {len(bucket_rows)} buckets for {len(qualified)} stations...",
            )

        items = sorted(qualified.items())
        total = len(items)
        final_result: list[dict[str, Any]] = []
        done = 0
        for chunk_start in range(0, total, MHEARD_PROGRESS_CHUNK):
            chunk = items[chunk_start : chunk_start + MHEARD_PROGRESS_CHUNK]
            chunk_result = await asyncio.to_thread(
                self._build_series_chunk,
                chunk,
                gap_threshold_s=gap_threshold_s,
                gap_offset_s=gap_offset_s,
            )
            final_result.extend(chunk_result)
            done += len(chunk)

            if progress_callback:
                last_callsign = chunk[-1][0]
                await progress_callback(
                    "gaps",
                    f"Building chart for {last_callsign} ({done}/{total})...",
                    last_callsign,
                )

        result, stats_count, callsign_count = await asyncio.to_thread(
            self._finalize_series, final_result
        )

        if progress_callback:
            await progress_callback(
                "done",
                f"{stats_count} data points for {callsign_count} stations",
            )
        return result

    async def get_stats(self, hours: int) -> dict[str, Any]:
        """Get message statistics for the given time window.

        ST-09: msg/pos counts pushed into one grouped SQL query instead of
        fetching every row in the window and counting in Python. Distinct
        users still needs the relay-path split (`src.split(",")[0]`), which
        isn't expressible in portable SQL, but only the *distinct* raw `src`
        values are fetched now (typically orders of magnitude fewer rows than
        every message in the window) rather than one row per message.
        """
        cutoff_ms = int((time.time() - hours * 3600) * 1000)

        type_counts = await self._query(
            "SELECT type, COUNT(*) as cnt FROM messages WHERE timestamp >= ? GROUP BY type",
            (cutoff_ms,),
        )
        counts_by_type = {row["type"]: row["cnt"] for row in type_counts}

        src_rows = await self._query(
            "SELECT DISTINCT src FROM messages WHERE timestamp >= ? AND type = 'msg'",
            (cutoff_ms,),
        )
        users = {row["src"].split(",")[0] for row in src_rows if row["src"]}

        return {
            "msg_count": counts_by_type.get("msg", 0),
            "pos_count": counts_by_type.get("pos", 0),
            "users": users,
        }

    async def get_mheard_stations(self, _limit: int, _msg_type: str) -> dict[str, Any]:
        """Get recently heard stations aggregated by callsign."""
        rows = await self._query(
            "SELECT src, type, timestamp FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
            " WHERE type IN ('msg', 'pos') AND src != ''"
            f" ORDER BY timestamp DESC LIMIT {MHEARD_STATION_SCAN_LIMIT}",
        )

        stations: dict[str, dict[str, int]] = defaultdict(
            lambda: {"last_msg": 0, "msg_count": 0, "last_pos": 0, "pos_count": 0}
        )

        for row in rows:
            data_type = row["type"]
            src = row["src"]
            timestamp = row["timestamp"]

            if not src:
                continue

            call = src.split(",")[0]

            if data_type == "msg":
                stations[call]["msg_count"] += 1
                stations[call]["last_msg"] = max(stations[call]["last_msg"], timestamp)
            elif data_type == "pos":
                stations[call]["pos_count"] += 1
                stations[call]["last_pos"] = max(stations[call]["last_pos"], timestamp)

        return dict(stations)

    async def get_search_summary(
        self,
        callsign: str,
        days: int,
        search_type: str,
    ) -> dict[str, Any]:
        """Aggregate search: counts, last timestamps, destinations, SIDs."""
        cutoff_ms = int((time.time() - days * SECONDS_PER_DAY) * 1000)

        # Build src filter based on search type. The callsign is user input, so it goes
        # through escape_like + ESCAPE '\' (as get_positions already did) — otherwise a
        # search for '%' matched every src and ran three unindexed 30-day aggregates
        # over the whole messages table.
        params: tuple[Any, ...] = ()
        escaped = escape_like(callsign.upper())
        if search_type == "prefix":
            src_filter = " AND UPPER(src) LIKE ? ESCAPE '\\'"
            params = (cutoff_ms, f"%{escaped}-%")
        elif search_type == "exact":
            src_filter = " AND UPPER(src) LIKE ? ESCAPE '\\'"
            params = (cutoff_ms, f"%{escaped}%")
        else:
            src_filter = ""
            params = (cutoff_ms,)

        # Query 1: counts and last timestamps by type
        rows = await self._query(
            "SELECT type, COUNT(*) as cnt, MAX(timestamp) as last_ts"  # noqa: S608 - identifiers from fixed set; values parameterized
            f" FROM messages WHERE timestamp >= ?{src_filter}"
            " GROUP BY type",
            params,
        )
        result: dict[str, Any] = {
            "msg_count": 0,
            "pos_count": 0,
            "last_msg": None,
            "last_pos": None,
            "destinations": [],
            "sids": {},
        }
        for row in rows:
            if row["type"] == "msg":
                result["msg_count"] = row["cnt"]
                result["last_msg"] = row["last_ts"]
            elif row["type"] == "pos":
                result["pos_count"] = row["cnt"]
                result["last_pos"] = row["last_ts"]

        # Query 2: distinct numeric and hashtag destinations. GLOB '#*' was
        # missing before this fix, making every hashtag-addressed conversation
        # invisible to search (hashtag_dst_vectors.json).
        dest_rows = await self._query(
            "SELECT DISTINCT dst FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
            f" WHERE timestamp >= ? AND type = 'msg'{src_filter}"
            " AND (dst GLOB '[0-9]*' OR dst GLOB '#*')",
            params,
        )
        # GLOB '[0-9]*' only guarantees the dst STARTS with a digit, so a dst like
        # '1abc' used to raise ValueError inside key=int and 500 the request.
        # Numeric behaviour is unchanged: same filter, same sort.
        numeric_destinations = sorted(
            (r["dst"] for r in dest_rows if str(r["dst"]).isdigit()), key=int
        )
        # GLOB '#*' only guarantees the dst STARTS with '#'; is_hashtag()
        # re-validates the tag charset so a malformed '#' dst ('#OE_SOTA', bare
        # '#') -- which addresses nobody (hashtag_dst_vectors.json 'unknown') --
        # is excluded rather than surfaced as a searchable destination.
        hashtag_destinations = sorted(r["dst"] for r in dest_rows if is_hashtag(r["dst"]))
        result["destinations"] = numeric_destinations + hashtag_destinations

        # Query 3: SID activity (prefix search only)
        if search_type == "prefix":
            sid_rows = await self._query(
                "SELECT src, MAX(timestamp) as last_ts FROM messages"  # noqa: S608 - identifiers from fixed set; values parameterized
                f" WHERE timestamp >= ?{src_filter}"
                " GROUP BY src",
                params,
            )
            sids: dict[str, int] = {}
            pattern = callsign.upper() + "-"
            for row in sid_rows:
                for part in row["src"].split(","):
                    norm = part.strip().upper()
                    if norm.startswith(pattern) and "-" in norm:
                        sid = norm.split("-")[1]
                        if sid not in sids or row["last_ts"] > sids[sid]:
                            sids[sid] = row["last_ts"]
            result["sids"] = sids

        return result

    async def get_positions(self, callsign: str, days: int) -> list[dict[str, Any]]:
        """Get position data for a callsign."""
        cutoff_ms = int((time.time() - days * SECONDS_PER_DAY) * 1000)
        escaped_callsign = escape_like(callsign.upper())

        rows = await self._query(
            "SELECT raw_json FROM messages"
            " WHERE type = 'pos' AND timestamp >= ?"
            " AND UPPER(src) LIKE ? ESCAPE '\\'"
            " ORDER BY timestamp DESC",
            (cutoff_ms, f"%{escaped_callsign}%"),
        )

        positions: list[dict[str, Any]] = []
        for row in rows:
            try:
                raw_data = json.loads(row["raw_json"])
                lat = raw_data.get("lat")
                lon = raw_data.get("lon") or raw_data.get("long")
                timestamp = raw_data.get("timestamp", 0)
                if lat and lon:
                    time_str = time.strftime("%H:%M", time.localtime(timestamp / 1000))
                    positions.append(
                        {"lat": lat, "lon": lon, "time": time_str, "timestamp": timestamp}
                    )
            except (json.JSONDecodeError, TypeError):
                continue

        return positions

    async def load_dump(self, filename: str) -> int:
        """Load messages from JSON dump file."""
        path = Path(filename)
        if not path.exists():  # noqa: ASYNC240 - rare admin op, cheap stat call
            logger.info("Dump file not found: %s", filename)
            return 0

        def _load() -> list[dict[str, Any]]:
            with path.open(encoding="utf-8") as f:
                return cast(list[dict[str, Any]], json.load(f))

        data = await asyncio.to_thread(_load)

        # Bulk insert
        insert_query = """
            INSERT INTO messages
            (msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params_list = []
        for item in data:
            raw = item.get("raw", "")
            timestamp_str = item.get("timestamp", "")

            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            # Skip filtered messages (matching store_message logic)
            if self._should_filter_message(parsed):
                continue

            params_list.append(
                (
                    parsed.get("msg_id"),
                    parsed.get("src", ""),
                    parsed.get("dst", ""),
                    parsed.get("msg", ""),
                    parsed.get("type", "msg"),
                    parsed.get("timestamp", 0),
                    parsed.get("rssi"),
                    parsed.get("snr"),
                    parsed.get("src_type", ""),
                    raw,
                    timestamp_str,
                )
            )

        if params_list:
            await self._execute_many(insert_query, params_list)

        count = await self.get_message_count()
        logger.info("Loaded %d messages from %s (total: %d)", len(params_list), filename, count)
        return len(params_list)

    async def save_dump(self, filename: str) -> int:
        """Save messages to JSON dump file (for compatibility)."""
        query = "SELECT raw_json, created_at FROM messages ORDER BY timestamp"
        rows = await self._query(query)

        data = [{"raw": row["raw_json"], "timestamp": row["created_at"]} for row in rows]

        def _save() -> None:
            with Path(filename).open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        await asyncio.to_thread(_save)
        logger.info("Saved %d messages to %s", len(data), filename)
        return len(data)

    async def get_telemetry_chart_data(self, hours: int = 48) -> list[dict[str, Any]]:
        """Return telemetry data for chart display, limited to recent data."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        return await self._query(
            "SELECT callsign, timestamp, temp1, temp2, hum, hum2,"
            " qfe, qnh, gas, co2, alt, batt, extras"
            " FROM telemetry WHERE timestamp > ? ORDER BY callsign, timestamp",
            (cutoff,),
        )

    async def get_telemetry_chart_data_bucketed(
        self, hours: int = HOURS_PER_YEAR
    ) -> list[dict[str, Any]]:
        """Return telemetry aggregated into 4-hour buckets with min/max."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        bucket_ms = TELEMETRY_BUCKET_MS
        return await self._query(
            f"""
            SELECT
                callsign,
                (timestamp / {bucket_ms}) * {bucket_ms} AS bucket_ts,
                MIN(temp1) AS temp1_min, MAX(temp1) AS temp1_max,
                MIN(temp2) AS temp2_min, MAX(temp2) AS temp2_max,
                MIN(hum) AS hum_min, MAX(hum) AS hum_max,
                MIN(hum2) AS hum2_min, MAX(hum2) AS hum2_max,
                MIN(qfe) AS qfe_min, MAX(qfe) AS qfe_max,
                MIN(gas) AS gas_min, MAX(gas) AS gas_max,
                MIN(alt) AS alt_min, MAX(alt) AS alt_max,
                COUNT(*) AS count
            FROM telemetry
            WHERE timestamp > ?
              AND (temp1 IS NOT NULL OR hum IS NOT NULL OR qfe IS NOT NULL
                   OR gas IS NOT NULL)
            GROUP BY callsign, bucket_ts
            ORDER BY callsign, bucket_ts
            """,  # noqa: S608 - identifiers from fixed set; values parameterized
            (cutoff,),
        )

    async def get_message_acks(self, msg_id: str) -> list[dict[str, Any]]:
        """Every acknowledgement recorded for one outbound message, oldest first.

        Rows come from `message_acks` (schema v29). `from_call` is exposed as
        `from` and mapped to `None` when the ledger holds the '' placeholder —
        the storage-side sentinel is an implementation detail of the UNIQUE
        constraint and must not leak to the API.

        The result is clamped to `ACK_MSG_ID_WINDOW_MS` before the NEWEST ack
        recorded for this msg_id, because the ledger key carries no message
        identity beyond the msg_id and a firmware msg_id is reused every ~1000
        frames the sending node originates (see the constant). Without the
        clamp a bubble showed the acks of whichever earlier message last held
        the same counter value. `_record_message_ack` already prunes those rows
        as it writes, so this is what makes ledger rows written BEFORE that
        prune existed read correctly too — no backfill, no migration. A msg_id
        with a `held` row gets `HELD_ACK_WINDOW_MS` instead, for the same reason
        the binding does: a store-and-forward ack arrives days after the node
        and gateway acks, and all of them belong to the one message.
        """
        rows = await self._query(
            "SELECT kind, from_call, via, timestamp FROM message_acks"
            " WHERE msg_id = ?"
            "   AND timestamp >= ("
            "        SELECT MAX(timestamp) FROM message_acks WHERE msg_id = ?"
            "   ) - CASE WHEN EXISTS ("
            "        SELECT 1 FROM message_acks WHERE msg_id = ? AND kind = 'held'"
            "   ) THEN ? ELSE ? END"
            " ORDER BY timestamp ASC",
            (msg_id, msg_id, msg_id, HELD_ACK_WINDOW_MS, ACK_MSG_ID_WINDOW_MS),
        )
        return [
            {
                "kind": row["kind"],
                "from": row["from_call"] or None,
                "via": row["via"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]
