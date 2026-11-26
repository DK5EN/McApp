"""Shared types for the classifier package.

This module has no runtime dependencies on the rest of ``meshcom_mock``
so template/score modules can import it without cycles.
It also defines the Protocols that allow the classifier to live as a
git subtree in other packages without importing meshcom_mock directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

# -- Shared compiled patterns (CLS-05) ------------------------------------
# Previously template.py and score.py each defined their own EMOJI_RE, with
# a subtle semantic difference: score.py's variation-selector code point was
# INSIDE the character class (matching it as its own standalone occurrence,
# double-counting an emoji + its trailing selector as two matches), while
# template.py's was an optional SUFFIX on the character class (consuming an
# emoji and its trailing selector as one atomic match). One canonical
# definition here uses template's (more correct for both substitution and
# counting) form.

URL_RE: re.Pattern[str] = re.compile(r"https?://\S+")

EMOJI_RE: re.Pattern[str] = re.compile(
    "["  # start character class
    "\U0001f300-\U0001faff"  # Misc Symbols & Pictographs through Symbols & Pictographs Ext-A
    "\u2600-\u27bf"  # Misc Symbols, Dingbats
    "\u2300-\u23ff"  # Misc Technical (clocks, arrows, etc.)
    "]"
    "\ufe0f?"  # optional variation selector -- consumed as part of the preceding match
)

# -- Directed-destination predicate ---------------------------------------
# "Directed" is a property of the ROUTING, not of the text: a message is
# directed when its destination resolves to a personal callsign rather than
# to a group / broadcast / time channel. Pinned across all three repos by
# directed_dst_vectors.json (canonical copy sits beside this module and
# rides the classifier subtree into MCProxy; the webapp vendors a
# parse-equal copy and replays it against classifyDst()).
#
# The pattern is applied with .search() to the RAW dst, so it resolves
# via-routing itself: `(?:^|,)` plus the `$` anchor and a comma-free
# character class force the match onto the LAST comma component, which is
# the real target ('DJ8MEH-82,DJ8MEH-8' -> 'DJ8MEH-8'). This mirrors the
# webapp's effectiveDst(). The rest:
#   (?!(?:ALL|TEST|TIME)\s*$)  broadcast alias / group 'TEST' / time channel
#   (?=[^,]*[A-Z])             must contain a letter -- rejects every numeric
#                              group id, in or out of the 1..99999 range, and
#                              '*' (which the character class rejects anyway)
#   [A-Z0-9./-]+               callsign with an OPTIONAL -SSID, so a bare
#                              'DL1EEN' is directed too; '.' and '/' keep
#                              portable//P-style suffixes on the person side
# `(?i)` must stay leading -- Python rejects a mid-pattern global flag.
DIRECTED_DST_PATTERN: str = r"(?i)(?:^|,)\s*(?!(?:ALL|TEST|TIME)\s*$)(?=[^,]*[A-Z])[A-Z0-9./-]+\s*$"

DIRECTED_DST_RE: re.Pattern[str] = re.compile(DIRECTED_DST_PATTERN)

# CLS-05: the 12-hex-char SHA-1 prefix length was a bare `[:12]` literal
# duplicated in template.py's fingerprint() and classify.py's _fallback_hash().
TEMPLATE_HASH_LEN: int = 12


# Rules whose match marks a message as a clock/timestamp beacon: Storage.store_message does not
# persist those (ADR-0009). Names come from the mcmap contract; tests pin that both still exist.
TIMESTAMP_BEACON_RULES: frozenset[str] = frozenset({"CET timestamp", "Time bot reply"})

# -- Timestamp helper -------------------------------------------------------
# Duplicated from meshcom_mock.storage so the classifier package is self-
# contained when used as a git subtree in other packages.


def ms_to_zulu(ms: int) -> str:
    """Convert millisecond epoch to ISO 8601 UTC string.

    CLS-04: public (was `_ms_to_zulu`) — MCProxy's storage layer imports this
    across the subtree boundary; it was reaching into a private symbol that
    any mc-chat rename would silently break.
    """
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# -- SSE types ---------------------------------------------------------------


@dataclass
class SSEEvent:
    """A single event to be sent to SSE clients."""

    event_type: str
    data: Any


@runtime_checkable
class EventBusProtocol(Protocol):
    async def publish(self, event: SSEEvent) -> None: ...


# -- Storage Protocol ---------------------------------------------------------
# All methods that the classifier calls on the storage object.  Both
# meshcom_mock.storage.Storage and MCProxy's sqlite_storage must satisfy
# this Protocol structurally.


class StorageProtocol(Protocol):
    # Templates
    async def get_beacon_template(self, template_hash: str) -> dict[str, Any] | None: ...
    async def upsert_beacon_template(
        self, hash_: str, msg: str, src: str, now_ms: int
    ) -> dict[str, Any]: ...
    async def set_template_auto_beacon(
        self, template_hash: str, auto_beacon: bool
    ) -> dict[str, Any] | None: ...
    async def count_recent_messages_by_template_src(
        self, hash_: str, src: str, since_ms: int
    ) -> int: ...
    async def template_has_human_history(self, template_hash: str) -> bool: ...
    async def clear_stale_auto_beacons(self, min_tokens: int) -> int: ...

    # Classification / backfill
    async def get_classifier_version(self) -> int: ...
    async def count_messages_to_classify(
        self, *, classifier_ver_below: int | None = None
    ) -> int: ...
    async def get_messages_to_classify(
        self,
        *,
        classifier_ver_below: int | None = None,
        since_ms: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    async def update_message_classification(  # noqa: PLR0913 - signature fixed by call sites
        self,
        row_id: Any,
        *,
        tags: list[str],
        info_score: float,
        template_hash: str,
        classifier_ver: int,
        origin: str | None = None,
        origin_rule: str | None = None,
    ) -> None: ...
    async def update_classification_batch(
        self,
        *,
        changed: list[tuple[str, float, str, int, int]],
        unchanged_ids: list[int],
        classifier_ver: int,
        origins: dict[int, tuple[str, str]] | None = None,
    ) -> None:
        """Write one reclassify batch in one write unit and one commit.

        ``changed`` tuples are ``(tags_json, info_score,
        template_hash, classifier_ver, row_id)`` (full UPDATE);
        ``unchanged_ids`` are only stamped with ``classifier_ver``. ``origins`` maps a
        row id to ``(origin, origin_rule)``; None keeps the stored values.
        """
        ...

    # Meta / versioning
    async def get_meta(self, key: str) -> str | None: ...
    async def set_meta(self, key: str, value: str) -> None: ...
    async def bump_classifier_version(self) -> int: ...

    # Stats (used by collect_stats)
    async def count_messages_by_tag(self, since_ms: int) -> dict[str, int]: ...
    async def count_messages_by_origin(self, since_ms: int) -> dict[str, int]: ...
    async def get_top_beacon_templates(
        self, since_ms: int, limit: int = 10
    ) -> list[dict[str, Any]]: ...
    async def count_auto_beacon_templates(self) -> int: ...


# -- Classification result ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of running the classifier on a single message.

    All fields are always populated -- unclassified rows are the job of
    the backfill, not of this type.
    """

    tags: tuple[str, ...]
    info_score: float
    template_hash: str
    classifier_version: int
    # Names of the matched L1 rules (engine order); feeds the timestamp-beacon drop.
    rules: tuple[str, ...] = ()
