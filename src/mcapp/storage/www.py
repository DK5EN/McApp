"""Predicate for frames that came from the meshmap internet feed (`src_type == "www"`).

A www frame is a message another box heard on RF and published to meshmap; we
did not hear it. It is therefore chat text and nothing else: no RF signal, no
hook that reasons about our own link (uptime beacon, RM1 reply), no ack
matching against our own DMs, no telemetry / position / MHeard. Every guard in
`IngestMixin.store_message` asks this one predicate; do not compare the string
inline. Design: doc/2026-10-11_1005-internet-feed-concept.md §4.3-4.4.
"""

from typing import Any

WWW_SRC_TYPE = "www"


def is_www_src_type(src_type: Any) -> bool:
    """True for the `src_type` value of an internet-feed frame (non-str tolerant)."""
    return isinstance(src_type, str) and src_type == WWW_SRC_TYPE


def is_www_frame(message: Any) -> bool:
    """True when `message` is an internet-feed frame (non-dict / non-str tolerant)."""
    return isinstance(message, dict) and is_www_src_type(message.get("src_type"))
