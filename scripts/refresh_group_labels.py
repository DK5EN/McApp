#!/usr/bin/env python3
"""Refresh src/mcapp/group_labels_icssw.json from the ICSSW MeshCom group table.

The page embeds the table as a serialized JSON array (header row first:
Group, Praefix, Hinweis, Reichweite). Parsing is done in pure functions so the
tests can feed a fixture string. Fails loudly (exit 1, no file written) when
fewer than MIN_ROWS rows parse.
"""

import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_URL = "https://icssw.org/meshcom-grc-gruppen/"
MIN_ROWS = 50
_MIN_COLS = 3
OUT_PATH = Path(__file__).resolve().parent.parent / "src" / "mcapp" / "group_labels_icssw.json"

_HEADER_MARK = '[["Group","Pr'
_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_EXACT_RE = re.compile(r"^\d+$")
_PREFIX_RANGE_RE = re.compile(r"^([A-Za-z]*?)(\d+)\s*-\s*([A-Za-z]*?)(\d+)$")


class ParseError(ValueError):
    """The page did not yield a plausible table."""


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ("", "null") else text


def extract_rows(html: str) -> list[list[Any]]:
    """Return the data rows (header dropped) of the embedded serialized table."""
    start = html.find(_HEADER_MARK)
    if start < 0:
        raise ParseError("table array not found in page")
    try:
        table, _ = json.JSONDecoder().raw_decode(html[start:])
    except json.JSONDecodeError as exc:
        raise ParseError(f"table array is not valid JSON: {exc}") from exc
    if not isinstance(table, list):
        raise ParseError("table array has unexpected shape")
    return [r for r in table[1:] if isinstance(r, list) and len(r) >= _MIN_COLS]


def parse_prefix_range(prefix: str) -> dict[str, Any]:
    """Parse 'OE1-OE9' / 'DL00-DL99' into prefix_base, first_suffix, width.

    Returns all-None fields when the Praefix does not follow that shape.
    """
    none: dict[str, Any] = {"prefix_base": None, "first_suffix": None, "width": None}
    m = _PREFIX_RANGE_RE.match(prefix.strip())
    if not m or m.group(1) != m.group(3):
        return none
    first = m.group(2)
    return {"prefix_base": m.group(1), "first_suffix": int(first), "width": len(first)}


def build_entries(rows: list[list[Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        group = _clean(row[0])
        prefix = _clean(row[1])
        if group is None or prefix is None:
            continue
        hint = _clean(row[2])
        remark = _clean(row[3]) if len(row) > _MIN_COLS else None
        rng = _RANGE_RE.match(group)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if hi < lo:
                continue
            entry: dict[str, Any] = {"lo": lo, "hi": hi, "prefix": prefix}
            entry.update(parse_prefix_range(prefix))
            entry["hint"] = hint
            entry["remark"] = remark
            entries.append(entry)
        elif _EXACT_RE.match(group):
            entries.append({"group": group, "prefix": prefix, "hint": hint, "remark": remark})
    if len(entries) < MIN_ROWS:
        raise ParseError(f"only {len(entries)} rows parsed (need >= {MIN_ROWS})")

    def sort_key(e: dict[str, Any]) -> tuple[int, int]:
        n = int(e["group"]) if "group" in e else int(e["lo"])
        return (n, 1 if "lo" in e else 0)

    entries.sort(key=sort_key)
    return entries


def build_snapshot(html: str, fetched_at: str) -> dict[str, Any]:
    return {
        "source_url": SOURCE_URL,
        "fetched_at": fetched_at,
        "entries": build_entries(extract_rows(html)),
    }


def render(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "mcapp-refresh-group-labels"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # fixed https URL
            html = resp.read().decode("utf-8")
        snapshot = build_snapshot(html, datetime.now(UTC).date().isoformat())
    except (OSError, ParseError) as exc:
        print(f"refresh_group_labels: FAILED: {exc}", file=sys.stderr)
        return 1
    OUT_PATH.write_text(render(snapshot), encoding="utf-8")
    print(f"wrote {len(snapshot['entries'])} entries to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
