"""Regression suite for the origin axis (contract schema 2): storage + ingest + re-tag.

  1. Migration v43 -> v44: a v43 database gains `messages.origin` / `origin_rule` (NULL on old
     rows), the version is stamped, and the migration is idempotent.
  2. Ingest: a machine-flagged rule text -> machine/<rule>; `!wx` typed by a human ->
     human/default-human; a template-promoted beacon -> machine/beacon-template; the live dict
     and the history dict carry the raw `origin`/`origin_rule` keys.
  3. Degraded mode (no axes engine): machine rule and beacon template still decide, the rest
     stays NULL.
  4. `update_message_classification` / `update_classification_batch` keep a stored origin when
     the new one is None (COALESCE) and write it otherwise; `count_messages_by_origin` shape.
  5. A reclassify sweep fills `origin` from the STORED timing of rows that have none.
  6. Wire: live and history dicts carry `origin` + `topics` only (no `tags` alias, no
     `category`), and `get_conversation_summary`'s hideAutoBeacons unread subtraction works on
     a row whose stored tags are topics only.

Ephemeral tempfile SQLite only; no network.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from ..axes_contract import load_axes
from ..classifier.classify import ORIGIN_RULE_BEACON_TEMPLATE, BeaconClassification, Classifier
from ..classifier_contract import contract_id, load_engine
from ..sqlite_storage import create_sqlite_storage
from ..util import now_ms
from .constants import LATEST_SCHEMA_VERSION

_T0 = 1_800_000_000_000
_SEC = 1000
_MACHINE_TEXT = "unbekannter befehl: foo"
_RULE_TEXT = "https://github.com/icssw-org/example-repo/"  # contract vector: machine/machine-rule
_HUMAN_TEXT = "hello how are you today"

Results = list[tuple[str, bool]]


def _chat(src: str, msg_id: str, ts: int, msg: str, dst: str = "*") -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "msg": msg,
        "type": "msg",
        "msg_id": msg_id,
        "timestamp": ts,
        "src_type": "udp",
    }


async def _ingest(storage: Any, message: dict[str, Any]) -> dict[str, Any]:
    await storage.store_message(message, json.dumps(message))
    return message


async def _origin_of(storage: Any, msg_id: str) -> tuple[str | None, str | None]:
    rows = await storage._query(
        "SELECT origin, origin_rule FROM messages WHERE msg_id = ?", (msg_id,)
    )
    assert len(rows) == 1, f"expected one row for {msg_id}, got {len(rows)}"
    return rows[0]["origin"], rows[0]["origin_rule"]


async def _test_migration(results: Results, tmp: str) -> None:
    path = Path(tmp) / "v43.db"
    storage = await create_sqlite_storage(path)
    await storage.close()
    # Rewind a fresh DB to a v43 shape: drop the two columns, stamp 43, keep one old row.
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE messages DROP COLUMN origin")
        conn.execute("ALTER TABLE messages DROP COLUMN origin_rule")
        conn.execute(
            "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
            " VALUES ('OLD00001', 'DL1OLD-1', '*', 'old row', 'msg', 1)"
        )
        conn.execute("UPDATE schema_version SET version = 43")
        conn.commit()
    finally:
        conn.close()
    results.append(("migration: latest schema is at least v44", LATEST_SCHEMA_VERSION >= 44))
    for attempt in ("v43 -> v44", "re-open (idempotent)"):
        storage = await create_sqlite_storage(path)
        try:
            cols = {r["name"] for r in await storage._query("PRAGMA table_info(messages)")}
            ver = (await storage._query("SELECT version FROM schema_version"))[0]["version"]
            old = await _origin_of(storage, "OLD00001")
            results.append(
                (
                    f"migration {attempt}: columns exist, old row NULL, version stamped",
                    {"origin", "origin_rule"} <= cols
                    and old == (None, None)
                    and ver == LATEST_SCHEMA_VERSION,
                )
            )
        finally:
            await storage.close()


async def _test_ingest(results: Results, tmp: str) -> None:
    storage = await create_sqlite_storage(Path(tmp) / "ingest.db")
    engine = load_engine()
    axes = load_axes()
    classifier = Classifier(storage, engine, contract_id(engine, axes.engine), axes.engine)
    await classifier.load()
    storage.set_classifier(classifier)
    storage.set_axes(axes)
    try:
        live_machine = await _ingest(storage, _chat("DL1BOT-1", "A0000001", _T0, _MACHINE_TEXT))
        live_human = await _ingest(storage, _chat("DL1HUM-1", "A0000002", _T0 + _SEC, "!wx"))
        machine = await _origin_of(storage, "A0000001")
        human = await _origin_of(storage, "A0000002")
        results.append(
            (
                f"ingest: machine-flagged rule text -> machine/machine-rule ({machine})",
                machine == ("machine", "machine-rule"),
            )
        )
        results.append(
            (
                f"ingest: '!wx' typed by a human -> human/default-human ({human})",
                human == ("human", "default-human"),
            )
        )
        results.append(
            (
                "ingest: live dicts carry origin/origin_rule",
                (live_machine["origin"], live_machine["origin_rule"]) == machine
                and (live_human["origin"], live_human["origin_rule"]) == human,
            )
        )
        page = await storage.get_messages_page("*", before_timestamp=_T0 + 10 * _SEC)
        built = {m["msg_id"]: m for m in map(json.loads, page["messages"])}.get("A0000001", {})
        results.append(
            (
                "ingest: history dict carries origin/origin_rule",
                (built.get("origin"), built.get("origin_rule")) == machine,
            )
        )

        # Beacon template: force the L2 verdict for a human-looking text.
        async def _beacon(msg: dict[str, Any]) -> BeaconClassification:
            base = await Classifier.classify(classifier, msg)
            return BeaconClassification(
                tags=base.tags,
                info_score=base.info_score,
                template_hash=base.template_hash,
                classifier_version=base.classifier_version,
                is_beacon=True,
            )

        classifier.classify = _beacon  # type: ignore[assignment,method-assign]  # test seam
        await _ingest(storage, _chat("DL1TPL-1", "A0000003", _T0 + 2 * _SEC, "hello there friend"))
        tpl = await _origin_of(storage, "A0000003")
        results.append(
            (
                f"ingest: beacon-template verdict -> machine/{ORIGIN_RULE_BEACON_TEMPLATE} ({tpl})",
                tpl == ("machine", ORIGIN_RULE_BEACON_TEMPLATE),
            )
        )

        # Degraded: no axes engine -> machine rule / beacon template still decide.
        storage.set_axes(None)
        await _ingest(storage, _chat("DL1BOT-2", "A0000004", _T0 + 3 * _SEC, _MACHINE_TEXT))
        del classifier.classify  # back to the class method
        await _ingest(storage, _chat("DL1HUM-2", "A0000005", _T0 + 4 * _SEC, "plain human text"))
        results.append(
            (
                "degraded (no axes): machine rule decides, plain text stays NULL",
                await _origin_of(storage, "A0000004") == ("machine", "machine-rule")
                and await _origin_of(storage, "A0000005") == (None, None),
            )
        )
    finally:
        await storage.close()


async def _test_storage_writes(results: Results, tmp: str) -> None:
    storage = await create_sqlite_storage(Path(tmp) / "writes.db")
    try:
        for i in range(3):
            await storage._mutate(
                "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                " VALUES (?, 'DL1W-1', '*', 'text', 'msg', ?)",
                (f"W000000{i}", _T0 + i),
            )
        ids = [r["id"] for r in await storage._query("SELECT id FROM messages ORDER BY id")]
        kw: dict[str, Any] = {
            "tags": [],
            "info_score": 0.0,
            "template_hash": "h",
            "classifier_ver": 1,
        }
        await storage.update_message_classification(
            ids[0], origin="machine", origin_rule="machine-rule", **kw
        )
        await storage.update_message_classification(ids[0], **kw)  # None keeps the value
        kept = await _origin_of(storage, "W0000000")
        await storage.update_classification_batch(
            changed=[],
            unchanged_ids=[ids[2]],
            classifier_ver=2,
            origins={ids[1]: ("human", "default-human")},
        )
        batch = await _origin_of(storage, "W0000001")
        counts = await storage.count_messages_by_origin(0)
        results.append(
            (
                "writes: None keeps stored origin (COALESCE), batch origins are written",
                kept == ("machine", "machine-rule") and batch == ("human", "default-human"),
            )
        )
        results.append(
            (
                f"count_messages_by_origin shape {{human, machine, null}} ({counts})",
                counts == {"human": 1, "machine": 1, "null": 1},
            )
        )
    finally:
        await storage.close()


async def _test_reclassify(results: Results, tmp: str) -> None:
    storage = await create_sqlite_storage(Path(tmp) / "reclass.db")
    engine = load_engine()
    axes = load_axes()
    classifier = Classifier(storage, engine, contract_id(engine, axes.engine), axes.engine)
    await classifier.load()
    storage.set_classifier(classifier)
    try:
        rows = [
            ("R0000001", "DL1BOT-1", _MACHINE_TEXT, None),
            ("R0000002", "DL1HUM-1", "hello how are you today", "one-shot"),
            ("R0000003", "DL1CLK-1", "{CET}2026-11-21 12:00:00", "scheduled"),
        ]
        for mid, src, text, timing in rows:
            await storage._mutate(
                "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp, timing)"
                " VALUES (?, ?, '*', ?, 'msg', ?, ?)",
                (mid, src, text, _T0, timing),
            )
        job = await classifier.reclassify(force=True)
        assert job._task is not None
        await job._task
        got = [await _origin_of(storage, r[0]) for r in rows]
        results.append(
            (
                f"reclassify: fills origin on rows that had none ({got})",
                got[0] == ("machine", "machine-rule")
                and got[1] == ("human", "default-human")
                and got[2][0] == "machine",
            )
        )
    finally:
        await storage.close()


async def _test_wire_tags(results: Results, tmp: str) -> None:
    storage = await create_sqlite_storage(Path(tmp) / "wire.db")
    engine = load_engine()
    axes = load_axes()
    classifier = Classifier(storage, engine, contract_id(engine, axes.engine), axes.engine)
    await classifier.load()
    storage.set_classifier(classifier)
    storage.set_axes(axes)
    t0 = now_ms() - 600 * _SEC
    try:
        live_m = await _ingest(storage, _chat("DL1BOT-1", "B0000001", t0, _RULE_TEXT, "42"))
        live_h = await _ingest(storage, _chat("DL1HUM-1", "B0000002", t0 + _SEC, _HUMAN_TEXT, "43"))
        live_by_id = {"B0000001": live_m, "B0000002": live_h}
        for live, want, label in ((live_m, "machine", "machine"), (live_h, "human", "human")):
            results.append(
                (
                    f"wire live ({label}): origin + topics, no tags alias, no category",
                    live.get("origin") == want
                    and isinstance(live.get("topics"), list)
                    and "tags" not in live
                    and "category" not in live,
                )
            )

        for dst, msg_id, label in (("42", "B0000001", "machine"), ("43", "B0000002", "human")):
            page = await storage.get_messages_page(dst, before_timestamp=t0 + 10 * _SEC)
            hist = {m["msg_id"]: m for m in map(json.loads, page["messages"])}.get(msg_id, {})
            results.append(
                (
                    f"wire history ({label}): origin + topics, no tags alias, no category",
                    hist.get("origin") == label
                    and isinstance(hist.get("topics"), list)
                    and hist["topics"] == live_by_id[msg_id].get("topics")
                    and "tags" not in hist
                    and "category" not in hist,
                )
            )

        # Stored tags of a v8 row are topics only; the alias must come from origin.
        stored = await storage._query("SELECT tags FROM messages WHERE msg_id = 'B0000001'")
        stored_tags = json.loads(stored[0]["tags"] or "[]")
        results.append(
            (
                f"wire: stored tags of the machine row hold no retired id ({stored_tags})",
                "auto_beacon" not in stored_tags and "qso" not in stored_tags,
            )
        )

        before = await storage.get_conversation_summary("DK5EN-98")
        await storage.set_filter_prefs({"enabled": True, "hideAutoBeacons": True})
        after = await storage.get_conversation_summary("DK5EN-98")
        results.append(
            (
                f"unread: hideAutoBeacons drops the origin=machine v8 row ({before} -> {after})",
                before["42"]["unread"] == 1
                and after["42"]["unread"] == 0
                and after["42"]["count"] == 1
                and after["43"]["unread"] == 1,
            )
        )
    finally:
        await storage.close()


async def _test_ingest_timing(results: Results, tmp: str) -> None:
    """A scheduled data-format sender: timing comes from the STORED sender history and must
    reach the origin verdict (machine/data-format-timed). Guards `_origin_cols`' timing arg."""
    storage = await create_sqlite_storage(Path(tmp) / "timing.db")
    engine = load_engine()
    axes = load_axes()
    classifier = Classifier(storage, engine, contract_id(engine, axes.engine), axes.engine)
    await classifier.load()
    storage.set_classifier(classifier)
    storage.set_axes(axes)
    try:
        for i in range(12):  # one WX-style data text every 600 s
            text = f"T 21.{i} H 6{i % 10} P 101{i % 10}"
            await _ingest(storage, _chat("DL1WX-1", f"S{i:07d}", _T0 + i * 600 * _SEC, text))
        rows = await storage._query(
            "SELECT timing, origin, origin_rule FROM messages WHERE msg_id = 'S0000011'"
        )
        got = (rows[0]["timing"], rows[0]["origin"], rows[0]["origin_rule"])
        results.append(
            (
                f"ingest: scheduled data-format sender -> machine/data-format-timed ({got})",
                got == ("scheduled", "machine", "data-format-timed"),
            )
        )
    finally:
        await storage.close()


async def _drain(classifier: Classifier) -> None:
    for job in list(classifier._jobs.values()):
        if job._task is not None:
            await job._task


async def _test_startup_axes_failure(results: Results, tmp: str) -> None:
    """Boot 1: the axes backfill raises -> no sweep, no marker. Boot 2 (axes OK) must then
    sweep and yield the ingest-equivalent origin (machine/data-format-timed). Before the fix
    boot 1 swept from NULL timing, set the marker, and boot 2 left origin human."""
    from ..main import run_classifier_startup  # local: main pulls the whole app graph

    storage = await create_sqlite_storage(Path(tmp) / "startup.db")
    engine = load_engine()
    axes = load_axes()
    cid = contract_id(engine, axes.engine)
    try:
        for i in range(12):  # pre-v44 rows of a 600 s scheduled data sender
            await storage._mutate(
                "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                " VALUES (?, 'DL1WX-1', '*', ?, 'msg', ?)",
                (f"W{i:07d}", f"T 21.{i} H 6{i % 10} P 101{i % 10}", _T0 + i * 600 * _SEC),
            )
        c1 = Classifier(storage, engine, cid, axes.engine)
        await c1.load()
        storage.set_classifier(c1)
        storage.set_axes(axes)
        real = storage.backfill_message_axes

        async def boom(**_: Any) -> int | None:
            raise RuntimeError("database is locked")

        storage.backfill_message_axes = boom  # type: ignore[method-assign]
        promo_calls: list[int] = []
        real_promo = storage.backfill_auto_beacon_templates

        async def promo(*a: Any, **k: Any) -> int:
            promo_calls.append(1)
            return await real_promo(*a, **k)

        storage.backfill_auto_beacon_templates = promo  # type: ignore[method-assign]
        await run_classifier_startup(c1, storage, None)
        results.append(
            (
                "startup: failed axes backfill still runs the template promotion backfill",
                promo_calls == [1],
            )
        )
        await _drain(c1)
        marker = await storage.get_meta(f"backfill_done:v{c1.version}")
        results.append(("startup: failed axes backfill sets no sweep marker", marker is None))
        storage.backfill_message_axes = real  # type: ignore[method-assign]
        c2 = Classifier(storage, engine, cid, axes.engine)
        await c2.load()
        storage.set_classifier(c2)
        await run_classifier_startup(c2, storage, None)
        await _drain(c2)
        got = await _origin_of(storage, "W0000011")
        results.append(
            (
                f"startup: boot after a failed axes backfill redoes both ({got})",
                got == ("machine", "data-format-timed"),
            )
        )
        results.append(
            (
                "startup: marker set once the sweep finished",
                await storage.get_meta(f"backfill_done:v{c2.version}") is not None,
            )
        )
    finally:
        await storage.close()


async def _test_startup_marker_on_finish(results: Results, tmp: str) -> None:
    """The sweep marker means FINISHED: a sweep still running when startup is cancelled
    (shutdown) must leave no marker."""
    import asyncio

    from ..main import run_classifier_startup

    storage = await create_sqlite_storage(Path(tmp) / "marker.db")
    engine = load_engine()
    axes = load_axes()
    classifier = Classifier(storage, engine, contract_id(engine, axes.engine), axes.engine)
    await classifier.load()
    storage.set_classifier(classifier)
    storage.set_axes(axes)
    try:
        await storage._mutate(
            "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
            " VALUES ('M0000001', 'DL1HUM-1', '*', 'hello there', 'msg', ?)",
            (_T0,),
        )
        gate = asyncio.Event()
        real = classifier._reclassify_batch

        async def slow(*a: Any, **k: Any) -> Any:
            await gate.wait()  # never released: the sweep is "killed mid-run"
            return await real(*a, **k)

        classifier._reclassify_batch = slow  # type: ignore[method-assign]
        task = asyncio.create_task(run_classifier_startup(classifier, storage, None))
        # Poll until the sweep job exists, so the cancel really lands mid-sweep (a fixed
        # sleep could cancel before reclassify() scheduled it and pass vacuously).
        for _ in range(200):
            if classifier._jobs:
                break
            await asyncio.sleep(0.01)
        results.append(
            ("startup: sweep job was scheduled before the cancel", bool(classifier._jobs))
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        marker = await storage.get_meta(f"backfill_done:v{classifier.version}")
        results.append(("startup: cancelled mid-sweep leaves no marker", marker is None))
        for job in list(classifier._jobs.values()):
            if job._task is not None:
                job._task.cancel()
    finally:
        await storage.close()


async def run_origin_tests() -> bool:
    """Run the origin-axis suite. Returns True iff every case passes."""
    results: Results = []
    with tempfile.TemporaryDirectory() as tmp:
        await _test_migration(results, tmp)
        await _test_ingest(results, tmp)
        await _test_storage_writes(results, tmp)
        await _test_reclassify(results, tmp)
        await _test_ingest_timing(results, tmp)
        await _test_startup_axes_failure(results, tmp)
        await _test_startup_marker_on_finish(results, tmp)
        await _test_wire_tags(results, tmp)
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    passed = all(ok for _, ok in results)
    print(f"  origin: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import asyncio

    raise SystemExit(0 if asyncio.run(run_origin_tests()) else 1)
