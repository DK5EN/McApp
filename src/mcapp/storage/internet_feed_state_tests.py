"""Regression suite for the internet feed state mixin (storage/internet_feed_state.py, schema v36).

Ephemeral tempfile SQLite DB built by the REAL migration chain (never the live DB).
Drives the mixin through the production SQLiteStorage, so it also proves the wiring.
"""

import tempfile
from pathlib import Path

from ..logging_setup import get_logger
from ..sqlite_storage import SQLiteStorage

logger = get_logger(__name__)


_Storage = SQLiteStorage


async def run_internet_feed_state_tests() -> bool:
    """Run the suite. Returns True iff all cases pass."""
    results: list[tuple[str, bool]] = []
    try:
        await _scenarios(results)
    except Exception:
        logger.exception("internet_feed_state scenarios raised")
        results.append(("internet_feed_state: scenarios ran without raising", False))

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    internet_feed_state: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


async def _raises_value_error(storage: _Storage, **fields: object) -> bool:
    try:
        await storage.update_internet_feed_state(**fields)
    except ValueError:
        return True
    return False


async def _scenarios(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "internet_feed_state.db"
        storage = _Storage(db_path)
        await storage.initialize()
        try:
            state = await storage.get_internet_feed_state()
            results.append(
                (
                    "defaults: off, unconfigured, no groups, broadcast on, pos off, 300 km",
                    state["enabled"] is False
                    and state["configured"] is False
                    and state["groups"] == []
                    and state["broadcast"] is True
                    and state["pos_mode"] == "off"
                    and state["radius_km"] == 300
                    and state["token_enc"] is None,
                )
            )

            await storage.update_internet_feed_state(
                enabled=True,
                configured=True,
                groups=["20", "262"],
                broadcast=False,
                radius_km=150,
                box_id="abc",
                last_error="boom",
                last_error_ms=123,
            )
            state = await storage.get_internet_feed_state()
            results.append(
                (
                    "roundtrip: bools, groups list, ints and text read back",
                    state["enabled"] is True
                    and state["configured"] is True
                    and state["groups"] == ["20", "262"]
                    and state["broadcast"] is False
                    and state["radius_km"] == 150
                    and state["box_id"] == "abc"
                    and state["last_error"] == "boom"
                    and state["last_error_ms"] == 123,
                )
            )

            await storage.update_internet_feed_state(cursor="c1")
            state = await storage.get_internet_feed_state()
            results.append(
                (
                    "partial update leaves other columns alone",
                    state["cursor"] == "c1" and state["groups"] == ["20", "262"],
                )
            )

            results.append(
                (
                    "unknown column rejected",
                    await _raises_value_error(storage, nonsense=1),
                )
            )
            injection = "enabled = 0, token_enc = 'x' --"
            rejected = await _raises_value_error(storage, **{injection: 1})
            state = await storage.get_internet_feed_state()
            results.append(
                (
                    "column-name injection rejected and nothing written",
                    rejected and state["enabled"] is True and state["token_enc"] is None,
                )
            )
        finally:
            await storage.close()
