"""UptimeMixin: the gateway-uptime segment ledger (schema v25).

Measures whether `MeshCom server → node uplink → node → proxy` is alive, by
watching arrivals of the `{CET}` time beacon the server originates and our
node relays. Arrival only — never a round trip, and never derived from the
beacon's own payload clock (upstream-misleading by design; see
`project_cet_beacon_time` and CLAUDE.md). Full design:
`doc/archive/2026-08-21_2350-gateway-uptime-plan.md`.

Storage model (plan §3): an append-only ledger of CLOSED state segments
(`link_uptime_segments`, kind `gap` | `dark`) plus one live state row
(`link_uptime_state`). `up` runs are never stored — they are implicit in
whatever is NOT covered by a `gap`/`dark` row, and get materialised only by
the reader (`get_link_uptime`). This keeps the ledger's row count
proportional to the number of state transitions (tens per month) rather than
to wall-clock time.

Three writers (plan §4), all here:

  (a) `record_link_beacon` — called by the ingest hook (wave 2, `ingest.py`)
      on every accepted `{CET}` uplink beacon.
  (b) `link_uptime_tick` — called by a 30s heartbeat task (wave 2, `main.py`);
      proves the PROXY PROCESS was watching, independent of link state.
  (c) `reconcile_link_uptime_startup` — called once at process start (wave 2,
      `main.py`), to tell real downtime (the proxy was not running) apart
      from a short/clean restart, so a deploy never reads as a link outage.

One reader, `get_link_uptime` (plan §5), builds a contiguous, gapless cover
of the requested window and the stats derived from it.

`is_uplink_time_beacon` is the gate predicate deciding which inbound frames
are "our own uplink is alive" evidence at all — module-level and pure so it
is testable without a database, and because the ingest hook (wave 2) needs
to call it before `store_message` touches any table.
"""

import asyncio
import sqlite3
from typing import Any

from ..logging_setup import get_logger
from ..util import now_ms
from ._base import StorageBase
from .constants import (
    DARK_THRESHOLD_MS,
    GAP_TOLERANCE_MS,
    OFF_MS,
    SILENT_MS,
    db_read,
    db_write,
)

logger = get_logger(__name__)

_MAX_RENDER_SEGMENTS = 500
_SEVERITY = {"up": 0, "dark": 1, "disabled": 2, "gap": 3}  # gap > disabled > dark > up
_HOUR_MS = 3_600_000
_DAY_MS = 24 * _HOUR_MS


def _bucket_size_ms(window_ms: int) -> int | None:
    """Bucket width for the bar of a `window_ms` window: 1 day from a year,
    6 hours from 30 days, none for the short ranges (the segment list is
    precise enough there). The window decides, so the route's range table
    stays the only one.
    """
    if window_ms >= 365 * _DAY_MS:
        return _DAY_MS
    if window_ms >= 30 * _DAY_MS:
        return 6 * _HOUR_MS
    return None


def gateway_mode_from_register(data: dict[str, Any]) -> bool | None:
    """The node's gateway mode from a BLE `SN` register frame, or None.

    Only `TYP == "SN"` with a real boolean `GW` counts. An absent or non-bool
    `GW` is unknown, never "off": 0/1 or strings are not what the node sends.
    """
    if data.get("TYP") != "SN":
        return None
    gw = data.get("GW")
    return gw if isinstance(gw, bool) else None


def _compute_buckets(
    segments: list[dict[str, Any]], start: int, end: int, size: int
) -> list[dict[str, Any]]:
    """Per-bucket time by kind from the exact, contiguous, sorted `segments`.

    One pass: every segment is distributed over the buckets it overlaps, so the
    cost is O(segments + buckets). `longest_gap_ms` is the full length of
    the longest gap touching the bucket, not its clipped part, so merging
    buckets with max stays exact.
    """
    count = -(-(end - start) // size)
    buckets = [
        {
            "start_ms": start + i * size,
            "end_ms": min(start + (i + 1) * size, end),
            "up_ms": 0,
            "gap_ms": 0,
            "dark_ms": 0,
            "disabled_ms": 0,
            "longest_gap_ms": 0,
        }
        for i in range(count)
    ]
    for seg in segments:
        kind = seg["kind"]
        i = (seg["start_ms"] - start) // size
        while i < count and buckets[i]["start_ms"] < seg["end_ms"]:
            b = buckets[i]
            overlap = min(seg["end_ms"], b["end_ms"]) - max(seg["start_ms"], b["start_ms"])
            if overlap > 0:
                b[kind + "_ms"] += overlap
                if kind == "gap":
                    b["longest_gap_ms"] = max(b["longest_gap_ms"], seg["end_ms"] - seg["start_ms"])
            i += 1
    return buckets


def is_uplink_time_beacon(message: dict[str, Any]) -> bool:
    """True iff `message` is OUR node's own uplink relay of a `{CET}` time beacon.

    Measured live on mcapp.local 2026-08-21 23:40:31 — the same beacon reaches
    the proxy in up to three copies:

      * `src_type="udp"`, `src="OE1XAR-33"`, no `via` at all → 0 hops → COUNT
      * `src_type="ble_remote"`, `src="OE1XAR-33"`, `via="OE1XAR-33"` → 0 hops
        → COUNT. `split_path` (`ble_protocol.py`) strips our own callsign
        from the relay path, so the originator is left behind, alone, in
        `via` — a plain `not via` test (the webapp watchdog's rule) would
        wrongly REJECT this copy and break a BLE-only box.
      * `src_type="lora"`, `src="OE1XAR-62,DB0AU-12,DB0HOB-12,DL2JA-2"`,
        `via="DB0AU-12,DB0HOB-12,DL2JA-2"` → 3 hops → REJECT. That is a
        FOREIGN gateway's uplink relayed over RF, not ours.

    Rule: `msg` starts with `{CET}`, AND `src_type != "lora"`, AND the `via`
    path has zero hops once the originating callsign (first component of
    `src`) is excluded from it — which is exactly what makes the `ble_remote`
    self-relay copy above still count as zero hops.

    Port 1799 is unauthenticated (same reasoning as `store_message`'s own
    coercion): a non-`str` `msg`/`src_type`/`src`/`via` is treated as absent
    rather than raising.
    """
    msg = message.get("msg")
    msg = msg if isinstance(msg, str) else ""
    if not msg.startswith("{CET}"):
        return False

    src_type = message.get("src_type")
    src_type = src_type if isinstance(src_type, str) else ""
    if src_type == "lora":
        return False

    src = message.get("src")
    src = src if isinstance(src, str) else ""
    via = message.get("via")
    via = via if isinstance(via, str) else ""

    origin = src.split(",", 1)[0].strip().upper()
    hops = [
        component.strip()
        for component in via.split(",")
        if component.strip() and component.strip().upper() != origin
    ]
    return not hops


def _collect_non_up_intervals(  # noqa: PLR0913 - keyword-only, one seam per read-path input
    *,
    first_observed_ms: int | None,
    last_beacon_ms: int | None,
    seg_rows: list[sqlite3.Row],
    start: int,
    end: int,
    now: int,
    disabled_since_ms: int | None = None,
) -> list[dict[str, Any]]:
    """The non-`up` intervals overlapping `[start, end)`, clipped to it and
    sorted by start — everything `get_link_uptime` fills the `up` holes
    around. Split out of `get_link_uptime` only to keep that function's own
    branch count readable; see its docstring for what each source means.
    """
    non_up: list[dict[str, Any]] = []

    # Everything before first_observed_ms — or the whole window, if nothing
    # has EVER been observed — is 'dark', not merely a gap: nothing was
    # watching, so nothing can be claimed as measured.
    dark_before = end if first_observed_ms is None else min(first_observed_ms, end)
    if dark_before > start:
        non_up.append({"start_ms": start, "end_ms": dark_before, "kind": "dark"})

    for row in seg_rows:
        seg_start = max(row["start_ms"], start)
        seg_end = min(row["end_ms"], end)
        if seg_end > seg_start:
            non_up.append({"start_ms": seg_start, "end_ms": seg_end, "kind": row["kind"]})

    # Live tail: silence since the last counted beacon that has already
    # exceeded GAP_TOLERANCE_MS but has no CLOSING beacon yet to write it as
    # a stored row — there is no shutdown hook, so the reader materialises it
    # instead of a writer ever persisting it.
    #
    # If NO beacon has EVER been recorded, there is no last_beacon_ms to
    # anchor on — measure the silence from first_observed_ms instead, and
    # split on the same GAP_TOLERANCE_MS: within tolerance it is too early to
    # call the link down, so it must NOT render as `up` (that is the fresh-
    # ledger 100%-uptime bug) — fill it `dark` instead. `dark` normally means
    # "the proxy was not running"; here it is deliberately widened to also
    # mean "the proxy is running but has never heard anything yet, so this
    # stretch cannot be claimed as measured". Beyond tolerance it becomes an
    # honest `gap`, exactly like the has-beaconed case below.
    #
    # While the node's gateway mode is off (`disabled_since_ms`) the silence is
    # expected, not an outage: the open tail is `disabled` from that instant,
    # and the preceding tail (a silence beyond tolerance was closed as a stored
    # gap when the mode went off) is not materialised again.
    tail_now = now if disabled_since_ms is None else disabled_since_ms
    tail_end = end if disabled_since_ms is None else min(end, disabled_since_ms)
    if last_beacon_ms is None:
        if first_observed_ms is not None:
            tail_start = max(first_observed_ms, start)
            if tail_end > tail_start:
                kind = "gap" if tail_now - first_observed_ms > GAP_TOLERANCE_MS else "dark"
                non_up.append({"start_ms": tail_start, "end_ms": tail_end, "kind": kind})
    elif disabled_since_ms is None and now - last_beacon_ms > GAP_TOLERANCE_MS:
        tail_start = max(last_beacon_ms, start)
        if end > tail_start:
            non_up.append({"start_ms": tail_start, "end_ms": end, "kind": "gap"})

    if disabled_since_ms is not None:
        dis_start = max(disabled_since_ms, start)
        if end > dis_start:
            non_up.append({"start_ms": dis_start, "end_ms": end, "kind": "disabled"})

    non_up.sort(key=lambda s: s["start_ms"])
    return non_up


def _fill_up_segments(non_up: list[dict[str, Any]], start: int, end: int) -> list[dict[str, Any]]:
    """Fill every hole between `non_up`'s (already sorted, clipped) intervals
    with `up`, producing a contiguous, gapless cover of `[start, end]`.
    """
    segments: list[dict[str, Any]] = []
    cursor = start
    for seg in non_up:
        if seg["start_ms"] > cursor:
            segments.append({"start_ms": cursor, "end_ms": seg["start_ms"], "kind": "up"})
        segments.append(seg)
        cursor = max(cursor, seg["end_ms"])
    if cursor < end:
        segments.append({"start_ms": cursor, "end_ms": end, "kind": "up"})
    return segments


def _link_state(last_beacon_ms: int | None, now: int) -> str:
    """`state` from the age of `last_beacon_ms` (plan §5): no beacon ever
    seen → 'unknown'; younger than `SILENT_MS` → 'active'; younger than
    `OFF_MS` → 'silent'; older still → 'off'.
    """
    if last_beacon_ms is None:
        return "unknown"
    age = now - last_beacon_ms
    if age < SILENT_MS:
        return "active"
    if age < OFF_MS:
        return "silent"
    return "off"


def _downsample(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge `segments` (an already-contiguous, gapless cover) down to at most
    `_MAX_RENDER_SEGMENTS`, for rendering only — every stat in `get_link_uptime`
    is computed from the exact list BEFORE this runs (plan §5).

    Splits the list into at most `_MAX_RENDER_SEGMENTS` index-contiguous
    chunks (standard `(i * n) // k` bucketing: strictly covers `[0, n)`, so no
    input segment is dropped and the result stays a gapless cover of the same
    range) and merges each chunk into one segment spanning it, taking the
    worst-severity kind present (`gap` > `disabled` > `dark` > `up`) — a merge must never
    hide a real outage behind a wider `up` bar.
    """
    n = len(segments)
    if n <= _MAX_RENDER_SEGMENTS:
        return segments

    merged: list[dict[str, Any]] = []
    for i in range(_MAX_RENDER_SEGMENTS):
        lo = (i * n) // _MAX_RENDER_SEGMENTS
        hi = ((i + 1) * n) // _MAX_RENDER_SEGMENTS
        if lo >= hi:
            continue
        chunk = segments[lo:hi]
        worst_kind = max((s["kind"] for s in chunk), key=lambda k: _SEVERITY[k])
        merged.append(
            {"start_ms": chunk[0]["start_ms"], "end_ms": chunk[-1]["end_ms"], "kind": worst_kind}
        )
    return merged


class UptimeMixin(StorageBase):
    async def record_link_beacon(self, arrival_ms: int) -> None:
        """Record one accepted `{CET}` uplink-beacon arrival (plan §4a).

        Extends the currently open `up` run, unless the silence since the
        last counted beacon already exceeded `GAP_TOLERANCE_MS` — in which
        case that silence is closed off as a stored `gap` segment and a new
        `up` run opens starting now.

        The beacon legitimately arrives twice per cycle (`ble_remote` +
        `udp`, ~60 ms apart in the 2026-08-21 capture); both calls land
        inside the same `up` run because the tolerance is two orders of
        magnitude larger than that spacing. An out-of-order or duplicate
        arrival (`arrival_ms <= last_beacon_ms`) never moves `last_beacon_ms`
        backwards.
        """

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO link_uptime_state (id, first_observed_ms) VALUES (1, ?)",
                    (arrival_ms,),
                )
                row = conn.execute(
                    "SELECT last_beacon_ms, disabled_since_ms, first_observed_ms"
                    " FROM link_uptime_state WHERE id = 1"
                ).fetchone()
                last_beacon_ms = row[0] if row else None
                disabled_since = row[1] if row else None
                first_observed = row[2] if row else None

                if disabled_since is not None:
                    # A counted hop-0 beacon proves the uplink is alive, so a
                    # stale GW=false is wrong: close the disabled stretch.
                    if last_beacon_ms is None:
                        # Never beaconed: the silence before the disable has no
                        # stored row yet (the reader showed it as a tail).
                        self._store_unanchored_silence(
                            conn, first_observed, disabled_since, allow_dark=True
                        )
                    self._close_disabled(conn, disabled_since, arrival_ms)
                    conn.execute(
                        "UPDATE link_uptime_state"
                        " SET last_beacon_ms = ?, open_up_start_ms = ?,"
                        " gateway_enabled = 1, disabled_since_ms = NULL"
                        " WHERE id = 1",
                        (arrival_ms, arrival_ms),
                    )
                    return

                if last_beacon_ms is None:
                    # First beacon this ledger has ever seen. A silence beyond
                    # tolerance since the proxy first watched was already shown
                    # as a gap by the reader's tail; store it, or this beacon
                    # would turn it into `up`. Within tolerance it is too early
                    # to say, and stays as before.
                    self._store_unanchored_silence(
                        conn, first_observed, arrival_ms, allow_dark=False
                    )
                    conn.execute(
                        "UPDATE link_uptime_state"
                        " SET last_beacon_ms = ?, open_up_start_ms = ?"
                        " WHERE id = 1",
                        (arrival_ms, arrival_ms),
                    )
                    return

                if arrival_ms - last_beacon_ms > GAP_TOLERANCE_MS:
                    conn.execute(
                        "INSERT INTO link_uptime_segments (start_ms, end_ms, kind)"
                        " VALUES (?, ?, 'gap')",
                        (last_beacon_ms, arrival_ms),
                    )
                    conn.execute(
                        "UPDATE link_uptime_state"
                        " SET last_beacon_ms = ?, open_up_start_ms = ?"
                        " WHERE id = 1",
                        (arrival_ms, arrival_ms),
                    )
                elif arrival_ms > last_beacon_ms:
                    conn.execute(
                        "UPDATE link_uptime_state SET last_beacon_ms = ? WHERE id = 1",
                        (arrival_ms,),
                    )
                # Remaining case: arrival_ms <= last_beacon_ms, within tolerance
                # — a duplicate or out-of-order copy. Nothing to extend.

        await asyncio.to_thread(_run)

    @staticmethod
    def _close_disabled(conn: sqlite3.Connection, since_ms: int, end_ms: int) -> None:
        """Store the `disabled` row `[since_ms, end_ms)`. Zero-length rows are
        skipped, and a repeated start_ms extends the existing disabled row
        instead of violating the PRIMARY KEY.
        """
        if end_ms <= since_ms:
            return
        conn.execute(
            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind)"
            " VALUES (?, ?, 'disabled')"
            " ON CONFLICT(start_ms) DO UPDATE SET end_ms = MAX(end_ms, excluded.end_ms)"
            " WHERE kind = 'disabled'",
            (since_ms, end_ms),
        )

    @staticmethod
    def _store_unanchored_silence(
        conn: sqlite3.Connection, first_observed_ms: int | None, end_ms: int, *, allow_dark: bool
    ) -> None:
        """Store the silence of a ledger that has never counted a beacon, from
        `first_observed_ms` to `end_ms`, exactly as the reader's tail shows it:
        a `gap` beyond `GAP_TOLERANCE_MS`, else `dark` (when `allow_dark`).
        Idempotent per start_ms, like every other writer here.
        """
        if first_observed_ms is None or end_ms <= first_observed_ms:
            return
        if end_ms - first_observed_ms > GAP_TOLERANCE_MS:
            kind = "gap"
        elif allow_dark:
            kind = "dark"
        else:
            return
        conn.execute(
            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind) VALUES (?, ?, ?)"
            " ON CONFLICT(start_ms) DO UPDATE SET end_ms = MAX(end_ms, excluded.end_ms)"
            " WHERE kind = excluded.kind",
            (first_observed_ms, end_ms, kind),
        )

    async def record_link_gateway_mode(
        self, enabled: bool, now_ms_: int, *, seed_only: bool = False
    ) -> None:
        """Record the node's gateway mode (the `SN` register's `GW`).

        `seed_only` marks a value replayed from ble_service's age-less register
        cache: firmware pushes `SN` only on a BLE-driven change, so that copy
        can be stale. It may seed an unknown mode (`gateway_enabled IS NULL`)
        and never changes a known one. Live frames are authoritative.

        `False` while not disabled: a silence beyond `GAP_TOLERANCE_MS` is
        closed as a `gap` ending now, then `disabled` opens. `True` while
        disabled: the `disabled` row is stored and the beacon anchors reset to
        now, so no gap spans the disabled time. Repeats are no-ops (registers
        replay on every reconnect).
        """

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO link_uptime_state (id, first_observed_ms) VALUES (1, ?)",
                    (now_ms_,),
                )
                row = conn.execute(
                    "SELECT last_beacon_ms, disabled_since_ms, first_observed_ms,"
                    " gateway_enabled FROM link_uptime_state WHERE id = 1"
                ).fetchone()
                last_beacon_ms, disabled_since, first_observed, known = (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                )

                if seed_only and known is not None:
                    return
                # A fresh counted beacon outranks a stale cached GW=false: the uplink
                # is demonstrably up, so the replay says nothing about it.
                if (
                    seed_only
                    and not enabled
                    and last_beacon_ms is not None
                    and now_ms_ - last_beacon_ms <= GAP_TOLERANCE_MS
                ):
                    return

                if not enabled:
                    if disabled_since is not None:
                        return
                    if last_beacon_ms is None:
                        # Never beaconed: anchor the silence on first_observed
                        # and store it as the reader's tail showed it, or it
                        # would read as `up` once the mode turns back on.
                        self._store_unanchored_silence(
                            conn, first_observed, now_ms_, allow_dark=True
                        )
                        conn.execute(
                            "UPDATE link_uptime_state"
                            " SET last_beacon_ms = ?, open_up_start_ms = ? WHERE id = 1",
                            (now_ms_, now_ms_),
                        )
                    elif now_ms_ - last_beacon_ms > GAP_TOLERANCE_MS:
                        conn.execute(
                            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind)"
                            " VALUES (?, ?, 'gap')"
                            " ON CONFLICT(start_ms) DO UPDATE SET"
                            " end_ms = MAX(end_ms, excluded.end_ms) WHERE kind = 'gap'",
                            (last_beacon_ms, now_ms_),
                        )
                        conn.execute(
                            "UPDATE link_uptime_state"
                            " SET last_beacon_ms = ?, open_up_start_ms = ? WHERE id = 1",
                            (now_ms_, now_ms_),
                        )
                    conn.execute(
                        "UPDATE link_uptime_state"
                        " SET gateway_enabled = 0, disabled_since_ms = ? WHERE id = 1",
                        (now_ms_,),
                    )
                    return

                if disabled_since is None:
                    conn.execute(
                        "UPDATE link_uptime_state SET gateway_enabled = 1"
                        " WHERE id = 1 AND gateway_enabled IS NOT 1"
                    )
                    return
                self._close_disabled(conn, disabled_since, now_ms_)
                # last_beacon_ms is set to NOW although no beacon arrived: the
                # same reset reconcile does. The badge therefore reads active
                # for up to 12 min after the mode turns on, because it is too
                # early to say otherwise (a real beacon or its absence decides).
                conn.execute(
                    "UPDATE link_uptime_state"
                    " SET last_beacon_ms = ?, open_up_start_ms = ?,"
                    " gateway_enabled = 1, disabled_since_ms = NULL WHERE id = 1",
                    (now_ms_, now_ms_),
                )

        await asyncio.to_thread(_run)

    async def link_uptime_tick(self, now_ms_: int) -> None:
        """Heartbeat (plan §4b): proves the PROXY PROCESS itself was watching
        at this instant, independent of whether any beacon arrived. Written
        by a background task every 30s; `reconcile_link_uptime_startup` reads
        it at the next startup to tell real downtime apart from a short
        restart.
        """

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO link_uptime_state (id, first_observed_ms) VALUES (1, ?)",
                    (now_ms_,),
                )
                conn.execute(
                    "UPDATE link_uptime_state SET last_tick_ms = ? WHERE id = 1",
                    (now_ms_,),
                )

        await asyncio.to_thread(_run)

    async def reconcile_link_uptime_startup(self, now_ms_: int) -> None:
        """Startup reconciliation (plan §4c), run once per process start.

        - No state row at all (fresh install / first run ever) → seed
          `first_observed_ms`, no `dark` row — there is no prior watch period
          to have missed anything during.
        - A state row exists but `last_tick_ms` is still NULL (a beacon
          landed before any heartbeat ever ran) → same as fresh: nothing to
          reconcile against yet.
        - `now - last_tick_ms > DARK_THRESHOLD_MS` → the PROXY PROCESS was
          not running for that stretch (3 missed 30s heartbeats), so it is
          charged to `dark` (COVERAGE), never `gap` (UPTIME) — silence during
          a period we were not watching is not evidence the link was down.
          `last_beacon_ms` AND `open_up_start_ms` both reset to `now_ms_` so
          the next beacon opens a clean `up` run rather than one that
          silently spans the outage.
        - Otherwise (short/clean restart) → state is left exactly as-is, no
          `dark` row — a deploy restart must never read as a link outage.
        """

        def _run() -> None:
            with db_write(self.db_path) as conn:
                row = conn.execute(
                    "SELECT last_tick_ms, disabled_since_ms, last_beacon_ms, first_observed_ms"
                    " FROM link_uptime_state WHERE id = 1"
                ).fetchone()

                if row is None:
                    conn.execute(
                        "INSERT INTO link_uptime_state (id, first_observed_ms) VALUES (1, ?)",
                        (now_ms_,),
                    )
                    return

                last_tick_ms = row[0]
                disabled_since = row[1]
                if last_tick_ms is None or now_ms_ - last_tick_ms <= DARK_THRESHOLD_MS:
                    return

                # A `disabled` or `gap` row closed after the last heartbeat and
                # before the crash already covers part of [last_tick_ms, now):
                # start `dark` after it, or one bucket sums above its width.
                # Stored `dark` rows are excluded on purpose: a dark row left by a
                # crash before the first heartbeat starts AT last_tick_ms, and the
                # upsert below extends it (the 2026-10-20 crash-loop heal).
                stored_end = conn.execute(
                    "SELECT MAX(end_ms) FROM link_uptime_segments WHERE kind != 'dark'"
                ).fetchone()
                dark_start = max(last_tick_ms, stored_end[0] or 0)

                # The anchors reset below, so silence the proxy watched before it
                # went down must be stored now, or it reads as `up` afterwards.
                last_beacon_ms, first_observed = row[2], row[3]
                if disabled_since is None:
                    if last_beacon_ms is None:
                        self._store_unanchored_silence(
                            conn, first_observed, dark_start, allow_dark=True
                        )
                    elif dark_start - last_beacon_ms > GAP_TOLERANCE_MS:
                        conn.execute(
                            "INSERT INTO link_uptime_segments (start_ms, end_ms, kind)"
                            " VALUES (?, ?, 'gap')"
                            " ON CONFLICT(start_ms) DO UPDATE SET"
                            " end_ms = MAX(end_ms, excluded.end_ms) WHERE kind = 'gap'",
                            (last_beacon_ms, dark_start),
                        )

                # Upsert, and advance last_tick_ms: a process that crashes after
                # this point but before its first heartbeat tick used to leave
                # last_tick_ms behind, so every later start re-inserted the same
                # start_ms and died on the PRIMARY KEY (2026-10-20, mcapp.local,
                # permanent crash loop). The ON CONFLICT also heals a DB already
                # in that state by extending the existing dark row.
                if now_ms_ > dark_start:
                    conn.execute(
                        "INSERT INTO link_uptime_segments (start_ms, end_ms, kind)"
                        " VALUES (?, ?, 'dark')"
                        " ON CONFLICT(start_ms) DO UPDATE SET end_ms = MAX(end_ms, excluded.end_ms)"
                        " WHERE kind = 'dark'",
                        (dark_start, now_ms_),
                    )
                if disabled_since is not None:
                    # Disabled up to the last heartbeat, dark while we were not
                    # running, disabled again from now (the mode is unchanged).
                    self._close_disabled(conn, disabled_since, dark_start)
                    conn.execute(
                        "UPDATE link_uptime_state SET disabled_since_ms = ? WHERE id = 1",
                        (now_ms_,),
                    )
                conn.execute(
                    "UPDATE link_uptime_state"
                    " SET last_beacon_ms = ?, open_up_start_ms = ?, last_tick_ms = ?"
                    " WHERE id = 1",
                    (now_ms_, now_ms_, now_ms_),
                )

        await asyncio.to_thread(_run)

    async def get_link_uptime(self, window_ms: int, now_ms_: int | None = None) -> dict[str, Any]:
        """Build the `/api/uptime` response body for a `window_ms`-wide window
        ending at `now_ms_` (defaults to `now_ms()`) — plan §5.

        Collects the non-`up` intervals — stored `gap`/`dark` rows overlapping
        the window, the live tail (silence since `last_beacon_ms` that has
        already exceeded `GAP_TOLERANCE_MS`, rendered `gap`; or, if no beacon
        was EVER seen, silence since `first_observed_ms` instead, rendered
        `gap` beyond `GAP_TOLERANCE_MS` and `dark` within it — never `up`,
        which is what let a fresh ledger report a false 100% uptime), and
        everything before `first_observed_ms` (or the whole window, if there
        is no state row at all) as `dark` — clips each to the window, and
        fills every remaining hole with `up`. These three sources are disjoint and chronologically
        ordered by construction of the writers above (a stored segment's
        `end_ms` always becomes the new `last_beacon_ms`/`open_up_start_ms`,
        which is where the next one starts), so no merge step is needed
        before filling.

        Every statistic is computed from that exact, pre-downsample list.
        Only the `segments` field in the response is downsampled
        (`_downsample`), for rendering.
        """
        now = now_ms_ if now_ms_ is not None else now_ms()
        start = now - window_ms
        end = now

        def _run() -> dict[str, Any]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                state_row = conn.execute(
                    "SELECT first_observed_ms, last_beacon_ms, gateway_enabled, disabled_since_ms"
                    " FROM link_uptime_state WHERE id = 1"
                ).fetchone()
                seg_rows = conn.execute(
                    "SELECT start_ms, end_ms, kind FROM link_uptime_segments"
                    " WHERE end_ms > ? AND start_ms < ? ORDER BY start_ms",
                    (start, end),
                ).fetchall()

            first_observed_ms = state_row["first_observed_ms"] if state_row else None
            last_beacon_ms = state_row["last_beacon_ms"] if state_row else None
            disabled_since_ms = state_row["disabled_since_ms"] if state_row else None
            gw_raw = state_row["gateway_enabled"] if state_row else None
            gateway_enabled = None if gw_raw is None else bool(gw_raw)

            non_up = _collect_non_up_intervals(
                first_observed_ms=first_observed_ms,
                last_beacon_ms=last_beacon_ms,
                seg_rows=seg_rows,
                start=start,
                end=end,
                now=now,
                disabled_since_ms=disabled_since_ms,
            )
            segments = _fill_up_segments(non_up, start, end)

            up_ms = sum(s["end_ms"] - s["start_ms"] for s in segments if s["kind"] == "up")
            gap_durations = [s["end_ms"] - s["start_ms"] for s in segments if s["kind"] == "gap"]
            gap_ms = sum(gap_durations)
            observed_ms = up_ms + gap_ms

            uptime_pct = (100.0 * up_ms / observed_ms) if observed_ms > 0 else None
            coverage_pct = (100.0 * observed_ms / window_ms) if window_ms > 0 else 0.0
            longest_outage_ms = max(gap_durations) if gap_durations else 0

            bucket_ms = _bucket_size_ms(window_ms)
            buckets = (
                _compute_buckets(segments, start, end, bucket_ms)
                if bucket_ms is not None and end > start
                else None
            )

            return {
                "start_ms": start,
                "end_ms": end,
                "state": "disabled"
                if disabled_since_ms is not None
                else _link_state(last_beacon_ms, now),
                "gateway_enabled": gateway_enabled,
                "buckets": buckets,
                "uptime_pct": uptime_pct,
                "coverage_pct": coverage_pct,
                "longest_outage_ms": longest_outage_ms,
                "last_beacon_ms": last_beacon_ms,
                "thresholds": {"silent_ms": SILENT_MS, "off_ms": OFF_MS},
                "segments": _downsample(segments),
            }

        return await asyncio.to_thread(_run)
