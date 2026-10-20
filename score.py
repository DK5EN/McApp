"""Layer 3 — info score.

Compute a 0..1 blended score that the UI uses for ranking and threshold
filtering.  Positive contributors: distinct word count, first-seen
template, directed message, group conversational context.  Negative
contributors: emoji ratio, URL presence, known-beacon tag.

Weights are module constants — iterate them here once real-traffic data
is available.  The formula is intentionally transparent: one glance
shows which signals dominate.

    Weight  Signal          Direction
    ------  --------------  ---------
    +0.30   len_factor      length in words (caps at 15)
    +0.20   directed        directed flag (L1 or dst predicate)
    +0.20   group_chat      'qso' in tags
    +0.15   freshness       1 / (1 + template_count)
    -0.25   emoji_density   emoji chars / text length
    -0.10   url_density     URL in the text
    -0.40   known_beacon    any automated tag or 'auto_beacon'

A +0.5 offset centres a neutral message near 0.5.  The result is
clamped to [0.0, 1.0].
"""

from __future__ import annotations

import re
from collections.abc import Collection

from .types import AUTO_BEACON_TAG, EMOJI_RE, URL_RE

# CLS-05: EMOJI_RE now imported from .types (single canonical definition,
# shared with template.py -- previously this was a deliberately separate
# local copy that had drifted into a subtly different regex).

# ── Weights (tune here) ─────────────────────────────────────────────────────
_W_LEN_FACTOR: float = 0.30
_W_DIRECTED: float = 0.20
_W_GROUP_CHAT: float = 0.20
_W_FRESHNESS: float = 0.15
_W_EMOJI_DENSITY: float = -0.25
_W_URL_DENSITY: float = -0.10
_W_KNOWN_BEACON: float = -0.40

_LEN_CAP: float = 15.0  # word count that saturates len_factor

# Clamp ceilings for low-value content.
_BOT_COMMAND_CAP: float = 0.25
_MINIMAL_CONTENT_CAP: float = 0.30

# A "minimal content" message has <=2 whitespace-separated tokens after
# strip() — too short to carry real signal (e.g. ``123``, ``info``, ``test``,
# ``--help`` echoes that slipped past the regex).
_MINIMAL_TOKEN_CAP: int = 2


def compute(
    msg: dict,  # type: ignore[type-arg]
    tags: Collection[str],
    template_count: int,
    directed: bool = False,
    automated_tags: Collection[str] = (),
) -> float:
    """Return a 0..1 info score.

    Weighted blend::

        + 0.30 * len_factor       # min(word_count / 15, 1.0)
        + 0.20 * directed         # 1.0 if the directed flag is set
        + 0.20 * group_chat       # 1.0 if 'qso' in tags else 0.0
        + 0.15 * freshness        # 1.0 / (1 + template_count)
        - 0.25 * emoji_density    # emoji_count / max(len(text), 1)
        - 0.10 * url_density      # 1.0 if the text contains a URL else 0.0
        - 0.40 * known_beacon     # 1.0 if an automated tag or 'auto_beacon' is present

    ``directed`` comes from L1 / the dst predicate, ``automated_tags`` is the
    engine's automated tag set (the bot clamp keys on the ``bot`` tag).

    Final value = clamp(score + 0.5, 0.0, 1.0).

    ``msg`` is the webapp message dict; this function only reads msg['msg'].
    """
    tag_set: frozenset[str] = frozenset(tags)
    text: str = msg.get("msg") or ""

    # Positive contributors
    word_count: int = len(re.findall(r"\w+", text))
    len_factor: float = min(word_count / _LEN_CAP, 1.0)
    directed_f: float = 1.0 if directed else 0.0
    group_chat: float = 1.0 if "qso" in tag_set else 0.0
    freshness: float = 1.0 / (1 + template_count)

    # Negative contributors
    emoji_count: int = len(EMOJI_RE.findall(text))
    emoji_density: float = emoji_count / max(len(text), 1)
    url_density: float = 1.0 if URL_RE.search(text) else 0.0
    automated = {AUTO_BEACON_TAG, *automated_tags}
    known_beacon: float = 1.0 if automated & tag_set else 0.0

    score: float = (
        _W_LEN_FACTOR * len_factor
        + _W_DIRECTED * directed_f
        + _W_GROUP_CHAT * group_chat
        + _W_FRESHNESS * freshness
        + _W_EMOJI_DENSITY * emoji_density
        + _W_URL_DENSITY * url_density
        + _W_KNOWN_BEACON * known_beacon
    )

    final: float = max(0.0, min(1.0, score + 0.5))

    # Bot clamp: bot commands are noise regardless of length/directness.
    if "bot" in tag_set:
        return min(final, _BOT_COMMAND_CAP)

    # Minimal-content clamp: catches raw tokens that slipped past the regex
    # (stray whitespace, numeric-only pings, etc.).
    stripped = text.strip()
    if stripped and len(stripped.split()) <= _MINIMAL_TOKEN_CAP:
        return min(final, _MINIMAL_CONTENT_CAP)

    return final
