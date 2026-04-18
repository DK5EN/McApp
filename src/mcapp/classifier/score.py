"""Layer 3 — blended info score.

All contributors are normalized to [0, 1] and combined with fixed weights.
Weights centre the output around 0.5 so unclassified / neutral messages
score near the middle of the range. Weights are tunable constants; revisit
once we have real traffic data.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Positive weights — higher score is "more informative".
W_LENGTH = 0.30     # up to 15 distinct words
W_DIRECTED = 0.20   # 1-to-1 messages are usually conversational
W_GROUPCHAT = 0.20  # QSO category = live conversation
W_FRESHNESS = 0.15  # rarer templates score higher
# Negative weights — drag towards 0.
W_EMOJI = 0.25
W_URL = 0.10
W_BEACON = 0.40

LENGTH_CAP_WORDS = 15.0

# Emoji detection: same character classes used by the template fingerprint.
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]"
)
_WORD_RE = re.compile(r"\w+")


def _emoji_density(text: str) -> float:
    if not text:
        return 0.0
    return len(_EMOJI_RE.findall(text)) / max(len(text), 1)


def compute(
    msg: dict[str, Any],
    category: str,
    tags: Iterable[str],
    tpl_count: int,
) -> float:
    """Return info score in [0.0, 1.0]."""
    text = str(msg.get("msg") or "")
    tag_set = set(tags)

    word_count = len(_WORD_RE.findall(text))
    len_factor = min(word_count / LENGTH_CAP_WORDS, 1.0)
    directed = 1.0 if category == "directed" else 0.0
    group_chat = 1.0 if category == "qso" else 0.0
    freshness = 1.0 / (1 + max(tpl_count, 0))

    emoji = _emoji_density(text)
    url = 1.0 if "has_url" in tag_set else 0.0
    beacon = 1.0 if ("beacon" in tag_set or "auto_beacon" in tag_set) else 0.0

    raw = (
        W_LENGTH * len_factor
        + W_DIRECTED * directed
        + W_GROUPCHAT * group_chat
        + W_FRESHNESS * freshness
        - W_EMOJI * emoji
        - W_URL * url
        - W_BEACON * beacon
    )
    return max(0.0, min(1.0, raw + 0.5))
