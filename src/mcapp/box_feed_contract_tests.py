"""Startup regression suite for the meshmap box-feed CONTRACT (McApp side).

`box_feed_vectors.json` is a byte-verbatim copy of the canonical corpus in mcmap
(`proxy/src/boxFeed/box_feed_vectors.json`, contract: mcmap
`docs/proposals/data-mcapp-box-feed.md` §8; this repo's consumer design:
`doc/2026-10-11_1005-internet-feed-concept.md`). mcmap is UPSTREAM: edit the
corpus there and re-copy, never in place here.

Today this suite pins the copy (sha256 drift tripwire) and the wire shapes the
McApp client will parse: `feedVersion`, the `hello`/`positions`/`batch` event
fields, and the `Message`/`Position` keys. It does NOT yet replay the
destination, subscription, radius and cursor vectors -- there is no McApp
implementation of those yet. When `internet_feed.py` lands, replay them here
against the real client code (concept doc, "Contract" row).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

from .commands.constants import has_console

_VECTORS_PATH = pathlib.Path(__file__).parent / "box_feed_vectors.json"

# Captured from mcmap commit a08037b (feedVersion 1). A mismatch means the copy was
# edited in place (don't -- edit it in mcmap and re-copy) or a newer mcmap corpus was
# copied in; for an intentional update, re-capture the hash in the same commit.
_EXPECTED_SHA256 = "644a3ff58446c43cf6b7b9b36d5cf4578941f3804c5394812f4eec894b0f5cc1"

# The feed major this client understands; an unknown major in `hello` must stop the
# feed loop (concept doc, "Feed version").
_FEED_VERSION = 1

_VECTOR_SECTIONS = ("destination", "emergency", "subscription", "radius", "cursor", "after")
_HELLO_KEYS = {"batchIntervalS", "boxId", "effective", "feedVersion", "serverTime"}
_EFFECTIVE_KEYS = {"bcast", "groups", "lat", "lon", "pos", "radiusKm"}
_BATCH_KEYS = {"messages", "positions", "removed", "seq", "serverTime"}
_MESSAGE_KEYS = {
    "country",
    "from",
    "hopCount",
    "loraParams",
    "msgId",
    "source",
    "text",
    "timeUnix",
    "to",
    "via",
}
_POSITION_KEYS = {
    "altitudeM",
    "aprsSym",
    "aprsSymGrp",
    "batteryPercent",
    "call",
    "firmware",
    "hardware",
    "isGateway",
    "lastSeenUnix",
    "lat",
    "lon",
}


def run_box_feed_contract_tests() -> bool:
    """Return True iff the vendored corpus is unmodified and carries the expected shapes."""
    if has_console:
        print("\n🧪 Testing meshmap box-feed contract corpus:")
        print("=" * 55)

    results: list[bool] = []

    def _record(label: str, ok: bool) -> None:
        results.append(ok)
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    raw = _VECTORS_PATH.read_bytes()
    _record(
        "box_feed_vectors.json sha256 matches captured hash (drift tripwire)",
        hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256,
    )
    corpus: dict[str, Any] = json.loads(raw)

    _record(f"feedVersion is {_FEED_VERSION}", corpus.get("feedVersion") == _FEED_VERSION)
    for section in _VECTOR_SECTIONS:
        cases = corpus.get(section)
        _record(
            f"section {section!r} is a non-empty vector list",
            isinstance(cases, list) and len(cases) > 0,
        )

    events = corpus.get("events", {})
    hello = events.get("hello", {})
    _record("hello event keys", set(hello) == _HELLO_KEYS)
    _record("hello.effective keys", set(hello.get("effective", {})) == _EFFECTIVE_KEYS)
    _record("hello.feedVersion matches", hello.get("feedVersion") == _FEED_VERSION)

    positions_event = events.get("positions", {})
    _record("positions event is a reset", positions_event.get("reset") is True)
    sample_positions = positions_event.get("positions", [])
    _record(
        "Position keys",
        len(sample_positions) > 0 and all(set(p) == _POSITION_KEYS for p in sample_positions),
    )

    batch = events.get("batch", {})
    _record("batch event keys", set(batch) == _BATCH_KEYS)
    sample_messages = batch.get("messages", [])
    _record(
        "Message keys (no snrDb/rssiDbm: never the box's own signal)",
        len(sample_messages) > 0 and all(set(m) == _MESSAGE_KEYS for m in sample_messages),
    )
    newest = max(sample_messages, key=lambda m: (m["timeUnix"], m["msgId"]), default=None)
    _record(
        "SSE id is timeUnix:msgId of the newest batch message",
        newest is not None and events.get("sseId") == f"{newest['timeUnix']}:{newest['msgId']}",
    )

    passed = all(results)
    if has_console:
        print(f"\nbox_feed_contract: {sum(results)}/{len(results)} passed")
    return passed
