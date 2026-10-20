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

Public surface: ``Classification``, ``CATEGORIES``, ``CLASSIFIER_SCHEMA_VERSION``.
"""

from __future__ import annotations

from .classify import Classifier
from .types import (
    AUTO_BEACON_TAG,
    CATEGORIES,
    CLASSIFIER_SCHEMA_VERSION,
    Classification,
    MessageCategory,
    derive_category,
)

__all__ = [
    "AUTO_BEACON_TAG",
    "CATEGORIES",
    "CLASSIFIER_SCHEMA_VERSION",
    "Classification",
    "Classifier",
    "MessageCategory",
    "derive_category",
]
