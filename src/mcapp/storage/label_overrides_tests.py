"""Regression suite for operator overrides (migration 39, label_overrides.py).

Covers: both tables exist at LATEST_SCHEMA_VERSION; group set/get/revert and the
all-None delete; per-field callsign merge with the `manual` flag; an
override-only callsign appearing; unchanged payload without overrides; length
limits; a QRZ upsert after an override leaving the override intact.

Ephemeral temp DB, offline. All timestamps are milliseconds.
"""

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from ..sqlite_storage import create_sqlite_storage
from .constants import LATEST_SCHEMA_VERSION

_LEGACY_KEYS = {"first_name", "qth", "country", "lat", "lon", "locator"}


async def _raises_value_error(coro_factory: object) -> bool:
    try:
        await coro_factory()  # type: ignore[operator]  # test helper takes a zero-arg coroutine factory
    except ValueError:
        return True
    return False


async def _limit_cases(storage: Any, results: list[tuple[str, bool]]) -> None:
    # Limits
    results.append(
        (
            "prefix over 12 raises",
            await _raises_value_error(
                lambda: storage.set_group_label_override("1", "x" * 13, None, None)
            ),
        )
    )
    results.append(
        (
            "hint over 40 raises",
            await _raises_value_error(
                lambda: storage.set_group_label_override("1", None, "x" * 41, None)
            ),
        )
    )
    results.append(
        (
            "remark over 80 raises",
            await _raises_value_error(
                lambda: storage.set_group_label_override("1", None, None, "x" * 81)
            ),
        )
    )
    results.append(
        (
            "first_name over 30 raises",
            await _raises_value_error(
                lambda: storage.set_callsign_override("DK5EN", "x" * 31, None)
            ),
        )
    )
    results.append(
        (
            "qth over 40 raises",
            await _raises_value_error(
                lambda: storage.set_callsign_override("DK5EN", None, "x" * 41)
            ),
        )
    )
    results.append(
        (
            "nothing was written by failures",
            await storage.get_group_label_overrides() == {}
            and await storage.get_callsign_overrides() == {},
        )
    )
    # Exactly-at-limit values are legal (a `>=` check would 422 a 12-char prefix).
    await storage.set_group_label_override("99999", "p" * 12, "h" * 40, "r" * 80)
    await storage.set_callsign_override("DL9ZZZ", "f" * 30, "q" * 40)
    at_limit_ok = (await storage.get_group_label_overrides()).get("99999") == {
        "prefix": "p" * 12,
        "hint": "h" * 40,
        "remark": "r" * 80,
    } and (await storage.get_callsign_overrides()).get("DL9ZZZ") == {
        "first_name": "f" * 30,
        "qth": "q" * 40,
    }
    results.append(("limits are inclusive (exact-limit values are stored)", at_limit_ok))
    await storage.delete_group_label_override("99999")
    await storage.delete_callsign_override("DL9ZZZ")


async def _callsign_cases(storage: Any, results: list[tuple[str, bool]]) -> None:
    # Callsign map: no override -> legacy payload
    await storage.upsert_callsign_info(
        "DK5EN",
        "found",
        1_790_000_000_000,
        first_name="Martin",
        qth="Munich",
        country="Germany",
        lat=48.1,
        lon=11.5,
        locator="JN58",
    )
    plain = (await storage.get_callsign_info_map())["DK5EN"]
    results.append(
        (
            "no-override payload has the legacy keys only",
            set(plain) == _LEGACY_KEYS and plain["first_name"] == "Martin",
        )
    )

    # Per-field merge (SSID in the call is normalised to base)
    await storage.set_callsign_override("dk5en-98", "Marty", None)
    merged = (await storage.get_callsign_info_map())["DK5EN"]
    results.append(
        (
            "override wins per field, other fields inherit, manual set",
            merged["first_name"] == "Marty"
            and merged["qth"] == "Munich"
            and merged["country"] == "Germany"
            and merged["locator"] == "JN58"
            and merged["manual"] is True,
        )
    )
    single = await storage.get_callsign_info_merged("DK5EN-14")
    results.append(("single merged getter agrees with the map", single == merged))

    # QRZ refresh after an override keeps the override
    await storage.upsert_callsign_info(
        "DK5EN", "found", 1_790_000_100_000, first_name="Martin", qth="Berlin"
    )
    after = (await storage.get_callsign_info_map())["DK5EN"]
    results.append(
        (
            "QRZ upsert does not erase the override",
            after["first_name"] == "Marty" and after["qth"] == "Berlin" and after["manual"],
        )
    )

    # Override on a callsign whose QRZ row is unusable (not_found, country only):
    # the row's country survives and the map agrees with the single getter.
    await storage.upsert_callsign_info("OE1XYZ", "not_found", 1_790_000_200_000, country="Austria")
    await storage.set_callsign_override("OE1XYZ", "Hans", None)
    nf_map = (await storage.get_callsign_info_map()).get("OE1XYZ")
    nf_single = await storage.get_callsign_info_merged("OE1XYZ")
    results.append(
        (
            "override on an unusable QRZ row keeps its country; map == single getter",
            nf_map is not None
            and nf_map == nf_single
            and nf_map["country"] == "Austria"
            and nf_map["first_name"] == "Hans"
            and nf_map["manual"] is True,
        )
    )

    # Override-only callsign
    await storage.set_callsign_override("DL1ABC", None, "Hamburg")
    only = (await storage.get_callsign_info_map())["DL1ABC"]
    results.append(
        (
            "override-only callsign appears with null country",
            only["qth"] == "Hamburg"
            and only["first_name"] is None
            and only["country"] is None
            and only["manual"] is True,
        )
    )
    results.append(
        (
            "override-only single getter works; unknown call is None",
            (await storage.get_callsign_info_merged("DL1ABC")) == only
            and await storage.get_callsign_info_merged("DB0NOPE") is None,
        )
    )

    # Revert
    await storage.set_callsign_override("DL1ABC", "", None)
    await storage.delete_callsign_override("DK5EN")
    final = await storage.get_callsign_info_map()
    results.append(
        (
            "revert restores upstream and drops override-only rows",
            "DL1ABC" not in final
            and "manual" not in final["DK5EN"]
            and final["DK5EN"]["first_name"] == "Martin",
        )
    )


async def run_label_overrides_tests() -> bool:
    """Run the override regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "label_overrides_test.db"
        storage = await create_sqlite_storage(db_path)
        try:
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            finally:
                conn.close()
            results.append(
                (
                    "migration creates both override tables",
                    {"group_label_overrides", "callsign_info_overrides"} <= tables,
                )
            )
            results.append(("LATEST_SCHEMA_VERSION is 39 or later", LATEST_SCHEMA_VERSION >= 39))

            # groups: set, get, partial, revert
            await storage.set_group_label_override("262", " DL-X ", "", "  note ")
            got = await storage.get_group_label_overrides()
            results.append(
                (
                    "group set trims and maps empty to None",
                    got == {"262": {"prefix": "DL-X", "hint": None, "remark": "note"}},
                )
            )
            await storage.set_group_label_override("262", "P2", None, None)
            got = await storage.get_group_label_overrides()
            results.append(
                (
                    "group set replaces all fields",
                    got["262"] == {"prefix": "P2", "hint": None, "remark": None},
                )
            )
            await storage.set_group_label_override("262", None, "", "   ")
            results.append(
                (
                    "all-None group set deletes the row",
                    await storage.get_group_label_overrides() == {},
                )
            )
            await storage.set_group_label_override("#OE-SOTA", "S", None, None)
            await storage.delete_group_label_override("#OE-SOTA")
            results.append(
                ("group delete reverts", await storage.get_group_label_overrides() == {})
            )

            await _limit_cases(storage, results)
            await _callsign_cases(storage, results)
        finally:
            await storage.close()

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    label_overrides: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
