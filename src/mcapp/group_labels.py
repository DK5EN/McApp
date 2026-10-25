"""Group label payload: ICSSW snapshot + operator overrides (plan D1-D3, D11).

The snapshot (`group_labels_icssw.json`, written by
`scripts/refresh_group_labels.py`) holds exact group entries and a few numeric
ranges. `build_label_payload` expands ranges server-side and merges per-field
overrides, producing the flat wire map the webapp does a plain lookup on.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_SNAPSHOT_PATH = Path(__file__).with_name("group_labels_icssw.json")
MAX_RANGE_WIDTH = 1000
_COUNTRY_DIGITS = 3
_FIELDS = ("prefix", "hint", "remark")

Label = dict[str, Any]


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    """Load the ICSSW snapshot (default: next to this module)."""
    with (path or DEFAULT_SNAPSHOT_PATH).open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _range_label(entry: Mapping[str, Any], n: int) -> Label:
    """Derive the label of number `n` inside a range entry (plan D2)."""
    prefix = entry["prefix"]
    base = entry.get("prefix_base")
    first = entry.get("first_suffix")
    width = entry.get("width")
    if base is not None and first is not None and width is not None:
        prefix = str(base) + str(int(first) + n - int(entry["lo"])).zfill(int(width))
    return {
        "prefix": prefix,
        "hint": entry.get("hint"),
        "remark": entry.get("remark"),
        "source": "range",
    }


def upstream_for(snapshot: Mapping[str, Any]) -> dict[str, Label]:
    """Pre-override map: exact entries (source "icssw") beat expanded ranges ("range")."""
    entries: list[Mapping[str, Any]] = snapshot.get("entries", [])
    out: dict[str, Label] = {}
    for entry in entries:
        if "lo" not in entry:
            continue
        lo, hi = int(entry["lo"]), int(entry["hi"])
        if hi - lo + 1 > MAX_RANGE_WIDTH:
            logger.warning("group_labels: range %d-%d too wide, not expanded", lo, hi)
            continue
        for n in range(lo, hi + 1):
            out[str(n)] = _range_label(entry, n)
    for entry in entries:
        if "group" in entry:
            out[str(entry["group"])] = {
                "prefix": entry.get("prefix"),
                "hint": entry.get("hint"),
                "remark": entry.get("remark"),
                "source": "icssw",
            }
    return out


def build_label_payload(
    snapshot: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, str | None]],
) -> dict[str, dict[str, Label]]:
    """Return `{"labels": {...}, "countries": {...}}` per the wire contract.

    Override rule (per field, None = inherit the upstream value):
      * key without upstream and at least one non-None override field: the entry
        is created from the override, source "own";
      * key with upstream and at least one non-None override field: source
        "mixed" (even when every field is overridden);
      * no non-None override field: the override is ignored.
    `countries` = every resulting entry whose key is exactly three digits.
    """
    labels = upstream_for(snapshot)
    for key, ov in overrides.items():
        given = {f: ov.get(f) for f in _FIELDS if ov.get(f) is not None}
        if not given:
            continue
        base = labels.get(key)
        if base is None:
            labels[key] = {"prefix": None, "hint": None, "remark": None, **given, "source": "own"}
        else:
            labels[key] = {**base, **given, "source": "mixed"}
    countries = {k: v for k, v in labels.items() if len(k) == _COUNTRY_DIGITS and k.isdigit()}
    return {"labels": labels, "countries": countries}
