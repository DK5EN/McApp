"""Operator overrides for group labels and callsign name/QTH (migration 39).

Per-field overrides, `NULL` = inherit the upstream value; an all-NULL row is
deleted (= revert). Callsign overrides are keyed by BASE callsign. Merging with
the ICSSW snapshot / QRZ data happens elsewhere; this mixin only reads and
writes. Plan: doc/2026-10-25_0900-conversation-nav-group-labels-plan.md (D4/D5).
"""

from __future__ import annotations

from ..qrz_client import base_callsign
from ..util import now_ms
from ._base import StorageBase

# Wire-contract limits (plan §3). Exceeding one raises ValueError; the route
# layer maps that to 422 without echoing the input.
PREFIX_MAX = 12
HINT_MAX = 40
REMARK_MAX = 80
FIRST_NAME_MAX = 30
QTH_MAX = 40


def _clean(value: str | None, field: str, limit: int) -> str | None:
    """Trim; empty -> None; over-long -> ValueError (message never echoes the value)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        msg = f"{field} exceeds {limit} characters"
        raise ValueError(msg)
    return text


class LabelOverridesMixin(StorageBase):
    async def get_group_label_overrides(self) -> dict[str, dict[str, str | None]]:
        rows = await self._query(
            "SELECT group_key, prefix, hint, remark FROM group_label_overrides"
        )
        return {
            r["group_key"]: {"prefix": r["prefix"], "hint": r["hint"], "remark": r["remark"]}
            for r in rows
        }

    async def set_group_label_override(
        self, key: str, prefix: str | None, hint: str | None, remark: str | None
    ) -> None:
        group_key = (key or "").strip()
        if not group_key:
            msg = "group key is empty"
            raise ValueError(msg)
        p = _clean(prefix, "prefix", PREFIX_MAX)
        h = _clean(hint, "hint", HINT_MAX)
        r = _clean(remark, "remark", REMARK_MAX)
        if p is None and h is None and r is None:
            await self.delete_group_label_override(group_key)
            return
        await self._mutate(
            """INSERT INTO group_label_overrides (group_key, prefix, hint, remark, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(group_key) DO UPDATE SET
                   prefix = excluded.prefix,
                   hint = excluded.hint,
                   remark = excluded.remark,
                   updated_at = excluded.updated_at""",
            (group_key, p, h, r, now_ms()),
        )

    async def delete_group_label_override(self, key: str) -> None:
        await self._mutate("DELETE FROM group_label_overrides WHERE group_key = ?", (key.strip(),))

    async def get_callsign_overrides(self) -> dict[str, dict[str, str | None]]:
        rows = await self._query("SELECT callsign, first_name, qth FROM callsign_info_overrides")
        return {r["callsign"]: {"first_name": r["first_name"], "qth": r["qth"]} for r in rows}

    async def set_callsign_override(
        self, callsign: str, first_name: str | None, qth: str | None
    ) -> None:
        base = base_callsign(callsign or "")
        if not base:
            msg = "callsign is empty"
            raise ValueError(msg)
        name = _clean(first_name, "first_name", FIRST_NAME_MAX)
        place = _clean(qth, "qth", QTH_MAX)
        if name is None and place is None:
            await self.delete_callsign_override(base)
            return
        await self._mutate(
            """INSERT INTO callsign_info_overrides (callsign, first_name, qth, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(callsign) DO UPDATE SET
                   first_name = excluded.first_name,
                   qth = excluded.qth,
                   updated_at = excluded.updated_at""",
            (base, name, place, now_ms()),
        )

    async def delete_callsign_override(self, callsign: str) -> None:
        await self._mutate(
            "DELETE FROM callsign_info_overrides WHERE callsign = ?",
            (base_callsign(callsign or ""),),
        )
