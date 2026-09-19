"""Regression suite for the nightly prune/rollup logic in `query.py`.

This is the exact area that bit once in production (the mHeard-gap bug:
`_nightly_prune` job ordering + naive-`utcnow` TZ handling — see
`doc/charts-wrong.md` §13 and the NOTE block in `QueryMixin.prune_messages`).
It earns real coverage.

Mirrors the ephemeral-tempfile pattern of `sqlite_storage.run_startup_tests`
and the classifier suite: a throwaway SQLite DB is created per run so the live
DB is never touched. Exposes `run_query_tests() -> bool` (async — the startup
orchestrator awaits it).

Coverage:
  (a) `aggregate_hourly_buckets` count-weighted averaging is EXACT — seed 5-min
      buckets with known counts + rssi/snr, assert the by-hand count-weighted
      hourly average and min/max/count.
  (b) Ordering invariant — aggregate-THEN-prune preserves history that
      prune-THEN-aggregate would lose (the production bug class). Both orders are
      run on identical seed data and the surviving hourly history is asserted.
  (c) Prune cutoffs are UTC-correct — `prune_messages` uses TZ-aware UTC
      (`datetime.now(UTC)`), not naive local wall-clock. Rows straddling the
      8-day pos cutoff by ±30 min are seeded; the correct ones must survive. A
      naive-local cutoff (non-zero UTC offset ≥ 1 h) would shift the boundary
      past both rows and fail this test.
  (d) The ':ack<N>' predicate seam (ack_predicate_vectors.json v2, single
      strict tier) — replays the vendored fixture's `is_ack` column against
      BOTH directions of the REAL SQL clause pair query.py's read paths use
      (exclusion "... AND msg NOT GLOB '*:ack[0-9]*'", acks query
      "msg GLOB '*:ack[0-9]*'" — asserting they are exact complements per
      vector), and its `is_ack`/`ack_number` columns against a documented
      literal mirror of ingest.py's regex (see the comment above
      _INGEST_ACK_REGEX_MIRROR for why it's a mirror, not a call into
      ingest.py — that call site has no clean seam to unit test in isolation
      without a much larger harness).
  (e) Case-sensitivity drift, behaviorally — a human message containing
      ':ACK99' and a real firmware ack ':ack931' are inserted, then the REAL
      client-facing query methods (get_messages_page,
      get_smart_initial_with_summary) are asserted on: the ':ACK99' row must
      come back as a normal message and NOT as an ack. Under the pre-v2
      case-insensitive `LIKE '%:ack%'` SQL it silently vanished from history
      sync (SQLite LIKE is case-insensitive for ASCII) while the webapp
      showed it live.
  (f) Hashtag destinations (hashtag_dst_vectors.json, conversation_key_vectors.json
      v4) — `get_messages_page`'s dispatch used to evaluate `is_dm` before
      `is_group_dst`, and `is_dm` was true for a hashtag dst whenever `src`
      was supplied, so a hashtag conversation's page always ran the DM
      branch (computing a corrupted conversation_key) instead of matching the
      real one. Covers a plain hashtag dst, a via-routed hashtag dst param,
      and `get_search_summary`'s destinations list (which used to GLOB-filter
      on digits only, making every hashtag conversation invisible to search).
  (g) `store_telemetry()` qnh storage (2026-09-11 decision: firmware item 174
      gave the barometric QNH reference a plausibility gate and re-latch, so
      MCProxy stores `qnh` again instead of dropping it) — a plausible BLE
      qnh persists to both `telemetry.qnh` and `station_positions.qnh`; a
      wrong-unit UDP `tele` qnh (mmHg junk, e.g. 760) is rejected by
      `_QNH_PLAUSIBLE_HPA_RANGE` and stores NULL without deriving a qfe from
      it; and a later frame with no qnh at all leaves NULL in its OWN
      `telemetry` row (an honest INSERT once outside the dedup window) while
      `station_positions.qnh` keeps the last real value via its
      `COALESCE(excluded.qnh, station_positions.qnh)` upsert clause.

All timestamps are MILLISECONDS (project-wide DB convention).
"""

import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import (
    BUCKET_SECONDS,
    DEFAULT_POS_RETENTION_HOURS,
    HOURLY_BUCKET_MS,
    MIN_DATAPOINTS_FOR_STATS,
    SPARSE_MIN_DATAPOINTS,
    TELEMETRY_DEDUP_WINDOW_MS,
    compute_conversation_key,
)
from .query import MHEARD_PROGRESS_CHUNK

logger = get_logger(__name__)

_FIVE_MIN_MS = BUCKET_SECONDS * 1000
_MS_PER_HOUR = 3600 * 1000
_MS_PER_DAY = 24 * _MS_PER_HOUR
_POS_RETENTION_MS = DEFAULT_POS_RETENTION_HOURS * _MS_PER_HOUR  # 8 days

# Vendored copy of the cross-repo ':ack<N>' predicate truth table (v2, single
# strict tier: case-sensitive ':ack' + ASCII digit [0-9]). mc-chat is
# upstream/canonical (tests/fixtures/ack_predicate_vectors.json); this copy
# must stay byte-identical (webapp's predicates.spec.ts drift-checks its own
# copy, parsed, against both this file and mc-chat's).
_ACK_VECTORS_PATH = Path(__file__).parent / "ack_predicate_vectors.json"

# Mirrors ingest.py's inline ack-number regex BYTE FOR BYTE. That call
# site (`if msg and ":ack" in msg: ack_match = re.search(r":ack([0-9]+)", msg)`)
# sits inside store_message(), a large function that also does echo_id
# lookup, time-windowed dedup, and a DB mutation — there is no clean seam to
# import or call it in isolation without building a much larger integration
# harness around store_message's full side effects. This constant is an
# explicitly-flagged, honest substitute: it pins the PATTERN so a future
# unreviewed edit to ingest.py's regex is at least visible as a diff here,
# but it is NOT proof the production code path still behaves identically.
# mc-chat's decoder.py uses the textually identical pattern and IS exercised
# through real production code in mc-chat/tests/test_decoder.py — that is
# the actual regex-engine coverage for this exact pattern. [0-9], not \d:
# Python's \d matches any Unicode Nd digit, which the firmware
# ('%-9.9s:ack%03i') never emits — see the fixture's unicode_digit_rejected.
_INGEST_ACK_REGEX_MIRROR = re.compile(r":ack([0-9]+)")


def _load_ack_vectors() -> list[dict[str, Any]]:
    with _ACK_VECTORS_PATH.open(encoding="utf-8") as f:
        contract = json.load(f)
    return list(contract["vectors"])


# --- fixtures/reference for _build_chart_series chunking regression (below) ---
# _build_chart_series was restructured (2026-09-19) to run its CPU-bound work
# via asyncio.to_thread and throttle progress events to one per
# MHEARD_PROGRESS_CHUNK stations instead of one per station (measured on
# mcapp.local: a yearly mheard dump blocked the event loop for ~1.4 s with the
# thread pool otherwise idle). These helpers build a bucket_rows fixture and an
# independently-written expected series (NOT calling _build_chart_series,
# _group_and_qualify or _build_series_chunk) to pin exact output equivalence.
_CHART_GAP_THRESHOLD_S = 1800  # 30 min
_CHART_GAP_OFFSET_S = BUCKET_SECONDS  # 300s, matches the production 5-min-bucket call


def _chart_bucket_row(callsign: str, ts_s: int, idx: int) -> dict[str, Any]:
    """One synthetic signal_buckets row. Values vary deterministically by
    `idx` only so the independent reference below can recompute them without
    depending on insertion order.
    """
    return {
        "callsign": callsign,
        "bucket_ts": ts_s * 1000,
        "rssi_avg": -100.0 + idx,
        "rssi_min": -105 - idx,
        "rssi_max": -95 + idx,
        "snr_avg": 5.0 + idx,
        "snr_min": 3.0 + idx,
        "snr_max": 7.0 + idx,
        "count": idx + 1,
    }


def _reference_chart_rows_for_station(
    callsign: str,
    offsets_s: list[int],
    base_ts_s: int,
    *,
    gap_threshold_s: int = _CHART_GAP_THRESHOLD_S,
    gap_offset_s: int = _CHART_GAP_OFFSET_S,
) -> list[dict[str, Any]]:
    """Independent (not production-code-calling) expected-rows builder for one
    station's offsets, mirroring the documented gap-marker/segment-id contract
    of _build_chart_series/_build_series_chunk.
    """
    rows: list[dict[str, Any]] = []
    segment_id = 0
    prev_time: int | None = None
    for idx, off_s in enumerate(offsets_s):
        bucket_time = base_ts_s + off_s
        if prev_time and (bucket_time - prev_time) > gap_threshold_s:
            rows.append(
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
        rows.append(
            {
                "src_type": "STATS",
                "timestamp": bucket_time,
                "callsign": callsign,
                "rssi": -100.0 + idx,
                "snr": 5.0 + idx,
                "rssi_min": -105 - idx,
                "rssi_max": -95 + idx,
                "snr_min": 3.0 + idx,
                "snr_max": 7.0 + idx,
                "count": idx + 1,
                "segment_id": f"{callsign}_seg_{segment_id}",
                "segment_size": 1,
            }
        )
        prev_time = bucket_time
    return rows


def _build_dense_gap_sparse_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Builds (bucket_rows, expected_result) for the output-equivalence test.

    - 11 "dense" stations (STA00..STA10), each with exactly
      MIN_DATAPOINTS_FOR_STATS (10) consecutive 5-min buckets, no gap.
    - 1 station (STAGAP) with 10 buckets and one gap wider than
      _CHART_GAP_THRESHOLD_S (a gap marker must appear).
    - 1 station (STASPARSE) with only 5 buckets — below
      MIN_DATAPOINTS_FOR_STATS, and since the other 12 stations qualify
      strictly, the sparse floor must NOT engage: STASPARSE is dropped
      entirely.
    - Qualified station count is 12 (STA00..STA10 + STAGAP) — NOT a multiple
      of MHEARD_PROGRESS_CHUNK (10), so chunking splits it 10 + 2 (partial
      last chunk), crossing one chunk boundary.
    """
    base_ts_s = 2_000_000_000
    bucket_rows: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []

    dense_offsets = [i * BUCKET_SECONDS for i in range(MIN_DATAPOINTS_FOR_STATS)]
    for i in range(11):
        callsign = f"STA{i:02d}"
        for idx, off_s in enumerate(dense_offsets):
            bucket_rows.append(_chart_bucket_row(callsign, base_ts_s + off_s, idx))
        expected.extend(_reference_chart_rows_for_station(callsign, dense_offsets, base_ts_s))

    # Gap station: 5 consecutive buckets, then a 1-hour jump (> 1800s
    # threshold), then 5 more consecutive buckets. 10 entries total, so it
    # qualifies strictly like the dense stations.
    gap_offsets = [
        0,
        300,
        600,
        900,
        1200,
        1200 + 3600,
        1200 + 3900,
        1200 + 4200,
        1200 + 4500,
        1200 + 4800,
    ]
    bucket_rows.extend(
        _chart_bucket_row("STAGAP", base_ts_s + off_s, idx) for idx, off_s in enumerate(gap_offsets)
    )
    expected.extend(_reference_chart_rows_for_station("STAGAP", gap_offsets, base_ts_s))

    # Below-threshold station: must be dropped (other stations qualify strictly).
    sparse_offsets = [i * BUCKET_SECONDS for i in range(SPARSE_MIN_DATAPOINTS + 4)]
    bucket_rows.extend(
        _chart_bucket_row("STASPARSE", base_ts_s + off_s, idx)
        for idx, off_s in enumerate(sparse_offsets)
    )

    expected.sort(key=lambda x: (x["callsign"], x["timestamp"]))
    return bucket_rows, expected


async def run_query_tests() -> bool:  # noqa: PLR0915 - test suite lists one case per assertion
    """Prune/rollup regression suite. Returns True iff every case passes."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "query_prune_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _seed_5min(
                callsign: str,
                bucket_ts: int,
                stats: tuple[float, int, int, float, float, float],
                count: int,
            ) -> None:
                # stats packs the six signal columns in this order: rssi avg, rssi
                # min, rssi max, snr avg, snr min, snr max.
                rssi_avg, rssi_min, rssi_max, snr_avg, snr_min, snr_max = stats
                await storage._mutate(
                    "INSERT OR REPLACE INTO signal_buckets"
                    " (callsign, bucket_ts, bucket_size, rssi_avg, rssi_min, rssi_max,"
                    "  snr_avg, snr_min, snr_max, count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        callsign,
                        bucket_ts,
                        _FIVE_MIN_MS,
                        rssi_avg,
                        rssi_min,
                        rssi_max,
                        snr_avg,
                        snr_min,
                        snr_max,
                        count,
                    ),
                )

            async def _hourly_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM signal_buckets"
                    " WHERE callsign = ? AND bucket_size = ?"
                    " ORDER BY bucket_ts",
                    (callsign, HOURLY_BUCKET_MS),
                )
                return rows[0] if rows else None

            async def _count_5min(callsign: str) -> int:
                rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM signal_buckets"
                    " WHERE callsign = ? AND bucket_size = ?",
                    (callsign, _FIVE_MIN_MS),
                )
                return int(rows[0]["c"])

            async def _wipe_buckets() -> None:
                await storage._mutate("DELETE FROM signal_buckets")

            # --- (a) count-weighted averaging is EXACT -------------------------------
            # Two 5-min buckets in ONE hour, both older than the 8-day rollup cutoff.
            # Hand-computed count-weighted hourly averages:
            #   rssi_avg = (-100*1 + -90*3) / (1+3) = -370/4 = -92.5
            #   snr_avg  = (   4*1 +   8*3) / (1+3) =   28/4 =   7.0
            #   count    = 1 + 3 = 4
            #   rssi_min = min(-105, -92) = -105 ; rssi_max = max(-95, -88) = -88
            #   snr_min  = min(3, 7) = 3         ; snr_max  = max(5, 9) = 9
            nine_days_ago = now_ms() - 9 * _MS_PER_DAY
            hour_start = (nine_days_ago // HOURLY_BUCKET_MS) * HOURLY_BUCKET_MS
            await _seed_5min("AVGCS", hour_start, (-100.0, -105, -95, 4.0, 3.0, 5.0), 1)
            await _seed_5min(
                "AVGCS", hour_start + _FIVE_MIN_MS, (-90.0, -92, -88, 8.0, 7.0, 9.0), 3
            )
            await storage.aggregate_hourly_buckets()
            agg = await _hourly_row("AVGCS")

            expected_rssi_avg = -92.5
            expected_snr_avg = 7.0
            expected_count = 4
            expected_rssi_min = -105
            expected_rssi_max = -88
            expected_snr_min = 3.0
            expected_snr_max = 9.0
            results.append(
                (
                    "aggregate: hourly bucket created at floored hour_ts",
                    agg is not None and agg["bucket_ts"] == hour_start,
                )
            )
            results.append(
                (
                    "aggregate: count-weighted rssi_avg exact (-92.5)",
                    agg is not None and agg["rssi_avg"] == expected_rssi_avg,
                )
            )
            results.append(
                (
                    "aggregate: count-weighted snr_avg exact (7.0)",
                    agg is not None and agg["snr_avg"] == expected_snr_avg,
                )
            )
            results.append(
                (
                    "aggregate: summed count exact (4)",
                    agg is not None and agg["count"] == expected_count,
                )
            )
            results.append(
                (
                    "aggregate: rssi_min/max spans both buckets (-105/-88)",
                    agg is not None
                    and agg["rssi_min"] == expected_rssi_min
                    and agg["rssi_max"] == expected_rssi_max,
                )
            )
            results.append(
                (
                    "aggregate: snr_min/max spans both buckets (3/9)",
                    agg is not None
                    and agg["snr_min"] == expected_snr_min
                    and agg["snr_max"] == expected_snr_max,
                )
            )
            results.append(
                (
                    "aggregate: source 5-min buckets consumed (deleted)",
                    await _count_5min("AVGCS") == 0,
                )
            )

            # --- (b) ordering invariant: aggregate-THEN-prune vs prune-THEN-aggregate --
            # Identical seed of three 5-min buckets in one hour, 9 days old (older than
            # both the 8-day rollup cutoff and the 8-day pos prune cutoff), counts 2+3+5.
            # Correct order (aggregate first) rolls them into a 1-hour bucket that then
            # survives prune (hourly retention = 365 d). Wrong order (prune first)
            # deletes the 5-min buckets before the rollup can see them → history lost.
            # This is the exact production bug class.
            seed_counts = (2, 3, 5)
            expected_rollup_count = sum(seed_counts)  # 10
            filler_stats = (-90.0, -95, -85, 6.0, 4.0, 8.0)

            async def _seed_ordering(callsign: str) -> None:
                for i, cnt in enumerate(seed_counts):
                    await _seed_5min(callsign, hour_start + i * _FIVE_MIN_MS, filler_stats, cnt)

            # Correct order.
            await _wipe_buckets()
            await _seed_ordering("ORDERC")
            await storage.aggregate_hourly_buckets()
            await storage.prune_messages(prune_hours=720, block_list=[])
            correct = await _hourly_row("ORDERC")
            results.append(
                (
                    "ordering: aggregate-THEN-prune preserves rolled-up hourly history",
                    correct is not None and correct["count"] == expected_rollup_count,
                )
            )

            # Wrong order (demonstrate the loss the correct order avoids).
            await _wipe_buckets()
            await _seed_ordering("ORDERW")
            await storage.prune_messages(prune_hours=720, block_list=[])
            await storage.aggregate_hourly_buckets()
            wrong = await _hourly_row("ORDERW")
            results.append(
                (
                    "ordering: prune-THEN-aggregate loses history (no hourly bucket)",
                    wrong is None,
                )
            )

            # --- (c) prune cutoff is UTC-correct (TZ-aware, not naive local) ----------
            # prune_messages deletes 5-min signal_buckets older than now_utc - 8 days.
            # Seed two rows straddling that boundary by ±30 min. With the correct
            # datetime.now(UTC) cutoff the newer survives and the older is deleted.
            # A naive utcnow().timestamp() cutoff (interpreted as local time) would
            # shift the boundary by the machine's UTC offset — for any offset ≥ 1 h
            # both ±30 min rows land on the same side and one assertion below fails,
            # flagging the regression loudly.
            await _wipe_buckets()
            half_hour_ms = 30 * 60 * 1000
            cutoff_ref_ms = now_ms() - _POS_RETENTION_MS
            # UTCNEW: 30 min NEWER than the cutoff → must survive.
            await _seed_5min("UTCNEW", cutoff_ref_ms + half_hour_ms, filler_stats, 1)
            # UTCOLD: 30 min OLDER than the cutoff → must be deleted.
            await _seed_5min("UTCOLD", cutoff_ref_ms - half_hour_ms, filler_stats, 1)
            # prune_hours (msg retention) kept large so only the pos/bucket cutoff bites.
            await storage.prune_messages(prune_hours=720, block_list=[])
            results.append(
                (
                    "utc-cutoff: bucket 30 min newer than UTC cutoff survives prune",
                    await _count_5min("UTCNEW") == 1,
                )
            )
            results.append(
                (
                    "utc-cutoff: bucket 30 min older than UTC cutoff deleted by prune",
                    await _count_5min("UTCOLD") == 0,
                )
            )

            # --- (d) ':ack<N>' predicate seam (ack_predicate_vectors.json v2) -----
            ack_vectors = _load_ack_vectors()
            ack_vectors_present_label = (
                "ack predicate: vendored fixture carries vectors to replay"
                " (guards against a silently empty loop)"
            )
            results.append((ack_vectors_present_label, len(ack_vectors) > 0))

            for i, vector in enumerate(ack_vectors):
                # (d1) is_ack, replayed against BOTH directions of the REAL SQL
                # clause pair query.py's read paths use
                # (get_smart_initial_with_summary, get_messages_page, etc.):
                # exclusion "... AND msg NOT GLOB '*:ack[0-9]*'" on every
                # message/history query, positive "msg GLOB '*:ack[0-9]*'" on
                # the acks query. Asserting both per vector pins that the pair
                # stays an exact complement — every non-NULL msg lands on
                # exactly one side of the messages/acks partition.
                msg_id = f"ACKVEC-{i}"
                await storage._mutate(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, src_type)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (msg_id, "TESTCALL-1", "232", vector["text"], "msg", now_ms() + i, "lora"),
                )
                not_glob_rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM messages"
                    " WHERE msg_id = ? AND msg NOT GLOB '*:ack[0-9]*'",
                    (msg_id,),
                )
                glob_rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM messages"
                    " WHERE msg_id = ? AND msg GLOB '*:ack[0-9]*'",
                    (msg_id,),
                )
                survives_exclusion = not_glob_rows[0]["c"] == 1
                matches_acks_query = glob_rows[0]["c"] == 1
                is_ack = bool(vector["is_ack"])
                side_word = "ack side" if is_ack else "message side"
                glob_pair_label = (
                    f"ack predicate SQL GLOB pair: {vector['name']} (partitions to the {side_word})"
                )
                results.append(
                    (
                        glob_pair_label,
                        survives_exclusion == (not is_ack) and matches_acks_query == is_ack,
                    )
                )

                # (d2) is_ack / ack_number, replayed against the literal mirror
                # of ingest.py's regex (see _INGEST_ACK_REGEX_MIRROR's
                # docstring-comment for why this is a mirror, not a direct
                # call). v2 has no runtime_overrides — the strict semantics is
                # engine-independent, so the vector's fields are the expectation
                # for every runtime.
                match = (
                    _INGEST_ACK_REGEX_MIRROR.search(vector["text"])
                    if vector["text"] and ":ack" in vector["text"]
                    else None
                )  # mirrors ingest.py's short-circuit guard exactly
                actual_strict = match is not None
                actual_number = match.group(1) if match else None
                results.append(
                    (
                        f"ack predicate ingest.py-mirror regex: {vector['name']} is_ack",
                        actual_strict == vector["is_ack"],
                    )
                )
                results.append(
                    (
                        f"ack predicate ingest.py-mirror regex: {vector['name']} ack_number",
                        actual_number == vector["ack_number"],
                    )
                )

            # --- (e) case-sensitivity drift, behaviorally (what reaches the client) --
            # A human GROUP message containing ':ACK99' (uppercase — NOT a
            # firmware ack; the firmware only emits lowercase ':ack' + 3 ASCII
            # digits) and a real ack ':ack931' go through the REAL client-facing
            # query methods. Pre-v2, the case-insensitive `LIKE '%:ack%'` SQL
            # silently dropped the ':ACK99' row from history sync (messages page
            # AND smart-initial messages) and mis-filed it in the acks payload,
            # while the webapp's case-sensitive live path showed it — the exact
            # cross-repo drift ack_predicate_vectors.json v2 unifies away.
            drift_ts = now_ms()
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-HUMAN",
                    "OE1ABC-1",
                    "232",
                    "Sag mal, kam der Test :ACK99 bei dir durch?",
                    "msg",
                    drift_ts,
                    "lora",
                    "232",
                ),
            )
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-ACK",
                    "OE9XYZ-1",
                    "232",
                    "OE1ABC-1 :ack931",
                    "msg",
                    drift_ts + 1,
                    "lora",
                    "232",
                ),
            )
            # Explicit cursor: the default before_timestamp=now_ms() races the
            # rows inserted at drift_ts/drift_ts+1 when this block runs within
            # one millisecond (the page query is strictly `timestamp < ?`),
            # making the assertions below flaky.
            page = await storage.get_messages_page("232", before_timestamp=drift_ts + 10)
            page_msgs = page["messages"]
            results.append(
                (
                    "drift: human ':ACK99' message IS returned by the messages page",
                    any(":ACK99" in m for m in page_msgs),
                )
            )
            results.append(
                (
                    "drift: real ':ack931' ack is NOT returned by the messages page",
                    not any(":ack931" in m for m in page_msgs),
                )
            )
            initial, _ = await storage.get_smart_initial_with_summary()
            results.append(
                (
                    "drift: human ':ACK99' message IS in smart-initial messages",
                    any(":ACK99" in m for m in initial["messages"]),
                )
            )
            results.append(
                (
                    "drift: human ':ACK99' message is NOT in the smart-initial acks payload",
                    not any(":ACK99" in m for m in initial["acks"]),
                )
            )
            results.append(
                (
                    "drift: real ':ack931' ack IS in the smart-initial acks payload",
                    any(":ack931" in m for m in initial["acks"]),
                )
            )

            # --- unified group predicate in get_messages_page (group_dst_vectors.json) --
            # Post-v2 ingest gives a message to out-of-range dst '0' a NULL
            # conversation_key. The old page branch classified '0' as a group
            # (bare isdigit) and queried conversation_key = '0' — missing the
            # row entirely. Unified, '0' is no group: the request falls to the
            # exact-dst arm and the row stays reachable.
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-ZERO",
                    "OE1ABC-1",
                    "0",
                    "message to out-of-range dst zero",
                    "msg",
                    drift_ts + 2,
                    "lora",
                    None,
                ),
            )
            # Explicit cursor: the default before_timestamp=now_ms() can equal
            # the row's timestamp when this block runs within one millisecond,
            # and the page query is strictly `timestamp < ?`.
            zero_page = await storage.get_messages_page("0", before_timestamp=drift_ts + 10)
            results.append(
                (
                    (
                        "unified group predicate: page for out-of-range dst '0' serves its"
                        " NULL-key row via the exact-dst arm (old group-shape missed it)"
                    ),
                    any("out-of-range dst zero" in m for m in zero_page["messages"]),
                )
            )

            # --- hashtag destinations (hashtag_dst_vectors.json, conversation_key --
            # --- _vectors.json v4) ---------------------------------------------------
            # A plain hashtag dst message, stored with the SAME conversation_key
            # store_message would compute (compute_conversation_key already covers
            # this — the point here is the QUERY dispatch, not key derivation).
            htag_ts = drift_ts + 3
            htag_key = compute_conversation_key("OE3ABC-1", "#OE-SOTA")
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-HASHTAG",
                    "OE3ABC-1",
                    "#OE-SOTA",
                    "SOTA activation on 145.500",
                    "msg",
                    htag_ts,
                    "lora",
                    htag_key,
                ),
            )
            # Before the fix, is_dm was true here (src supplied, dst not digit-only,
            # dst != '*') so the DM branch ran and computed a corrupted key instead
            # of matching htag_key — this page would come back empty.
            htag_page = await storage.get_messages_page(
                "#OE-SOTA", before_timestamp=htag_ts + 10, src="OE3ABC-1"
            )
            results.append(
                (
                    (
                        "hashtag: get_messages_page for a plain hashtag dst takes the"
                        " group-style conversation_key path (not the DM branch)"
                    ),
                    any("SOTA activation" in m for m in htag_page["messages"]),
                )
            )

            # A via-routed hashtag dst PARAM ('RELAY-1,#OE-SOTA') must resolve to the
            # same conversation_key match — the common case on a mesh.
            htag_via_page = await storage.get_messages_page(
                "RELAY-1,#OE-SOTA", before_timestamp=htag_ts + 10, src="OE3ABC-1"
            )
            results.append(
                (
                    (
                        "hashtag: get_messages_page reaches the same conversation via a"
                        " via-routed dst param ('RELAY-1,#OE-SOTA')"
                    ),
                    any("SOTA activation" in m for m in htag_via_page["messages"]),
                )
            )

            # get_search_summary must surface the hashtag destination alongside
            # numeric groups, without breaking the existing numeric int-sort.
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DRIFT-HASHTAG-NUM",
                    "OE3ABC-1",
                    "42",
                    "also active in group 42",
                    "msg",
                    htag_ts + 1,
                    "lora",
                    "42",
                ),
            )
            search_summary = await storage.get_search_summary("OE3ABC", 30, "all")
            results.append(
                (
                    "hashtag: get_search_summary includes a hashtag destination",
                    "#OE-SOTA" in search_summary["destinations"],
                )
            )
            results.append(
                (
                    "hashtag: get_search_summary keeps numeric destinations, sorted by int",
                    "42" in search_summary["destinations"],
                )
            )

            # --- signal_via key presence (station_positions_rssi_via.md fix) ---------
            # `_build_position_dict` must emit the "signal_via" KEY on every row
            # carrying an rssi, even when the stored value is '' (unknown / legacy,
            # pre-v22 row) — presence, not truthiness, is how the client tells a live
            # single-frame observation (safe to derive attribution from) apart from
            # an aggregated/legacy snapshot row (must NOT derive: its via_shortest is
            # a historical shortest path unrelated to who actually delivered THIS
            # reading). A row with no rssi at all must omit the key entirely.
            _base_row: dict[str, Any] = {
                "callsign": "SIGVIA-1",
                "source": "local",
                "via_shortest": "",
                "via_paths": "[]",
                "last_seen": now_ms(),
                "lat": None,
                "lon": None,
                "alt": None,
                "lat_dir": "",
                "lon_dir": "",
                "hw_id": None,
                "firmware": None,
                "fw_sub": None,
                "aprs_symbol": None,
                "aprs_symbol_group": None,
                "batt": None,
                "gw": None,
                "lora_mod": None,
                "mesh": None,
                "snr": None,
                "signal_via": "",
            }
            with_rssi_row = dict(_base_row, rssi=-95, signal_via="")
            with_rssi_dict = storage._build_position_dict(with_rssi_row)
            results.append(
                (
                    (
                        "_build_position_dict: emits 'signal_via' key for an rssi row"
                        " even when the stored value is '' (unknown/legacy)"
                    ),
                    "signal_via" in with_rssi_dict and with_rssi_dict["signal_via"] == "",
                )
            )

            no_rssi_row = dict(_base_row, rssi=None, signal_via="")
            no_rssi_dict = storage._build_position_dict(no_rssi_row)
            results.append(
                (
                    "_build_position_dict: omits 'signal_via' key for a row with no rssi",
                    "signal_via" not in no_rssi_dict,
                )
            )

            # --- store-forward delivery_status/holder round-trip (schema v31) -------
            # `_MSG_SELECT` (constants.py) is the only place that decides which
            # columns the history queries fetch; `_build_message_dict` already
            # tolerates the columns being present or absent. This proves the
            # wiring end to end through the REAL client-facing read path
            # (get_messages_page) rather than calling _build_message_dict
            # directly, since a regression could just as easily be "the SELECT
            # forgot the columns" as "the dict-builder forgot the keys".
            sf_ts = now_ms()
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type,"
                " conversation_key, delivery_status, holder)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "SF-HELD",
                    "OE1ABC-1",
                    "OE9XYZ-1",
                    "message held at a store node",
                    "msg",
                    sf_ts,
                    "lora",
                    compute_conversation_key("OE1ABC-1", "OE9XYZ-1"),
                    "held",
                    "OE5REL-1",
                ),
            )
            await storage._mutate(
                "INSERT INTO messages"
                " (msg_id, src, dst, msg, type, timestamp, src_type, conversation_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "SF-PLAIN",
                    "OE1ABC-1",
                    "OE9XYZ-1",
                    "ordinary message, no store-forward state",
                    "msg",
                    sf_ts + 1,
                    "lora",
                    compute_conversation_key("OE1ABC-1", "OE9XYZ-1"),
                ),
            )
            sf_page = await storage.get_messages_page(
                "OE9XYZ-1", before_timestamp=sf_ts + 10, src="OE1ABC-1"
            )
            sf_emitted = [json.loads(m) for m in sf_page["messages"]]
            sf_held = next((m for m in sf_emitted if m.get("msg_id") == "SF-HELD"), None)
            sf_plain = next((m for m in sf_emitted if m.get("msg_id") == "SF-PLAIN"), None)
            results.append(
                (
                    (
                        "store-forward: a stored delivery_status/holder round-trips through"
                        " get_messages_page with the right values"
                    ),
                    sf_held is not None
                    and sf_held.get("delivery_status") == "held"
                    and sf_held.get("holder") == "OE5REL-1",
                )
            )
            results.append(
                (
                    (
                        "store-forward: a row with NULL delivery_status/holder emits NEITHER"
                        " key (absence, not null)"
                    ),
                    sf_plain is not None
                    and "delivery_status" not in sf_plain
                    and "holder" not in sf_plain,
                )
            )

            # --- mheard sparse-floor fallback (doc/plan-mheard-fresh-install-fix.md) --
            # A "datapoint" is a distinct 5-min signal_buckets row per callsign, not a
            # packet. `_build_chart_series` used to hard-drop any callsign below
            # MIN_DATAPOINTS_FOR_STATS (10) with no fallback — a fresh install with
            # sparse history rendered an empty mHeard chart for hours. The fix adds an
            # adaptive floor: fall back to SPARSE_MIN_DATAPOINTS (1) only when NO
            # callsign reaches the strict threshold.
            recent_bucket_start = (now_ms() // _FIVE_MIN_MS) * _FIVE_MIN_MS
            sparse_stats = (-95.0, -100, -90, 5.0, 3.0, 7.0)

            async def _seed_n_buckets(callsign: str, n: int) -> None:
                for i in range(n):
                    await _seed_5min(
                        callsign, recent_bucket_start - i * _FIVE_MIN_MS, sparse_stats, 1
                    )

            # (1) 9 buckets per callsign (below the strict floor of 10) for THREE
            # callsigns still returns every station via the sparse floor. This is the
            # fresh-install repro from the bug report and FAILS on pre-fix code (which
            # has no fallback and returns []).
            await _wipe_buckets()
            sparse_callsigns = ["SPARSE1", "SPARSE2", "SPARSE3"]
            for cs in sparse_callsigns:
                await _seed_n_buckets(cs, 9)
            sparse_series = await storage.process_mheard_store_parallel()
            sparse_series_callsigns = {e["callsign"] for e in sparse_series}
            results.append(
                (
                    ("mheard: 9 buckets per callsign still returns all stations via sparse floor"),
                    len(sparse_series) > 0 and sparse_series_callsigns == set(sparse_callsigns),
                )
            )

            # (2) When at least one callsign reaches the strict threshold, the sparse
            # fallback must NOT engage — the dense-box no-regression guard.
            await _wipe_buckets()
            await _seed_n_buckets("DENSE1", 12)
            await _seed_n_buckets("DENSE2", 2)
            dense_series = await storage.process_mheard_store_parallel()
            dense_series_callsigns = {e["callsign"] for e in dense_series}
            results.append(
                (
                    (
                        "mheard: 10+ buckets keeps the strict threshold"
                        " (sparse floor does not fire when someone qualifies)"
                    ),
                    dense_series_callsigns == {"DENSE1"},
                )
            )

            # (3) Empty DB (no signal_buckets, no signal_log rows in the window):
            # empty list out, no exception.
            await _wipe_buckets()
            await storage._mutate("DELETE FROM signal_log")
            empty_series = await storage.process_mheard_store_parallel()
            results.append(
                (
                    "mheard: empty DB returns empty series",
                    empty_series == [],
                )
            )

            # (4) Legacy fallback reads signal_log (not `messages`) when
            # signal_buckets is empty. Seed signal_log directly (never touching
            # signal_buckets) and confirm the station comes back through the sparse
            # floor — proving the fallback query actually reads signal_log.
            await _wipe_buckets()
            await storage._mutate("DELETE FROM signal_log")
            siglog_ts = now_ms()
            for _ in range(3):
                await storage._mutate(
                    "INSERT INTO signal_log (callsign, timestamp, rssi, snr, source)"
                    " VALUES (?, ?, ?, ?, ?)",
                    ("SIGLOGCS", siglog_ts, -95, 5.0, "mheard"),
                )
            siglog_series = await storage.process_mheard_store_parallel()
            siglog_series_callsigns = {e["callsign"] for e in siglog_series}
            results.append(
                (
                    "mheard: legacy fallback reads signal_log",
                    siglog_series_callsigns == {"SIGLOGCS"},
                )
            )
            await storage._mutate("DELETE FROM signal_log")

            # (5) Monthly/yearly variants (through _query_rolled_up_buckets) also
            # honour the sparse floor.
            await _wipe_buckets()
            for cs in sparse_callsigns:
                await _seed_n_buckets(cs, 9)
            monthly_series = await storage.process_mheard_monthly()
            monthly_series_callsigns = {e["callsign"] for e in monthly_series}
            results.append(
                (
                    "mheard: monthly/yearly honour the sparse floor",
                    len(monthly_series) > 0 and monthly_series_callsigns == set(sparse_callsigns),
                )
            )
            await _wipe_buckets()

            # --- (g) store_telemetry() qnh storage (2026-09-11 decision) ---
            # Firmware item 174 gave the barometric QNH reference a plausibility
            # gate + re-latch (shipped 4.35s/4.35t), so MCProxy stores `qnh`
            # again instead of dropping it (see ingest.py's qnh_reading note and
            # telemetry_reconcile.ALL_FIELDS).

            async def _telemetry_qnh_qfe(callsign: str) -> tuple[Any, Any]:
                rows = await storage._query(
                    "SELECT qnh, qfe FROM telemetry WHERE callsign = ? ORDER BY id DESC LIMIT 1",
                    (callsign,),
                )
                return (rows[0]["qnh"], rows[0]["qfe"]) if rows else (None, None)

            async def _station_qnh(callsign: str) -> Any:
                rows = await storage._query(
                    "SELECT qnh FROM station_positions WHERE callsign = ?", (callsign,)
                )
                return rows[0]["qnh"] if rows else None

            # (g1) A plausible BLE qnh persists to both telemetry.qnh and
            # station_positions.qnh.
            qnh_t0 = now_ms()
            await storage.store_telemetry(
                "QNHTEST-1", {"src_type": "ble", "timestamp": qnh_t0, "qnh": 1013.2}
            )
            tele_qnh, _ = await _telemetry_qnh_qfe("QNHTEST-1")
            results.append(
                (
                    "qnh: a plausible BLE qnh persists to telemetry.qnh",
                    tele_qnh == 1013.2,
                )
            )
            results.append(
                (
                    "qnh: a plausible BLE qnh persists to station_positions.qnh",
                    await _station_qnh("QNHTEST-1") == 1013.2,
                )
            )

            # (g2) A wrong-unit UDP tele qnh (mmHg junk, ~760) is outside
            # _QNH_PLAUSIBLE_HPA_RANGE (850-1100): stores NULL and derives no qfe.
            await storage.store_telemetry(
                "QNHTEST-2",
                {"src_type": "node", "timestamp": now_ms(), "temp1": 15.0, "qnh": 760},
            )
            junk_qnh, junk_qfe = await _telemetry_qnh_qfe("QNHTEST-2")
            results.append(
                (
                    "qnh: a wrong-unit (mmHg) UDP qnh stores NULL, not the junk value",
                    junk_qnh is None,
                )
            )
            results.append(("qnh: a wrong-unit UDP qnh still derives no qfe", junk_qfe is None))

            # (g3) A second frame with no qnh at all, far enough outside the dedup
            # window to be its own honest INSERT: NULL in its OWN telemetry row,
            # but station_positions.qnh keeps the prior value via COALESCE.
            qnh3_t0 = now_ms()
            await storage.store_telemetry(
                "QNHTEST-3", {"src_type": "ble", "timestamp": qnh3_t0, "qnh": 1009.5}
            )
            await storage.store_telemetry(
                "QNHTEST-3",
                {
                    "src_type": "ble",
                    "timestamp": qnh3_t0 + 3 * TELEMETRY_DEDUP_WINDOW_MS,
                    "temp1": 20.0,
                },
            )
            later_qnh, _ = await _telemetry_qnh_qfe("QNHTEST-3")
            results.append(
                (
                    "qnh: a later frame without qnh writes NULL into its OWN telemetry row",
                    later_qnh is None,
                )
            )
            results.append(
                (
                    "qnh: station_positions.qnh keeps the prior value via COALESCE",
                    await _station_qnh("QNHTEST-3") == 1009.5,
                )
            )

            # --- _build_chart_series chunking regression (2026-09-19) ---------------
            # Restructured into async _build_chart_series + sync _group_and_qualify /
            # _build_series_chunk run via asyncio.to_thread, with progress throttled
            # to one "gaps" event per MHEARD_PROGRESS_CHUNK stations instead of one
            # per station (fixes /api/send blocking 0.5-1.4s on the mheard dump).

            # (1) Output equivalence: the new chunked/threaded implementation must
            # return an IDENTICAL series to an independently-written reference for a
            # fixture covering several callsigns, a below-threshold station (dropped),
            # a gap wider than gap_threshold_s (gap marker), and a qualified station
            # count (12) that is NOT a multiple of MHEARD_PROGRESS_CHUNK (10) — so the
            # last progress chunk is partial and one chunk boundary is crossed.
            chart_bucket_rows, chart_expected = _build_dense_gap_sparse_fixture()
            chart_progress_calls: list[tuple[Any, ...]] = []

            async def _capture_chart_progress(*args: Any) -> None:
                chart_progress_calls.append(args)

            chart_actual = await storage._build_chart_series(
                chart_bucket_rows,
                gap_threshold_s=_CHART_GAP_THRESHOLD_S,
                gap_offset_s=_CHART_GAP_OFFSET_S,
                progress_callback=_capture_chart_progress,
            )
            results.append(
                (
                    (
                        "chart series: chunked/threaded output is byte-identical to the"
                        " independent reference (dense + gap + below-threshold stations)"
                    ),
                    chart_actual == chart_expected,
                )
            )
            results.append(
                (
                    (
                        "chart series: below-threshold STASPARSE is dropped"
                        " (other stations qualify strictly, sparse floor does not engage)"
                    ),
                    "STASPARSE" not in {e["callsign"] for e in chart_actual},
                )
            )
            results.append(
                (
                    "chart series: STAGAP's gap marker is present",
                    any(e["callsign"] == "STAGAP" and e.get("is_gap_marker") for e in chart_actual),
                )
            )

            # (1b) "done" event text, pinned exactly: this fixture has BOTH gap
            # markers (1, in STAGAP) and real data points (11 dense * 10 + STAGAP's
            # 10 = 120), so the is_gap_marker exclusion in _finalize_series is
            # actually exercised — a version that forgot the exclusion would report
            # 121 data points, not 120. 12 qualified stations (11 dense + STAGAP).
            chart_done_calls = [c for c in chart_progress_calls if c[0] == "done"]
            results.append(
                (
                    (
                        'chart series progress: "done" event reports 120 data points'
                        " for 12 stations (excludes the 1 gap marker)"
                    ),
                    chart_done_calls == [("done", "120 data points for 12 stations")],
                )
            )

            # (2) Progress throttling: N=23 qualified stations (not a multiple of
            # MHEARD_PROGRESS_CHUNK) must emit exactly ceil(N/CHUNK) "gaps" events,
            # each carrying a callsign, with the idx counter reaching N on the last
            # one. Before the fix this emitted N (23) events, one per station.
            progress_n_stations = 23
            progress_base_ts_s = 3_000_000_000
            progress_offsets = [i * BUCKET_SECONDS for i in range(MIN_DATAPOINTS_FOR_STATS)]
            progress_bucket_rows = [
                _chart_bucket_row(f"PROG{i:02d}", progress_base_ts_s + off_s, idx)
                for i in range(progress_n_stations)
                for idx, off_s in enumerate(progress_offsets)
            ]
            progress_calls: list[tuple[Any, ...]] = []

            async def _capture_progress(*args: Any) -> None:
                progress_calls.append(args)

            await storage._build_chart_series(
                progress_bucket_rows,
                gap_threshold_s=_CHART_GAP_THRESHOLD_S,
                gap_offset_s=_CHART_GAP_OFFSET_S,
                progress_callback=_capture_progress,
            )
            gaps_calls = [c for c in progress_calls if c[0] == "gaps"]
            expected_gap_events = math.ceil(progress_n_stations / MHEARD_PROGRESS_CHUNK)
            results.append(
                (
                    (
                        f"chart series progress: {progress_n_stations} stations emit"
                        f" exactly ceil(N/{MHEARD_PROGRESS_CHUNK}) = {expected_gap_events}"
                        ' "gaps" events (was one per station before the fix)'
                    ),
                    len(gaps_calls) == expected_gap_events,
                )
            )
            # Pin the exact callsign and counter of EVERY "gaps" event, not just
            # that one is present: with PROG00..PROG22 chunked 10/10/3, each event
            # must carry its chunk's LAST station and the running total. A weaker
            # "a callsign is present, and the last counter is N" pair still passes
            # if the chunk boundaries or the last-of-chunk rule are wrong.
            idx_pattern = re.compile(r"\((\d+)/(\d+)\)")
            gaps_callsigns = [c[2] if len(c) == 3 else None for c in gaps_calls]
            gaps_counters = [
                (int(m.group(1)), int(m.group(2)))
                for m in (idx_pattern.search(c[1]) for c in gaps_calls)
                if m is not None
            ]
            results.append(
                (
                    (
                        'chart series progress: each "gaps" event carries its chunk\'s'
                        " LAST callsign (PROG09/PROG19/PROG22)"
                    ),
                    gaps_callsigns == ["PROG09", "PROG19", "PROG22"],
                )
            )
            results.append(
                (
                    (
                        'chart series progress: the "gaps" counters advance by chunk'
                        f" (10, 20, {progress_n_stations}), each out of"
                        f" {progress_n_stations}"
                    ),
                    gaps_counters
                    == [
                        (MHEARD_PROGRESS_CHUNK, progress_n_stations),
                        (2 * MHEARD_PROGRESS_CHUNK, progress_n_stations),
                        (progress_n_stations, progress_n_stations),
                    ],
                )
            )

            # (3) Sparse fallback still works through the restructured pipeline: a
            # fixture where NOBODY reaches MIN_DATAPOINTS_FOR_STATS still returns the
            # SPARSE_MIN_DATAPOINTS-floor stations, none dropped.
            sparse_only_base_ts_s = 4_000_000_000
            sparse_only_callsigns = ["SP1", "SP2", "SP3"]
            sparse_only_offsets = [i * BUCKET_SECONDS for i in range(SPARSE_MIN_DATAPOINTS + 1)]
            sparse_only_rows = [
                _chart_bucket_row(cs, sparse_only_base_ts_s + off_s, idx)
                for cs in sparse_only_callsigns
                for idx, off_s in enumerate(sparse_only_offsets)
            ]
            sparse_only_result = await storage._build_chart_series(
                sparse_only_rows,
                gap_threshold_s=_CHART_GAP_THRESHOLD_S,
                gap_offset_s=_CHART_GAP_OFFSET_S,
            )
            results.append(
                (
                    (
                        "chart series: sparse fallback (nobody reaches"
                        " MIN_DATAPOINTS_FOR_STATS) still returns every station"
                    ),
                    {e["callsign"] for e in sparse_only_result} == set(sparse_only_callsigns),
                )
            )

            # (4) No-progress-callback path returns the identical series to the
            # with-callback path (reuses the dense+gap+sparse fixture from (1)).
            chart_actual_no_cb = await storage._build_chart_series(
                chart_bucket_rows,
                gap_threshold_s=_CHART_GAP_THRESHOLD_S,
                gap_offset_s=_CHART_GAP_OFFSET_S,
                progress_callback=None,
            )
            results.append(
                (
                    (
                        "chart series: no-progress-callback path returns the same series"
                        " as the with-callback path"
                    ),
                    chart_actual_no_cb == chart_actual,
                )
            )

            # (5) Empty input: _finalize_series must not raise on an empty
            # final_result (the `if stats_entries else 0` guard) and must report
            # 0/0 rather than a ZeroDivisionError or a KeyError from an empty set.
            empty_progress_calls: list[tuple[Any, ...]] = []

            async def _capture_empty_progress(*args: Any) -> None:
                empty_progress_calls.append(args)

            empty_result = await storage._build_chart_series(
                [],
                gap_threshold_s=_CHART_GAP_THRESHOLD_S,
                gap_offset_s=_CHART_GAP_OFFSET_S,
                progress_callback=_capture_empty_progress,
            )
            empty_done_calls = [c for c in empty_progress_calls if c[0] == "done"]
            results.append(
                (
                    "chart series: empty input returns an empty result, no exception",
                    empty_result == [],
                )
            )
            results.append(
                (
                    (
                        'chart series progress: empty input still emits a "done" event'
                        ' with "0 data points for 0 stations"'
                    ),
                    empty_done_calls == [("done", "0 data points for 0 stations")],
                )
            )
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  query: {'PASS' if passed else 'FAIL'}")
    return passed
