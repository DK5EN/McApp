"""Classifier orchestrator — combines Layer 1 (rules), Layer 2 (template),
Layer 3 (score) into a single Classification per message.

The classifier runs inline during store_message(); a failure in any
layer must never block ingestion, so exceptions are caught and a
deterministic fallback classification is returned.

Template events (`proxy:classifier_template_event`) and reclassify
progress events are surfaced via optional async callbacks wired by
main.py — the classifier itself never imports the SSE layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from . import rules as rules_mod
from . import score as score_mod
from . import template as template_mod

logger = get_logger(__name__)

OnTemplateEvent = Callable[[dict[str, Any]], Awaitable[None]]
OnReclassifyProgress = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Classification:
    category: str
    tags: tuple[str, ...]
    info_score: float
    template_hash: str
    classifier_version: int


@dataclass
class _ReclassifyJob:
    job_id: str
    total: int
    processed: int = 0
    done: bool = False
    started_at: float = field(default_factory=time.time)


def _fallback_classification(msg_text: str | None, version: int) -> Classification:
    """Deterministic fallback when any layer raises. No DB I/O."""
    text = msg_text or ""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return Classification(
        category="other",
        tags=(),
        info_score=0.5,
        template_hash=digest,
        classifier_version=version,
    )


class Classifier:
    def __init__(self, storage: Any) -> None:
        self._storage = storage
        self._rules: list[rules_mod.CompiledRule] = []
        self._version: int = 1
        self._lock = asyncio.Lock()
        self._jobs: dict[str, _ReclassifyJob] = {}
        self.on_template_event: OnTemplateEvent | None = None
        self.on_reclassify_progress: OnReclassifyProgress | None = None

    @property
    def classifier_version(self) -> int:
        return self._version

    async def load(self) -> None:
        """Load version + rules from DB, compile regexes."""
        async with self._lock:
            self._version = await self._read_version()
            self._rules = await rules_mod.load_rules(self._storage)
            logger.info(
                "classifier loaded: version=%d rules=%d",
                self._version,
                len(self._rules),
            )

    async def reload(self) -> None:
        """Alias for load() — used after rule mutations."""
        await self.load()

    async def bump_version(self) -> int:
        """Increment classifier_version, persist, refresh compiled rules."""
        async with self._lock:
            new_version = self._version + 1
            await self._write_meta("classifier_ver", str(new_version))
            self._version = new_version
            self._rules = await rules_mod.load_rules(self._storage)
            logger.info("classifier version bumped to %d", new_version)
            return new_version

    async def classify(
        self,
        msg: dict[str, Any],
        *,
        touch_stats: bool = True,
    ) -> Classification:
        """Classify a single message.

        touch_stats=False skips beacon_templates UPSERT and auto_beacon
        threshold side effects; used by reclassify() so historical rows
        don't double-count.
        """
        text = msg.get("msg")
        now_ms = int(msg.get("timestamp") or time.time() * 1000)
        src = str(msg.get("src") or "")

        try:
            category, tag_set = rules_mod.match(msg, self._rules)

            tpl_hash = template_mod.fingerprint(text)

            tpl_count = 0
            user_action: str | None = None
            if touch_stats:
                row = await template_mod.update_stats(
                    self._storage, tpl_hash, msg, now_ms
                )
                tpl_count = int(row.get("count") or 0)
                user_action = row.get("user_action")
                is_auto, just_crossed = await template_mod.auto_beacon_status(
                    self._storage, tpl_hash, src, now_ms, user_action,
                )
                if is_auto:
                    tag_set.add("auto_beacon")
                if just_crossed and self.on_template_event is not None:
                    try:
                        await self.on_template_event({
                            **row,
                            "auto_beacon": True,
                        })
                    except Exception:
                        logger.warning(
                            "on_template_event callback failed",
                            exc_info=True,
                        )
            else:
                # Look up existing count (if any) without touching stats.
                rows = await self._storage._execute(
                    "SELECT count, user_action FROM beacon_templates "
                    "WHERE template_hash = ?",
                    (tpl_hash,),
                )
                if rows:
                    tpl_count = int(rows[0]["count"] or 0)
                    user_action = rows[0]["user_action"]
                if user_action == "promote":
                    tag_set.add("auto_beacon")

            info_score = score_mod.compute(msg, category, tag_set, tpl_count)

            return Classification(
                category=category,
                tags=tuple(sorted(tag_set)),
                info_score=info_score,
                template_hash=tpl_hash,
                classifier_version=self._version,
            )
        except Exception:
            logger.warning("classifier failed, using fallback", exc_info=True)
            return _fallback_classification(text, self._version)

    async def reclassify(
        self,
        *,
        since: int | None = None,
        category: str | None = None,
    ) -> tuple[str, int]:
        """Kick off a background reclassify job. Returns (job_id, estimated_rows)."""
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("timestamp >= ?")
            params.append(since)
        if category is not None:
            where.append("category = ?")
            params.append(category)
        else:
            where.append("(classifier_ver IS NULL OR classifier_ver < ?)")
            params.append(self._version)

        where_sql = " AND ".join(where)
        count_rows = await self._storage._execute(
            f"SELECT COUNT(*) AS n FROM messages WHERE {where_sql}",
            tuple(params),
        )
        total = int(count_rows[0]["n"]) if count_rows else 0

        job_id = secrets.token_hex(6)
        self._jobs[job_id] = _ReclassifyJob(job_id=job_id, total=total)
        asyncio.create_task(self._run_reclassify(job_id, where_sql, tuple(params)))
        logger.info("reclassify scheduled job=%s rows=%d", job_id, total)
        return job_id, total

    async def _run_reclassify(
        self,
        job_id: str,
        where_sql: str,
        where_params: tuple[Any, ...],
    ) -> None:
        job = self._jobs[job_id]
        batch = 500
        last_id = 0
        try:
            while True:
                rows = await self._storage._execute(
                    f"SELECT id, msg_id, src, dst, msg, type, timestamp "
                    f"FROM messages WHERE {where_sql} AND id > ? "
                    f"ORDER BY id ASC LIMIT ?",
                    (*where_params, last_id, batch),
                )
                if not rows:
                    break
                for row in rows:
                    cls = await self.classify(
                        {
                            "msg": row["msg"],
                            "src": row["src"],
                            "dst": row["dst"],
                            "type": row["type"],
                            "timestamp": row["timestamp"],
                        },
                        touch_stats=False,
                    )
                    await self._storage._execute(
                        "UPDATE messages SET category = ?, tags = ?, "
                        "info_score = ?, template_hash = ?, classifier_ver = ? "
                        "WHERE id = ?",
                        (
                            cls.category,
                            json.dumps(list(cls.tags)),
                            cls.info_score,
                            cls.template_hash,
                            cls.classifier_version,
                            row["id"],
                        ),
                        fetch=False,
                    )
                    last_id = row["id"]
                    job.processed += 1
                await self._emit_progress(job, done=False)
        except Exception:
            logger.warning("reclassify job=%s failed", job_id, exc_info=True)
        finally:
            job.done = True
            await self._emit_progress(job, done=True)

    async def _emit_progress(self, job: _ReclassifyJob, *, done: bool) -> None:
        if self.on_reclassify_progress is None:
            return
        try:
            await self.on_reclassify_progress({
                "job_id": job.job_id,
                "total": job.total,
                "processed": job.processed,
                "done": done,
            })
        except Exception:
            logger.warning(
                "on_reclassify_progress callback failed", exc_info=True
            )

    async def collect_stats(self) -> dict[str, Any]:
        """Stats payload for the 60-second proxy:classifier_stats broadcast."""
        now_ms = int(time.time() * 1000)
        cutoff_30d = now_ms - 30 * 24 * 60 * 60 * 1000
        cutoff_24h = now_ms - 24 * 60 * 60 * 1000
        cutoff_7d = now_ms - 7 * 24 * 60 * 60 * 1000

        counts_rows = await self._storage._execute(
            "SELECT category, COUNT(*) AS n FROM messages "
            "WHERE category IS NOT NULL AND timestamp >= ? "
            "GROUP BY category",
            (cutoff_30d,),
        )
        recent_rows = await self._storage._execute(
            "SELECT category, COUNT(*) AS n FROM messages "
            "WHERE category IS NOT NULL AND timestamp >= ? "
            "GROUP BY category",
            (cutoff_24h,),
        )
        top_rows = await self._storage._execute(
            "SELECT template_hash, example_msg, count, auto_beacon "
            "FROM beacon_templates "
            "WHERE last_seen >= ? "
            "ORDER BY count DESC LIMIT 10",
            (self._iso_from_ms(cutoff_7d),),
        )

        return {
            "counts": {r["category"]: int(r["n"]) for r in counts_rows},
            "recent_24h": {r["category"]: int(r["n"]) for r in recent_rows},
            "top_templates": [
                {
                    "template_hash": r["template_hash"],
                    "example_msg": r["example_msg"],
                    "count": int(r["count"]),
                    "auto_beacon": bool(r["auto_beacon"]),
                }
                for r in top_rows
            ],
        }

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job.job_id,
            "total": job.total,
            "processed": job.processed,
            "done": job.done,
        }

    def all_jobs(self) -> list[dict[str, Any]]:
        return [
            {
                "job_id": j.job_id,
                "total": j.total,
                "processed": j.processed,
                "done": j.done,
            }
            for j in self._jobs.values()
        ]

    async def _read_version(self) -> int:
        rows = await self._storage._execute(
            "SELECT value FROM classifier_meta WHERE key = 'classifier_ver'"
        )
        if rows:
            try:
                return int(rows[0]["value"])
            except (TypeError, ValueError):
                return 1
        await self._write_meta("classifier_ver", "1")
        return 1

    async def _write_meta(self, key: str, value: str) -> None:
        await self._storage._execute(
            "INSERT INTO classifier_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
            fetch=False,
        )

    @staticmethod
    def _iso_from_ms(ms: int) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
