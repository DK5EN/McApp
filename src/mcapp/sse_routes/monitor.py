"""RF Monitor REST surface: `GET /api/monitor/frames` (webapp
`docs/rf-monitor-plan.md`, "REST") plus the node debug console bridge
(`GET/POST /api/monitor/console*` — see `node_console.py`). Thin HTTP layer
over `manager.wire_monitor` (a `wire_monitor.WireMonitor`) and
`manager.node_console` (a `node_console.NodeConsoleSession`) — validates the
query/idempotency and delegates to `monitor.page()` /
`session.status()/start()/stop()`.

`wire_monitor`/`node_console` are read via `getattr` rather than a typed
attribute lookup on `SSEManager` at import time — mirrors `sse_routes/
stalls.py`'s `_require_recorder`: this module is written against the RF
Monitor wire contract from a sibling wiring step (`build_app` in `main.py`),
so `manager` here is only required to *possibly* carry the attribute. Absent
(`None`) is the ordinary "not wired up yet" case every other `sse_routes`
module already uses `require_storage()`/`require_classifier()` for —
reproduced here as a plain 503, since there is no shared `require_*` helper
for either optional field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Query

if TYPE_CHECKING:
    from ..node_console import NodeConsoleSession
    from ..sse_handler import SSEManager
    from ..wire_monitor import WireMonitor

# Contract: "limit=<n> (default 500, max 2000)".
_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2000


def _require_monitor(manager: SSEManager) -> WireMonitor:
    monitor = getattr(manager, "wire_monitor", None)
    if monitor is None:
        raise HTTPException(status_code=503, detail="Wire monitor not available")
    return cast("WireMonitor", monitor)


def _require_node_console(manager: SSEManager) -> NodeConsoleSession:
    session = getattr(manager, "node_console", None)
    if session is None:
        raise HTTPException(status_code=503, detail="Node console not available")
    return cast("NodeConsoleSession", session)


def build_monitor_router(manager: SSEManager) -> APIRouter:
    """Build the /api/monitor/* router."""
    router = APIRouter()

    @router.get("/api/monitor/frames")
    async def get_monitor_frames(
        before: int | None = None,
        after: int | None = None,
        limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    ) -> dict[str, Any]:
        """`{boot, frames, has_more}`, `frames` oldest -> newest. `before`
        (older than) and `after` (newer than, for reconnect gap-fill) are
        mutually exclusive — given together this is a client bug, rejected
        rather than silently preferring one.
        """
        monitor = _require_monitor(manager)
        if before is not None and after is not None:
            raise HTTPException(
                status_code=422, detail="'before' and 'after' are mutually exclusive"
            )
        return monitor.page(before=before, after=after, limit=limit)

    @router.get("/api/monitor/console")
    async def get_console_status() -> dict[str, Any]:
        """Current DBG session status — never includes the password."""
        return _require_node_console(manager).status()

    @router.post("/api/monitor/console/start")
    async def start_console() -> dict[str, Any]:
        """Start (or, idempotently, report) the DBG session against the
        node's debug console. Returns immediately with the session's
        current status; connect/handshake/flag-set progress arrives as
        `monitor:console` SSE broadcasts and `link="console"` notices."""
        return await _require_node_console(manager).start()

    @router.post("/api/monitor/console/stop")
    async def stop_console() -> dict[str, Any]:
        """Stop (or, idempotently, report) the DBG session. Returns
        promptly — flag restore-with-confirmation and the graceful close
        happen in the background; progress arrives as `monitor:console`
        SSE broadcasts and `link="console"` notices, same as `start()`.
        (From an `"error"` state this instead blocks briefly to settle a
        background restore retry — see `NodeConsoleSession.stop`.)"""
        return await _require_node_console(manager).stop()

    return router
