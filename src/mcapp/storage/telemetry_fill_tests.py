"""Built-in suite for the telemetry gap fill, Wave 1 (plan
doc/archive/2026-11-07_1000-telemetry-gap-fill-plan.md).

Pure rules (`telemetry_fill`), the storage methods on a temp DB, the RF-wins
change in `store_telemetry`, and the v41 migration step. Every case names the
mutation it kills.
"""

import asyncio
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..sqlite_storage import create_sqlite_storage
from .constants import CREATE_SCHEMA_SQL, CREATE_SCHEMA_V2_SQL, LATEST_SCHEMA_VERSION, db_write
from .telemetry_fill import (
    FILL_MATCH_WINDOW_MS,
    FILL_SETTLE_MS,
    FILL_TWIN_WINDOW_S,
    FillRow,
    collapse_twins,
    map_entry,
    select_fill_rows,
)

_NOW = 1_800_000_000_000  # ms
_T = _NOW - 10 * 3_600_000  # a sample ten hours old
_CS = "DL1TEST-12"


def _check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))


def _case(results: list[tuple[str, bool]], label: str, fn: Callable[[], bool]) -> None:
    """Record `fn()`; an exception is a failed case with the reason in the label."""
    try:
        ok = fn()
    except Exception as exc:
        label = f"{label} [raised {type(exc).__name__}: {exc}]"
        ok = False
    results.append((label, ok))


def _fr(ts_ms: int, **kw: Any) -> FillRow:
    """FillRow with every column absent unless given."""
    cols: dict[str, Any] = dict.fromkeys(
        ("temp1", "temp2", "hum", "qfe", "qnh", "alt", "gas", "co2", "batt", "extras_json")
    )
    cols.update(kw)
    return FillRow(timestamp_ms=ts_ms, **cols)


def _e(ts_ms: int, **kw: Any) -> dict[str, Any]:
    """Meshmap entry at `ts_ms` (the API speaks seconds)."""
    return {"timestamp": ts_ms / 1000, "temp": 10.0, **kw}


def _pure(results: list[tuple[str, bool]]) -> None:
    # Twin collapse. Kills: no collapse; collapse measured from the previous row
    # instead of the last KEPT row (3 rows 40 s apart must keep rows 1 and 3).
    twins = [_e(_T), _e(_T + 40_000), _e(_T + 80_000)]
    kept = collapse_twins(twins)
    _check(
        results,
        "collapse: run of 3 twins keeps the first and the one 80 s from it",
        [k["timestamp"] for k in kept] == [_T / 1000, (_T + 80_000) / 1000],
    )
    unsorted = collapse_twins([_e(_T + 600_000), _e(_T)])
    _check(
        results,
        "collapse: sorts by timestamp",
        [k["timestamp"] for k in unsorted] == [_T / 1000, (_T + 600_000) / 1000],
    )
    _check(
        results,
        "collapse: exactly window_s apart is NOT a twin",
        len(collapse_twins([_e(_T), _e(_T + 60_000)])) == 2,
    )
    bad: list[dict[str, Any]] = [
        {"timestamp": "x"},
        {"timestamp": None},
        {},
        {"timestamp": float("nan")},
        {"timestamp": True},
        _e(_T),
    ]
    try:
        out = collapse_twins(bad)
        ok = len(out) == 1
    except Exception:
        ok = False
    _check(results, "collapse: malformed timestamps are dropped, never raised", ok)

    # Mapping.
    row = map_entry(
        _e(
            _T,
            temp=1.5,
            temp2=2.5,
            hum=55.0,
            press=977.1,
            qnh=1026.4,
            qnhReduced=1030.0,
            alt=425,
            batt=87.0,
            gas_res=550.2,
            eco2=500,
            wlevel=3.5,
            wtemp=11.0,
        )
    )
    _check(
        results,
        "map: every field lands in its column (press -> qfe)",
        row is not None
        and (row.temp1, row.temp2, row.hum, row.qfe, row.qnh, row.alt)
        == (1.5, 2.5, 55.0, 977.1, 1026.4, 425.0)
        # gas stays fractional: thousands of RF rows hold e.g. 550.2, despite the
        # column's INTEGER affinity, so rounding would put steps in the gas chart.
        and (row.gas, row.co2, row.batt) == (550.2, 500, 87)
        and row.timestamp_ms == _T,
    )
    _check(
        results,
        "map: qnhReduced is stored nowhere; wlevel/wtemp only in extras",
        row is not None
        and row.qnh == 1026.4
        and row.extras_json == '{"wlevel":3.5,"wtemp":11.0}'
        and "1030" not in repr(row),
    )
    plain = map_entry(_e(_T))
    _check(
        results,
        "map: extras are None when wlevel/wtemp absent",
        plain is not None and plain.extras_json is None,
    )
    _check(
        results,
        "map: alt alone is not a reading",
        map_entry({"timestamp": _T / 1000, "alt": 400}) is None,
    )
    _check(
        results,
        "map: non-finite and non-numeric values are absent, a bool is not a number",
        map_entry({"timestamp": 1.0, "temp": float("inf"), "hum": "5", "batt": True}) is None,
    )
    part = map_entry({"timestamp": 1.0, "temp": 5.0, "hum": float("nan"), "press": "x"})
    _check(
        results,
        "map: a bad field is absent but the others survive",
        part is not None and part.temp1 == 5.0 and part.hum is None and part.qfe is None,
    )

    _pure_mapping_rules(results)
    _pure_selection(results)


def _pure_mapping_rules(results: list[tuple[str, bool]]) -> None:
    ts = _T / 1000
    # Item 1: an exact 0 is "no sensor" for every numeric field except alt (the
    # RF rule of `_wire_reading`). One case per field. Kills: the zero rule
    # removed for that field.
    fields = (
        ("temp", "temp1"),
        ("temp2", "temp2"),
        ("hum", "hum"),
        ("press", "qfe"),
        ("qnh", "qnh"),
        ("gas_res", "gas"),
        ("eco2", "co2"),
        ("batt", "batt"),
    )
    for key, attr in fields:
        anchor = {"hum": 5.0} if key != "hum" else {"temp": 5.0}
        if key == "temp":
            anchor = {"hum": 5.0}

        def zero_absent(key: str = key, attr: str = attr, anchor: dict[str, Any] = anchor) -> bool:
            row = map_entry({"timestamp": ts, **anchor, key: 0})
            return row is not None and getattr(row, attr) is None

        _case(results, f"map: {key}=0 is absent (no-probe sentinel)", zero_absent)
    _case(
        results,
        "map: wlevel=0 / wtemp=0 are not put into extras",
        lambda: (
            (r := map_entry({"timestamp": ts, "temp": 5.0, "wlevel": 0, "wtemp": 0.0})) is not None
            and r.extras_json is None
        ),
    )
    _case(
        results,
        "map: alt=0 is a real altitude and stays",
        lambda: (
            (r := map_entry({"timestamp": ts, "temp": 5.0, "alt": 0})) is not None and r.alt == 0.0
        ),
    )
    _case(
        results,
        "map: a row whose only values were zeros does not survive",
        lambda: (
            map_entry({"timestamp": ts, "temp": 0, "temp2": 0.0, "hum": 0, "wlevel": 0}) is None
            and map_entry({"timestamp": ts, "temp2": 0, "alt": 0}) is None
        ),
    )

    # Item 8: wlevel/wtemp alone make a row; batt is rounded.
    _case(
        results,
        "map: a wlevel-only row survives with the value in extras",
        lambda: (
            (r := map_entry({"timestamp": ts, "wlevel": 3.5})) is not None
            and r.extras_json == '{"wlevel":3.5}'
            and r.temp1 is None
        ),
    )
    _case(
        results,
        "map: a wtemp-only row survives",
        lambda: (
            (r := map_entry({"timestamp": ts, "wtemp": 11.0})) is not None
            and r.extras_json == '{"wtemp":11.0}'
        ),
    )
    _case(
        results,
        "map: batt 87.6 rounds to 88 and eco2 499.5 to an int",
        lambda: (
            (r := map_entry({"timestamp": ts, "batt": 87.6, "eco2": 612.4})) is not None
            and r.batt == 88
            and isinstance(r.batt, int)
            and r.co2 == 612
        ),
    )

    # Item 5: qfe / qnh outside the RF plausibility ranges are absent.
    def plaus(key: str, value: float) -> float | None:
        row = map_entry({"timestamp": ts, "temp": 5.0, key: value})
        assert row is not None
        return row.qfe if key == "press" else row.qnh

    _case(
        results,
        "map: qfe outside 300..1100 hPa is absent, the bounds are inclusive",
        lambda: (
            plaus("press", 299.9) is None
            and plaus("press", 1100.1) is None
            and plaus("press", 300) == 300
            and plaus("press", 1100) == 1100
            and plaus("press", 977.1) == 977.1
        ),
    )
    _case(
        results,
        "map: qnh outside 850..1100 hPa is absent (qnh 760 mmHg junk), bounds inclusive",
        lambda: (
            plaus("qnh", 760) is None
            and plaus("qnh", 849.9) is None
            and plaus("qnh", 101325) is None
            and plaus("qnh", 850) == 850
            and plaus("qnh", 1100) == 1100
        ),
    )

    # Item 6: nothing int64-unsafe leaves the pure code.
    _case(
        results,
        "map: eco2=1e30 / batt=2**63 become absent, the row survives on its other values",
        lambda: (
            (r := map_entry({"timestamp": ts, "temp": 5.0, "eco2": 1e30, "batt": 2**63}))
            is not None
            and r.co2 is None
            and r.batt is None
            and r.temp1 == 5.0
        ),
    )
    _case(
        results,
        "map: the largest int64-safe integer is kept",
        lambda: (
            (r := map_entry({"timestamp": ts, "eco2": 9.0e18})) is not None
            and r.co2 == 9_000_000_000_000_000_000
        ),
    )
    _case(
        results,
        "map: timestamp <= 0 or outside int64 ms drops the entry, never raises",
        lambda: all(
            map_entry({"timestamp": bad, "temp": 5.0}) is None
            for bad in (0, -5, 0.0004, 1e16, 1e300, 1e306, float(2**63))
        ),
    )
    _case(
        results,
        "map: a far-future but int64-safe timestamp is kept",
        lambda: (
            (r := map_entry({"timestamp": 1e12, "temp": 5.0})) is not None
            and r.timestamp_ms == 10**15
        ),
    )


def _pure_selection(results: list[tuple[str, bool]]) -> None:
    def sel(entry_ms: int, rf: list[int], filled: list[int] | None = None) -> list[FillRow]:
        return select_fill_rows([_e(entry_ms)], rf, filled or [], _NOW)

    _check(results, "select: no neighbours -> filled", len(sel(_T, [])) == 1)
    _check(
        results,
        "select: RF row exactly 15 min away is a match (not filled)",
        sel(_T, [_T + FILL_MATCH_WINDOW_MS]) == [] and sel(_T, [_T - FILL_MATCH_WINDOW_MS]) == [],
    )
    _check(
        results,
        "select: RF row 15 min + 1 ms away is NOT a match (filled)",
        len(sel(_T, [_T + FILL_MATCH_WINDOW_MS + 1])) == 1,
    )
    _check(
        results,
        "select: late RF row at +14 min blocks the fill",
        sel(_T, [_T + 14 * 60_000]) == [],
    )
    _check(
        results,
        "select: early RF row at -14 min also blocks the fill",
        sel(_T, [_T - 14 * 60_000]) == [],
    )
    # Unsorted input. Kills: sorted() removed on either list (bisect on an unsorted
    # list lands on the far neighbour and misses the close one).
    _check(
        results,
        "select: unsorted RF timestamps are handled",
        sel(_T, [_T + 10**9, _T + 14 * 60_000]) == [],
    )
    _check(
        results,
        "select: unsorted filled timestamps are handled",
        sel(_T, [], [_T + 10**9, _T + 30_000]) == [],
    )

    # Item 3: a filled neighbour is a twin check (60 s), not an RF match (15 min).
    # Kills: filled rows treated like RF rows; the twin bound off by one.
    win = FILL_TWIN_WINDOW_S * 1000
    _check(
        results,
        "select: filled row 15 min away does NOT block the fill",
        len(sel(_T, [], [_T + FILL_MATCH_WINDOW_MS])) == 1
        and len(sel(_T, [], [_T - 5 * 60_000])) == 1,
    )
    _check(
        results,
        "select: filled row closer than 60 s blocks the fill, exactly 60 s does not",
        sel(_T, [], [_T + win - 1]) == []
        and sel(_T, [], [_T - win + 1]) == []
        and sel(_T, [], [_T]) == []
        and len(sel(_T, [], [_T + win])) == 1
        and len(sel(_T, [], [_T - win])) == 1,
    )
    cadence = [_e(_T + i * 300_000) for i in range(12)]
    whole = select_fill_rows(cadence, [], [], _NOW)
    first = select_fill_rows(cadence[:6], [], [], _NOW)
    second = select_fill_rows(cadence[6:], [], [r.timestamp_ms for r in first], _NOW)
    _check(
        results,
        "select: a 5 min cadence split across chunks keeps all rows, same as one chunk",
        len(whole) == 12
        and [r.timestamp_ms for r in first + second] == [r.timestamp_ms for r in whole],
    )
    # Twins across a chunk boundary: A, B, C 40 s apart. One chunk keeps A and C, so
    # chunk 2 = [B, C] against filled [A] must keep C, not drop it behind a blocked B.
    abc = [_e(_T + i * 40_000, temp=1.0 + i) for i in range(3)]
    in_one = [r.timestamp_ms for r in select_fill_rows(abc, [], [], _NOW)]
    split = [r.timestamp_ms for r in select_fill_rows(abc[1:], [], [_T], _NOW)]
    _check(
        results,
        "select: a twin run split across chunks keeps the same rows as one chunk",
        in_one == [_T, _T + 80_000] and split == [_T + 80_000],
    )
    huge = json.loads('{"timestamp": 1000, "temp": 5, "eco2": 1' + "0" * 400 + "}")
    huge_ts = json.loads('{"timestamp": 1' + "0" * 400 + ', "temp": 5}')
    try:
        ok = (
            map_entry(huge) is not None
            and map_entry(huge).co2 is None  # type: ignore[union-attr]  # checked on the left
            and map_entry(huge_ts) is None
            and collapse_twins([huge_ts]) == []
            and select_fill_rows([huge, huge_ts], [], [], _NOW) != []
        )
    except OverflowError:
        ok = False
    _check(results, "map: an integer beyond the float range is absent, never raised", ok)
    rerun = select_fill_rows(cadence, [], [r.timestamp_ms for r in whole], _NOW)
    _check(results, "select: a rerun of a filled window inserts 0 rows", rerun == [])
    _check(
        results,
        "select: RF neighbour wins over a far filled one (RF check still applies)",
        select_fill_rows([_e(_T)], [_T + 600_000], [_T + 600_000], _NOW) == [],
    )

    _check(
        results,
        "select: exactly 30 min old IS eligible, 1 ms younger is not",
        len(select_fill_rows([_e(_NOW - FILL_SETTLE_MS)], [], [], _NOW)) == 1
        and select_fill_rows([_e(_NOW - FILL_SETTLE_MS + 1)], [], [], _NOW) == [],
    )
    two = select_fill_rows([_e(_T), _e(_T + 30_000), _e(_T + 3_600_000)], [], [], _NOW)
    _check(
        results,
        "select: twins collapse before filling (2 rows, not 3)",
        [r.timestamp_ms for r in two] == [_T, _T + 3_600_000],
    )

    # Item 7: map before collapse. Kills: collapse before the sensor filter (the
    # alt-only entry would absorb the real sample 30 s later).
    kept = select_fill_rows([{"timestamp": _T / 1000, "alt": 400}, _e(_T + 30_000)], [], [], _NOW)
    _check(
        results,
        "select: an unusable entry does not swallow the real sample 30 s later",
        [r.timestamp_ms for r in kept] == [_T + 30_000],
    )
    zero_twin = select_fill_rows(
        [{"timestamp": _T / 1000, "temp2": 0}, _e(_T + 30_000)], [], [], _NOW
    )
    _check(
        results,
        "select: a zero-only entry does not swallow the real sample either",
        [r.timestamp_ms for r in zero_twin] == [_T + 30_000],
    )
    _case(
        results,
        "select: out-of-range entries (eco2 1e30, timestamp 1e300) neither raise nor survive",
        lambda: (
            [
                r.timestamp_ms
                for r in select_fill_rows(
                    [
                        _e(_T, eco2=1e30),
                        {"timestamp": 1e300, "temp": 1.0},
                        {"timestamp": -3, "temp": 1},
                    ],
                    [],
                    [],
                    _NOW,
                )
            ]
            == [_T]
        ),
    )
    many = select_fill_rows(
        [_e(_T + i * 120_000) for i in range(5000)],
        [_T + i * 60_000 for i in range(0, 30_000, 7)],
        [_T + i * 60_000 for i in range(0, 30_000, 11)],
        _NOW + 10**9,
    )
    _check(results, "select: 5k entries x 30k neighbours runs (bisect)", isinstance(many, list))


async def _count(storage: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await storage._query(sql, params)
    return rows


async def _storage(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "fill.db")
        try:
            # station_positions pre-state: must survive the fill untouched.
            await storage._mutate(
                "INSERT INTO station_positions (callsign, temp1, hum, last_seen)"
                " VALUES (?, ?, ?, ?)",
                (_CS, 20.0, 40.0, _NOW),
            )
            before = await _count(storage, "SELECT * FROM station_positions ORDER BY callsign")
            fills = select_fill_rows(
                [_e(_T, hum=50.0, qnhReduced=1.0, wlevel=2.0), _e(_T + 3_600_000)], [], [], _NOW
            )
            n = await storage.insert_filled_telemetry(_CS, fills)
            other = await storage.insert_filled_telemetry("DL9NEW-1", fills[:1])
            rows = await _count(
                storage, "SELECT * FROM telemetry WHERE callsign = ? ORDER BY timestamp", (_CS,)
            )
            _check(results, "insert: returns the inserted count", n == 2 and other == 1)
            _check(
                results,
                "insert: filled rows carry origin 'meshmap' and the mapped values",
                len(rows) == 2
                and all(r["origin"] == "meshmap" for r in rows)
                and rows[0]["timestamp"] == _T
                and rows[0]["hum"] == 50.0
                and rows[0]["extras"] == '{"wlevel":2.0}',
            )
            _check(
                results,
                "insert: qnhReduced is not stored anywhere",
                "qnhReduced" not in repr(rows)
                and "1.0" not in str(rows[0]["extras"]).replace("2.0", ""),
            )
            after = await _count(storage, "SELECT * FROM station_positions ORDER BY callsign")
            _check(
                results,
                "insert: station_positions untouched (rows and values), no row for a new station",
                before == after and len(after) == 1,
            )
            _check(
                results,
                "insert: empty batch inserts nothing",
                await storage.insert_filled_telemetry(_CS, []) == 0,
            )
            ts_cs = "DL8TS-1"
            rf_t, fill_b = _T + 120_000, _T + 240_000
            await storage.insert_filled_telemetry(ts_cs, [_fr(_T, temp1=1.0), _fr(fill_b, hum=2.0)])
            await storage.store_telemetry(
                ts_cs, {"timestamp": rf_t, "src_type": "ble_remote", "temp1": 3.0}
            )
            await storage.store_telemetry(
                "DL8TS-2", {"timestamp": rf_t, "src_type": "ble_remote", "temp1": 3.0}
            )
            _check(
                results,
                "timestamps: (rf, filled), inclusive range, ascending, per callsign",
                await storage.get_telemetry_timestamps(ts_cs, _T, fill_b) == ([rf_t], [_T, fill_b])
                and await storage.get_telemetry_timestamps(ts_cs, _T + 1, fill_b - 1)
                == ([rf_t], [])
                and await storage.get_telemetry_timestamps(ts_cs, rf_t + 1, fill_b - 1) == ([], [])
                and await storage.get_telemetry_timestamps("DL8TS-3", 0, _NOW) == ([], []),
            )

            # Item 6/8: out-of-range entries never reach the insert; a failing batch rolls back.
            odd = select_fill_rows(
                [_e(_T + 7_200_000, eco2=1e30, batt=2**64), {"timestamp": 1e300, "temp": 1.0}],
                [],
                [],
                _NOW,
            )
            odd_rows: list[dict[str, Any]] = []
            try:
                n_odd = await storage.insert_filled_telemetry("DL8ODD-1", odd)
                odd_rows = await _count(
                    storage, "SELECT co2, batt, temp1 FROM telemetry WHERE callsign = 'DL8ODD-1'"
                )
            except Exception:
                n_odd = -1
            _check(
                results,
                "insert: eco2=1e30 entry stored with NULL co2/batt, 1e300 timestamp dropped",
                n_odd == 1
                and [(r["co2"], r["batt"], r["temp1"]) for r in odd_rows] == [(None, None, 10.0)],
            )
            try:
                await storage.insert_filled_telemetry(
                    "DL8RB-1",
                    [_fr(_T, temp1=1.0), _fr(_T + 1000, temp1=2.0), _fr(2**70, temp1=3.0)],
                )
                raised = False
            except Exception:
                raised = True
            left = await _count(storage, "SELECT id FROM telemetry WHERE callsign = 'DL8RB-1'")
            _check(
                results,
                "insert: a failing batch raises and rolls back completely (no partial rows)",
                raised and left == [],
            )

            # Cursor.
            _check(
                results, "cursor: none before first set", await storage.get_fill_cursor(_CS) is None
            )
            await storage.set_fill_cursor(_CS, 1000)
            await storage.set_fill_cursor(_CS, 500)
            _check(
                results,
                "cursor: a lower value never moves it back",
                await storage.get_fill_cursor(_CS) == 1000,
            )
            await storage.set_fill_cursor(_CS, 2000)
            _check(
                results,
                "cursor: a higher value advances it; other stations independent",
                await storage.get_fill_cursor(_CS) == 2000
                and await storage.get_fill_cursor("DL9NEW-1") is None,
            )

            await _rf_wins(storage, results)
        finally:
            await storage.close()


async def _origins(
    storage: Any, cs: str
) -> list[tuple[int, str | None, float | None, float | None]]:
    rows = await _count(
        storage,
        "SELECT id, origin, temp1, qfe FROM telemetry WHERE callsign = ? ORDER BY id",
        (cs,),
    )
    return [(r["id"], r["origin"], r["temp1"], r["qfe"]) for r in rows]


async def _rf_wins(storage: Any, results: list[tuple[str, bool]]) -> None:
    ble = "ble_remote"

    async def seed(cs: str) -> None:
        await storage.insert_filled_telemetry(
            cs, [FillRow(_T, 10.0, None, 50.0, None, None, None, None, None, None, None)]
        )

    # SKIP: identical values, no value changes. Kills: origin only cleared on UPDATE.
    seed_cs = "DL2SKIP-1"
    await seed(seed_cs)
    first = await _origins(storage, seed_cs)
    await storage.store_telemetry(
        seed_cs, {"timestamp": _T + 10_000, "src_type": ble, "temp1": 10.0, "hum": 50.0}
    )
    got = await _origins(storage, seed_cs)
    _check(
        results,
        "RF over filled (SKIP): same row, values kept, origin cleared",
        len(got) == 1 and got[0][0] == first[0][0] and got[0][1] is None and got[0][2] == 10.0,
    )

    # UPDATE_EXISTING: a derived qfe fills in. Kills: origin only cleared on SKIP.
    upd_cs = "DL3UPD-1"
    await storage.insert_filled_telemetry(
        upd_cs, [FillRow(_T, 10.0, None, 50.0, None, 1013.0, None, None, None, None, None)]
    )
    first = await _origins(storage, upd_cs)
    await storage.store_telemetry(
        upd_cs,
        {
            "timestamp": _T + 10_000,
            "src_type": ble,
            "temp1": 10.0,
            "hum": 50.0,
            "qnh": 1013.0,
            "alt": 100.0,
        },
    )
    got = await _origins(storage, upd_cs)
    _check(
        results,
        "RF over filled (UPDATE_EXISTING): same row updated (qfe derived), origin cleared",
        len(got) == 1 and got[0][0] == first[0][0] and got[0][3] is not None and got[0][1] is None,
    )

    # REPLACE_EXISTING: a measured newer value replaces; the new row is RF.
    rep_cs = "DL4REP-1"
    await seed(rep_cs)
    await storage.store_telemetry(
        rep_cs, {"timestamp": _T + 10_000, "src_type": ble, "temp1": 11.0, "hum": 51.0}
    )
    got = await _origins(storage, rep_cs)
    _check(
        results,
        "RF over filled (REPLACE_EXISTING): one row, origin NULL",
        len(got) == 1 and got[0][1] is None and got[0][2] == 11.0,
    )

    # Outside the 60 s window the filled row is left alone.
    far_cs = "DL5FAR-1"
    await seed(far_cs)
    await storage.store_telemetry(
        far_cs, {"timestamp": _T + 120_000, "src_type": ble, "temp1": 10.0, "hum": 50.0}
    )
    got = await _origins(storage, far_cs)
    _check(
        results,
        "RF frame outside the 60 s window: filled row keeps its origin, RF row is NULL",
        len(got) == 2 and got[0][1] == "meshmap" and got[1][1] is None,
    )

    await _rf_wins_more(storage, results)


async def _rf_wins_more(storage: Any, results: list[tuple[str, bool]]) -> None:
    ble = "ble_remote"

    # Item 4a: an RF frame OLDER than the filled row still wins. Kills: incoming_is_newer
    # not widened for a filled row (the tie then goes to the existing filled values).
    old_cs = "DL7OLD-2"
    await storage.insert_filled_telemetry(old_cs, [_fr(_T, temp1=10.0, hum=50.0)])
    await storage.store_telemetry(
        old_cs, {"timestamp": _T - 30_000, "src_type": ble, "temp1": 11.0, "hum": 51.0}
    )
    got = await _origins(storage, old_cs)
    _check(
        results,
        "RF over filled (4a): an older RF frame's values replace the filled ones, origin NULL",
        len(got) == 1 and got[0][1] is None and got[0][2] == 11.0,
    )

    # Item 4b: the dedup lookup prefers an RF row over a newer filled one. Kills: the
    # ORDER BY back to `timestamp DESC` (the RF frame then matches, and consumes, the
    # filled row instead of its own RF twin).
    pref_cs = "DL7PRF-1"
    await storage.store_telemetry(
        pref_cs, {"timestamp": _T, "src_type": ble, "temp1": 12.0, "hum": 40.0}
    )
    await storage.insert_filled_telemetry(pref_cs, [_fr(_T + 30_000, temp1=10.0, hum=50.0)])
    before = await _origins(storage, pref_cs)
    await storage.store_telemetry(
        pref_cs, {"timestamp": _T + 20_000, "src_type": ble, "temp1": 12.0, "hum": 40.0}
    )
    got = await _origins(storage, pref_cs)
    _check(
        results,
        "RF over filled (4b): the frame matches its RF twin, the filled row is left alone",
        got == before and [g[1] for g in got] == [None, "meshmap"],
    )

    # Item 2: station_positions must say only what the RF frame says. The filled row
    # holds hum / qfe / extras, the RF frame holds temp1 only. Kills: binding the
    # merged values (the filled hum 50 / qfe / wlevel leak into the sidebar cache).
    leak_cs = "DL7LEAK-1"
    await storage.insert_filled_telemetry(
        leak_cs,
        [_fr(_T + 30_000, temp1=10.0, hum=50.0, qfe=977.0, extras_json='{"wlevel":2.0}')],
    )
    await storage.store_telemetry(leak_cs, {"timestamp": _T, "src_type": ble, "temp1": 11.0})
    sp = await _count(
        storage,
        "SELECT temp1, hum, qfe, extras, telemetry_ts FROM station_positions WHERE callsign = ?",
        (leak_cs,),
    )
    _check(
        results,
        "RF over filled (2): station_positions carries only the RF frame's values and timestamp",
        len(sp) == 1
        and sp[0]["temp1"] == 11.0
        and sp[0]["hum"] is None
        and sp[0]["qfe"] is None
        and sp[0]["extras"] is None
        and sp[0]["telemetry_ts"] == _T,
    )
    # ...and the same frame over a PURE RF row binds merged values as before.
    rfm_cs = "DL7RFM-1"
    await storage.store_telemetry(rfm_cs, {"timestamp": _T, "src_type": ble, "hum": 40.0})
    await storage.store_telemetry(
        rfm_cs, {"timestamp": _T + 10_000, "src_type": ble, "temp1": 11.0}
    )
    sp = await _count(
        storage, "SELECT temp1, hum FROM station_positions WHERE callsign = ?", (rfm_cs,)
    )
    _check(
        results,
        "RF over RF: station_positions still carries the merged values (unchanged)",
        len(sp) == 1 and sp[0]["temp1"] == 11.0 and sp[0]["hum"] == 40.0,
    )

    # An RF row stays byte-identical in behaviour: origin NULL before and after.
    rf_cs = "DL6RF-1"
    await storage.store_telemetry(rf_cs, {"timestamp": _T, "src_type": ble, "temp1": 10.0})
    await storage.store_telemetry(rf_cs, {"timestamp": _T + 5_000, "src_type": ble, "temp1": 10.0})
    got = await _origins(storage, rf_cs)
    _check(
        results,
        "RF row: origin stays NULL through a repeat frame",
        len(got) == 1 and got[0][1] is None,
    )


async def _migration(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fill_v40.db"

        def _make_v40() -> None:
            with db_write(db_path) as conn:
                conn.executescript(CREATE_SCHEMA_SQL)
                conn.executescript(CREATE_SCHEMA_V2_SQL)
                conn.execute(
                    "CREATE TABLE telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " callsign TEXT NOT NULL, timestamp INTEGER NOT NULL, temp1 REAL,"
                    " temp2 REAL, hum REAL, hum2 REAL, qfe REAL, qnh REAL, gas INTEGER,"
                    " co2 INTEGER, alt REAL, batt INTEGER, extras TEXT)"
                )
                conn.execute(
                    "INSERT INTO telemetry (callsign, timestamp, temp1) VALUES (?, ?, ?)",
                    ("DL7OLD-1", _T, 9.0),
                )
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (40)")

        await asyncio.to_thread(_make_v40)
        storage = await create_sqlite_storage(db_path)
        try:
            ver = await _count(storage, "SELECT version FROM schema_version")
            _check(
                results,
                f"v41: marker is {LATEST_SCHEMA_VERSION} and is at least 41",
                bool(ver) and ver[0]["version"] == LATEST_SCHEMA_VERSION >= 41,
            )
            rows = await _count(storage, "SELECT origin, temp1 FROM telemetry")
            _check(
                results,
                "v41: existing telemetry rows keep origin NULL, values untouched",
                len(rows) == 1 and rows[0]["origin"] is None and rows[0]["temp1"] == 9.0,
            )
            tbl = await _count(storage, "PRAGMA table_info(telemetry_fill_state)")
            _check(
                results,
                "v41: telemetry_fill_state(callsign PK, filled_until_ms NOT NULL) exists",
                {r["name"] for r in tbl} == {"callsign", "filled_until_ms"}
                and {r["name"] for r in tbl if r["pk"]} == {"callsign"}
                and all(r["notnull"] for r in tbl if r["name"] == "filled_until_ms"),
            )
        finally:
            await storage.close()


async def run_telemetry_fill_tests() -> bool:
    """Run the telemetry gap-fill suite. Returns True iff all cases pass."""
    results: list[tuple[str, bool]] = []
    _pure(results)
    await _storage(results)
    await _migration(results)
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    telemetry_fill: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
