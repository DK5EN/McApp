"""Storage suite for `update_classification_batch` (mc-chat 5f43b21).

Pins: equivalence with repeated `update_message_classification`, changed rows get
the full column set while unchanged rows only get the version stamp, empty input
is a no-op, and a mid-batch failure leaves nothing half written (one write unit,
one commit). Ephemeral tempfile SQLite only; no network.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from ..sqlite_storage import create_sqlite_storage

_COLS = "id, category, tags, info_score, template_hash, classifier_ver"


async def _seed(storage: Any, n: int = 4) -> list[int]:
    for i in range(n):
        await storage._mutate(
            "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, category, tags,"
            " info_score, template_hash, classifier_ver)"
            " VALUES (?, 'DK5EN-1', '20', 'x', 'msg', ?, 'old', '[\"old\"]', 0.1, 'oldh', 1)",
            (f"A{i}", 1_700_000_000_000 + i),
        )
    rows = await storage._query("SELECT id FROM messages ORDER BY id")
    return [int(r["id"]) for r in rows]


async def _snapshot(storage: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await storage._query(
        f"SELECT {_COLS} FROM messages ORDER BY id"  # noqa: S608
    )
    return rows


def _changed(ids: list[int]) -> list[tuple[str, str, float, str, int, int]]:
    return [
        ("qso", json.dumps(["qso", "directed"]), 0.8, "h-a", 7, ids[0]),
        ("bot", json.dumps(["bot"]), 0.2, "h-b", 7, ids[1]),
    ]


async def _test_equivalence(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        a = await create_sqlite_storage(Path(tmp) / "a.db")
        b = await create_sqlite_storage(Path(tmp) / "b.db")
        try:
            ids_a = await _seed(a)
            ids_b = await _seed(b)
            changed = _changed(ids_a)
            unchanged = ids_a[2:]
            try:
                await a.update_classification_batch(
                    changed=changed, unchanged_ids=unchanged, classifier_ver=7
                )
            except AttributeError:
                results.append(("batch: method exists", False))
                return
            for cat, tags_json, score, th, ver, rid in _changed(ids_b):
                await b.update_message_classification(
                    rid,
                    category=cat,
                    tags=json.loads(tags_json),
                    info_score=score,
                    template_hash=th,
                    classifier_ver=ver,
                )
            for rid in ids_b[2:]:
                await b._mutate("UPDATE messages SET classifier_ver = ? WHERE id = ?", (7, rid))
            snap_a, snap_b = await _snapshot(a), await _snapshot(b)
            results.append(("batch == repeated update_message_classification", snap_a == snap_b))
            results.append(
                (
                    "changed rows get full values",
                    snap_a[0]["category"] == "qso"
                    and json.loads(snap_a[0]["tags"]) == ["qso", "directed"]
                    and snap_a[0]["template_hash"] == "h-a"
                    and snap_a[0]["classifier_ver"] == 7,
                )
            )
            results.append(
                (
                    "unchanged rows only get the version",
                    all(
                        r["category"] == "old"
                        and r["tags"] == '["old"]'
                        and r["template_hash"] == "oldh"
                        and r["info_score"] == 0.1
                        and r["classifier_ver"] == 7
                        for r in snap_a[2:]
                    ),
                )
            )
        finally:
            await a.close()
            await b.close()


async def _test_empty_noop(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = await create_sqlite_storage(Path(tmp) / "e.db")
        try:
            await _seed(s)
            before = await _snapshot(s)
            try:
                await s.update_classification_batch(changed=[], unchanged_ids=[], classifier_ver=9)
                ok = await _snapshot(s) == before
            except AttributeError:
                ok = False
            results.append(("batch: empty lists are a no-op", ok))
            try:
                ids = [r["id"] for r in before]
                await s.update_classification_batch(
                    changed=[], unchanged_ids=ids[:1], classifier_ver=9
                )
                after = await _snapshot(s)
                ok = after[0]["classifier_ver"] == 9 and after[1:] == before[1:]
            except AttributeError:
                ok = False
            results.append(("batch: only unchanged_ids stamps just those rows", ok))
        finally:
            await s.close()


async def _test_atomic_rollback(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = await create_sqlite_storage(Path(tmp) / "r.db")
        try:
            ids = await _seed(s)
            await s._mutate(
                f"CREATE TRIGGER boom BEFORE UPDATE ON messages WHEN NEW.id = {int(ids[3])} "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
            before = await _snapshot(s)
            raised = False
            try:
                await s.update_classification_batch(
                    changed=_changed(ids), unchanged_ids=[ids[2], ids[3]], classifier_ver=7
                )
            except AttributeError:
                pass
            except Exception:
                raised = True
            results.append(("batch: failure raises and rolls everything back", raised))
            results.append(
                ("batch: no half-written rows after failure", await _snapshot(s) == before)
            )
            await s._mutate("DROP TRIGGER boom")
            await s.update_classification_batch(
                changed=[], unchanged_ids=[ids[0]], classifier_ver=8
            )
            results.append(
                (
                    "batch: storage usable after rollback",
                    (await _snapshot(s))[0]["classifier_ver"] == 8,
                )
            )
        finally:
            await s.close()


async def run_classifier_batch_tests() -> bool:
    """Run the classification-batch storage suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []
    await _test_equivalence(results)
    await _test_empty_noop(results)
    try:
        await _test_atomic_rollback(results)
    except AttributeError:
        results.append(("batch: rollback suite (method missing)", False))
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    passed = all(ok for _, ok in results)
    print(f"  classifier_batch: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import sys

    sys.exit(0 if asyncio.run(run_classifier_batch_tests()) else 1)
