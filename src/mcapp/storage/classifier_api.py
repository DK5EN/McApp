"""ClassifierApiMixin: beacon_templates CRUD and classification bookkeeping.

Moved out of sqlite_storage.py (ST-04). Backs the classifier subtree's
StorageProtocol (`classifier/types.py`) so mc-chat's classifier package can run
against this storage without any meshcom_mock imports.
"""

import asyncio
import json
from typing import Any

from ..classifier.types import ms_to_zulu
from ..util import now_ms
from ._base import StorageBase
from .constants import _MSG_SELECT, db_write, escape_like


class ClassifierApiMixin(StorageBase):
    # ── Classifier Storage API ─────────────────────────────────────────────
    # These methods satisfy the StorageProtocol defined in
    # classifier/types.py, allowing the shared classifier subtree to run
    # in MCProxy without any meshcom_mock imports.

    async def list_beacon_templates(
        self,
        min_count: int = 0,
        auto_only: bool = False,
        limit: int = 100,
        user_action_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List beacon templates, optionally filtered by min count / auto-beacon flag."""
        where: list[str] = []
        params: list[Any] = []
        if user_action_only:
            # Every override, regardless of count: min_count is ignored on purpose.
            where.append("user_action IS NOT NULL")
        elif min_count > 0:
            where.append("count >= ?")
            params.append(min_count)
        if auto_only:
            where.append("auto_beacon = 1")
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        return await self._query(
            "SELECT template_hash, example_msg, example_src, srcs, count, "  # noqa: S608 - identifiers from fixed set; values parameterized
            "first_seen, last_seen, auto_beacon, user_action "
            f"FROM beacon_templates{where_sql} "
            "ORDER BY count DESC LIMIT ?",
            tuple(params),
        )

    async def beacon_template_exists(self, template_hash: str) -> bool:
        """Check whether a beacon_templates row with this hash exists."""
        rows = await self._query(
            "SELECT template_hash FROM beacon_templates WHERE template_hash = ?",
            (template_hash,),
        )
        return bool(rows)

    async def set_beacon_template_user_action(self, template_hash: str, action: str | None) -> None:
        """Set (or clear, if action is None) the user override on a beacon template."""
        await self._mutate(
            "UPDATE beacon_templates SET user_action = ? WHERE template_hash = ?",
            (action, template_hash),
        )

    async def get_messages_by_template_hash(
        self, template_hash: str, limit: int
    ) -> list[dict[str, Any]]:
        """Recent messages matching a beacon template hash (classifier template preview)."""
        return await self._query(
            "SELECT id, msg_id, src, dst, msg, type, timestamp, category, tags, "
            "info_score FROM messages WHERE template_hash = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (template_hash, limit),
        )

    async def get_beacon_template(self, template_hash: str) -> dict[str, Any] | None:
        rows = await self._query(
            "SELECT * FROM beacon_templates WHERE template_hash = ?",
            (template_hash,),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            row["srcs"] = json.loads(row["srcs"]) if row.get("srcs") else []
        except (json.JSONDecodeError, TypeError):
            row["srcs"] = []
        row["auto_beacon"] = bool(row["auto_beacon"])
        return row

    async def upsert_beacon_template(
        self, hash_: str, msg: str, src: str, now_ms: int
    ) -> dict[str, Any]:
        now_zulu = ms_to_zulu(now_ms)

        def _run() -> None:
            with db_write(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO beacon_templates "
                    "(template_hash, example_msg, example_src, srcs, count, "
                    " first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (hash_, msg, src, json.dumps([src]), now_zulu, now_zulu),
                )
                # Load current srcs and merge
                row = conn.execute(
                    "SELECT srcs FROM beacon_templates WHERE template_hash = ?",
                    (hash_,),
                ).fetchone()
                srcs: list[str] = []
                if row and row[0]:
                    try:
                        srcs = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        srcs = []
                if src not in srcs:
                    srcs.append(src)
                conn.execute(
                    "UPDATE beacon_templates "
                    "SET count = count + 1, example_msg = ?, example_src = ?, "
                    "    srcs = ?, last_seen = ? "
                    "WHERE template_hash = ?",
                    (msg, src, json.dumps(srcs), now_zulu, hash_),
                )
                conn.commit()

        await asyncio.to_thread(_run)
        result = await self.get_beacon_template(hash_)
        if result is None:
            raise RuntimeError("result is unexpectedly None")
        return result

    async def set_template_auto_beacon(
        self, template_hash: str, auto_beacon: bool
    ) -> dict[str, Any] | None:
        await self._mutate(
            "UPDATE beacon_templates SET auto_beacon = ? WHERE template_hash = ?",
            (int(auto_beacon), template_hash),
        )
        return await self.get_beacon_template(template_hash)

    async def count_recent_messages_by_template_src(
        self, hash_: str, src: str, since_ms: int
    ) -> int:
        # MCProxy stores timestamp as INTEGER ms (not ISO string)
        rows = await self._query(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE template_hash = ? AND src = ? AND timestamp >= ?",
            (hash_, src, since_ms),
        )
        return int(rows[0]["n"]) if rows else 0

    async def clear_stale_auto_beacons(
        self, human_categories: frozenset[str], min_tokens: int
    ) -> int:
        from ..classifier.template import _tokenize_normalized  # noqa: PLC0415 - circular

        cleared = 0
        placeholders = ",".join("?" * len(human_categories))

        def _clear_by_category() -> int:
            with db_write(self.db_path) as conn:
                cur = conn.execute(
                    f"UPDATE beacon_templates SET auto_beacon = 0 "  # noqa: S608 - identifiers from fixed set; values parameterized
                    f"WHERE user_action IS NULL AND auto_beacon = 1 "
                    f"AND EXISTS ("
                    f"  SELECT 1 FROM messages m "
                    f"  WHERE m.template_hash = beacon_templates.template_hash "
                    f"  AND (m.category IN ({placeholders}) OR EXISTS ("
                    f"    SELECT 1 FROM json_each("
                    f"      CASE WHEN json_valid(m.tags) THEN m.tags ELSE '[]' END"
                    f"    ) j WHERE j.value = 'alert'"
                    f"  )) LIMIT 1"
                    f")",
                    tuple(human_categories),
                )
                conn.commit()
                return cur.rowcount

        cleared += await asyncio.to_thread(_clear_by_category)

        rows = await self._query(
            "SELECT template_hash, example_msg FROM beacon_templates "
            "WHERE auto_beacon = 1 AND user_action IS NULL"
        )
        # ST-18: tokenization (Python-only, can't push into SQL) decides which rows
        # qualify, but the UPDATEs themselves are batched into one _execute_many call
        # instead of one _mutate (= one connection open/commit) per stale template.
        stale_hashes = [
            row["template_hash"]
            for row in rows
            if len(_tokenize_normalized(row["example_msg"] or "")) <= min_tokens
        ]
        if stale_hashes:
            await self._execute_many(
                "UPDATE beacon_templates SET auto_beacon = 0 WHERE template_hash = ?",
                [(h,) for h in stale_hashes],
            )
        cleared += len(stale_hashes)
        return cleared

    async def get_classifier_version(self) -> int:
        ver_str = await self.get_meta("classifier_version")
        try:
            return int(ver_str) if ver_str is not None else 0
        except (ValueError, TypeError):
            return 0

    async def count_messages_to_classify(self, *, classifier_ver_below: int | None = None) -> int:
        if classifier_ver_below is None:
            rows = await self._query("SELECT COUNT(*) AS n FROM messages")
        else:
            rows = await self._query(
                "SELECT COUNT(*) AS n FROM messages "
                "WHERE classifier_ver IS NULL OR classifier_ver < ?",
                (classifier_ver_below,),
            )
        return int(rows[0]["n"]) if rows else 0

    async def get_messages_to_classify(
        self,
        *,
        classifier_ver_below: int | None = None,
        category: str | None = None,
        since_ms: int | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[object] = []
        if classifier_ver_below is not None:
            conditions.append("(classifier_ver IS NULL OR classifier_ver < ?)")
            params.append(classifier_ver_below)
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if since_ms is not None:
            conditions.append("timestamp >= ?")
            params.append(since_ms)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return await self._query(
            f"SELECT id, {_MSG_SELECT} FROM messages {where} ORDER BY id ASC LIMIT ? OFFSET ?",  # noqa: S608 - identifiers from fixed set; values parameterized
            (*params, limit, offset),
        )

    async def update_message_classification(  # noqa: PLR0913 - signature fixed by call sites
        self,
        row_id: Any,
        *,
        category: str,
        tags: list[str],
        info_score: float,
        template_hash: str,
        classifier_ver: int,
    ) -> None:
        await self._mutate(
            "UPDATE messages SET category = ?, tags = ?, info_score = ?, "
            "template_hash = ?, classifier_ver = ? WHERE id = ?",
            (category, json.dumps(tags), info_score, template_hash, classifier_ver, row_id),
        )

    async def get_meta(self, key: str) -> str | None:
        rows = await self._query("SELECT value FROM classifier_meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._mutate(
            "INSERT INTO classifier_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def count_messages_by_category(self, since_ms: int) -> dict[str, int]:
        # MCProxy timestamp is INTEGER ms
        rows = await self._query(
            "SELECT category, COUNT(*) AS n FROM messages "
            "WHERE category IS NOT NULL AND timestamp >= ? GROUP BY category",
            (since_ms,),
        )
        return {r["category"]: r["n"] for r in rows}

    async def count_messages_by_tag(self, since_ms: int) -> dict[str, int]:
        """Per-tag message counts since ``since_ms`` (INTEGER ms timestamps).

        A multi-tag message counts once per tag, so values do not sum to the
        message count. Malformed ``tags`` JSON is skipped, never raised: the
        ``json_valid`` guard sits inside the ``json_each`` argument.
        """
        rows = await self._query(
            "SELECT j.value AS tag, COUNT(*) AS n FROM messages m,"
            " json_each(CASE WHEN json_valid(m.tags) THEN m.tags ELSE '[]' END) j"
            " WHERE m.timestamp >= ? AND j.type = 'text' GROUP BY j.value",
            (since_ms,),
        )
        return {r["tag"]: r["n"] for r in rows}

    async def backfill_auto_beacon_templates(self) -> int:
        """Flag templates that now cross the AUTO_BEACON_RULES thresholds.

        Mirrors (8, any window) / (5, 24h) / (3, 72h). The live path counts the
        24h/72h rules per sender and the lifetime rule only for single-sender
        templates; the template row has no per-sender counts, so every arm
        requires a single sender here -- three people greeting each other must
        not become a beacon on the next startup. Rows with a user override
        (promote/demote) are left alone. Returns the number newly flagged.
        """
        now = now_ms()
        c24, c72 = ms_to_zulu(now - 24 * 3600 * 1000), ms_to_zulu(now - 72 * 3600 * 1000)
        where = (
            "auto_beacon = 0"
            " AND (user_action IS NULL OR user_action NOT IN ('promote','demote'))"
            " AND json_array_length(CASE WHEN json_valid(srcs) THEN srcs ELSE '[]' END) = 1"
            " AND (count >= 8"
            " OR (count >= 5 AND last_seen >= ?)"
            " OR (count >= 3 AND last_seen >= ?))"
        )
        return await self._mutate(
            f"UPDATE beacon_templates SET auto_beacon = 1 WHERE {where}",  # noqa: S608 - fixed text; values bound
            (c24, c72),
        )

    async def count_blocked_text_hits_24h(self) -> dict[str, int]:
        """Per blocked-text substring-match count over the last 24h.

        Mirrors the frontend ``isTextBlocked()`` semantics: case-insensitive
        substring match. Returns ``{text: count}`` for every blocked text.

        ST-08: this used to fetch every message body from the last 24h into
        Python and substring-match each one against every blocked text — run
        from the 60s classifier-stats broadcast. ASCII blocked texts are now
        counted with SQL `LIKE` (COUNT pushed into SQLite, no message bodies
        pulled into Python). SQLite's `LIKE` only case-folds ASCII letters
        (case-sensitive for anything else), so a text containing non-ASCII
        characters (e.g. umlauts) falls back to the original full-scan Python
        matching — otherwise a blocked German word would silently stop
        matching differently-cased occurrences.
        """
        texts = await self.get_blocked_texts()
        if not texts:
            return {}
        since_ms = now_ms() - 24 * 3600 * 1000
        counts = dict.fromkeys(texts, 0)

        ascii_texts = [t for t in texts if t.isascii()]
        unicode_texts = [t for t in texts if not t.isascii()]

        for text in ascii_texts:
            escaped = escape_like(text)
            rows = await self._query(
                "SELECT COUNT(*) as c FROM messages"
                " WHERE timestamp >= ? AND msg LIKE ? ESCAPE '\\'",
                (since_ms, f"%{escaped}%"),
            )
            counts[text] = rows[0]["c"] if rows else 0

        if unicode_texts:
            rows = await self._query(
                "SELECT msg FROM messages WHERE timestamp >= ? AND msg IS NOT NULL",
                (since_ms,),
            )
            lowered = [(t, t.lower()) for t in unicode_texts]
            for r in rows:
                m = (r["msg"] or "").lower()
                for orig, tl in lowered:
                    if tl in m:
                        counts[orig] += 1

        return counts

    async def get_top_beacon_templates(
        self, since_ms: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        cutoff = ms_to_zulu(since_ms)
        rows = await self._query(
            "SELECT template_hash, example_msg, count, auto_beacon "
            "FROM beacon_templates WHERE last_seen >= ? "
            "ORDER BY count DESC LIMIT ?",
            (cutoff, limit),
        )
        for r in rows:
            r["auto_beacon"] = bool(r["auto_beacon"])
        return rows

    async def count_auto_beacon_templates(self) -> int:
        rows = await self._query("SELECT COUNT(*) AS n FROM beacon_templates WHERE auto_beacon = 1")
        return int(rows[0]["n"]) if rows else 0

    def get_heartbeat_window_size(self) -> int:
        return 0

    async def bump_classifier_version(self) -> int:
        current = await self.get_classifier_version()
        new_ver = current + 1
        await self.set_meta("classifier_version", str(new_ver))
        return new_ver
