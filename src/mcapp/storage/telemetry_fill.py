"""Pure merge logic for the telemetry gap fill from meshmap.

Plan: doc/archive/2026-11-07_1000-telemetry-gap-fill-plan.md ("Rules for the fill",
"Field mapping"). No I/O, no clock: callers pass `now_ms` and the local
timestamps. Meshmap `timestamp` is UNIX SECONDS; everything this module
returns is in milliseconds, like the rest of the database.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .constants import _QFE_PLAUSIBLE_HPA_RANGE, _QNH_PLAUSIBLE_HPA_RANGE

# Central rows closer than this to the last KEPT row are twins (hub re-emits). The
# same bound applies against rows already filled (a rerun, or a chunk boundary).
FILL_TWIN_WINDOW_S = 60
# An RF row within this distance is "the same sample": RF timestamps are arrival
# times and run up to about 14 min late. Exactly this far counts as a match. Filled
# rows are NOT matched with this window: a 5 min cadence would swallow itself.
FILL_MATCH_WINDOW_MS = 15 * 60 * 1000
# Only samples at least this old are filled, so a late RF copy can arrive first.
FILL_SETTLE_MS = 30 * 60 * 1000
# Value of `telemetry.origin` for filled rows (NULL = RF).
ORIGIN_MESHMAP = "meshmap"

# SQLite INTEGER is a signed 64-bit value; a larger Python int raises at bind time.
_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)


@dataclass(frozen=True)
class FillRow:
    """One central sample mapped onto `telemetry` columns."""

    timestamp_ms: int
    temp1: float | None
    temp2: float | None
    hum: float | None
    qfe: float | None
    qnh: float | None
    alt: float | None
    gas: float | None
    co2: int | None
    batt: int | None
    extras_json: str | None


def _num(value: object) -> float | None:
    """A finite real number, else None. A bool is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except OverflowError:  # a JSON integer beyond the float range
        return None
    return f if math.isfinite(f) else None


def _reading(value: object) -> float | None:
    """A sensor reading: finite, and not an exact 0.

    Meshmap relays the Extern-UDP `tele` JSON, where a zero-initialised firmware
    field means "no sensor fitted" (DF2KX-12, DG7RJ-8, DM6CS-12 send temp2=0 on every
    row). The RF path treats 0 as absent for the same reason (`_wire_reading`).
    """
    f = _num(value)
    return None if f is None or f == 0 else f


def _int(value: object) -> int | None:
    """A `_reading` rounded to an int that SQLite can store, else None."""
    f = _reading(value)
    if f is None:
        return None
    n = round(f)
    return n if _INT64_MIN <= n <= _INT64_MAX else None


def _in_range(value: float | None, bounds: tuple[int, int]) -> float | None:
    return value if value is not None and bounds[0] <= value <= bounds[1] else None


def _timestamp_ms(entry: Mapping[str, Any]) -> int | None:
    """The entry's `timestamp` (seconds) as positive int64 milliseconds, else None."""
    ts = _num(entry.get("timestamp"))
    if ts is None or ts <= 0 or ts > _INT64_MAX / 1000:
        return None
    ms = round(ts * 1000)
    return ms if 0 < ms <= _INT64_MAX else None


def collapse_twins(
    entries: Sequence[Mapping[str, Any]], window_s: int = FILL_TWIN_WINDOW_S
) -> list[Mapping[str, Any]]:
    """Sort by `timestamp` (seconds) and keep the first of any run of twins.

    A row is dropped when it is closer than `window_s` to the last KEPT row
    (not to its predecessor), so a chain of 3 rows 40 s apart keeps rows 1 and 3.
    Entries with a missing or non-finite timestamp are dropped, never raised.
    """
    valid: list[tuple[float, Mapping[str, Any]]] = []
    for entry in entries:
        ts = _num(entry.get("timestamp"))
        if ts is not None:
            valid.append((ts, entry))
    valid.sort(key=lambda pair: pair[0])
    kept: list[Mapping[str, Any]] = []
    last_kept: float | None = None
    for ts, entry in valid:
        if last_kept is not None and ts - last_kept < window_s:
            continue
        kept.append(entry)
        last_kept = ts
    return kept


def map_entry(entry: Mapping[str, Any]) -> FillRow | None:
    """Map a meshmap entry per the plan's field table; None if nothing usable.

    An exact 0 is absent for every field except `alt` (see `_reading`). `qfe` and
    `qnh` outside the RF plausibility ranges are absent. An integer SQLite cannot
    store is absent, and a timestamp that is not positive int64 ms drops the entry,
    so nothing returned here can raise at insert time. `qnhReduced` is never stored.
    `wlevel`/`wtemp` go to extras JSON only when present. `alt` alone is not a
    sensor reading, so it does not make a row.
    """
    ts_ms = _timestamp_ms(entry)
    if ts_ms is None:
        return None
    extras: dict[str, float] = {}
    for key in ("wlevel", "wtemp"):
        v = _reading(entry.get(key))
        if v is not None:
            extras[key] = v
    temp1 = _reading(entry.get("temp"))
    temp2 = _reading(entry.get("temp2"))
    hum = _reading(entry.get("hum"))
    qfe = _in_range(_reading(entry.get("press")), _QFE_PLAUSIBLE_HPA_RANGE)
    qnh = _in_range(_reading(entry.get("qnh")), _QNH_PLAUSIBLE_HPA_RANGE)
    alt = _num(entry.get("alt"))
    gas = _reading(entry.get("gas_res"))
    co2 = _int(entry.get("eco2"))
    batt = _int(entry.get("batt"))
    sensors = (temp1, temp2, hum, qfe, qnh, gas, co2, batt)
    if all(v is None for v in sensors) and not extras:
        return None
    return FillRow(
        timestamp_ms=ts_ms,
        temp1=temp1,
        temp2=temp2,
        hum=hum,
        qfe=qfe,
        qnh=qnh,
        alt=alt,
        gas=gas,
        co2=co2,
        batt=batt,
        extras_json=json.dumps(extras, separators=(",", ":")) if extras else None,
    )


def _nearest_distance(sorted_ts: Sequence[int], ts: int) -> int | None:
    """Distance from `ts` to its nearest neighbour in an ascending list, or None."""
    i = bisect_left(sorted_ts, ts)
    return min(
        (abs(sorted_ts[j] - ts) for j in (i - 1, i) if 0 <= j < len(sorted_ts)),
        default=None,
    )


def select_fill_rows(
    entries: Sequence[Mapping[str, Any]],
    rf_timestamps_ms: Sequence[int],
    filled_timestamps_ms: Sequence[int],
    now_ms: int,
) -> list[FillRow]:
    """Map, drop unusable entries, collapse twins, then keep settled, unmatched rows.

    Mapping comes BEFORE the collapse, so an entry with no usable sensor cannot
    absorb the real sample a few seconds later. A row is dropped when an RF row is
    within `FILL_MATCH_WINDOW_MS` (inclusive), or a filled row is closer than
    `FILL_TWIN_WINDOW_S` (a rerun, or a chunk boundary).
    """
    rf = sorted(rf_timestamps_ms)
    filled = sorted(filled_timestamps_ms)
    twin_ms = FILL_TWIN_WINDOW_S * 1000
    # A filled row is a KEPT twin from an earlier chunk: entries in its shadow go
    # before the collapse, or a blocked entry would swallow the next real sample.
    usable: list[Mapping[str, Any]] = []
    for e in entries:
        mapped = map_entry(e)
        if mapped is None:
            continue
        near = _nearest_distance(filled, mapped.timestamp_ms)
        if near is None or near >= twin_ms:
            usable.append(e)
    out: list[FillRow] = []
    for entry in collapse_twins(usable):
        row = map_entry(entry)
        if row is None or now_ms - row.timestamp_ms < FILL_SETTLE_MS:
            continue
        near_rf = _nearest_distance(rf, row.timestamp_ms)
        if near_rf is not None and near_rf <= FILL_MATCH_WINDOW_MS:
            continue
        near_filled = _nearest_distance(filled, row.timestamp_ms)
        if near_filled is not None and near_filled < twin_ms:
            continue
        out.append(row)
    return out
