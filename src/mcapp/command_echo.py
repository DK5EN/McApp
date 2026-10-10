"""Process-local registry of node command echoes MCProxy expects.

When MCProxy itself sends a `--` command (the DBG console session's
`--loradebug on/off`, `--txcapture on/off`), the node also answers it over
BLE as a `response` MSG, duplicating what the DBG view shows. The sender
registers the exact command text here right before writing it;
`sse_handler.is_auto_command_echo` treats a `response` frame whose stripped
text matches a live entry as an echo and drops it.

Entries expire after a TTL (monotonic clock) and the registry is bounded, so
a command whose echo never arrives cannot grow it without limit. Kept
dependency-free so `node_console` can import it without pulling in
`sse_handler`.
"""

from __future__ import annotations

import time
from collections.abc import Callable

DEFAULT_TTL_S = 15.0
_MAX_ENTRIES = 64

# Injectable for tests (patched instead of sleeping through the TTL).
clock: Callable[[], float] = time.monotonic

# command text -> monotonic expiry
_expected: dict[str, float] = {}


def _prune(now: float) -> None:
    for text in [t for t, exp in _expected.items() if exp <= now]:
        del _expected[text]


def expect_command_echo(text: str, ttl_s: float = DEFAULT_TTL_S) -> None:
    """Register `text` (stripped) as an echo to expect within `ttl_s`."""
    now = clock()
    _prune(now)
    key = text.strip()
    _expected.pop(key, None)  # re-insert at the end: a refresh is the newest entry
    _expected[key] = now + ttl_s
    while len(_expected) > _MAX_ENTRIES:
        del _expected[next(iter(_expected))]  # oldest insertion


def is_expected_command_echo(text: str) -> bool:
    """True iff `text` (stripped) matches a non-expired registered echo."""
    _prune(clock())
    return text.strip() in _expected


def clear() -> None:
    """Drop every entry (tests)."""
    _expected.clear()
