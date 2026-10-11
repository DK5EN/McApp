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


# Set in place by `store_message` on a www frame it recognised as a duplicate
# (RF copy already stored, or meshmap re-sending its lookback window). The
# router hands ONE dict to every subscriber in order and storage runs first,
# so the broadcast and push subscribers of `www_message` read this flag and
# skip the frame: a replay must not re-notify or re-render anything.
WWW_DUPLICATE_KEY = "_www_duplicate"

# A www message older than this is history, not news: it is stored and counted
# as unread, but never pushed. Covers meshmap's 2 h backfill on first connect
# and the cursor/lookback replay after a restart, when the in-memory push dedup
# is empty.
WWW_PUSH_MAX_AGE_MS = 15 * 60 * 1000


def is_www_duplicate(message: Any) -> bool:
    """True when storage flagged this www frame as already stored."""
    return is_www_frame(message) and message.get(WWW_DUPLICATE_KEY) is True


def www_push_allowed(message: Any, now_ms: int) -> bool:
    """A www frame may reach push only when new to storage and recent."""
    if is_www_duplicate(message):
        return False
    ts = message.get("timestamp") if isinstance(message, dict) else None
    if isinstance(ts, bool) or not isinstance(ts, int):
        return False
    return now_ms - ts <= WWW_PUSH_MAX_AGE_MS
