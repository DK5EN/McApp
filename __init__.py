"""Server-side message classification.

Layered tagging of inbound messages so the webapp can hide noisy
traffic (timestamp beacons, WX beacons, advert banners, ...) while
keeping everything in the database.

Three layers plus an orchestrator:

    engine.py     -- Layer 1: rule engine executing the mcmap classifier contract
    template.py   -- Layer 2: normalized fingerprint + auto-beacon
    score.py      -- Layer 3: 0..1 info score
    classify.py   -- glue that runs all three per message
    types.py      -- shared dataclasses (Classification, ...)

Public surface: ``Classification``, ``CATEGORIES``.
"""

from __future__ import annotations

from .classify import Classifier
from .origin import (
    ORIGIN_HUMAN,
    ORIGIN_MACHINE,
    OriginResult,
    OriginSpec,
    carries_values,
    message_origin,
    origin_of,
)
from .types import (
    AUTO_BEACON_TAG,
    CATEGORIES,
    Classification,
    MessageCategory,
    derive_category,
    legacy_tag_view,
)

__all__ = [
    "AUTO_BEACON_TAG",
    "CATEGORIES",
    "ORIGIN_HUMAN",
    "ORIGIN_MACHINE",
    "Classification",
    "Classifier",
    "MessageCategory",
    "OriginResult",
    "OriginSpec",
    "carries_values",
    "derive_category",
    "legacy_tag_view",
    "message_origin",
    "origin_of",
]
