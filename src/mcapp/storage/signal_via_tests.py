"""Built-in regression suite for `station_positions.signal_via` (ingest.py).

Pins the fix for the measured bug: `station_positions.rssi/snr` describes exactly
ONE radio link — the LAST HOP to us — but was stored keyed only by the
ORIGINATING station, so most rows attributed the reading to the wrong station
(live evidence: of 105 snapshot rows carrying an rssi, only 25 had
originator == last hop). `signal_via` records whose link the rssi/snr on that
row actually belongs to, derived fresh in `store_message` from the same frame
that carries rssi/snr and threaded through `_ingest_signal` into the 'signal'
branch of `_upsert_station_position` so it is written atomically with them —
never independently, never backfilled from a stale path.

Ephemeral tempfile SQLite DB per run (never touches the live DB), mirroring the
UDP-2.0 Track U suite in sqlite_storage.py and this package's other *_tests.py
modules. Drives the REAL production entry point (`storage.store_message`), not
a reimplementation of the derivation.

Coverage:
  * A relayed frame (`src` = 'ORIGIN,HOP-A,HOP-B') stores signal_via = the LAST
    hop, not the originator.
  * A direct frame (no relay path) stores signal_via = the station itself.
  * A BLE MHeard frame (no msg_id, src_type 'ble', type 'pos' — never carries a
    relay path) stores signal_via = the station itself.
  * signal_buckets accumulation is keyed by the derived last-hop callsign, not
    the originator, for a relayed frame — forced to flush by sending a second
    frame one bucket period later.
  * signal_log keeps its existing originator-keyed row (source='lora') even
    though signal_buckets keys differently — the two tables serve different
    purposes (see ingest.py's `_ingest_signal` docstring) and only the bucket
    key changes.
  * Own-echo gating: our own frame relayed back to us over RF (`callsign` ==
    the configured own callsign) writes NO signal_log row and NO
    station_positions signal upsert onto our own row, but the relay's link
    (`signal_via`) still accumulates into signal_buckets — it is a genuine
    measurement of that link. A different SSID of the same base operator
    callsign (e.g. DK5EN-1 vs the configured DK5EN-98) is NOT gated: exact
    match only, never base-callsign.
  * `clear_own_signal` nulls a pre-poisoned own station_positions row exactly
    once (idempotent: a second call against the same row is a no-op), and
    never touches any other row.

All timestamps are milliseconds (project-wide DB convention).
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import BUCKET_SECONDS

logger = get_logger(__name__)

_BASE_TS = 1_770_100_000_000  # fixed ms timestamp so the suite is deterministic
_BUCKET_MS = BUCKET_SECONDS * 1000


async def run_signal_via_tests() -> bool:
    """Run the signal_via regression suite. Returns True iff every case passes."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "signal_via_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _station_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM station_positions WHERE callsign = ?", (callsign,)
                )
                return rows[0] if rows else None

            async def _signal_log_sources(callsign: str) -> list[str]:
                rows = await storage._query(
                    "SELECT source FROM signal_log WHERE callsign = ? ORDER BY timestamp",
                    (callsign,),
                )
                return [row["source"] for row in rows]

            async def _bucket_row_count(callsign: str) -> int:
                rows = await storage._query(
                    "SELECT COUNT(*) AS c FROM signal_buckets WHERE callsign = ?", (callsign,)
                )
                return int(rows[0]["c"])

            # 1. Relayed frame: src = 'ORIGIN,HOP-A,HOP-B' -> signal_via = last hop.
            originator = "DL5RAS-1"
            hop_a = "DK2WV-3"
            last_hop = "DL2JA-2"
            relay_msg = {
                "msg_id": "SIGVIA001",
                "src": f"{originator},{hop_a},{last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 1,
                "rssi": -120,
                "snr": -10.0,
            }
            await storage.store_message(relay_msg, json.dumps(relay_msg))
            relay_row = await _station_row(originator)
            results.append(
                (
                    (
                        "relayed frame: signal_via on the ORIGINATOR's row is the LAST hop,"
                        f" not the originator ({last_hop!r})"
                    ),
                    relay_row is not None and relay_row.get("signal_via") == last_hop,
                )
            )

            # signal_log still keyed by the originator, tagged 'lora' (unchanged).
            results.append(
                (
                    (
                        "relayed frame: signal_log row is still keyed by the ORIGINATOR"
                        " (unchanged; only the bucket key and station_positions.signal_via move)"
                    ),
                    await _signal_log_sources(originator) == ["lora"],
                )
            )

            # 2. Direct frame (no relay path): signal_via = the station itself.
            direct_cs = "DB0ED-99"
            direct_msg = {
                "msg_id": "SIGVIA002",
                "src": direct_cs,
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 2,
                "rssi": -122,
                "snr": -14.0,
            }
            await storage.store_message(direct_msg, json.dumps(direct_msg))
            direct_row = await _station_row(direct_cs)
            results.append(
                (
                    "direct frame (no via path): signal_via = the station itself",
                    direct_row is not None and direct_row.get("signal_via") == direct_cs,
                )
            )

            # 3. BLE MHeard frame (no msg_id, src_type 'ble', type 'pos'): signal_via = itself.
            mheard_cs = "OE1MHD-1"
            mheard_msg = {
                "msg_id": None,
                "src": mheard_cs,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "ble",
                "timestamp": _BASE_TS + 3,
                "rssi": -90,
                "snr": 3.0,
            }
            await storage.store_message(mheard_msg, json.dumps(mheard_msg))
            mheard_row = await _station_row(mheard_cs)
            results.append(
                (
                    (
                        "BLE MHeard frame: signal_via = the station itself"
                        " (MHeard entries are always direct receptions)"
                    ),
                    mheard_row is not None and mheard_row.get("signal_via") == mheard_cs,
                )
            )

            # 4. Bucket accumulation is keyed by the last hop, not the originator.
            # Send a second relayed frame, same path, one full bucket period later,
            # so _accumulate_signal completes and flushes the FIRST bucket.
            bucket_originator = "DL5RAS-2"
            bucket_last_hop = "DL2JA-3"
            bucket_msg_1 = {
                "msg_id": "SIGVIA003",
                "src": f"{bucket_originator},{bucket_last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 4,
                "rssi": -100,
                "snr": -5.0,
            }
            bucket_start = (bucket_msg_1["timestamp"] // _BUCKET_MS) * _BUCKET_MS  # type: ignore[operator]
            bucket_msg_2 = {
                "msg_id": "SIGVIA004",
                "src": f"{bucket_originator},{bucket_last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": bucket_start + _BUCKET_MS + 1,  # next bucket -> flushes the first
                "rssi": -101,
                "snr": -5.5,
            }
            await storage.store_message(bucket_msg_1, json.dumps(bucket_msg_1))
            await storage.store_message(bucket_msg_2, json.dumps(bucket_msg_2))
            results.append(
                (
                    (
                        "bucket accumulation: a completed bucket lands under the LAST hop"
                        f" ({bucket_last_hop!r}), not the originator"
                    ),
                    await _bucket_row_count(bucket_last_hop) >= 1,
                )
            )
            results.append(
                (
                    "bucket accumulation: the ORIGINATOR gets no signal_buckets row at all",
                    await _bucket_row_count(bucket_originator) == 0,
                )
            )
        finally:
            await storage.close()

    await _test_own_echo_and_clear_own_signal(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    signal_via: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


async def _test_own_echo_and_clear_own_signal(results: list[tuple[str, bool]]) -> None:
    """Own-echo gating in `_ingest_signal` + `clear_own_signal` cleanup.

    Our own frame, relayed back to us over RF, arrives as an ordinary lora
    reception with `callsign` == our own configured callsign — see the
    docstrings on `IngestMixin._ingest_signal` and `clear_own_signal` for the
    live-evidence bug this pins.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "signal_via_own_echo_test.db"
        storage = await create_sqlite_storage(db_path)
        try:

            async def _station_row(callsign: str) -> dict[str, Any] | None:
                rows = await storage._query(
                    "SELECT * FROM station_positions WHERE callsign = ?", (callsign,)
                )
                return rows[0] if rows else None

            async def _signal_log_sources(callsign: str) -> list[str]:
                rows = await storage._query(
                    "SELECT source FROM signal_log WHERE callsign = ? ORDER BY timestamp",
                    (callsign,),
                )
                return [row["source"] for row in rows]

            # 1. Own-echo gating: our own frame, relayed back to us over RF, must not
            # poison our own station_positions row or write a signal_log row for us,
            # but the relay's link (last hop) is still a genuine measurement and must
            # still accumulate.
            own_callsign = "DK5EN-98"
            own_last_hop = "DL2JA-2"
            storage.set_own_callsign(own_callsign)

            # Seed a station_positions row for our own callsign the way a real own
            # position beacon would (direct, no relay path, no rssi/snr) — mirrors
            # production, where the node already has a row before a relay ever
            # echoes it back.
            own_seed_msg = {
                "msg_id": "SIGVIA005",
                "src": own_callsign,
                "dst": "*",
                "msg": "",
                "type": "pos",
                "src_type": "lora",
                "timestamp": _BASE_TS + 5,
                "lat": 48.2,
                "lon": 16.3,
            }
            await storage.store_message(own_seed_msg, json.dumps(own_seed_msg))

            own_echo_msg = {
                "msg_id": "SIGVIA006",
                "src": f"{own_callsign},DL2UD-1,{own_last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 6,
                "rssi": -118,
                "snr": -8.0,
            }
            own_bucket_start = (own_echo_msg["timestamp"] // _BUCKET_MS) * _BUCKET_MS  # type: ignore[operator]
            await storage.store_message(own_echo_msg, json.dumps(own_echo_msg))

            own_row = await _station_row(own_callsign)
            results.append(
                (
                    (
                        "own echo: station_positions for our own callsign keeps NULL rssi/snr"
                        " and no signal_via (the relay's rssi/snr is not ours)"
                    ),
                    own_row is not None
                    and own_row.get("rssi") is None
                    and own_row.get("snr") is None
                    and (own_row.get("signal_via") or "") == "",
                )
            )
            results.append(
                (
                    "own echo: no signal_log row is written for our own callsign",
                    await _signal_log_sources(own_callsign) == [],
                )
            )
            own_bucket_key = (own_last_hop, own_bucket_start)
            own_bucket_accumulated = (
                own_bucket_key in storage._bucket_accumulators
                and len(storage._bucket_accumulators[own_bucket_key]["rssi"]) >= 1
            )
            results.append(
                (
                    (
                        "own echo: signal_buckets accumulation for the relay"
                        f" ({own_last_hop!r}) still happens — it is a real measurement"
                        " of that link"
                    ),
                    own_bucket_accumulated,
                )
            )

            # 2. A different SSID of the SAME base operator callsign is a distinct,
            # real station and must NOT be gated (exact match only, never base).
            same_base_cs = "DK5EN-1"
            same_base_last_hop = "DL2JA-2"
            same_base_msg = {
                "msg_id": "SIGVIA007",
                "src": f"{same_base_cs},{same_base_last_hop}",
                "dst": "*",
                "msg": "",
                "type": "msg",
                "src_type": "lora",
                "timestamp": _BASE_TS + 7,
                "rssi": -95,
                "snr": 2.0,
            }
            await storage.store_message(same_base_msg, json.dumps(same_base_msg))
            same_base_row = await _station_row(same_base_cs)
            results.append(
                (
                    (
                        "same-base, different SSID (DK5EN-1 vs own DK5EN-98): signal IS"
                        " written — exact match only, never base-callsign"
                    ),
                    same_base_row is not None
                    and same_base_row.get("signal_via") == same_base_last_hop,
                )
            )
            results.append(
                (
                    "same-base, different SSID: signal_log row IS written",
                    await _signal_log_sources(same_base_cs) == ["lora"],
                )
            )

            # 3. clear_own_signal: idempotent cleanup of a pre-poisoned own row,
            # never touching any other station's row.
            await storage._mutate(
                "UPDATE station_positions SET rssi = ?, snr = ?, signal_via = ? WHERE callsign = ?",
                (-118, -8.0, own_last_hop, own_callsign),
            )
            poisoned_row = await _station_row(own_callsign)
            results.append(
                (
                    "clear_own_signal setup: own row is poisoned before the cleanup call",
                    poisoned_row is not None and poisoned_row.get("rssi") == -118,
                )
            )

            cleared_first = await storage.clear_own_signal(own_callsign)
            cleaned_row = await _station_row(own_callsign)
            results.append(
                (
                    "clear_own_signal: first call nulls the poisoned row and returns 1",
                    cleared_first == 1
                    and cleaned_row is not None
                    and cleaned_row.get("rssi") is None
                    and cleaned_row.get("snr") is None
                    and (cleaned_row.get("signal_via") or "") == "",
                )
            )

            cleared_second = await storage.clear_own_signal(own_callsign)
            results.append(
                (
                    "clear_own_signal: second call against the same (now clean) row is a no-op",
                    cleared_second == 0,
                )
            )

            other_row_after_clear = await _station_row(same_base_cs)
            results.append(
                (
                    "clear_own_signal: an unrelated station's row is left untouched",
                    other_row_after_clear is not None
                    and other_row_after_clear.get("signal_via") == same_base_last_hop,
                )
            )

            cleared_empty = await storage.clear_own_signal("")
            results.append(
                (
                    "clear_own_signal: an empty callsign is a no-op and returns 0",
                    cleared_empty == 0,
                )
            )
        finally:
            await storage.close()
