"""QRZ.com callsign lookup endpoints (issue #14).

The password is write-only: no endpoint returns it, status reports only
whether one is stored. Plan: doc/2026-10-04_0848-qrz-callsign-lookup-plan.md §6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from ..schemas import QrzCredentialsRequest, QrzEnabledRequest

if TYPE_CHECKING:
    from ..qrz_service import QrzLookupService
    from ..sse_handler import SSEManager


def build_qrz_router(manager: SSEManager) -> APIRouter:
    router = APIRouter()

    def service() -> QrzLookupService:
        if manager.qrz_service is None:
            raise HTTPException(status_code=503, detail="QRZ lookup not available")
        return manager.qrz_service

    @router.get("/api/qrz/status")
    async def get_status() -> dict[str, Any]:
        return await service().status()

    @router.put("/api/qrz/credentials")
    async def put_credentials(body: QrzCredentialsRequest) -> dict[str, Any]:
        svc = service()
        try:
            await svc.set_credentials(body.username, body.password.get_secret_value())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await svc.status()

    @router.delete("/api/qrz/credentials")
    async def delete_credentials() -> dict[str, Any]:
        svc = service()
        await svc.clear_credentials()
        return await svc.status()

    @router.put("/api/qrz/enabled")
    async def put_enabled(body: QrzEnabledRequest) -> dict[str, Any]:
        svc = service()
        await svc.set_enabled(body.enabled)
        return await svc.status()

    @router.get("/api/callsign_info")
    async def get_callsign_info() -> dict[str, dict[str, str | None]]:
        return await manager.require_storage().get_callsign_info_map()

    return router
