"""Built-in regression suite for the Node Admin storage mixin (storage/node_admin.py, schema v35).

Covers (plan doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md §4, §6.1 A2):

  1. Counter allocation: `MAX(ctr + 1, last_hwm + 1, unix_floor)` for the four
     (ctr, hwm, floor) corners, the 4294967295 refusal (which must not burn a
     value), and a read-back through a FRESH storage object. The fresh read is
     the test that catches the `_query` / `_mutate` trap: a counter bumped on a
     read connection (never commits) or returned through a rowcount-only helper
     is lost or unreadable, so two allocations must read back as ctr 2 after a
     reopen.
  2. `raise_node_admin_hwm` is a max-merge and never lowers.
  3. Deleting a key keeps the state row: a re-added key continues the counter.
  4. `apply_node_admin_reply`: a verified row is final, an unverified one is
     upgraded, and the True return happens exactly once per verification.
  5. The partial unique index: a duplicate `(target, ctr > 0)` fails, any
     number of `ctr = 0` sync rows are allowed.
  6. Migration v34 -> HEAD creates the three tables and both indexes, and the
     SQLite library supports `UPDATE ... RETURNING` (>= 3.35).
  7. Smaller contracts: hand-off stamp, history order, stale-row sweep, prune,
     target listing, `tx_max` handling.

Ephemeral tempfile SQLite DB per scenario (never the live DB). All timestamps
are milliseconds. Drives the REAL mixin methods.
"""

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import (
    CREATE_SCHEMA_SQL,
    CREATE_SCHEMA_V2_SQL,
    LATEST_SCHEMA_VERSION,
    db_read,
    db_write,
)
from .node_admin import NODE_ADMIN_CTR_MAX, NodeAdminCounterExhausted

logger = get_logger(__name__)

_BASE_TS = 1_790_000_000_000
_TARGET = "DK5EN-98"
_SRC = "DK5EN-14"


def _seed_state(db_path: Path, target: str, ctr: int, hwm: int) -> None:
    with db_write(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO node_admin_state (target_call, ctr, last_hwm) VALUES (?, ?, ?)",
            (target, ctr, hwm),
        )


def _read_state(db_path: Path, target: str) -> tuple[int, int] | None:
    """(ctr, last_hwm) read through a brand-new connection, or None."""
    with db_read(db_path) as conn:
        row = conn.execute(
            "SELECT ctr, last_hwm FROM node_admin_state WHERE target_call = ?", (target,)
        ).fetchone()
        return (row[0], row[1]) if row else None


def _count_log(db_path: Path, target: str) -> int:
    with db_read(db_path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM node_admin_log WHERE target_call = ?", (target,)
            ).fetchone()[0]
        )


async def _alloc(
    storage: Any, target: str, floor: int = 0, now: int = _BASE_TS, cmd: str = "txpower"
) -> dict[str, Any]:
    args = "15" if cmd == "txpower" else None
    line = f"{cmd} {args}" if args else cmd
    result: dict[str, Any] = await storage.allocate_node_admin_command(
        target, _SRC, cmd, args, "udp", now, floor, lambda c: f"RM1|{target}|{c}|{line}"
    )
    return result


async def _test_allocation(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_alloc.db"
        storage = await create_sqlite_storage(db_path)
        try:
            corners = [
                ("T10H50", 10, 50, 0, 51),  # hwm ahead of ctr
                ("T50H50", 50, 50, 0, 51),  # equal -> next
                ("T60H50", 60, 50, 0, 61),  # ctr ahead of hwm
                ("TFLOOR", 5, 7, 1_790_000_000, 1_790_000_000),  # floor wins when larger
                ("TLOWFL", 2_000_000_000, 7, 1_790_000_000, 2_000_000_001),  # floor smaller
            ]
            for target, ctr, hwm, floor, expected in corners:
                await asyncio.to_thread(_seed_state, db_path, target, ctr, hwm)
                got = await _alloc(storage, target, floor)
                results.append(
                    (
                        f"allocation: (ctr {ctr}, hwm {hwm}, floor {floor}) -> {expected}",
                        got["ctr"] == expected,
                    )
                )

            await _test_allocation_edges(storage, db_path, results)
        finally:
            await storage.close()
    await _test_allocation_fresh_connection(results)


async def _test_allocation_edges(
    storage: Any, db_path: Path, results: list[tuple[str, bool]]
) -> None:
    fresh = await _alloc(storage, "TFRESH")
    results.append(("allocation: unseen target starts at 1", fresh["ctr"] == 1))
    results.append(
        (
            "allocation: build_text receives the allocated ctr and its text is stored",
            fresh["text"] == "RM1|TFRESH|1|txpower 15"
            and (await storage.node_admin_history("TFRESH"))[0]["text"] == fresh["text"]
            and (await storage.node_admin_history("TFRESH"))[0]["id"] == fresh["id"],
        )
    )

    # Refusal at the u32 ceiling; the bump must roll back with it.
    await asyncio.to_thread(_seed_state, db_path, "TMAX", NODE_ADMIN_CTR_MAX - 1, 0)
    last = await _alloc(storage, "TMAX")
    results.append(("allocation: reaches 4294967295", last["ctr"] == NODE_ADMIN_CTR_MAX))
    refused = False
    try:
        await _alloc(storage, "TMAX")
    except NodeAdminCounterExhausted:
        refused = True
    results.append(("allocation: refuses past 4294967295", refused))
    state = await asyncio.to_thread(_read_state, db_path, "TMAX")
    results.append(
        (
            "allocation: a refusal rolls back (counter and log untouched)",
            state == (NODE_ADMIN_CTR_MAX, 0)
            and await asyncio.to_thread(_count_log, db_path, "TMAX") == 1,
        )
    )

    # A failing build_text must not burn a counter value either.
    def _boom(_c: int) -> str:
        raise RuntimeError("build failed")

    await asyncio.to_thread(_seed_state, db_path, "TBOOM", 3, 0)
    raised = False
    try:
        await storage.allocate_node_admin_command(
            "TBOOM", _SRC, "x", None, "udp", _BASE_TS, 0, _boom
        )
    except RuntimeError:
        raised = True
    state = await asyncio.to_thread(_read_state, db_path, "TBOOM")
    results.append(
        (
            "allocation: a build_text failure rolls the counter back",
            raised and state == (3, 0),
        )
    )


async def _test_allocation_fresh_connection(results: list[tuple[str, bool]]) -> None:
    # The commit trap: two allocations, then a FRESH storage object and a raw
    # fresh connection must both see ctr == 2.
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_fresh.db"
        storage = await create_sqlite_storage(db_path)
        try:
            first = await _alloc(storage, _TARGET)
            second = await _alloc(storage, _TARGET)
        finally:
            await storage.close()
        reopened = await create_sqlite_storage(db_path)
        try:
            targets = await reopened.list_node_admin_targets()
            history = await reopened.node_admin_history(_TARGET)
        finally:
            await reopened.close()
        raw_state = await asyncio.to_thread(_read_state, db_path, _TARGET)
        results.append(
            (
                "allocation: two allocations return 1 then 2",
                (first["ctr"], second["ctr"]) == (1, 2),
            )
        )
        results.append(
            (
                "allocation: counter 2 and both log rows survive a FRESH connection (commit trap)",
                raw_state == (2, 0)
                and [h["ctr"] for h in history] == [2, 1]
                and [t["ctr"] for t in targets if t["target"] == _TARGET] == [2],
            )
        )


async def _test_concurrent_allocation(results: list[tuple[str, bool]]) -> None:
    """Parallel allocations (separate worker threads, separate connections) must
    each get a distinct counter: the read-modify-write is one transaction."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_concurrent.db"
        storage = await create_sqlite_storage(db_path)
        try:
            await asyncio.to_thread(_seed_state, db_path, _TARGET, 100, 90)
            outcomes = await asyncio.gather(
                *(_alloc(storage, _TARGET) for _ in range(24)), return_exceptions=True
            )
            ctrs = sorted(o["ctr"] for o in outcomes if isinstance(o, dict))
            state = await asyncio.to_thread(_read_state, db_path, _TARGET)
            results.append(
                (
                    "allocation: 24 concurrent allocations get 24 distinct consecutive counters",
                    ctrs == list(range(101, 125)) and state == (124, 90),
                )
            )
        finally:
            await storage.close()


async def _test_hwm_merge(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_hwm.db"
        storage = await create_sqlite_storage(db_path)
        try:
            await storage.raise_node_admin_hwm(_TARGET, 41, sync_at_ms=_BASE_TS)
            await storage.raise_node_admin_hwm(_TARGET, 5)
            state = await asyncio.to_thread(_read_state, db_path, _TARGET)
            results.append(("hwm: 41 then 5 stays 41 (max-merge, never lowers)", state == (0, 41)))
            targets = await storage.list_node_admin_targets()
            row = next(t for t in targets if t["target"] == _TARGET)
            results.append(
                (
                    "hwm: last_sync_at stamped, unchanged by a later call without it",
                    row["last_sync_at"] == _BASE_TS and row["last_hwm"] == 41,
                )
            )
            nxt = await _alloc(storage, _TARGET)
            results.append(("hwm: next allocation is hwm + 1", nxt["ctr"] == 42))
            await storage.raise_node_admin_hwm(_TARGET, 100)
            nxt = await _alloc(storage, _TARGET)
            results.append(("hwm: a raised hwm lifts the counter past it", nxt["ctr"] == 101))
        finally:
            await storage.close()


async def _test_key_lifecycle(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_keys.db"
        storage = await create_sqlite_storage(db_path)
        try:
            results.append(
                (
                    "key: unknown target has no key",
                    await storage.get_node_admin_key(_TARGET) is None,
                )
            )
            await storage.set_node_admin_key(_TARGET, "v1:aaa")
            await storage.set_node_admin_key(_TARGET, "v1:bbb")
            results.append(
                (
                    "key: upsert replaces the token",
                    await storage.get_node_admin_key(_TARGET) == "v1:bbb",
                )
            )
            targets = await storage.list_node_admin_targets()
            row = targets[0]
            results.append(
                (
                    "key: new target listed with has_key, ctr 0 and default tx_max 15",
                    len(targets) == 1
                    and row["target"] == _TARGET
                    and row["has_key"] is True
                    and row["ctr"] == 0
                    and row["tx_max"] == 15,
                )
            )
            await storage.set_node_admin_key(_TARGET, "v1:ccc", tx_max=20)
            await storage.set_node_admin_key(_TARGET, "v1:ddd")
            row = (await storage.list_node_admin_targets())[0]
            results.append(
                ("key: tx_max set when given and left alone when omitted", row["tx_max"] == 20)
            )

            await _alloc(storage, _TARGET)
            await _alloc(storage, _TARGET)
            await storage.raise_node_admin_hwm(_TARGET, 30)
            await storage.delete_node_admin_key(_TARGET)
            results.append(
                ("key: delete removes the token", await storage.get_node_admin_key(_TARGET) is None)
            )
            listed = await storage.list_node_admin_targets()
            results.append(
                (
                    "key: delete keeps the state row (has_key False, ctr and hwm intact)",
                    len(listed) == 1
                    and listed[0]["has_key"] is False
                    and listed[0]["ctr"] == 2
                    and listed[0]["last_hwm"] == 30,
                )
            )
            results.append(
                (
                    "key: delete keeps the log",
                    len(await storage.node_admin_history(_TARGET)) == 2,
                )
            )
            await storage.set_node_admin_key(_TARGET, "v1:new")
            nxt = await _alloc(storage, _TARGET)
            results.append(
                (
                    "key: delete then re-add does NOT restart the counter (continues past hwm)",
                    nxt["ctr"] == 31,
                )
            )
            state = await asyncio.to_thread(_read_state, db_path, _TARGET)
            results.append(("key: re-add leaves the stored counter in place", state == (31, 30)))
        finally:
            await storage.close()


async def _test_apply_reply(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_reply.db"
        storage = await create_sqlite_storage(db_path)
        try:
            row_a = await _alloc(storage, _TARGET)
            first = await storage.apply_node_admin_reply(
                row_a["id"], "ok A", _BASE_TS + 1, "ok", True
            )
            late_bad = await storage.apply_node_admin_reply(
                row_a["id"], "forged", _BASE_TS + 2, "bad tag", False
            )
            again = await storage.apply_node_admin_reply(
                row_a["id"], "ok A2", _BASE_TS + 3, "ok", True
            )
            hist = {h["id"]: h for h in await storage.node_admin_history(_TARGET)}
            a = hist[row_a["id"]]
            results.append(("reply: the first verified reply returns True", first is True))
            results.append(
                (
                    "reply: a later unverified update returns False and leaves verified intact",
                    late_bad is False
                    and a["verified"] == 1
                    and a["reply_text"] == "ok A"
                    and a["result"] == "ok"
                    and a["reply_at"] == _BASE_TS + 1,
                )
            )
            results.append(
                ("reply: a second verified reply returns False (act exactly once)", again is False)
            )

            row_b = await _alloc(storage, _TARGET)
            bad = await storage.apply_node_admin_reply(
                row_b["id"], "garbled", _BASE_TS + 5, "bad tag", False
            )
            mid = {h["id"]: h for h in await storage.node_admin_history(_TARGET)}[row_b["id"]]
            good = await storage.apply_node_admin_reply(
                row_b["id"], "ok B", _BASE_TS + 6, "ok", True
            )
            end = {h["id"]: h for h in await storage.node_admin_history(_TARGET)}[row_b["id"]]
            results.append(
                (
                    "reply: unverified is recorded as verified = 0",
                    bad is True and mid["verified"] == 0 and mid["result"] == "bad tag",
                )
            )
            results.append(
                (
                    "reply: unverified then verified ends verified, True once",
                    good is True and end["verified"] == 1 and end["reply_text"] == "ok B",
                )
            )
            missing = await storage.apply_node_admin_reply(99999, "x", _BASE_TS, "ok", True)
            results.append(("reply: unknown log id returns False", missing is False))
        finally:
            await storage.close()


async def _test_unique_index(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_index.db"
        storage = await create_sqlite_storage(db_path)
        try:
            await _alloc(storage, _TARGET)  # ctr 1

            def _dup() -> bool:
                with db_write(db_path) as conn:
                    try:
                        conn.execute(
                            "INSERT INTO node_admin_log"
                            " (target_call, src_call, ctr, cmd, text, sent_at)"
                            " VALUES (?, ?, 1, 'x', 't', ?)",
                            (_TARGET, _SRC, _BASE_TS),
                        )
                    except sqlite3.IntegrityError:
                        return True
                return False

            results.append(
                ("index: duplicate (target, ctr > 0) is rejected", await asyncio.to_thread(_dup))
            )

            def _other_target() -> bool:
                with db_write(db_path) as conn:
                    conn.execute(
                        "INSERT INTO node_admin_log"
                        " (target_call, src_call, ctr, cmd, text, sent_at)"
                        " VALUES ('OTHER-1', ?, 1, 'x', 't', ?)",
                        (_SRC, _BASE_TS),
                    )
                return True

            results.append(
                (
                    "index: the same ctr on another target is allowed",
                    await asyncio.to_thread(_other_target),
                )
            )

            for i in range(3):
                await storage.insert_node_admin_sync_row(
                    _TARGET, _SRC, "udp", _BASE_TS + i, "RM1|sync"
                )
            syncs = [h for h in await storage.node_admin_history(_TARGET) if h["cmd"] == "sync"]
            state = await asyncio.to_thread(_read_state, db_path, _TARGET)
            results.append(
                (
                    "index: multiple ctr = 0 sync rows are allowed and leave the counter alone",
                    len(syncs) == 3 and all(h["ctr"] == 0 for h in syncs) and state == (1, 0),
                )
            )
        finally:
            await storage.close()


async def _test_find_log_row(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_find.db"
        storage = await create_sqlite_storage(db_path)
        try:
            a = await _alloc(storage, _TARGET, now=_BASE_TS)
            other = await _alloc(storage, "DK5EN-90", floor=5, now=_BASE_TS + 1)
            found = await storage.find_node_admin_log_row(_TARGET, a["ctr"], _BASE_TS + 5_000)
            results.append(
                (
                    "find: a counter resolves to its own target's row only",
                    found is not None
                    and found["id"] == a["id"]
                    and await storage.find_node_admin_log_row(
                        _TARGET, other["ctr"], _BASE_TS + 5_000
                    )
                    is None
                    and await storage.find_node_admin_log_row(
                        "DK5EN-90", other["ctr"], _BASE_TS + 5_000
                    )
                    is not None,
                )
            )

            # An abandoned row is still found, so a late reply can flip it.
            await storage.abandon_stale_node_admin_rows(_BASE_TS + 10_000)
            late = await storage.find_node_admin_log_row(_TARGET, a["ctr"], _BASE_TS + 5_000)
            results.append(
                (
                    "find: an abandoned row is still found (late reply after a restart)",
                    late is not None and late["result"] == "abandoned",
                )
            )

            # ctr 0 = newest sync row of the target without a reply.
            s1 = await storage.insert_node_admin_sync_row(
                _TARGET, _SRC, "ble", _BASE_TS + 20_000, "RM1 0 sync x"
            )
            s2 = await storage.insert_node_admin_sync_row(
                _TARGET, _SRC, "ble", _BASE_TS + 30_000, "RM1 0 sync y"
            )
            newest = await storage.find_node_admin_log_row(_TARGET, 0, _BASE_TS + 32_000)
            await storage.apply_node_admin_reply(s2, "ok ctr=5", _BASE_TS + 31_000, "ok", True)
            after = await storage.find_node_admin_log_row(_TARGET, 0, _BASE_TS + 32_000)
            results.append(
                (
                    "find: ctr 0 is the newest OPEN sync row, then the older one once answered",
                    newest is not None
                    and newest["id"] == s2
                    and after is not None
                    and after["id"] == s1
                    and await storage.find_node_admin_log_row("DK5EN-90", 0, _BASE_TS + 32_000)
                    is None,
                )
            )

            # W0 fix 2: ctr 0 binds only inside the 120 s reply window of its hand-off
            # (sent_at when it never got one); s1 (+20 s) is the last open sync row.
            await storage.mark_node_admin_handed_off(s1, _BASE_TS + 20_500)
            inside = await storage.find_node_admin_log_row(_TARGET, 0, _BASE_TS + 20_500 + 119_999)
            outside = await storage.find_node_admin_log_row(_TARGET, 0, _BASE_TS + 20_500 + 120_000)
            s3 = await storage.insert_node_admin_sync_row(
                _TARGET, _SRC, "ble", _BASE_TS + 500_000, "RM1 0 sync z"
            )
            never_handed = await storage.find_node_admin_log_row(
                _TARGET, 0, _BASE_TS + 500_000 + 119_999
            )
            never_handed_old = await storage.find_node_admin_log_row(
                _TARGET, 0, _BASE_TS + 500_000 + 120_000
            )
            results.append(
                (
                    (
                        "find: ctr 0 ignores an open sync row whose window (from hand-off, else "
                        "sent_at) is over: 119_999 ms binds, 120_000 ms does not"
                    ),
                    inside is not None
                    and inside["id"] == s1
                    and outside is None
                    and never_handed is not None
                    and never_handed["id"] == s3
                    and never_handed_old is None,
                )
            )

            # get-by-id finds any row, whatever its age or position in the history.
            got = await storage.get_node_admin_log_row(a["id"])
            results.append(
                (
                    "get: a row by id, None for an unknown id",
                    got is not None
                    and got["id"] == a["id"]
                    and got["ctr"] == a["ctr"]
                    and await storage.get_node_admin_log_row(999_999) is None,
                )
            )
        finally:
            await storage.close()


async def _test_verified_status_rows(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_status_rows.db"
        storage = await create_sqlite_storage(db_path)
        try:
            ok_old = await _alloc(storage, _TARGET, now=_BASE_TS, cmd="status")
            await storage.apply_node_admin_reply(
                ok_old["id"], "RM1 x", _BASE_TS + 1, "ok p=22/22", True
            )
            bad = await _alloc(storage, _TARGET, now=_BASE_TS + 10, cmd="status")
            await storage.apply_node_admin_reply(
                bad["id"], "RM1 x", _BASE_TS + 11, "ok p=1/1", False
            )
            unanswered = await _alloc(storage, _TARGET, now=_BASE_TS + 20, cmd="status")
            other_target = await _alloc(
                storage, "DK5EN-90", floor=5, now=_BASE_TS + 30, cmd="status"
            )
            await storage.apply_node_admin_reply(
                other_target["id"], "RM1 x", _BASE_TS + 31, "ok p=9/9", True
            )
            ok_new = await _alloc(storage, _TARGET, now=_BASE_TS + 40, cmd="status")
            await storage.apply_node_admin_reply(
                ok_new["id"], "RM1 x", _BASE_TS + 41, "ok p=15/20", True
            )
            rows = await storage.node_admin_verified_status_rows(_TARGET)
            results.append(
                (
                    "status rows: only this target's tag-verified status rows, newest first",
                    [r["id"] for r in rows] == [ok_new["id"], ok_old["id"]]
                    and unanswered["id"] not in [r["id"] for r in rows],
                )
            )
        finally:
            await storage.close()


async def _test_housekeeping(results: list[tuple[str, bool]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_misc.db"
        storage = await create_sqlite_storage(db_path)
        try:
            a = await _alloc(storage, _TARGET, now=_BASE_TS)
            await storage.mark_node_admin_handed_off(a["id"], _BASE_TS + 10)
            b = await _alloc(storage, _TARGET, now=_BASE_TS + 100)
            await storage.mark_node_admin_handed_off(b["id"], _BASE_TS + 110, send_error="no route")
            hist = await storage.node_admin_history(_TARGET)
            results.append(
                (
                    "log: history is newest first; hand-off and send_error stored",
                    [h["id"] for h in hist] == [b["id"], a["id"]]
                    and hist[1]["handed_off_at"] == _BASE_TS + 10
                    and hist[1]["send_error"] is None
                    and hist[0]["send_error"] == "no route",
                )
            )
            results.append(
                (
                    "log: limit is honoured and target=None spans targets",
                    len(await storage.node_admin_history(_TARGET, limit=1)) == 1
                    and len(await storage.node_admin_history(None)) == 2,
                )
            )

            # Startup sweep: only an outcome-less row older than the start.
            await storage.apply_node_admin_reply(a["id"], "ok", _BASE_TS + 20, "ok", True)
            c = await _alloc(storage, _TARGET, now=_BASE_TS + 200)
            d = await _alloc(storage, _TARGET, now=_BASE_TS + 5000)
            swept = await storage.abandon_stale_node_admin_rows(_BASE_TS + 1000)
            by_id = {h["id"]: h for h in await storage.node_admin_history(_TARGET)}
            results.append(
                (
                    "sweep: abandons stale outcome-less rows only (replied, newer ones kept)",
                    swept == 2
                    and by_id[b["id"]]["result"] == "abandoned"
                    and by_id[c["id"]]["result"] == "abandoned"
                    and by_id[a["id"]]["result"] == "ok"
                    and by_id[d["id"]]["result"] is None,
                )
            )
            late = await storage.apply_node_admin_reply(
                c["id"], "ok late", _BASE_TS + 2000, "ok", True
            )
            c_row = {h["id"]: h for h in await storage.node_admin_history(_TARGET)}[c["id"]]
            results.append(
                (
                    "sweep: a late verified reply still flips an abandoned row",
                    late is True and c_row["result"] == "ok" and c_row["verified"] == 1,
                )
            )

            for i in range(5):
                await storage.insert_node_admin_sync_row(
                    "OTHER-1", _SRC, "udp", _BASE_TS + i, "RM1|sync"
                )
            deleted = await storage.prune_node_admin_log(per_target_cap=2)
            mine = await storage.node_admin_history(_TARGET)
            other = await storage.node_admin_history("OTHER-1")
            state = await asyncio.to_thread(_read_state, db_path, _TARGET)
            results.append(
                (
                    "prune: keeps the newest N per target, counter state untouched",
                    deleted == 5
                    and [h["id"] for h in mine] == [d["id"], c["id"]]
                    and len(other) == 2
                    and state is not None
                    and state[0] == d["ctr"],
                )
            )
        finally:
            await storage.close()


async def _test_migration_from_v34(results: list[tuple[str, bool]]) -> None:
    results.append(
        (
            f"migration: LATEST_SCHEMA_VERSION is 35 (got {LATEST_SCHEMA_VERSION})",
            LATEST_SCHEMA_VERSION == 35,
        )
    )
    results.append(
        (
            f"migration: SQLite {sqlite3.sqlite_version} supports UPDATE ... RETURNING (>= 3.35)",
            sqlite3.sqlite_version_info >= (3, 35, 0),
        )
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "node_admin_v34.db"

        def _create_v34_db() -> None:
            with db_write(db_path) as conn:
                conn.executescript(CREATE_SCHEMA_SQL)
                conn.executescript(CREATE_SCHEMA_V2_SQL)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (34)")

        await asyncio.to_thread(_create_v34_db)
        try:
            storage = await create_sqlite_storage(db_path)
        except Exception:
            logger.exception("v35 node_admin migration raised")
            results.append(("migration: v34 -> HEAD runs without error", False))
            return
        try:
            version = await storage._query("SELECT version FROM schema_version LIMIT 1")
            results.append(
                (
                    "migration: v34 -> HEAD lands on the latest schema marker",
                    bool(version) and version[0]["version"] == LATEST_SCHEMA_VERSION,
                )
            )
            tables = await storage._query(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'node_admin_%'"
            )
            results.append(
                (
                    "migration: node_admin_keys, node_admin_state, node_admin_log created",
                    {t["name"] for t in tables}
                    == {"node_admin_keys", "node_admin_state", "node_admin_log"},
                )
            )
            indexes = await storage._query(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'index' AND tbl_name = 'node_admin_log'"
            )
            results.append(
                (
                    "migration: unique (target, ctr) and (target, id) indexes exist",
                    {"idx_node_admin_log_target_ctr", "idx_node_admin_log_target_id"}
                    <= {i["name"] for i in indexes},
                )
            )
            state_cols = {
                c["name"]: c for c in await storage._query("PRAGMA table_info(node_admin_state)")
            }
            results.append(
                (
                    "migration: state defaults (ctr 0, last_hwm 0, tx_max 15)",
                    state_cols["ctr"]["dflt_value"] == "0"
                    and state_cols["last_hwm"]["dflt_value"] == "0"
                    and state_cols["tx_max"]["dflt_value"] == "15",
                )
            )
        finally:
            await storage.close()


async def run_node_admin_storage_tests() -> bool:
    """Run the Node Admin storage regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    scenarios = (
        _test_allocation,
        _test_concurrent_allocation,
        _test_hwm_merge,
        _test_key_lifecycle,
        _test_apply_reply,
        _test_unique_index,
        _test_find_log_row,
        _test_verified_status_rows,
        _test_housekeeping,
        _test_migration_from_v34,
    )
    for scenario in scenarios:
        try:
            await scenario(results)
        except Exception:
            # A scenario that blows up is a failure, reported as one, not a traceback
            # that hides every other scenario's verdict.
            logger.exception("node_admin scenario %s raised", scenario.__name__)
            results.append((f"{scenario.__name__}: raised an exception", False))

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    node_admin_storage: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
