"""Replay of the cross-repo inline-`:ackNNN` matcher corpus (`ack_match_vectors.json`).

MCProxy is the REFERENCE implementation of the rule the webapp's
`findAckMessage` must mirror (doc/2026-10-04_1842-ack-matcher-fix-plan.md,
"Reference rule"): the ack's number equals the original's trailing `{NNN`
echo id exactly, the original is inside the 1 h window (168 h for a row at
`delivery_status = 'held'`, one-sided), the original's sender equals the ack's
`dst` and its target equals the ack's `src` (full callsign, SSID kept, case
insensitive), newest candidate first.

Every vector runs through the PRODUCTION path against a fresh ephemeral SQLite
DB (never the live one): residents go in through `store_message` (so `echo_id`
is computed by production code; held residents then get `delivery_status =
'held'` through the same `_mutate` seam the suite siblings use), then the ack
frame goes through `store_message` and the suite asserts WHICH resident row
ended up `acked = 1` (none for `expected: null`).

This corpus is canonical HERE and hand-copied to the webapp, which pins a
sha256 of its copy and drift-checks it parsed against `../MCProxy`. Nothing
syncs it for you: change it, re-capture `_ACK_MATCH_VECTORS_EXPECTED_SHA256`,
and copy it to the webapp in the same change. mc-chat is not part of it.

All timestamps are milliseconds (project-wide DB convention).
"""

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import DEDUP_WINDOW_MS, HELD_ACK_WINDOW_MS

logger = get_logger(__name__)

_VECTORS_PATH = Path(__file__).parent / "ack_match_vectors.json"
# sha256 of the v1 corpus. Re-capture in the SAME change as any edit, and copy
# the file to the webapp (`services/messageProcessor/__tests__/`) — nothing
# syncs it for you.
_ACK_MATCH_VECTORS_EXPECTED_SHA256 = (
    "b266e46a8d338e56b98d0f0e5773c4bac008b4c3a339f777cb01fd06aa2ca683"
)


class _RecordingRouter:
    """Minimal MessageRouter stand-in: only `.publish()`, recording every call."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, source: str, message_type: str, data: dict[str, Any]) -> None:
        self.published.append((source, message_type, data))


def _load_corpus() -> dict[str, Any]:
    corpus: dict[str, Any] = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
    return corpus


def _corpus_shape_problems(corpus: dict[str, Any]) -> list[str]:
    """Structural invariants every vector must satisfy (checked before replay)."""
    problems: list[str] = []
    vectors: list[dict[str, Any]] = corpus["vectors"]
    for vector in vectors:
        name = vector["name"]
        ids = [r["id"] for r in vector["resident"]]
        stamps = [r["timestamp"] for r in vector["resident"]] + [vector["ack"]["timestamp"]]
        if len(set(ids)) != len(ids):
            problems.append(f"{name}: duplicate resident ids")
        if len(set(stamps)) != len(stamps):
            problems.append(f"{name}: timestamps not distinct (server order has no id tiebreak)")
        if vector["expected"] is not None and vector["expected"] not in ids:
            problems.append(f"{name}: expected id is not a resident")
    names = [v["name"] for v in vectors]
    if len(set(names)) != len(names):
        problems.append("duplicate vector names")
    return problems


async def _replay(vector: dict[str, Any], index: int) -> tuple[bool, str]:
    """Replay one vector on a fresh DB. Returns (ok, detail)."""
    resident_ids: list[str] = [r["id"] for r in vector["resident"]]
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = await create_sqlite_storage(Path(tmp_dir) / "ack_match_vector.db")
        storage.set_message_router(_RecordingRouter())
        try:
            for resident in vector["resident"]:
                await storage.store_message(
                    {
                        "msg_id": resident["id"],
                        "src": resident["src"],
                        "dst": resident["dst"],
                        "msg": resident["msg"],
                        "type": "msg",
                        "src_type": "lora",
                        "timestamp": resident["timestamp"],
                    },
                    "{}",
                )
                if resident["delivery_status"] is not None:
                    await storage._mutate(
                        "UPDATE messages SET delivery_status = ? WHERE msg_id = ?",
                        (resident["delivery_status"], resident["id"]),
                    )

            stored = await storage._query(
                "SELECT msg_id FROM messages WHERE type = 'msg'",
                (),
            )
            stored_ids = sorted(str(r["msg_id"]) for r in stored)
            if stored_ids != sorted(resident_ids):
                return False, f"residents not all stored: {stored_ids} != {sorted(resident_ids)}"

            ack = vector["ack"]
            await storage.store_message(
                {
                    "msg_id": f"ACKFRAME{index:03d}",
                    "src": ack["src"],
                    "dst": ack["dst"],
                    "msg": ack["msg"],
                    "type": "msg",
                    "src_type": "lora",
                    "timestamp": ack["timestamp"],
                },
                "{}",
            )

            placeholders = ",".join("?" for _ in resident_ids)
            acked_rows = await storage._query(
                f"SELECT msg_id FROM messages WHERE type = 'msg' AND acked = 1"  # noqa: S608 - placeholders are '?' only; ids are bound parameters
                f" AND msg_id IN ({placeholders}) ORDER BY msg_id",
                tuple(resident_ids),
            )
            acked = [str(r["msg_id"]) for r in acked_rows]
        finally:
            await storage.close()

    expected = [] if vector["expected"] is None else [vector["expected"]]
    return acked == expected, f"acked={acked} expected={expected}"


async def run_ack_match_vector_tests() -> bool:
    """Replay `ack_match_vectors.json` through production. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    raw = _VECTORS_PATH.read_bytes()
    results.append(
        (
            "ack match corpus: sha256 pin (v1) — re-capture on any edit, copy to the webapp",
            hashlib.sha256(raw).hexdigest() == _ACK_MATCH_VECTORS_EXPECTED_SHA256,
        )
    )
    corpus = _load_corpus()
    results.append(("ack match corpus: version == 1", corpus["version"] == 1))
    results.append(
        (
            "ack match corpus: window constants equal production DEDUP/HELD windows",
            corpus["window_ms"] == DEDUP_WINDOW_MS
            and corpus["held_window_ms"] == HELD_ACK_WINDOW_MS,
        )
    )
    shape_problems = _corpus_shape_problems(corpus)
    for problem in shape_problems:
        print(f"    corpus shape: {problem}")
    results.append(("ack match corpus: structural invariants hold", not shape_problems))
    results.append(("ack match corpus: carries vectors to replay", len(corpus["vectors"]) > 0))

    for index, vector in enumerate(corpus["vectors"]):
        ok, detail = await _replay(vector, index)
        label = f"ack match vector: {vector['name']}"
        if not ok:
            label = f"{label} ({detail})"
        results.append((label, ok))

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    ack_match_vectors: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
