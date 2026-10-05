"""NodeAdminMixin: storage for the RM1 (HMAC remote admin) feature (schema v35).

Three tables (plan: `doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md` §4):

  * `node_admin_keys`  - one SecretBox token per managed node (`target_call`).
  * `node_admin_state` - the monotonic command counter `ctr`, the verified
    high-water mark `last_hwm`, `last_sync_at`, `tx_max`. KEPT when a key is
    deleted: a re-added key must continue the counter, because restarting at 1
    means every command is silently replay-rejected by the node.
  * `node_admin_log`   - one row per command (and per sync probe, `ctr = 0`).

All timestamps are MILLISECONDS. Every method here does its work through
`db_write` / `db_read` inside `asyncio.to_thread`, the sanctioned
read-modify-write pattern of `uptime.py` and `classifier_api.py`:

  * NEVER `_mutate` for the counter: it returns the row count only, so the
    allocated value could not be read back in the same transaction.
  * NEVER `_query` for anything that must commit: it opens a READ connection
    that never commits, and closing it rolls everything back with no error.

`target_call` is expected to be normalised (`strip().upper()`) by the caller at
the API boundary; this layer stores whatever it is given.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any

from ..logging_setup import get_logger
from ..util import now_ms as _now_ms
from ._base import StorageBase
from .constants import db_read, db_write

logger = get_logger(__name__)

# The command counter is an unsigned 32-bit value on the node.
NODE_ADMIN_CTR_MAX = 4_294_967_295
# Default `tx_max` of a state row (mirrors the column default).
NODE_ADMIN_DEFAULT_TX_MAX = 15


class NodeAdminCounterExhausted(ValueError):  # noqa: N818 - domain name reads better than *Error
    """The next counter value would exceed 4294967295; the command is refused."""


_LOG_COLUMNS = (
    "id, target_call, src_call, ctr, cmd, args, text, sent_at, handed_off_at,"
    " transport, send_error, reply_text, reply_at, verified, result"
)


class NodeAdminMixin(StorageBase):
    # ── keys ────────────────────────────────────────────────────────────────

    async def set_node_admin_key(
        self, target: str, password_enc: str, tx_max: int | None = None
    ) -> None:
        """Upsert the encrypted password; ensure a state row; set `tx_max` only if given.

        `created_at` survives a replacement. An existing counter is never touched.
        """
        ts = _now_ms()

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO node_admin_keys"
                    " (target_call, password_enc, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(target_call) DO UPDATE SET"
                    " password_enc = excluded.password_enc, updated_at = excluded.updated_at",
                    (target, password_enc, ts, ts),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO node_admin_state (target_call) VALUES (?)", (target,)
                )
                if tx_max is not None:
                    conn.execute(
                        "UPDATE node_admin_state SET tx_max = ? WHERE target_call = ?",
                        (tx_max, target),
                    )

        await asyncio.to_thread(_run)

    async def get_node_admin_key(self, target: str) -> str | None:
        """Return the stored SecretBox token for `target`, or None."""

        def _run() -> str | None:
            with db_read(self.db_path) as conn:
                row = conn.execute(
                    "SELECT password_enc FROM node_admin_keys WHERE target_call = ?", (target,)
                ).fetchone()
                return str(row[0]) if row else None

        return await asyncio.to_thread(_run)

    async def delete_node_admin_key(self, target: str) -> None:
        """Delete the KEY only. The state row (counter, hwm) and the log are kept."""

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute("DELETE FROM node_admin_keys WHERE target_call = ?", (target,))

        await asyncio.to_thread(_run)

    async def list_node_admin_targets(self) -> list[dict[str, Any]]:
        """Every target with a key or a state row, ordered by callsign.

        `has_key` is False for a target whose key was deleted but whose counter
        state is retained.
        """

        def _run() -> list[dict[str, Any]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT s.target_call AS target, (k.target_call IS NOT NULL) AS has_key,"
                    " s.ctr AS ctr, s.last_hwm AS last_hwm, s.last_sync_at AS last_sync_at,"
                    " s.tx_max AS tx_max"
                    " FROM node_admin_state s"
                    " LEFT JOIN node_admin_keys k ON k.target_call = s.target_call"
                    " UNION ALL"
                    " SELECT k.target_call, 1, 0, 0, NULL, ?"
                    " FROM node_admin_keys k"
                    " WHERE NOT EXISTS (SELECT 1 FROM node_admin_state s"
                    "                   WHERE s.target_call = k.target_call)"
                    " ORDER BY 1",
                    (NODE_ADMIN_DEFAULT_TX_MAX,),
                ).fetchall()
                out = [dict(r) for r in rows]
            for item in out:
                item["has_key"] = bool(item["has_key"])
            return out

        return await asyncio.to_thread(_run)

    # ── counter + log ───────────────────────────────────────────────────────

    async def allocate_node_admin_command(  # noqa: PLR0913, PLR0917 - signature pinned for W2 (plan §6)
        self,
        target: str,
        src_call: str,
        cmd: str,
        args: str | None,
        transport: str | None,
        now_ms: int,
        unix_floor_s: int,
        build_text: Callable[[int], str],
    ) -> dict[str, Any]:
        """Allocate the next counter value and write the log row, atomically.

        ONE `db_write` transaction: ensure the state row, then
        `ctr = MAX(ctr + 1, last_hwm + 1, unix_floor_s) ... RETURNING ctr`
        (needs SQLite >= 3.35), `build_text(ctr)` computed INSIDE the
        transaction, then the log INSERT. A raise from `build_text` or a
        refusal rolls the counter bump back too, so no value is burned by a
        failed build.

        `unix_floor_s` may be 0 (clock before 2024). Raises
        `NodeAdminCounterExhausted` if the value would exceed 4294967295.
        Returns `{id, ctr, text}`.
        """

        def _run() -> dict[str, Any]:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO node_admin_state (target_call) VALUES (?)", (target,)
                )
                rows = conn.execute(
                    "UPDATE node_admin_state"
                    " SET ctr = MAX(ctr + 1, last_hwm + 1, ?)"
                    " WHERE target_call = ? RETURNING ctr",
                    (unix_floor_s, target),
                ).fetchall()
                ctr = int(rows[0][0])
                if ctr > NODE_ADMIN_CTR_MAX:
                    raise NodeAdminCounterExhausted(
                        f"node admin counter for {target} would exceed {NODE_ADMIN_CTR_MAX}"
                    )
                text = build_text(ctr)
                cursor = conn.execute(
                    "INSERT INTO node_admin_log"
                    " (target_call, src_call, ctr, cmd, args, text, sent_at, transport)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (target, src_call, ctr, cmd, args, text, now_ms, transport),
                )
                return {"id": cursor.lastrowid, "ctr": ctr, "text": text}

        return await asyncio.to_thread(_run)

    async def insert_node_admin_sync_row(
        self, target: str, src_call: str, transport: str | None, now_ms: int, text: str
    ) -> int:
        """Log a sync probe (`ctr` 0, `cmd` 'sync'). Never touches the counter."""

        def _run() -> int:
            with db_write(self.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO node_admin_log"
                    " (target_call, src_call, ctr, cmd, text, sent_at, transport)"
                    " VALUES (?, ?, 0, 'sync', ?, ?, ?)",
                    (target, src_call, text, now_ms, transport),
                )
                return int(cursor.lastrowid or 0)

        return await asyncio.to_thread(_run)

    async def mark_node_admin_handed_off(
        self, log_id: int, handed_off_at: int, send_error: str | None = None
    ) -> None:
        """Stamp the hand-off to the transport; `send_error` records a failed transmit."""

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "UPDATE node_admin_log SET handed_off_at = ?, send_error = ? WHERE id = ?",
                    (handed_off_at, send_error, log_id),
                )

        await asyncio.to_thread(_run)

    async def apply_node_admin_reply(
        self, log_id: int, reply_text: str, reply_at: int, result: str, verified: bool
    ) -> bool:
        """Record a reply. A verified row is final: nothing overwrites it.

        `UPDATE ... WHERE id = ? AND verified IS NOT 1`, so a late unverified
        (bad tag) reply never downgrades a verified one, and an unverified one
        followed by a genuine one ends verified. Returns True only when a row
        changed: a second verified reply returns False, so the caller can act
        on the verification exactly once. Callers must act on the True of a
        `verified=True` call only; an unverified update also returns True.
        """

        def _run() -> bool:
            with db_write(self.db_path) as conn:
                cursor = conn.execute(
                    "UPDATE node_admin_log"
                    " SET reply_text = ?, reply_at = ?, result = ?, verified = ?"
                    " WHERE id = ? AND verified IS NOT 1",
                    (reply_text, reply_at, result, 1 if verified else 0, log_id),
                )
                return cursor.rowcount > 0

        return await asyncio.to_thread(_run)

    async def raise_node_admin_hwm(
        self, target: str, hwm: int, sync_at_ms: int | None = None
    ) -> None:
        """`last_hwm = MAX(last_hwm, hwm)`: never lowers. Optionally stamps `last_sync_at`.

        Call only with a value from a tag-verified reply.
        """

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO node_admin_state (target_call) VALUES (?)", (target,)
                )
                conn.execute(
                    "UPDATE node_admin_state SET last_hwm = MAX(last_hwm, ?) WHERE target_call = ?",
                    (hwm, target),
                )
                if sync_at_ms is not None:
                    conn.execute(
                        "UPDATE node_admin_state SET last_sync_at = ? WHERE target_call = ?",
                        (sync_at_ms, target),
                    )

        await asyncio.to_thread(_run)

    async def node_admin_history(
        self, target: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Log rows, newest first; one target or all."""

        def _run() -> list[dict[str, Any]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if target is None:
                    cursor = conn.execute(
                        f"SELECT {_LOG_COLUMNS} FROM node_admin_log"  # noqa: S608 - literal column list
                        " ORDER BY id DESC LIMIT ?",
                        (limit,),
                    )
                else:
                    cursor = conn.execute(
                        f"SELECT {_LOG_COLUMNS} FROM node_admin_log"  # noqa: S608 - literal column list
                        " WHERE target_call = ? ORDER BY id DESC LIMIT ?",
                        (target, limit),
                    )
                return [dict(r) for r in cursor.fetchall()]

        return await asyncio.to_thread(_run)

    async def abandon_stale_node_admin_rows(self, now_ms: int) -> int:
        """Startup sweep: mark rows a previous process left without any outcome.

        `now_ms` is this process's start. A row with no reply, no verdict and
        no result that was sent before it can never be completed by this
        process's in-memory state: it becomes `result = 'abandoned'`. A late
        reply may still flip it (`apply_node_admin_reply`). Returns the count.
        """

        def _run() -> int:
            with db_write(self.db_path) as conn:
                cursor = conn.execute(
                    "UPDATE node_admin_log SET result = 'abandoned'"
                    " WHERE reply_at IS NULL AND verified IS NULL AND result IS NULL"
                    " AND sent_at < ?",
                    (now_ms,),
                )
                return cursor.rowcount

        return await asyncio.to_thread(_run)

    async def prune_node_admin_log(self, per_target_cap: int = 1000) -> int:
        """Keep the newest `per_target_cap` rows per target. Returns rows deleted."""

        def _run() -> int:
            with db_write(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM node_admin_log WHERE id IN ("
                    " SELECT id FROM ("
                    "  SELECT id, ROW_NUMBER() OVER ("
                    "   PARTITION BY target_call ORDER BY id DESC) AS rn"
                    "  FROM node_admin_log"
                    " ) WHERE rn > ?)",
                    (per_target_cap,),
                )
                return cursor.rowcount

        return await asyncio.to_thread(_run)
