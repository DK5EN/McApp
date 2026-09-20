"""Regression suite for the live-broadcast classifier annotation
(`store_message` in `storage/ingest.py`).

Backend half of doc/2026-09-20_1000-live-classifier-and-dedup-plan.md F1 —
`MessageRouter.publish` hands ONE `data` dict to every "mesh_message"
subscriber in subscription order; `main.py:_storage_handler` (subscribed
first, in `MessageRouter.__init__`) classifies inside `store_message` and
used to keep the result to itself as INSERT parameters only, so
`SSEManager._broadcast_handler` (subscribed later, when the manager is
built) broadcast the message UNclassified. `store_message` now annotates
the shared `message` dict in place, using the exact presence rules
`_build_message_dict` applies to a stored row (both funnel through the one
`_classifier_fields` helper in `ingest.py`), so a live view and a reloaded
view of the same message cannot disagree by construction.

Follows the house pattern used by `storage.suppression_tests` /
`storage.ingest_dedup_tests`: a `(label, ok)` results list, a PASS/FAIL line
per case, and a bool return. Ephemeral tempfile SQLite DB per case, driving
the REAL `store_message` — no TTY, no network, no `/etc/mcapp`.

Cases:
  1. A classified chat message: the CALLER'S dict (not a copy) gains all
     five fields, and each value equals what `_build_message_dict` produces
     from the row that was just stored -- pins "live == history" against the
     stored row, not against a hardcoded constant.
  2. The live `tags` value is a Python `list`; the SQLite `tags` COLUMN is
     still the JSON string `store_message` has always written there.
  3. A classifier that raises: the message gains none of the five keys, and
     the row is still stored -- ingestion is never blocked (ADR invariant).
  4. A classifier returning `category=None` and empty `tags`/`template_hash`/
     `info_score`/`classifier_version=None`: no key is injected for any of
     them (presence rules, not truthiness-free defaults).
  5. Transport-duplicate second copy: the second `store_message` call for
     the same `(src, msg_id)` inside `DEDUP_WINDOW_MS` returns at the dedup
     gate and its OWN dict is left unannotated. Documented, accepted
     behaviour (plan doc F1, "Deliberately NOT covered") -- pinned here so a
     future change to it is a decision, not an accident.
  6. Subscriber ordering: in a real `MessageRouter` + `SSEManager` pair, the
     storage handler's index precedes the SSE broadcast handler's in BOTH
     `_subscribers["mesh_message"]` (the UDP-fed topic) AND
     `_subscribers["ble_notification"]` (what a BLE-only box carries live
     traffic on instead, under the identical constructor-order invariant).
     This is the invariant the whole feature rests on -- if it ever inverts
     on either topic, the SSE payload silently loses the classifier fields
     again, on whichever transport that box actually uses.
  7. The stored `raw_json` column for a classified message contains no
     `category` key, proving `raw_json` (serialized by the real caller,
     `main.py:_storage_handler`, BEFORE `store_message` runs) is unaffected
     by the in-place annotation.

All timestamps are milliseconds (project-wide DB convention).
"""

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..main import MessageRouter
from ..sqlite_storage import create_sqlite_storage
from ..sse_handler import SSEManager
from .constants import DEDUP_WINDOW_MS

_T0 = 1_800_000_000_000
_GAP_MS = 107  # arbitrary sub-window gap, mirrors ingest_dedup_tests' _GAP_MS


@dataclass
class _FakeClassification:
    """Duck-typed stand-in for `classifier.types.Classification`.

    `store_message` only reads `.category`/`.tags`/`.info_score`/
    `.template_hash`/`.classifier_version` off whatever its `_classifier`
    (typed `Any`) returns, so this suite never has to import from
    `src/mcapp/classifier/` -- a vendored subtree that must never be edited
    in place and need not be touched to test this call site.
    """

    category: str | None
    tags: tuple[str, ...]
    info_score: float | None
    template_hash: str
    classifier_version: int | None


class _FakeClassifier:
    """Minimal `classify()` stand-in: returns a fixed result or raises."""

    def __init__(
        self, result: _FakeClassification | None = None, exc: Exception | None = None
    ) -> None:
        self._result = result
        self._exc = exc

    async def classify(self, _payload: dict[str, Any]) -> _FakeClassification:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _chat(src: str, msg_id: str, ts: int, **extra: Any) -> dict[str, Any]:
    return {
        "src": src,
        "dst": "*",
        "msg": "--- live classification test message",
        "type": "msg",
        "msg_id": msg_id,
        "timestamp": ts,
        "src_type": "udp",
        **extra,
    }


async def _with_storage(name: str) -> tuple[Any, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    storage = await create_sqlite_storage(Path(tmp.name) / f"{name}.db")
    return storage, tmp


async def _stored_row(storage: Any, msg_id: str) -> dict[str, Any]:
    rows = await storage._query("SELECT * FROM messages WHERE msg_id = ?", (msg_id,))
    assert len(rows) == 1, f"expected exactly one stored row for msg_id={msg_id}, got {len(rows)}"
    return dict(rows[0])


async def _test_classified_message_matches_history(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("live_cls_match")
    try:
        storage.set_classifier(
            _FakeClassifier(
                _FakeClassification(
                    category="chatter",
                    tags=("greeting", "short"),
                    info_score=0.42,
                    template_hash="abcdef012345",
                    classifier_version=7,
                )
            )
        )
        message = _chat("DK5EN-98", "A0000001", _T0)
        raw = json.dumps(message)
        await storage.store_message(message, raw)

        row = await _stored_row(storage, "A0000001")
        built = storage._build_message_dict(row)

        results.append(
            (
                "live dict carries category/tags/info_score/template_hash/classifier_ver",
                all(
                    key in message
                    for key in (
                        "category",
                        "tags",
                        "info_score",
                        "template_hash",
                        "classifier_ver",
                    )
                ),
            )
        )
        results.append(
            (
                "live == history: category",
                message.get("category") == built.get("category") == "chatter",
            )
        )
        expected_tags = ["greeting", "short"]
        results.append(
            (
                "live == history: tags",
                message.get("tags") == built.get("tags") == expected_tags,
            )
        )
        results.append(
            (
                "live == history: info_score",
                message.get("info_score") == built.get("info_score") == 0.42,
            )
        )
        results.append(
            (
                "live == history: template_hash",
                message.get("template_hash") == built.get("template_hash") == "abcdef012345",
            )
        )
        results.append(
            (
                "live == history: classifier_ver",
                message.get("classifier_ver") == built.get("classifier_ver") == 7,
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_tags_shape_live_vs_column(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("live_cls_tags_shape")
    try:
        storage.set_classifier(
            _FakeClassifier(
                _FakeClassification(
                    category="info",
                    tags=("weather", "beacon"),
                    info_score=0.9,
                    template_hash="0123456789ab",
                    classifier_version=1,
                )
            )
        )
        message = _chat("DK5EN-98", "A0000002", _T0)
        await storage.store_message(message, json.dumps(message))

        row = await _stored_row(storage, "A0000002")
        results.append(
            ("live message['tags'] is a Python list", isinstance(message.get("tags"), list))
        )
        results.append(
            ("stored tags COLUMN is still the JSON string", isinstance(row.get("tags"), str))
        )
        results.append(
            (
                "stored tags COLUMN decodes to the same values",
                json.loads(row["tags"]) == ["weather", "beacon"],
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_classifier_raises_never_blocks_ingestion(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("live_cls_raises")
    try:
        storage.set_classifier(_FakeClassifier(exc=RuntimeError("boom")))
        message = _chat("DK5EN-98", "A0000003", _T0)
        await storage.store_message(message, json.dumps(message))

        results.append(
            (
                "classifier exception: no classifier keys injected into live dict",
                not any(
                    key in message
                    for key in (
                        "category",
                        "tags",
                        "info_score",
                        "template_hash",
                        "classifier_ver",
                    )
                ),
            )
        )
        rows = await storage._query(
            "SELECT COUNT(*) AS n FROM messages WHERE msg_id = ?", ("A0000003",)
        )
        results.append(("classifier exception: message is still stored", int(rows[0]["n"]) == 1))
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_empty_classification_injects_no_keys(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("live_cls_empty")
    try:
        storage.set_classifier(
            _FakeClassifier(
                _FakeClassification(
                    category=None,
                    tags=(),
                    info_score=None,
                    template_hash="",
                    classifier_version=None,
                )
            )
        )
        message = _chat("DK5EN-98", "A0000004", _T0)
        await storage.store_message(message, json.dumps(message))

        results.append(
            (
                (
                    "empty classification: no category/info_score/template_hash/"
                    "classifier_ver key on the live dict (presence rules, not truthiness defaults)"
                ),
                not any(
                    key in message
                    for key in ("category", "info_score", "template_hash", "classifier_ver")
                ),
            )
        )
        # `tags` is the one field gated on presence, not truthiness: an EMPTY
        # tag list is the common case (19644 of 21048 classified rows on the
        # live DB), mc-chat's meshcom_mock/wire.py emits `tags: []` for them,
        # and `_build_message_dict` did too before `_classifier_fields`
        # existed. Dropping the key on `[]` would have changed the history
        # payload of 93% of classified messages and diverged the two backends.
        results.append(
            (
                "empty classification: live dict carries tags == [] (NOT absent)",
                message.get("tags") == [],
            )
        )
        row = await _stored_row(storage, "A0000004")
        built = storage._build_message_dict(row)
        results.append(
            (
                "empty classification: stored tags COLUMN is '[]' and rebuilds to tags == []",
                row.get("tags") == "[]" and built.get("tags") == [],
            )
        )
        results.append(
            (
                "empty classification: live == history for the empty-tags row",
                message.get("tags") == built.get("tags"),
            )
        )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_dedup_duplicate_left_unannotated(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("live_cls_dedup")
    try:
        storage.set_classifier(
            _FakeClassifier(
                _FakeClassification(
                    category="chatter",
                    tags=("dup",),
                    info_score=0.5,
                    template_hash="ffffffffffff",
                    classifier_version=1,
                )
            )
        )
        first = _chat("DK5EN-98", "A0000005", _T0)
        second = _chat("DK5EN-98", "A0000005", _T0 + _GAP_MS)  # same sender+msg_id, inside window

        await storage.store_message(first, json.dumps(first))
        await storage.store_message(second, json.dumps(second))

        rows = await storage._query(
            "SELECT COUNT(*) AS n FROM messages WHERE msg_id = ?", ("A0000005",)
        )
        results.append(("dedup: exactly one row stored for the pair", int(rows[0]["n"]) == 1))
        results.append(("dedup: FIRST copy's dict is annotated", "category" in first))
        results.append(
            (
                (
                    "dedup: SECOND (deduped) copy's dict is left unannotated (documented, "
                    "accepted -- plan doc F1 'Deliberately NOT covered')"
                ),
                not any(
                    key in second
                    for key in (
                        "category",
                        "tags",
                        "info_score",
                        "template_hash",
                        "classifier_ver",
                    )
                ),
            )
        )
        assert DEDUP_WINDOW_MS > _GAP_MS, "test gap must be inside the dedup window"
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_subscriber_ordering(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("live_cls_order")
    try:
        router = MessageRouter(message_storage_handler=storage)
        sse = SSEManager("localhost", 0, message_router=router)

        # Both topics are wired by the identical constructor-order invariant
        # (main.py:379-380 subscribes the storage handler in
        # `MessageRouter.__init__`; sse_handler.py:233-235 subscribes the SSE
        # broadcast handler afterwards, when the manager is built around an
        # already-constructed router). "mesh_message" is the UDP-fed topic;
        # "ble_notification" is the one a BLE-only box carries its live
        # traffic on instead. An inversion on EITHER topic loses the
        # classifier fields on live traffic for whichever transport that box
        # actually uses, so both must be pinned, not just the UDP one.
        for topic in ("mesh_message", "ble_notification"):
            handlers = router._subscribers[topic]
            try:
                storage_idx = handlers.index(router._storage_handler)
                broadcast_idx = handlers.index(sse._broadcast_handler)
            except ValueError:
                results.append(
                    (
                        f"subscriber ordering: both handlers subscribed to {topic}",
                        False,
                    )
                )
                continue

            results.append(
                (
                    (
                        f"subscriber ordering ({topic}): storage handler runs BEFORE the SSE "
                        "broadcast handler (if this ever inverts, the live SSE payload "
                        "silently loses the classifier fields again -- on a BLE-only box "
                        "'ble_notification' is the topic carrying live traffic, so an "
                        "inversion there loses the fields on exactly the boxes with no UDP "
                        "path -- F1's whole premise)"
                    ),
                    storage_idx < broadcast_idx,
                )
            )
    finally:
        await storage.close()
        tmp.cleanup()


async def _test_raw_column_predates_classification(results: list[tuple[str, bool]]) -> None:
    storage, tmp = await _with_storage("live_cls_raw")
    try:
        storage.set_classifier(
            _FakeClassifier(
                _FakeClassification(
                    category="chatter",
                    tags=("x",),
                    info_score=0.1,
                    template_hash="aaaaaaaaaaaa",
                    classifier_version=1,
                )
            )
        )
        message = _chat("DK5EN-98", "A0000006", _T0)
        # Mirrors the real call site (main.py:_storage_handler): raw_json is
        # serialized from the message BEFORE store_message (and therefore
        # before classification) ever runs.
        raw_json = json.dumps(message)
        await storage.store_message(message, raw_json)

        row = await _stored_row(storage, "A0000006")
        stored_raw = json.loads(row["raw_json"])
        results.append(
            (
                (
                    "stored raw_json column has no 'category' key (serialized before "
                    "classification ran)"
                ),
                "category" not in stored_raw,
            )
        )
        results.append(("live dict WAS annotated after store_message", "category" in message))
    finally:
        await storage.close()
        tmp.cleanup()


async def run_live_classification_tests() -> bool:
    """Run the live-classification-annotation regression suite. Returns True
    iff every case passes."""
    results: list[tuple[str, bool]] = []

    await _test_classified_message_matches_history(results)
    await _test_tags_shape_live_vs_column(results)
    await _test_classifier_raises_never_blocks_ingestion(results)
    await _test_empty_classification_injects_no_keys(results)
    await _test_dedup_duplicate_left_unannotated(results)
    await _test_subscriber_ordering(results)
    await _test_raw_column_predates_classification(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  live_classification: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import asyncio

    raise SystemExit(0 if asyncio.run(run_live_classification_tests()) else 1)
