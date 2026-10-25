"""Group label + callsign override endpoints (plan D4-D7, §3 wire contract).

Reads merge the ICSSW snapshot with operator overrides; writes validate by hand
so a 422 never echoes the submitted input (no pydantic `input` field). No
Host/Origin guard: operator decision D7. Plan:
doc/2026-10-25_0900-conversation-nav-group-labels-plan.md
"""

from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response

from ..commands.parsing import is_group, is_hashtag
from ..group_labels import build_label_payload, load_snapshot, upstream_for
from ..qrz_client import base_callsign, is_lookup_candidate

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

GROUP_LABELS_EVENT = "proxy:group_labels"
CALLSIGN_INFO_EVENT = "proxy:callsign_info"
_KEY_MAX = 64
_BODY_MAX_BYTES = 2048
_HTTP_UNPROCESSABLE = 422
_LABEL_FIELDS = ("prefix", "hint", "remark")
_CALLSIGN_FIELDS = ("first_name", "qth")
_NULL_ENTRY: dict[str, Any] = {
    "first_name": None,
    "qth": None,
    "country": None,
    "lat": None,
    "lon": None,
    "locator": None,
}


@functools.lru_cache(maxsize=1)
def cached_snapshot() -> dict[str, Any]:
    """Load the ICSSW snapshot once per process."""
    return load_snapshot()


async def current_label_payload(manager: SSEManager) -> dict[str, Any]:
    """`{labels, countries}`: snapshot expanded and merged with stored overrides."""
    overrides = await manager.require_storage().get_group_label_overrides()
    return build_label_payload(cached_snapshot(), overrides)


def _unprocessable(what: str) -> HTTPException:
    # Fixed text on purpose: never include the submitted value.
    return HTTPException(status_code=_HTTP_UNPROCESSABLE, detail=f"invalid {what}")


def _valid_group_key(key: str) -> bool:
    if not key or len(key) > _KEY_MAX or key != key.strip():
        return False
    return key == "*" or is_group(key) or is_hashtag(key)


async def _read_fields(request: Request, fields: tuple[str, ...]) -> list[str | None]:
    """Parse the JSON body by hand: object, known keys only, string-or-null values."""
    raw = await request.body()
    if len(raw) > _BODY_MAX_BYTES:
        raise _unprocessable("body")
    try:
        body = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise _unprocessable("body") from exc
    if not isinstance(body, dict) or any(k not in fields for k in body):
        raise _unprocessable("body")
    values: list[str | None] = []
    for name in fields:
        value = body.get(name)
        if value is not None and not isinstance(value, str):
            raise _unprocessable(name)
        values.append(value)
    return values


def build_labels_router(manager: SSEManager) -> APIRouter:
    router = APIRouter()

    async def broadcast_labels() -> dict[str, Any]:
        payload = await current_label_payload(manager)
        await manager.broadcast_event(GROUP_LABELS_EVENT, payload)
        return payload

    async def broadcast_callsign(base: str) -> dict[str, Any] | None:
        merged = await manager.require_storage().get_callsign_info_merged(base)
        # None = an override-only callsign was reverted: the webapp store never
        # clears a key, so send an all-null entry that overwrites the stale name.
        await manager.broadcast_event(
            CALLSIGN_INFO_EVENT, {base: merged if merged is not None else dict(_NULL_ENTRY)}
        )
        return merged

    @router.get("/api/group_labels")
    async def get_group_labels() -> dict[str, Any]:
        snapshot = cached_snapshot()
        payload = await current_label_payload(manager)
        return {
            "labels": payload["labels"],
            "upstream": upstream_for(snapshot),
            "snapshot": {
                "fetched_at": snapshot.get("fetched_at"),
                "source_url": snapshot.get("source_url"),
            },
        }

    @router.put("/api/group_labels/{key}")
    async def put_group_label(key: str, request: Request) -> dict[str, Any]:
        if not _valid_group_key(key):
            raise _unprocessable("group key")
        prefix, hint, remark = await _read_fields(request, _LABEL_FIELDS)
        try:
            await manager.require_storage().set_group_label_override(key, prefix, hint, remark)
        except ValueError as exc:
            raise _unprocessable("label") from exc
        payload = await broadcast_labels()
        return {"label": payload["labels"].get(key)}

    @router.delete("/api/group_labels/{key}", status_code=204)
    async def delete_group_label(key: str) -> Response:
        if not _valid_group_key(key):
            raise _unprocessable("group key")
        await manager.require_storage().delete_group_label_override(key)
        await broadcast_labels()
        return Response(status_code=204)

    @router.put("/api/callsign_info/{call}/override")
    async def put_callsign_override(call: str, request: Request) -> dict[str, Any]:
        base = base_callsign(call)
        if len(call) > _KEY_MAX or not is_lookup_candidate(base):
            raise _unprocessable("callsign")
        first_name, qth = await _read_fields(request, _CALLSIGN_FIELDS)
        try:
            await manager.require_storage().set_callsign_override(base, first_name, qth)
        except ValueError as exc:
            raise _unprocessable("override") from exc
        return {"info": await broadcast_callsign(base)}

    @router.delete("/api/callsign_info/{call}/override", status_code=204)
    async def delete_callsign_override(call: str) -> Response:
        base = base_callsign(call)
        if len(call) > _KEY_MAX or not is_lookup_candidate(base):
            raise _unprocessable("callsign")
        await manager.require_storage().delete_callsign_override(base)
        await broadcast_callsign(base)
        return Response(status_code=204)

    return router
