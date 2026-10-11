"""Storage for the McApp internet feed settings and connection state.

Single-row `internet_feed_state` table from migration 36. Policy (when to
connect, what to filter) lives in the feed service; this mixin only reads and
writes. Design: doc/2026-10-11_1005-internet-feed-concept.md §4.1.
"""

from __future__ import annotations

import json
from typing import Any

from ._base import StorageBase

# Columns `update_internet_feed_state` may write. Column names are interpolated
# into the UPDATE, so they must come from this fixed set, never from a caller's keys.
_INTERNET_FEED_COLUMNS = frozenset(
    {
        "enabled",
        "configured",
        "groups",
        "broadcast",
        "pos_mode",
        "radius_km",
        "box_id",
        "token_enc",
        "identity_json",
        "cursor",
        "last_connected_ms",
        "last_batch_ms",
        "last_error",
        "last_error_ms",
    }
)

_BOOL_COLUMNS = ("enabled", "configured", "broadcast")


def _decode_groups(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else []
    except ValueError:
        return []
    return [str(g) for g in value] if isinstance(value, list) else []


class InternetFeedStateMixin(StorageBase):
    async def get_internet_feed_state(self) -> dict[str, Any]:
        rows = await self._query("SELECT * FROM internet_feed_state WHERE id = 1")
        if not rows:
            await self._mutate("INSERT OR IGNORE INTO internet_feed_state (id) VALUES (1)")
            rows = await self._query("SELECT * FROM internet_feed_state WHERE id = 1")
        state: dict[str, Any] = dict(rows[0])
        state["groups"] = _decode_groups(state.get("groups"))
        for col in _BOOL_COLUMNS:
            state[col] = bool(state.get(col))
        return state

    async def update_internet_feed_state(self, **fields: Any) -> None:
        unknown = set(fields) - _INTERNET_FEED_COLUMNS
        if unknown:
            msg = f"unknown internet_feed_state columns: {sorted(unknown)}"
            raise ValueError(msg)
        if not fields:
            return
        values = dict(fields)
        if "groups" in values and not isinstance(values["groups"], str):
            values["groups"] = json.dumps([str(g) for g in values["groups"]])
        for col in _BOOL_COLUMNS:
            if col in values and isinstance(values[col], bool):
                values[col] = int(values[col])
        assignments = ", ".join(f"{col} = ?" for col in values)
        await self._mutate(
            f"UPDATE internet_feed_state SET {assignments} WHERE id = 1",  # noqa: S608 - columns whitelisted by _INTERNET_FEED_COLUMNS, values bound
            tuple(values.values()),
        )
