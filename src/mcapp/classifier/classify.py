"""Orchestrator for the message classification pipeline (Layers 1-3).

Public surface:
    Classifier  -- load the engine, classify a message, run background reclassify.
    ReclassifyJob -- lightweight progress token returned by reclassify().

Usage::

    storage = Storage(":memory:")
    await storage.initialize()
    classifier = Classifier(storage, engine, contract_id)
    await classifier.load()
    storage.attach_classifier(classifier)

    # Per-message (called automatically by store_message when attached):
    cls = await classifier.classify(msg)

    # Batch backfill:
    job = await classifier.reclassify()
    # job.done is True when the background task finishes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .engine import Engine
from .origin import ORIGIN_HUMAN, ORIGIN_MACHINE, OriginResult, message_origin
from .score import compute as score_compute
from .template import (
    _AUTO_BEACON_MIN_TOKENS,
    check_only,
    fingerprint,
    update_and_check,
)
from .types import (
    DIRECTED_DST_RE,
    TEMPLATE_HASH_LEN,
    Classification,
    EventBusProtocol,
    SSEEvent,
    StorageProtocol,
)

if TYPE_CHECKING:
    from .axes import AxesEngine

CONTRACT_ID_META_KEY = "classifier_contract_id"
# mc-chat extension of the contract origin axis: a human verdict on a message whose L2
# template is an auto-beacon is overridden to machine with this rule name.
ORIGIN_RULE_BEACON_TEMPLATE = "beacon-template"

logger = logging.getLogger(__name__)

ProgressCallback = Callable[["ReclassifyJob"], Awaitable[None]]


def _fallback_hash(text: str) -> str:
    """Plain SHA-1 of raw text, no normalization.  Used in the exception path."""
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:TEMPLATE_HASH_LEN]


def is_beacon_of(cls: Classification) -> bool:
    """L2 beacon verdict of a classification (False for one that did not come from L2)."""
    return isinstance(cls, BeaconClassification) and cls.is_beacon


def origin_for(  # noqa: PLR0913, PLR0917 - positional API mirrors message_origin
    engine: Engine,
    axes: AxesEngine | None,
    text: str,
    src: str,
    dst: str,
    timing: str | None,
    *,
    is_beacon: bool,
) -> OriginResult | None:
    """Contract origin (``message_origin``) plus the mc-chat beacon-template extension.

    Without an axes engine (contract failed to load) only the two verdicts that need neither
    timing nor format are given -- a machine-flagged rule, then the beacon template -- and
    everything else stays None, so ``auto_beacon`` hiding survives a degraded start.
    """
    if axes is None:
        machine_rules = engine.classify(text, src, dst).machine_rules
        if machine_rules:
            return OriginResult(ORIGIN_MACHINE, engine.origin_spec.rules[0])
        return OriginResult(ORIGIN_MACHINE, ORIGIN_RULE_BEACON_TEMPLATE) if is_beacon else None
    res = message_origin(engine, axes, text, src, dst, timing)
    if res.origin == ORIGIN_HUMAN and is_beacon:
        return OriginResult(ORIGIN_MACHINE, ORIGIN_RULE_BEACON_TEMPLATE)
    return res


@dataclass(frozen=True)
class BeaconClassification(Classification):
    """``Classifier.classify`` result: a ``Classification`` that also carries the L2 verdict.

    ``tags`` are the topics. ``is_beacon`` is the template's auto-beacon decision.
    """

    is_beacon: bool = False


@dataclass
class ReclassifyJob:
    job_id: str
    total: int
    processed: int = 0
    done: bool = False
    error: str | None = None
    # Internal reference kept so the GC doesn't collect the asyncio task.
    _task: asyncio.Task[None] | None = field(default=None, compare=False, repr=False)


class Classifier:
    """Orchestrator for Layers 1-3.

    Caller is responsible for calling ``load()`` once after Storage is
    initialised.  The L1 rule set ships with the engine (``contract_id``
    identifies it); a changed id bumps the classifier version on load so
    the outdated-row backfill reclassifies.  Attach to a Storage
    instance via ``storage.attach_classifier(classifier)`` so
    ``store_message`` enriches rows automatically.
    """

    def __init__(
        self,
        storage: StorageProtocol,
        engine: Engine,
        contract_id: str,
        axes: AxesEngine | None = None,
    ) -> None:
        self.storage = storage
        self.engine = engine
        # Axes engine for the origin axis on the reclassify path; None = origin not computed.
        self.axes = axes
        self.contract_id = contract_id
        self.version: int = 0
        self._jobs: dict[str, ReclassifyJob] = {}
        self.event_bus: EventBusProtocol | None = None

    def set_event_bus(self, bus: EventBusProtocol) -> None:
        """Attach an EventBus so the classifier can emit SSE events."""
        self.event_bus = bus

    async def load(self) -> None:
        """Fetch the current version; bump it once when the contract id changed."""
        stored = await self.storage.get_meta(CONTRACT_ID_META_KEY)
        if stored != self.contract_id:
            self.version = await self.storage.bump_classifier_version()
            await self.storage.set_meta(CONTRACT_ID_META_KEY, self.contract_id)
            logger.info(
                "Classifier contract %r -> %r: version bumped to %d",
                stored,
                self.contract_id,
                self.version,
            )
        else:
            self.version = await self.storage.get_classifier_version()
        logger.debug("Classifier loaded: version=%d, contract=%s", self.version, self.contract_id)

    async def classify(  # noqa: PLR0912 - complex handler kept intact
        self,
        msg: dict[str, Any],
        *,
        now_ms: int | None = None,
        update_stats: bool = True,
    ) -> Classification:
        """Classify a single message.


        When ``update_stats`` is True (default, live-ingest path):
          - Layer 2 calls ``update_and_check`` which increments
            ``beacon_templates.count``.
        When False (reclassify path):
          - Compute ``template_hash`` via ``fingerprint`` only.
          - Read the existing ``beacon_templates`` row to derive
            ``is_beacon`` and ``template_count`` without mutating stats.
          - Missing template row → template_count=0, is_beacon=False.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        try:
            # Layer 1: the contract engine
            text: str = msg.get("msg") or ""
            dst: str = msg.get("dst") or ""
            l1 = self.engine.classify(text, msg.get("src") or "", dst)
            topic_set: set[str] = set(l1.topics)
            directed = l1.directed or DIRECTED_DST_RE.search(dst) is not None
            auto_beacon = False

            if update_stats:
                # Layer 2 (live path): upsert stats, decide auto-beacon
                beacon = await update_and_check(
                    self.storage, msg, now_ms, tags=l1.topics, directed=directed
                )
                template_hash = beacon.template_hash
                template_count = beacon.count
                auto_beacon = beacon.is_beacon
                # Emit SSE event when a template transitions to auto_beacon
                if beacon.transitioned and self.event_bus is not None:
                    tpl = await self.storage.get_beacon_template(beacon.template_hash)
                    if tpl is not None:
                        await self.event_bus.publish(
                            SSEEvent("proxy:classifier_template_event", tpl)
                        )
            else:
                # Layer 2 (reclassify path): fingerprint only, no stat mutation
                template_hash = fingerprint(msg.get("msg") or "")
                tpl = await self.storage.get_beacon_template(template_hash)
                if tpl is not None:
                    template_count = tpl["count"]
                    user_action = tpl["user_action"]
                    # Honour user overrides; otherwise rely on stored auto_beacon flag
                    if user_action == "promote":
                        is_beacon = True
                    elif user_action == "demote":
                        is_beacon = False
                    else:
                        is_beacon = bool(tpl["auto_beacon"])
                        # Apply exemptions even in reclassify path (CLS-02: via
                        # template's public check_only(), the same exemption
                        # logic update_and_check() uses on the live path — this
                        # now also checks directedness, which this reclassify
                        # branch's old private-import reimplementation missed).
                        if is_beacon and not user_action:
                            is_beacon = not check_only(msg, l1.topics, directed)
                    auto_beacon = is_beacon
                else:
                    template_count = 0

            # Owner 2026-11-11: a repeated greeting-plus-weather template is a weather
            # report, the greeting is non-human. A greeting alone stays (greeting bots
            # are flaggable, issue #17), and a single greeting+weather message keeps both.
            if auto_beacon and "weather" in topic_set:
                topic_set.discard("greeting")

            # Origin feeds the score (human = group chat, machine = clamp); a failure only
            # leaves it unknown, it does not discard the classification.
            origin: str | None = None
            try:
                res = origin_for(
                    self.engine,
                    self.axes,
                    text,
                    msg.get("src") or "",
                    dst,
                    msg.get("timing"),
                    is_beacon=auto_beacon,
                )
                origin = None if res is None else res.origin
            except Exception:
                logger.exception("Origin for score failed for msg_id=%r", msg.get("msg_id"))

            # Layer 3: info score
            info_score = score_compute(
                msg, topic_set, template_count, directed=directed, origin=origin
            )

        except Exception:
            logger.exception(
                "Classifier pipeline failed for msg_id=%r — using fallback",
                msg.get("msg_id"),
            )
            raw_text: str = msg.get("msg") or ""
            return BeaconClassification(
                tags=(),
                info_score=0.5,
                template_hash=_fallback_hash(raw_text),
                classifier_version=self.version,
            )

        return BeaconClassification(
            tags=tuple(sorted(topic_set)),
            info_score=info_score,
            template_hash=template_hash,
            classifier_version=self.version,
            rules=tuple(l1.rules),
            is_beacon=auto_beacon,
        )

    def _origin_of_row(self, row: dict[str, Any], is_beacon: bool) -> OriginResult | None:
        """Origin of a stored row from its STORED timing (degraded without axes, see origin_for).

        Chat rows only, like ingest: other row types keep origin NULL.
        """
        if (row.get("type") or "msg") != "msg":
            return None
        return origin_for(
            self.engine,
            self.axes,
            row.get("msg") or "",
            row.get("src") or "",
            row.get("dst") or "",
            row.get("timing"),
            is_beacon=is_beacon,
        )

    async def _cleanup_stale_auto_beacons(self) -> int:
        """Clear auto_beacon for templates that are now exempt.

        Returns count of cleared rows.
        """
        cleared = await self.storage.clear_stale_auto_beacons(_AUTO_BEACON_MIN_TOKENS)
        if cleared:
            logger.info("Cleared stale auto_beacon on %d templates", cleared)
        return cleared

    async def reclassify(
        self,
        *,
        since_ms: int | None = None,
        force: bool = False,
        batch_size: int = 500,
        progress_cb: ProgressCallback | None = None,
    ) -> ReclassifyJob:
        """Kick off a background reclassification.

        Creates a new ``ReclassifyJob`` with a fresh uuid4 ``job_id`` and
        registers it in ``self._jobs``.  Returns immediately after
        creating the asyncio task.

        When ``force=True`` the classifier version is bumped first so that
        every existing row matches the ``classifier_ver < self.version``
        filter and gets reprocessed. Rule-change flows that already bumped
        the version externally should leave ``force=False``.
        """
        if force:
            await self.storage.bump_classifier_version()
            await self.load()

        target_version = self.version

        total = await self.storage.count_messages_to_classify(classifier_ver_below=target_version)
        job_id = str(uuid.uuid4())
        job = ReclassifyJob(job_id=job_id, total=total)
        self._jobs[job_id] = job

        task = asyncio.create_task(
            self._run_reclassify(
                job=job,
                since_ms=since_ms,
                target_version=target_version,
                batch_size=batch_size,
                progress_cb=progress_cb,
            )
        )
        job._task = task  # noqa: SLF001 - framework wiring
        return job

    async def _run_reclassify(
        self,
        *,
        job: ReclassifyJob,
        since_ms: int | None,
        target_version: int,
        batch_size: int,
        progress_cb: ProgressCallback | None,
    ) -> None:
        """Background worker for reclassification."""
        try:
            # OFFSET stays at 0 for every batch: a successful UPDATE
            # removes the row from the filter set (its classifier_ver
            # now matches target_version), so unclassified rows slide
            # to the front of the shrinking set.  Advancing the offset
            # would skip rows still waiting behind the batch we just
            # processed.
            skipped_ids: set[int] = set()
            while True:
                batch = await self.storage.get_messages_to_classify(
                    classifier_ver_below=target_version,
                    since_ms=since_ms,
                    limit=batch_size,
                    offset=0,
                )
                if not batch:
                    break

                # If every row in a batch lands in skipped_ids, the
                # filter will never shrink and we'd loop forever on a
                # persistent error.  Log and bail instead.
                batch_ids = {int(row["id"]) for row in batch}
                if batch_ids <= skipped_ids:
                    logger.error(
                        "Reclassify job %s stalled on %d rows that all fail their UPDATE; aborting",
                        job.job_id,
                        len(batch_ids),
                    )
                    break

                await self._reclassify_batch(batch, job, skipped_ids, target_version)

                if progress_cb is not None:
                    try:
                        await progress_cb(job)
                    except Exception:
                        logger.exception("progress_cb raised — ignoring")

                # Allow other tasks to run between batches
                await asyncio.sleep(0)

        except Exception as exc:
            logger.exception("Reclassify job %s failed fatally", job.job_id)
            job.error = str(exc)

        finally:
            await self._cleanup_stale_auto_beacons()
            job.done = True
            await self.storage.set_meta("last_reclassify_ms", str(int(time.time() * 1000)))
            if progress_cb is not None:
                try:
                    await progress_cb(job)
                except Exception:
                    logger.exception("progress_cb (done) raised — ignoring")

    async def _reclassify_batch(
        self,
        batch: list[dict[str, Any]],
        job: ReclassifyJob,
        skipped_ids: set[int],
        target_version: int,
    ) -> None:
        """Classify one batch; full-write changed rows, only stamp the rest."""
        changed: list[tuple[str, float, str, int, int]] = []
        origins: dict[int, tuple[str, str]] = {}
        unchanged_ids: list[int] = []
        results: list[tuple[dict[str, Any], Classification, OriginResult | None]] = []
        for row in batch:
            try:
                cls = await self.classify(row, update_stats=False)
                origin = self._origin_of_row(row, is_beacon_of(cls))
            except Exception:
                logger.exception("Reclassify failed for row id=%r — skipping", row.get("id"))
                skipped_ids.add(int(row["id"]))
                job.processed += 1
                continue
            results.append((row, cls, origin))
            new = (json.dumps(list(cls.tags)), cls.info_score, cls.template_hash)
            old = (
                row.get("tags"),
                row.get("info_score"),
                row.get("template_hash"),
            )
            origin_changed = origin is not None and (origin.origin, origin.rule) != (
                row.get("origin"),
                row.get("origin_rule"),
            )
            if origin is not None and origin_changed:
                origins[int(row["id"])] = (origin.origin, origin.rule)
            if new != old or origin_changed:
                changed.append((*new, cls.classifier_version, int(row["id"])))
            else:
                unchanged_ids.append(int(row["id"]))
        try:
            await self.storage.update_classification_batch(
                changed=changed,
                origins=origins,
                unchanged_ids=unchanged_ids,
                classifier_ver=target_version,
            )
        except Exception:
            logger.exception("Reclassify batch write failed — falling back to per-row writes")
        else:
            job.processed += len(results)
            return
        for row, cls, origin in results:
            try:
                await self.storage.update_message_classification(
                    row["id"],
                    tags=list(cls.tags),
                    info_score=cls.info_score,
                    template_hash=cls.template_hash,
                    classifier_ver=cls.classifier_version,
                    origin=origin.origin if origin is not None else None,
                    origin_rule=origin.rule if origin is not None else None,
                )
            except Exception:
                logger.exception("Reclassify failed for row id=%r — skipping", row.get("id"))
                skipped_ids.add(int(row["id"]))
            job.processed += 1

    async def collect_stats(self) -> dict[str, Any]:
        """Return the proxy:classifier_stats payload.

        Shape::

            {
                "by_tag": {tag: int, ...},            # stored topics, last 30 days
                "by_origin": {"human": int, "machine": int, "null": int},  # last 30 days
                "top_templates": [
                    {"template_hash", "example_msg", "count", "auto_beacon"},
                    ...                                # top 10 by count, last 7 days
                ],
                "auto_beacon_total": int,
            }
        """
        now_ms = int(time.time() * 1000)
        by_tag = await self.storage.count_messages_by_tag(now_ms - 30 * 24 * 3600 * 1000)
        by_origin = await self.storage.count_messages_by_origin(now_ms - 30 * 24 * 3600 * 1000)
        top_templates = await self.storage.get_top_beacon_templates(now_ms - 7 * 24 * 3600 * 1000)
        auto_beacon_total = await self.storage.count_auto_beacon_templates()

        return {
            "by_tag": by_tag,
            "by_origin": by_origin,
            "top_templates": top_templates,
            "auto_beacon_total": auto_beacon_total,
        }

    def get_job(self, job_id: str) -> ReclassifyJob | None:
        """Return the job for the given id (or None)."""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[ReclassifyJob]:
        """Return a snapshot of all registered jobs."""
        return list(self._jobs.values())
