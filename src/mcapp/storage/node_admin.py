"""NodeAdminMixin: storage for the RM1 (HMAC remote admin) feature (schema v35).

Three tables (plan: `doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md` §4):

  * `node_admin_keys`  - one SecretBox token per managed node (`target_call`).
  * `node_admin_state` - the monotonic command counter `ctr`, the verified
    high-water mark `last_hwm`, `last_sync_at`, `tx_max`. Removed together with
    the key and the log (`delete_node_admin_target`): a re-added key starts a fresh
    row at the unix floor and learns the node's mark by a verified sync first.
    Replacing a password (`set_node_admin_key`) keeps the row and its counter.
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
from ..remote_cmd import RM_REPLY_TIMEOUT_MS
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

    async def delete_node_admin_target(self, target: str) -> None:
        """Remove the node completely: key, state row and every log row, in ONE transaction.

        The counter state is not needed afterwards: a fresh row starts at the unix floor
        (`allocate_node_admin_command`), and no command is sent before a verified sync has
        learned the node's own mark. The service refuses the call while the cool-down or the
        lockout window, which are derived from the log rows, is still running.
        """

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute("DELETE FROM node_admin_log WHERE target_call = ?", (target,))
                conn.execute("DELETE FROM node_admin_state WHERE target_call = ?", (target,))
                conn.execute("DELETE FROM node_admin_keys WHERE target_call = ?", (target,))

        await asyncio.to_thread(_run)

    async def list_node_admin_targets(self) -> list[dict[str, Any]]:
        """Every target with a key or a state row, ordered by callsign.

        `has_key` is False only for a state row without a key (a legacy row left by
        the old key-only delete).
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

    async def reset_node_admin_hwm(self, target: str) -> None:
        """`last_hwm = 0` for a node whose password was entered again (it may be re-flashed).

        `ctr` is kept: the counter stays monotone, and the allocation's unix floor keeps
        new values above whatever mark a re-flashed node starts from.
        """

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "UPDATE node_admin_state SET last_hwm = 0 WHERE target_call = ?", (target,)
                )

        await asyncio.to_thread(_run)

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

    async def node_admin_state_rows(self, target: str, recent: int = 200) -> list[dict[str, Any]]:
        """The rows the `/state` fold needs, newest first, in ONE read.

        The newest `recent` rows of `target` UNION the newest tag-verified row per
        `(cmd, args)` of it, deduplicated by id (one `WHERE ... OR` over the id set). The
        recent window carries every pending, silent and re-asked row; the verified-per-command
        rows keep a value the node reported long ago (an old `status`) in view however many
        rows were logged since. Prune keeps the same rows (`prune_node_admin_log`).
        """

        def _run() -> list[dict[str, Any]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"SELECT {_LOG_COLUMNS} FROM node_admin_log"  # noqa: S608 - literal column list
                    " WHERE target_call = ? AND ("
                    "  id IN (SELECT id FROM node_admin_log WHERE target_call = ?"
                    "         ORDER BY id DESC LIMIT ?)"
                    "  OR id IN (SELECT MAX(id) FROM node_admin_log"
                    "            WHERE target_call = ? AND verified = 1 GROUP BY cmd, args))"
                    " ORDER BY id DESC",
                    (target, target, recent, target),
                )
                return [dict(r) for r in cursor.fetchall()]

        return await asyncio.to_thread(_run)

    async def get_node_admin_log_row(self, log_id: int) -> dict[str, Any] | None:
        """One log row by its id, whatever its age or state, or None."""

        def _run() -> dict[str, Any] | None:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    f"SELECT {_LOG_COLUMNS} FROM node_admin_log WHERE id = ?",  # noqa: S608 - literal column list
                    (log_id,),
                ).fetchone()
                return dict(row) if row is not None else None

        return await asyncio.to_thread(_run)

    async def find_node_admin_log_row(
        self, target: str, ctr: int, now_ms: int
    ) -> dict[str, Any] | None:
        """The log row a reply to `(target, ctr)` belongs to, or None.

        `ctr > 0`: the unique row for that counter (the partial unique index
        guarantees one), whatever its state, so a late reply can still reach an
        abandoned row after a restart. `ctr == 0` is a sync reply: all syncs
        share ctr 0 and the reply tag carries no request freshness, so it is
        the NEWEST sync row of the target that has no reply yet AND whose
        hand-off (`sent_at` if it never got one) is less than
        `RM_REPLY_TIMEOUT_MS` before `now_ms`: an older open sync row has
        already been given up on and must not be rewritten by a late or
        duplicate reply.
        """

        def _run() -> dict[str, Any] | None:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if ctr > 0:
                    cursor = conn.execute(
                        f"SELECT {_LOG_COLUMNS} FROM node_admin_log"  # noqa: S608 - literal column list
                        " WHERE target_call = ? AND ctr = ?",
                        (target, ctr),
                    )
                else:
                    cursor = conn.execute(
                        f"SELECT {_LOG_COLUMNS} FROM node_admin_log"  # noqa: S608 - literal column list
                        " WHERE target_call = ? AND ctr = 0 AND cmd = 'sync' AND reply_at IS NULL"
                        " AND ? - COALESCE(handed_off_at, sent_at) < ?"
                        " ORDER BY id DESC LIMIT 1",
                        (target, now_ms, RM_REPLY_TIMEOUT_MS),
                    )
                row = cursor.fetchone()
                return dict(row) if row is not None else None

        return await asyncio.to_thread(_run)

    async def node_admin_verified_status_rows(
        self, target: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """The newest tag-verified `status` rows of `target` (newest first).

        Read by the txpower bound: the newest of them that carries `p=<cur>/<max>`
        names the board's maximum, however many other rows lie in between.
        """

        def _run() -> list[dict[str, Any]]:
            with db_read(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"SELECT {_LOG_COLUMNS} FROM node_admin_log"  # noqa: S608 - literal column list
                    " WHERE target_call = ? AND cmd = 'status' AND verified = 1"
                    " ORDER BY id DESC LIMIT ?",
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
        """Keep the newest `per_target_cap` rows per target. Returns rows deleted.

        The newest tag-verified row per `(target, cmd, args)` is kept beyond the cap: it is
        the last known state of the node (`node_admin_state_rows`).
        """

        def _run() -> int:
            with db_write(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM node_admin_log WHERE id IN ("
                    " SELECT id FROM ("
                    "  SELECT id, ROW_NUMBER() OVER ("
                    "   PARTITION BY target_call ORDER BY id DESC) AS rn"
                    "  FROM node_admin_log"
                    " ) WHERE rn > ?)"
                    " AND id NOT IN (SELECT MAX(id) FROM node_admin_log"
                    "                WHERE verified = 1 GROUP BY target_call, cmd, args)",
                    (per_target_cap,),
                )
                return cursor.rowcount

        return await asyncio.to_thread(_run)
