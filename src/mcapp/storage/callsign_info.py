"""Storage for the QRZ.com callsign lookup (issue #14).

Three tables from migration 33: the `callsign_info` cache, the `qrz_lookups`
ledger the daily hard cap counts, and the single-row `qrz_state`. Policy (what
is due, when to stop) lives in `qrz_service.py`; this mixin only reads and
writes. Plan: doc/2026-10-04_0848-qrz-callsign-lookup-plan.md.
"""

from __future__ import annotations

from typing import Any

from ._base import StorageBase
from .constants import sender_base_sql

# Columns `update_qrz_state` may write. The column names are interpolated into
# the UPDATE, so they must come from this fixed set, never from a caller's keys.
_QRZ_STATE_COLUMNS = frozenset(
    {
        "username",
        "password_enc",
        "enabled",
        "suspended_until_ms",
        "backoff_until_ms",
        "backoff_level",
        "last_request_ms",
        "last_login_ms",
        "auth_failed",
        "last_error",
        "last_error_ms",
        "server_count",
        "subscription",
    }
)


class CallsignInfoMixin(StorageBase):
    async def get_qrz_state(self) -> dict[str, Any]:
        rows = await self._query("SELECT * FROM qrz_state WHERE id = 1")
        if rows:
            return rows[0]
        await self._mutate("INSERT OR IGNORE INTO qrz_state (id) VALUES (1)")
        rows = await self._query("SELECT * FROM qrz_state WHERE id = 1")
        return rows[0]

    async def update_qrz_state(self, **fields: Any) -> None:
        unknown = set(fields) - _QRZ_STATE_COLUMNS
        if unknown:
            msg = f"unknown qrz_state columns: {sorted(unknown)}"
            raise ValueError(msg)
        if not fields:
            return
        assignments = ", ".join(f"{col} = ?" for col in fields)
        await self._mutate(
            f"UPDATE qrz_state SET {assignments} WHERE id = 1",  # noqa: S608 - columns whitelisted by _QRZ_STATE_COLUMNS, values bound
            tuple(fields.values()),
        )

    async def record_qrz_lookup(self, ts_ms: int, callsign: str) -> None:
        """Reserve one lookup in the ledger BEFORE the request goes out."""
        await self._mutate(
            "INSERT INTO qrz_lookups (ts_ms, callsign, outcome) VALUES (?, ?, 'pending')",
            (ts_ms, callsign),
        )

    async def set_qrz_lookup_outcome(self, ts_ms: int, callsign: str, outcome: str) -> None:
        await self._mutate(
            "UPDATE qrz_lookups SET outcome = ? WHERE ts_ms = ? AND callsign = ?",
            (outcome, ts_ms, callsign),
        )

    async def count_qrz_lookups_since(self, since_ms: int) -> int:
        rows = await self._query(
            "SELECT COUNT(*) AS n FROM qrz_lookups WHERE ts_ms >= ?", (since_ms,)
        )
        return int(rows[0]["n"])

    async def prune_qrz_lookups(self, before_ms: int) -> int:
        return await self._mutate("DELETE FROM qrz_lookups WHERE ts_ms < ?", (before_ms,))

    async def upsert_callsign_info(  # noqa: PLR0913 - one column per argument, called from one place
        self,
        callsign: str,
        status: str,
        fetched_at: int,
        *,
        first_name: str | None = None,
        qth: str | None = None,
        country: str | None = None,
        raw: dict[str, str] | None = None,
    ) -> None:
        raw = raw or {}
        await self._mutate(
            """INSERT INTO callsign_info
                   (callsign, status, first_name, qth, country, fname, name, addr2, state,
                    fetched_at, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'qrz')
               ON CONFLICT(callsign) DO UPDATE SET
                   status = excluded.status,
                   first_name = excluded.first_name,
                   qth = excluded.qth,
                   country = excluded.country,
                   fname = excluded.fname,
                   name = excluded.name,
                   addr2 = excluded.addr2,
                   state = excluded.state,
                   fetched_at = excluded.fetched_at,
                   source = excluded.source""",
            (
                callsign,
                status,
                first_name,
                qth,
                country,
                raw.get("fname"),
                raw.get("name"),
                raw.get("addr2"),
                raw.get("state"),
                fetched_at,
            ),
        )

    async def touch_callsign_info(self, callsign: str, fetched_at: int) -> None:
        """A `not_found` that keeps the earlier `found` data: only `status`
        and `fetched_at` move, so a refresh that QRZ answers with "not found"
        does not wipe a name we already had."""
        await self._mutate(
            "UPDATE callsign_info SET status = 'not_found', fetched_at = ? WHERE callsign = ?",
            (fetched_at, callsign),
        )

    async def get_callsign_info_index(self) -> dict[str, tuple[str, int]]:
        """{callsign: (status, fetched_at)} for every cached entry."""
        rows = await self._query("SELECT callsign, status, fetched_at FROM callsign_info")
        return {r["callsign"]: (r["status"], int(r["fetched_at"])) for r in rows}

    async def get_callsign_info_map(self) -> dict[str, dict[str, str | None]]:
        """Display data for every callsign with a name or QTH on file."""
        rows = await self._query(
            "SELECT callsign, first_name, qth, country FROM callsign_info"
            " WHERE first_name IS NOT NULL OR qth IS NOT NULL"
        )
        return {
            r["callsign"]: {"first_name": r["first_name"], "qth": r["qth"], "country": r["country"]}
            for r in rows
        }

    async def count_callsign_info(self) -> dict[str, int]:
        rows = await self._query("SELECT status, COUNT(*) AS n FROM callsign_info GROUP BY status")
        counts = {"found": 0, "not_found": 0}
        for r in rows:
            counts[r["status"]] = int(r["n"])
        return counts

    async def get_recent_callsign_activity(self, since_ms: int) -> list[dict[str, Any]]:
        """Callsigns (with SSID) seen since `since_ms`: message senders and
        heard stations, each with its newest timestamp and whether it sent a
        message. SSIDs are stripped by the caller, which also filters shapes."""
        sender = sender_base_sql("src")
        return await self._query(
            f"""SELECT callsign, MAX(ts) AS last_ts, MAX(chatted) AS chatted FROM (
                    SELECT {sender} AS callsign, MAX(timestamp) AS ts, 1 AS chatted
                      FROM messages WHERE timestamp >= ? AND type = 'msg'
                     GROUP BY {sender}
                    UNION ALL
                    SELECT UPPER(TRIM(callsign)), last_seen, 0
                      FROM station_positions WHERE last_seen >= ?
                ) GROUP BY callsign""",  # noqa: S608 - sender_base_sql is a fixed expression over a literal column
            (since_ms, since_ms),
        )
