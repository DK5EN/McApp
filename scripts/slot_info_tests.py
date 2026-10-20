"""Startup suite for the schema verdict in SSEManager.read_slot_info.

Activating a slot whose code carries an older DB schema than the live DB
crash-loops mcapp (schema 38 dropped classifier_rules). The API exposes the
verdict so the webapp can disable Activate: top-level ``db_schema_version``,
per slot ``schema_version`` and ``blocked_reason``; a blocked slot is never
the rollback target.
"""

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from mcapp.sse_handler import SSEManager

_failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        _failures.append(name)


def _make_slot(slots: Path, n: int, constants: str | None, deployed_at: str | None) -> None:
    if constants is not None:
        d = slots / f"slot-{n}" / "src" / "mcapp" / "storage"
        d.mkdir(parents=True)
        (d / "constants.py").write_text(constants)
    if deployed_at is not None:
        meta = slots / "meta"
        meta.mkdir(exist_ok=True)
        (meta / f"slot-{n}.json").write_text(
            json.dumps({"slot": n, "version": f"v{n}", "deployed_at": deployed_at})
        )


def _make_db(path: Path, version: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
    conn.commit()
    conn.close()


def _info(slots: Path, db: Path) -> dict[str, Any]:
    mgr = SSEManager.__new__(SSEManager)
    return mgr.read_slot_info(slots_dir=slots, db_path=db)


def run_slot_info_tests() -> bool:
    _failures.clear()
    print("slot info schema verdict")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        slots = root / "mcapp-slots"
        # slot-0 active (38), slot-1 older (37, newest deploy), slot-2 typed 38 older deploy
        _make_slot(slots, 0, "LATEST_SCHEMA_VERSION = 38\n", "2026-10-01")
        _make_slot(slots, 1, "X = 1\nLATEST_SCHEMA_VERSION = 37  # c\n", "2026-10-10")
        _make_slot(slots, 2, "LATEST_SCHEMA_VERSION: int = 38\n", "2026-10-05")
        (slots / "current").symlink_to(slots / "slot-0")
        db = root / "messages.db"
        _make_db(db, 38)

        r = _info(slots, db)
        s = {x["slot"]: x for x in r["slots"]}
        _check(
            "db_schema_version", r.get("db_schema_version") == 38, str(r.get("db_schema_version"))
        )
        _check(
            "schema_version 38 / 37 / typed 38",
            (s[0].get("schema_version"), s[1].get("schema_version"), s[2].get("schema_version"))
            == (38, 37, 38),
        )
        _check("active slot not blocked", s[0].get("blocked_reason") is None)
        _check(
            "older slot blocked",
            s[1].get("blocked_reason") == "slot-1 schema 37 is older than the database schema 38",
            str(s[1].get("blocked_reason")),
        )
        _check("equal schema not blocked", s[2].get("blocked_reason") is None)
        _check(
            "rollback skips blocked slot",
            r["rollback_target"] == 2 and r["can_rollback"] is True,
            str(r.get("rollback_target")),
        )

        # Only candidate blocked -> no rollback.
        (slots / "meta" / "slot-2.json").unlink()
        r = _info(slots, db)
        _check(
            "no unblocked candidate", r["rollback_target"] is None and r["can_rollback"] is False
        )

        # Missing constants file -> unreadable.
        shutil.rmtree(slots / "slot-2")
        _make_slot(slots, 2, None, "2026-10-05")
        r = _info(slots, db)
        s = {x["slot"]: x for x in r["slots"]}
        _check(
            "missing file unreadable",
            s[2].get("schema_version") is None
            and s[2].get("blocked_reason") == "slot-2 schema version unreadable",
            str(s[2]),
        )

        # Empty slot: keys present, not blocked.
        empty = slots / "meta" / "slot-1.json"
        empty.unlink()
        r = _info(slots, db)
        s = {x["slot"]: x for x in r["slots"]}
        _check(
            "empty slot unblocked",
            s[1]["status"] == "empty"
            and s[1].get("blocked_reason") is None
            and "schema_version" in s[1],
        )

        # Missing DB -> null, nothing blocked by schema.
        r = _info(slots, root / "absent.db")
        s = {x["slot"]: x for x in r["slots"]}
        _check(
            "missing db -> null and not blocked",
            r.get("db_schema_version") is None and s[0].get("blocked_reason") is None,
        )
        _check("missing db did not create file", not (root / "absent.db").exists())

    if _failures:
        print(f"\n  {len(_failures)} check(s) failed")
    return not _failures


if __name__ == "__main__":
    raise SystemExit(0 if run_slot_info_tests() else 1)
