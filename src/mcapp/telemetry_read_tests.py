"""Built-in suite for the columnar WX telemetry reads, Wave 5 (plan
doc/2026-11-07_1400-telemetry-downsampling-plan.md, "Wire contract").

Fully offline: a temp DB built through the real storage initialize, rows inserted
directly. Every case names the rule whose removal it catches.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import patch

from starlette.testclient import TestClient

from .main import MessageRouter
from .sqlite_storage import create_sqlite_storage
from .sse_handler import SSEManager
from .storage.query import _tm_bucketed_sql

_H = 3_600_000
_FIELDS = ("temp1", "temp2", "hum", "hum2", "qfe", "qnh", "gas", "alt")

Row = tuple[str, int, dict[str, Any], str | None]


def _insert(db: Path, rows: list[Row]) -> None:
    conn = sqlite3.connect(db)
    try:
        for cs, ts, vals, origin in rows:
            cols = ["callsign", "timestamp", "origin", *vals]
            conn.execute(
                f"INSERT INTO telemetry ({', '.join(cols)})"  # noqa: S608 - fixed test columns
                f" VALUES ({', '.join('?' * len(cols))})",
                [cs, ts, origin, *vals.values()],
            )
        conn.commit()
    finally:
        conn.close()


def _check(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    results.append((label, ok))


def _decode(body: bytes) -> dict[str, Any]:
    out = json.loads(body)
    assert isinstance(out, dict)
    return out


async def _cases(results: list[tuple[str, bool]], storage: Any, db: Path) -> None:  # noqa: PLR0915
    now = int(time.time() * 1000)
    b0 = (now - 100 * _H) // 7_200_000 * 7_200_000  # a 2 h bucket start, ~100 h back
    # RAW / station filter -------------------------------------------------
    _insert(
        db,
        [
            ("DL1RAW-1", now - 3 * _H, {"temp1": 10.5, "hum": 50.0, "alt": 100.0}, None),
            ("DL1RAW-1", now - 2 * _H, {"temp1": None}, None),  # all NULL: dropped
            ("DL1RAW-1", now - 1 * _H, {"temp1": 11.0, "qfe": 990.0}, "meshmap"),
            ("DL1ALT-1", now - 2 * _H, {"alt": 300.0, "batt": 90}, None),
            ("DL1ZER-1", now - 2 * _H, {"temp2": 0, "alt": 5.0}, None),  # zero is no sensor
            ("DL1MM-1", now - 2 * _H, {"temp1": 7.0}, "meshmap"),
        ],
    )
    raw = _decode(await storage.get_telemetry_chart_data(hours=48))
    st = raw["stations"]
    r = st.get("DL1RAW-1", {})
    _check(results, "raw: bucket_s 0", raw["bucket_s"] == 0)
    _check(
        results,
        "raw: ts ascending, all-NULL row dropped",
        r.get("ts") == [now - 3 * _H, now - 1 * _H],
    )
    _check(results, "raw: temp1 array", r.get("temp1") == [10.5, 11.0])
    _check(results, "raw: sparse field keeps nulls", r.get("hum") == [50.0, None])
    _check(results, "raw: origin 1 only for meshmap rows", r.get("origin") == [0, 1])
    _check(
        results,
        "raw: field without any value omitted; co2/batt/extras never sent",
        "temp2" not in r and "co2" not in r and "batt" not in r and "extras" not in r,
    )
    _check(results, "raw: no stats, no n", "stats" not in r and "n" not in r)
    _check(results, "filter: alt+batt-only station dropped", "DL1ALT-1" not in st)
    _check(results, "filter: zero-only WX value does not count", "DL1ZER-1" not in st)
    _check(
        results,
        "filter: station whose only WX value is a meshmap row is kept",
        st.get("DL1MM-1", {}).get("origin") == [1],
    )
    # MEAN BUCKETS ---------------------------------------------------------
    _insert(
        db,
        [
            ("DL2MEAN-1", b0 + 1000, {"temp1": 10.0, "temp2": 4.0, "hum2": 0, "gas": 0}, None),
            ("DL2MEAN-1", b0 + 2000, {"temp1": 11.0, "temp2": 0, "hum2": 40.0, "gas": 100}, None),
            ("DL2MEAN-1", b0 + 3000, {"temp1": 12.5}, "meshmap"),
            ("DL2ALLMM-1", b0 + 1000, {"temp1": 1.0}, "meshmap"),
            ("DL2ALLMM-1", b0 + 2000, {"temp1": 2.0}, "meshmap"),
            ("DL2MEAN-1", b0 + 7_200_000 + 1000, {"temp1": 20.0}, "meshmap"),
        ],
    )
    m = _decode(await storage.get_telemetry_chart_data(hours=744))
    s = m["stations"].get("DL2MEAN-1", {})
    _check(results, "mean: bucket_s 7200 beyond 192 h", m["bucket_s"] == 7200)
    _check(results, "mean: bucket start ts", s.get("ts") == [b0, b0 + 7_200_000])
    _check(results, "mean: temp1 ROUND(AVG, 2)", s.get("temp1") == [11.17, 20.0])
    _check(results, "mean: zeros ignored for temp2", s.get("temp2") == [4.0, None])
    _check(results, "mean: zeros ignored for hum2", s.get("hum2") == [40.0, None])
    _check(results, "mean: zeros ignored for gas", s.get("gas") == [100.0, None])
    _check(results, "mean: n counts raw rows", s.get("n") == [3, 1])
    _check(results, "mean: mixed bucket origin 0, all-meshmap bucket 1", s.get("origin") == [0, 1])
    _check(
        results,
        "mean: all-meshmap bucket origin 1",
        m["stations"].get("DL2ALLMM-1", {}).get("origin") == [1],
    )
    _check(
        results,
        "mean: 192 h is still raw",
        _decode(await storage.get_telemetry_chart_data(hours=192))["bucket_s"] == 0,
    )
    # STATS ------------------------------------------------------------------
    # DL3ODD: 5 values (old 3, recent 2); DL3EVN: 4 values; temp1 order mixed on purpose.
    odd = [9.0, 1.0, 5.0, 7.0, 3.0]
    evn = [4.0, 2.0, 8.0, 6.0]
    rows: list[Row] = [
        ("DL3ODD-1", now - (60 + i) * _H, {"temp1": v}, None) for i, v in enumerate(odd[:3])
    ]
    rows += [("DL3ODD-1", now - (10 + i) * _H, {"temp1": v}, None) for i, v in enumerate(odd[3:])]
    rows += [
        ("DL3EVN-1", now - (20 + i) * _H, {"temp1": v, "temp2": 0}, None) for i, v in enumerate(evn)
    ]
    rows.append(("DL3EVN-1", now - 20 * _H, {"hum": 40.0}, None))  # same ts as a temp1 row
    # temp2-only and gas-only rows outside the temp1 span: they feed their own stats but
    # not count/first/last, which mirror the webapp's temp/hum/qfe/qnh/alt union.
    rows.append(("DL3ODD-1", now - 70 * _H, {"temp2": 12.5}, None))
    rows.append(("DL3ODD-1", now - 5 * _H, {"gas": 100}, None))
    _insert(db, rows)
    a = _decode(await storage.get_telemetry_chart_data(hours=744))["stations"]
    so = a["DL3ODD-1"]["stats"]
    _check(
        results,
        "stats: count of distinct timestamps",
        so["count"] == 5 and a["DL3EVN-1"]["stats"]["count"] == 4,
    )
    _check(
        results,
        "stats: first/last ms",
        so["first_ms"] == now - 62 * _H and so["last_ms"] == now - 10 * _H,
    )
    _check(
        results,
        "stats: temp2/gas-only rows have stats but stay out of count",
        so.get("temp2") == [12.5, 12.5, 12.5] and so.get("gas") == [100, 100, 100],
    )
    _check(results, "stats: odd count median", so["temp1"] == [1.0, 5.0, 9.0])
    _check(
        results,
        "stats: even count -> lower middle",
        a["DL3EVN-1"]["stats"]["temp1"] == [2.0, 4.0, 8.0],
    )
    _check(
        results,
        "stats: zero-ignored field without value omitted",
        "temp2" not in a["DL3EVN-1"]["stats"],
    )
    _check(
        results, "stats: single-value field", a["DL3EVN-1"]["stats"]["hum"] == [40.0, 40.0, 40.0]
    )
    n = _decode(await storage.get_telemetry_chart_data(hours=744, stats_hours=24))["stations"]
    _check(
        results,
        "stats_hours narrows the stats window only",
        n["DL3ODD-1"]["stats"]["count"] == 2 and n["DL3ODD-1"]["stats"]["temp1"] == [3.0, 3.0, 7.0],
    )
    # A station whose stats window holds only temp2/gas rows (outside count fields) has no
    # stats key at all; removing the `if st is None` guard in _tm_stats crashes the read.
    _insert(
        db,
        [
            ("DL3SEN-1", now - 30 * _H, {"temp2": 4.5}, None),
            ("DL3SEN-1", now - 29 * _H, {"gas": 120.0}, None),
        ],
    )
    sen = _decode(await storage.get_telemetry_chart_data(hours=744))["stations"].get("DL3SEN-1")
    _check(
        results,
        "stats: station with only temp2/gas rows is listed without a stats key",
        sen is not None and "stats" not in sen and sen.get("temp2") is not None,
    )
    # YEARLY ---------------------------------------------------------------
    y0 = (now - 200 * _H) // 14_400_000 * 14_400_000
    _insert(
        db,
        [
            ("DL4YR-1", y0 + 1000, {"temp1": 3.0, "temp2": 0, "alt": 100.0, "qnh": 1000.0}, None),
            ("DL4YR-1", y0 + 2000, {"temp1": -2.0, "temp2": 6.0, "alt": 120.0}, "meshmap"),
            ("DL4YR-1", y0 + 14_400_000 + 1000, {"temp1": 9.0}, "meshmap"),
            ("DL4ALT-1", y0 + 1000, {"alt": 1.0, "batt": 3}, None),
        ],
    )
    y = _decode(await storage.get_telemetry_chart_data_bucketed())
    ys = y["stations"].get("DL4YR-1", {})
    _check(results, "yearly: bucket_s 14400", y["bucket_s"] == 14400)
    _check(results, "yearly: ts bucket start", ys.get("ts") == [y0, y0 + 14_400_000])
    _check(
        results,
        "yearly: temp1 min/max unrounded",
        ys.get("temp1_min") == [-2.0, 9.0] and ys.get("temp1_max") == [3.0, 9.0],
    )
    _check(results, "yearly: temp2 zero ignored", ys.get("temp2_min") == [6.0, None])
    _check(
        results,
        "yearly: alt min/max",
        ys.get("alt_min") == [100.0, None] and ys.get("alt_max") == [120.0, None],
    )
    _check(
        results, "yearly: qnh has no min/max series", "qnh_min" not in ys and "qnh_max" not in ys
    )
    _check(results, "yearly: n and origin", ys.get("n") == [2, 1] and ys.get("origin") == [0, 1])
    _check(results, "yearly: no stats (the view has no stats table)", "stats" not in ys)
    _check(results, "yearly: alt-only station dropped", "DL4ALT-1" not in y["stations"])
    # INDEX / PLAN (migration 42) ---------------------------------------------
    conn = sqlite3.connect(db)
    try:
        names = {r[1] for r in conn.execute("PRAGMA index_list(telemetry)")}
        _check(
            results,
            "migration 42: idx_telemetry_cs_b2h and idx_telemetry_cs_b4h exist",
            {"idx_telemetry_cs_b2h", "idx_telemetry_cs_b4h"} <= names,
        )
        conn.execute("CREATE TEMP TABLE _st AS SELECT DISTINCT callsign FROM telemetry")
        for bucket_s, index in ((7200, "idx_telemetry_cs_b2h"), (14400, "idx_telemetry_cs_b4h")):
            plan = list(
                conn.execute("EXPLAIN QUERY PLAN " + _tm_bucketed_sql(bucket_s).replace("?", "0"))
            )
            # Only the inner bucket GROUP BY is pinned: SQLite before ~3.50 (the Pi has
            # 3.46.1) still sorts the outer per-callsign GROUP BY over the co-routine.
            inner = {r[0] for r in plan if str(r[3]).startswith("CO-ROUTINE")}
            for r in plan:
                if r[1] in inner:
                    inner.add(r[0])
            inner_rows = [str(r[3]) for r in plan if r[1] in inner]
            _check(
                results,
                f"plan: bucket {bucket_s} s read uses {index}",
                any(f"USING INDEX {index}" in d for d in inner_rows),
            )
            _check(
                results,
                f"plan: bucket {bucket_s} s inner GROUP BY has no temp B-tree",
                not any("TEMP B-TREE FOR GROUP BY" in d for d in inner_rows),
            )
    finally:
        conn.close()
    # ARRAY ORDER: arrays carry no aggregate ORDER BY and rely on the inner ORDER BY
    # streaming into the outer GROUP BY. Rows inserted out of timestamp order must still
    # come back ascending, on every read shape.
    shuffled = [7, 2, 9, 0, 5, 3, 8, 1, 6, 4]
    _insert(db, [("DL9ORD-1", now - (2 + k * 5) * _H, {"temp1": float(k)}, None) for k in shuffled])
    for label, body in (
        ("raw 192 h", await storage.get_telemetry_chart_data(192)),
        ("2 h means", await storage.get_telemetry_chart_data(744)),
        ("yearly", await storage.get_telemetry_chart_data_bucketed()),
    ):
        ts = _decode(body)["stations"].get("DL9ORD-1", {}).get("ts", [])
        _check(
            results,
            f"order: {label} ts strictly ascending for shuffled inserts",
            len(ts) >= 5 and all(a < b for a, b in pairwise(ts)),
        )
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM telemetry WHERE callsign = 'DL9ORD-1'")
        conn.commit()
    finally:
        conn.close()
    # ROUTES -----------------------------------------------------------------
    with patch.dict(os.environ, {"MESHCOM_VAPID_PATH": str(db.parent / "vapid.json")}):
        router = MessageRouter(storage)
        router.set_callsign("DK5EN-98")
        manager = SSEManager("127.0.0.1", 0, message_router=router)
        client = TestClient(manager._create_app(), raise_server_exceptions=False)
        r1 = client.get("/api/telemetry?hours=48")
        _check(
            results,
            "route: /api/telemetry is application/json with the raw shape",
            r1.status_code == 200
            and r1.headers["content-type"].startswith("application/json")
            and r1.json()["bucket_s"] == 0
            and "DL1RAW-1" in r1.json()["stations"],
        )
        r2 = client.get("/api/telemetry?hours=99999&stats_hours=24")
        _check(
            results,
            "route: hours clamped to 744, stats_hours honoured",
            r2.json()["bucket_s"] == 7200
            and r2.json()["stations"]["DL3ODD-1"]["stats"]["count"] == 2,
        )
        r3 = client.get("/api/telemetry/yearly")
        _check(
            results,
            "route: yearly is application/json with bucket_s 14400",
            r3.status_code == 200
            and r3.headers["content-type"].startswith("application/json")
            and r3.json()["bucket_s"] == 14400,
        )


async def run_telemetry_read_tests() -> bool:
    """Run the columnar telemetry read suite. Returns True iff all cases pass."""
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "read.db"
        storage = await create_sqlite_storage(db)
        try:
            await _cases(results, storage, db)
        finally:
            await storage.close()
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    telemetry_read: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
