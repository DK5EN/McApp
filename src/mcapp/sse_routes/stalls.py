"""Stall-tracking REST surface: `GET /api/stalls`, `GET /api/stalls/summary`,
`POST /api/stalls/client` (plan `doc/2026-09-15_1530-stall-tracking-plan.md`
§4). Thin HTTP layer over `manager.stall_recorder` (a `StallRecorder`,
§6 of the same plan) — every route just validates its query/body and
delegates to `recorder.query()` / `recorder.summary()` / `recorder.ingest_client()`.

`stall_recorder` is read via `getattr` rather than a typed attribute on
`SSEManager`: this module is written against the §6 contract from a sibling
wave that wires the attribute onto `SSEManager` separately, so `manager` here
is only required to *possibly* carry it. Absent (`None`) is the ordinary
"not wired up yet" case every other `sse_routes` module already uses
`require_storage()`/`require_classifier()` for — mirrored here as a plain
503, since there is no shared `require_*` helper for this optional field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import RootModel

if TYPE_CHECKING:
    from ..sse_handler import SSEManager
    from ..stalls import StallRecorder

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 2000
_MAX_CLIENT_BATCH = 100

# POST /api/stalls/client accepts one record object or an array of them.
ClientStallPayload = RootModel[list[dict[str, Any]] | dict[str, Any]]


def _require_recorder(manager: SSEManager) -> StallRecorder:
    recorder = getattr(manager, "stall_recorder", None)
    if recorder is None:
        raise HTTPException(status_code=503, detail="Stall recorder not available")
    return cast("StallRecorder", recorder)


def build_stalls_router(manager: SSEManager) -> APIRouter:
    """Build the /api/stalls* router."""
    router = APIRouter()

    # Declared before the plain /api/stalls route (brief note: no path params
    # are involved here, but the ordering is kept explicit regardless).
    @router.get("/api/stalls/summary")
    async def get_stalls_summary(since: int | None = None) -> dict[str, Any]:
        """Per-path count/p50/p95/p99/max over `http` rows since `since` (ms),
        or over all recorded history when omitted."""
        recorder = _require_recorder(manager)
        rows = await recorder.summary(since_ms=since)
        return {"rows": rows}

    @router.get("/api/stalls")
    async def get_stalls(
        since: int | None = None,
        kind: str | None = None,
        severity: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Full stall records, newest first — what a coding agent reads
        to diagnose a specific slow call. `limit` is clamped to
        `[1, _MAX_LIMIT]`, never rejected."""
        recorder = _require_recorder(manager)
        clamped_limit = max(1, min(limit, _MAX_LIMIT))
        rows = await recorder.query(
            since_ms=since, kind=kind, severity=severity, limit=clamped_limit
        )
        return {"rows": rows}

    @router.post("/api/stalls/client")
    async def post_stalls_client(request: Request) -> dict[str, int]:
        """Client-side (webapp) stall records: one object or an array of up
        to `_MAX_CLIENT_BATCH`. A non-JSON body is a 400, not a silent drop —
        the client should know its report was rejected."""
        recorder = _require_recorder(manager)
        try:
            raw = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Body must be JSON") from exc

        try:
            payload = ClientStallPayload.model_validate(raw)
        except Exception as exc:  # any pydantic validation failure -> 400
            raise HTTPException(status_code=400, detail="Invalid stall record(s)") from exc

        records = payload.root if isinstance(payload.root, list) else [payload.root]
        records = records[:_MAX_CLIENT_BATCH]
        accepted = await recorder.ingest_client(records)
        return {"accepted": accepted}

    return router
