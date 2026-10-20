"""Classifier templates/reclassify/status REST endpoints (SSE-01).

Rule CRUD and the dry-run tester are retired: the rule set is mcmap's exported
contract (see classifier_contract.py), not editable at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from ..schemas import (
    ReclassifyRequest,
    TemplateActionRequest,
)

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

TEMPLATE_LIST_MAX = 500
TEMPLATE_PREVIEW_LIMIT = 20


def build_classifier_router(manager: SSEManager) -> APIRouter:
    """Build the /api/classifier/* router."""
    router = APIRouter()

    @router.get("/api/classifier/templates")
    async def get_classifier_templates(
        min_count: int = 0,
        auto_only: bool = False,
        user_action_only: bool = False,
        limit: int = 100,
    ) -> Any:
        storage = manager.require_storage()
        return await storage.list_beacon_templates(
            min_count=min_count,
            auto_only=auto_only,
            user_action_only=user_action_only,
            limit=max(1, min(limit, TEMPLATE_LIST_MAX)),
        )

    @router.patch("/api/classifier/templates/{template_hash}")
    async def patch_classifier_template(
        template_hash: str, body: TemplateActionRequest
    ) -> dict[str, Any]:
        storage = manager.require_storage()
        action = body.user_action
        if not await storage.beacon_template_exists(template_hash):
            raise HTTPException(status_code=404, detail="Template not found")
        await storage.set_beacon_template_user_action(template_hash, action)
        await manager.broadcast_event(
            "proxy:template_override",
            {"template_hash": template_hash, "user_action": action},
        )
        return {"status": "ok", "user_action": action}

    @router.post("/api/classifier/templates/{template_hash}/preview")
    async def preview_classifier_template(template_hash: str) -> dict[str, Any]:
        storage = manager.require_storage()
        rows = await storage.get_messages_by_template_hash(template_hash, TEMPLATE_PREVIEW_LIMIT)
        return {"template_hash": template_hash, "messages": rows}

    @router.post("/api/classifier/reclassify")
    async def post_classifier_reclassify(
        body: ReclassifyRequest | None = None,
    ) -> dict[str, Any]:
        classifier = manager.require_classifier()
        req = body or ReclassifyRequest()

        async def _progress(job: Any) -> None:
            await manager.broadcast_event(
                "proxy:reclassify_progress",
                {
                    "job_id": job.job_id,
                    "processed": job.processed,
                    "total": job.total,
                    "done": job.done,
                },
            )

        job = await classifier.reclassify(
            since_ms=req.since,
            category_filter=req.category or None,
            force=req.force,
            progress_cb=_progress,
        )
        return {"job_id": job.job_id, "estimated_rows": job.total}

    @router.get("/api/classifier/status")
    async def get_classifier_status() -> dict[str, Any]:
        classifier = manager.require_classifier()
        storage = manager.require_storage()
        total = await storage.count_messages_to_classify()
        pending = await storage.count_messages_to_classify(classifier_ver_below=classifier.version)
        return {
            "classifier_version": classifier.version,
            "contract_id": classifier.contract_id,
            "rule_count": classifier.engine.rule_count,
            "rows_classified": total - pending,
            "rows_unclassified": pending,
            "jobs": [
                {
                    "job_id": j.job_id,
                    "total": j.total,
                    "processed": j.processed,
                    "done": j.done,
                    "error": j.error,
                }
                for j in classifier.get_all_jobs()
            ],
        }

    return router
