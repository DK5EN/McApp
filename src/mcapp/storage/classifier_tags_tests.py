"""Storage-level regression suite for the classifier unification (tags).

Pins the MCProxy side of mc-chat 4300263 (doc/2026-10-20_classifier-unification.md):

  1. `count_messages_by_tag` (incl. malformed `tags` JSON never raising).
  2. `backfill_auto_beacon_templates`: single-sender templates only, on every arm.
  3. `clear_stale_auto_beacons`: a message tagged `alert` exempts its template.
  4. `set_filter_prefs` maps a legacy `hiddenCategories` write to `hiddenTags`.
  5. The v38 migration: custom classifier rules are saved to
     `classifier_rules_retired.json` next to the DB (never overwritten, WARNING
     logged), the table is dropped, stored `hiddenCategories` becomes
     `hiddenTags` (junk-tolerant, merging, idempotent).

Ephemeral tempfile SQLite only; no network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import (
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    LATEST_SCHEMA_VERSION,
    db_write,
)

_HOUR_MS = 3_600_000


def _zulu(ms: int) -> str:
    from ..classifier.types import ms_to_zulu

    return ms_to_zulu(ms)


async def _add_template(
    storage: Any, hash_: str, srcs: list[str], count: int, age_h: float, **extra: Any
) -> None:
    """Insert a beacon_templates row directly (count/last_seen fully controlled)."""
    await storage._mutate(
        "INSERT INTO beacon_templates (template_hash, example_msg, example_src, srcs, count,"
        " first_seen, last_seen, auto_beacon, user_action) VALUES (?, 'm', ?, ?, ?, ?, ?, 0, ?)",
        (
            hash_,
            srcs[0],
            json.dumps(srcs),
            count,
            _zulu(now_ms() - int((age_h + 1) * _HOUR_MS)),
            _zulu(now_ms() - int(age_h * _HOUR_MS)),
            extra.get("user_action"),
        ),
    )


async def _add_message(storage: Any, hash_: str, category: str, tags: Any) -> None:
    await storage._mutate(
        "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, template_hash,"
        " category, tags) VALUES ('A1', 'DK5EN-1', '20', 'x', 'msg', ?, ?, ?, ?)",
        (now_ms(), hash_, category, tags),
    )


async def _flagged(storage: Any) -> set[str]:
    rows = await storage._query("SELECT template_hash FROM beacon_templates WHERE auto_beacon = 1")
    return {r["template_hash"] for r in rows}


async def _test_count_by_tag(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "t.db")
        try:
            await _add_message(storage, "h", "qso", json.dumps(["qso", "greeting"]))
            await _add_message(storage, "h", "other", json.dumps(["greeting"]))
            await _add_message(storage, "h", "other", "not-json{")
            await _add_message(storage, "h", "other", None)
            try:
                counts = await storage.count_messages_by_tag(0)
            except AttributeError:
                counts = {}
            results.append(
                (
                    "count_messages_by_tag: per-tag counts, malformed/NULL tags skipped",
                    counts == {"qso": 1, "greeting": 2},
                )
            )
            try:
                future = await storage.count_messages_by_tag(now_ms() + 10 * _HOUR_MS)
            except AttributeError:
                future = {"missing": 1}
            results.append(("count_messages_by_tag: window excludes older rows", future == {}))
        finally:
            await storage.close()


async def _test_backfill(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "t.db")
        try:
            await _add_template(storage, "solo_life", ["A"], 8, 500)
            await _add_template(storage, "solo_24h", ["A"], 5, 1)
            await _add_template(storage, "solo_72h", ["A"], 3, 48)
            await _add_template(storage, "solo_cold", ["A"], 3, 200)
            await _add_template(storage, "multi_life", ["A", "B", "C"], 9, 500)
            await _add_template(storage, "multi_24h", ["A", "B"], 5, 1)
            await _add_template(storage, "multi_72h", ["A", "B"], 3, 48)
            await _add_template(storage, "demoted", ["A"], 9, 1, user_action="demote")
            try:
                n = await storage.backfill_auto_beacon_templates()
            except AttributeError:
                n = -1
            results.append(("backfill: returns the number newly flagged (3)", n == 3))
            results.append(
                (
                    (
                        "backfill: single-sender templates flagged on every arm; "
                        "multi-sender and demoted ones stay conversation"
                    ),
                    await _flagged(storage) == {"solo_life", "solo_24h", "solo_72h"},
                )
            )
            results.append(
                (
                    "backfill: idempotent (second run flags nothing)",
                    await storage.backfill_auto_beacon_templates() == 0,
                )
            )
        finally:
            await storage.close()


async def _test_clear_stale(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "t.db")
        try:
            long_msg = "one two three four five six seven eight"
            for h in ("alerted", "directed", "plain", "badtags"):
                await _add_template(storage, h, ["A"], 9, 1)
                await storage._mutate(
                    "UPDATE beacon_templates SET auto_beacon = 1, example_msg = ?"
                    " WHERE template_hash = ?",
                    (long_msg, h),
                )
            await _add_message(storage, "alerted", "other", json.dumps(["alert", "weather"]))
            await _add_message(storage, "directed", "directed", json.dumps([]))
            await _add_message(storage, "plain", "other", json.dumps(["time"]))
            await _add_message(storage, "badtags", "other", "not-json{")
            cleared = await storage.clear_stale_auto_beacons(frozenset({"directed", "alert"}), 3)
            results.append(
                (
                    "clear_stale: alert-tagged and directed templates are un-flagged only",
                    cleared == 2 and await _flagged(storage) == {"plain", "badtags"},
                )
            )
        finally:
            await storage.close()


async def _test_prefs_write(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "t.db")
        try:
            posted = {
                "enabled": True,
                "hiddenCategories": ["timestamp_beacon", "directed", 5, "bot_command", "qso"],
            }
            await storage.set_filter_prefs(posted)
            prefs = await storage.get_filter_prefs()
            results.append(
                (
                    "set_filter_prefs: legacy hiddenCategories -> hiddenTags, key removed",
                    "hiddenCategories" not in prefs
                    and prefs.get("hiddenTags") == ["time", "bot", "qso"]
                    and prefs.get("enabled") is True,
                )
            )
            results.append(
                ("set_filter_prefs: caller's dict is not mutated", "hiddenCategories" in posted)
            )
            await storage.set_filter_prefs({"hiddenTags": ["node"], "hiddenCategories": ["alert"]})
            prefs = await storage.get_filter_prefs()
            results.append(
                (
                    "set_filter_prefs: legacy list merges into an existing hiddenTags",
                    prefs.get("hiddenTags") == ["node", "alert"],
                )
            )
            await storage.set_filter_prefs({"hiddenTags": ["greeting"]})
            prefs = await storage.get_filter_prefs()
            results.append(
                (
                    "set_filter_prefs: plain hiddenTags write passes through",
                    prefs["hiddenTags"] == ["greeting"],
                )
            )
        finally:
            await storage.close()


def _make_v37(db_path: Path, rules: list[tuple[str, int]], prefs: str | None) -> None:
    with db_write(db_path) as conn:
        conn.executescript(CREATE_SCHEMA_SQL)
        conn.executescript(CREATE_SCHEMA_V2_SQL)
        conn.execute(
            "CREATE TABLE classifier_rules (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL, pattern TEXT NOT NULL,"
            " scope TEXT NOT NULL DEFAULT 'msg', category TEXT NOT NULL,"
            " extra_tags TEXT, priority INTEGER NOT NULL DEFAULT 100, enabled INTEGER NOT NULL"
            " DEFAULT 1, builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        for name, builtin in rules:
            conn.execute(
                "INSERT INTO classifier_rules (name, pattern, category, builtin, created_at,"
                " updated_at) VALUES (?, 'x', 'greeting', ?, 't', 't')",
                (name, builtin),
            )
        conn.execute(
            "CREATE TABLE filter_prefs (id INTEGER PRIMARY KEY CHECK (id = 1),"
            " prefs TEXT NOT NULL DEFAULT '{}', updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        if prefs is not None:
            conn.execute("INSERT INTO filter_prefs (id, prefs) VALUES (1, ?)", (prefs,))
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (37)")
        conn.commit()


def _glob(folder: str, pattern: str) -> list[Path]:
    return sorted(Path(folder).glob(pattern))


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def _migrate(db_path: Path) -> tuple[Any, _Capture]:
    cap = _Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    try:
        storage = await create_sqlite_storage(db_path)
    finally:
        root.removeHandler(cap)
    return storage, cap


async def _table_exists(storage: Any, name: str) -> bool:
    rows = await storage._query(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return bool(rows)


async def _test_migration(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        prefs = json.dumps(
            {
                "enabled": True,
                "hiddenCategories": ["wx_beacon", "directed", "junk", 3, "qso"],
                "hiddenTags": ["time"],
            }
        )
        await asyncio.to_thread(
            _make_v37, db, [("builtin-a", 1), ("mine-1", 0), ("mine-2", 0)], prefs
        )
        backup = Path(tmp) / "classifier_rules_retired.json"
        backup.write_text("PRECIOUS")
        storage, cap = await _migrate(db)
        try:
            ver = (await storage._query("SELECT version FROM schema_version"))[0]["version"]
            results.append(
                (
                    f"v38: schema_version is LATEST ({LATEST_SCHEMA_VERSION}) and > 37",
                    ver == LATEST_SCHEMA_VERSION and ver > 37,
                )
            )
            results.append(
                (
                    "v38: classifier_rules table dropped",
                    not await _table_exists(storage, "classifier_rules"),
                )
            )
            results.append(
                ("v38: existing backup never overwritten", backup.read_text() == "PRECIOUS")
            )
            stamped = _glob(tmp, "classifier_rules_retired.*.json")
            saved: list[dict[str, Any]] = (
                json.loads(stamped[0].read_text()) if len(stamped) == 1 else []
            )
            results.append(
                (
                    "v38: custom (builtin=0) rules only, to a timestamp-suffixed file",
                    sorted(r["name"] for r in saved) == ["mine-1", "mine-2"],
                )
            )
            results.append(
                (
                    "v38: WARNING names the retired-rule count",
                    any("retired 2 custom classifier rules" in r.getMessage() for r in cap.records),
                )
            )
            p = await storage.get_filter_prefs()
            results.append(
                (
                    "v38: hiddenCategories -> hiddenTags, merged after existing, junk dropped",
                    "hiddenCategories" not in p
                    and p.get("hiddenTags") == ["time", "weather", "qso"]
                    and p.get("enabled") is True,
                )
            )
        finally:
            await storage.close()

        # Re-open: idempotent, nothing re-written.
        before = _glob(tmp, "*.json")
        storage = await create_sqlite_storage(db)
        try:
            results.append(
                (
                    "v38: re-open is a no-op (no new files, prefs unchanged)",
                    _glob(tmp, "*.json") == before
                    and (await storage.get_filter_prefs()).get("hiddenTags")
                    == ["time", "weather", "qso"],
                )
            )
        finally:
            await storage.close()

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        await asyncio.to_thread(_make_v37, db, [("builtin-a", 1)], "not-json{")
        try:
            storage, _ = await _migrate(db)
            ok = not await _table_exists(storage, "classifier_rules")
            await storage.close()
        except Exception:
            ok = False
        results.append(
            (
                "v38: builtin-only rules, malformed prefs -> no file, no raise",
                ok and not _glob(tmp, "*.json"),
            )
        )

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        await asyncio.to_thread(_make_v37, db, [("mine", 0)], None)
        storage, _ = await _migrate(db)
        try:
            first = Path(tmp) / "classifier_rules_retired.json"
            results.append(("v38: first backup uses the plain file name", first.exists()))
        finally:
            await storage.close()


async def run_classifier_tags_tests() -> bool:
    """Run the classifier-tags storage suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []
    await _test_count_by_tag(results)
    await _test_backfill(results)
    await _test_clear_stale(results)
    await _test_prefs_write(results)
    await _test_migration(results)
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    passed = all(ok for _, ok in results)
    print(f"  classifier_tags: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import sys

    sys.exit(0 if asyncio.run(run_classifier_tags_tests()) else 1)
