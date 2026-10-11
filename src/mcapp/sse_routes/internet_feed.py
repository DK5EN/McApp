"""Internet feed settings endpoints (meshmap box feed).

Design: doc/2026-10-11_1005-internet-feed-concept.md §4.1. The routes talk only
to `InternetFeedApi`; the box token never leaves the service. Every route sits
behind the same LAN-only Host + Origin guard as Node Admin. The PUT is the
complete new state (wholesale replace), never a patch.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..commands.parsing import is_hashtag
from ..internet_feed_types import (
    MAX_GROUPS,
    POS_MODES,
    RADIUS_KM_MAX,
    RADIUS_KM_MIN,
    InternetFeedSettingsError,
)
from .node_admin import host_allowed, origin_allowed

if TYPE_CHECKING:
    from ..internet_feed_types import InternetFeedApi
    from ..sse_handler import SSEManager

_HTTP_UNPROCESSABLE = 422
_GROUP_MAX = 99999
_GROUP_RE = re.compile(r"^[1-9][0-9]*$")
_TOKEN_ECHO_MAX = 24
_SETTINGS_KEYS = ("enabled", "groups", "broadcast", "pos_mode", "radius_km")
_GCB_SLOTS = 6


def _normalise_group(raw: object) -> str | None:
    """A valid group token (numeric 1..99999 or `#TAG`, upper-cased) or None."""
    if not isinstance(raw, str):
        return None
    if _GROUP_RE.match(raw):
        return raw if int(raw) <= _GROUP_MAX else None
    # is_hashtag() judges only the last comma component and strips blanks, so
    # "x,#A" or "#A " would pass it; the group token must be the whole string.
    if raw.startswith("#") and "," not in raw and raw == raw.strip() and is_hashtag(raw):
        return raw.upper()
    return None


def _dedup(groups: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        key = group.upper()
        if key not in seen:
            seen.add(key)
            out.append(group)
    return out


def validate_settings(body: Any) -> dict[str, Any]:
    """Validate a settings PUT body; return exactly the five settings keys.

    Raises InternetFeedSettingsError. The message echoes at most the offending
    group token (clipped), nothing else from the input.
    """
    if not isinstance(body, dict):
        raise InternetFeedSettingsError("body must be a JSON object")
    for key in _SETTINGS_KEYS:
        if key not in body:
            raise InternetFeedSettingsError(f"missing key: {key}")
    enabled = body["enabled"]
    broadcast = body["broadcast"]
    if not isinstance(enabled, bool):
        raise InternetFeedSettingsError("enabled must be true or false")
    if not isinstance(broadcast, bool):
        raise InternetFeedSettingsError("broadcast must be true or false")

    raw_groups = body["groups"]
    if not isinstance(raw_groups, list):
        raise InternetFeedSettingsError("groups must be a list")
    groups: list[str] = []
    for raw in raw_groups:
        group = _normalise_group(raw)
        if group is None:
            shown = raw[:_TOKEN_ECHO_MAX] if isinstance(raw, str) else "non-string entry"
            raise InternetFeedSettingsError(f"invalid group: {shown!r}")
        groups.append(group)
    groups = _dedup(groups)
    if len(groups) > MAX_GROUPS:
        raise InternetFeedSettingsError(f"at most {MAX_GROUPS} groups")

    pos_mode = body["pos_mode"]
    if not isinstance(pos_mode, str) or pos_mode not in POS_MODES:
        raise InternetFeedSettingsError("pos_mode must be one of: " + ", ".join(POS_MODES))
    radius = body["radius_km"]
    if isinstance(radius, bool) or not isinstance(radius, int):
        raise InternetFeedSettingsError("radius_km must be an integer")
    if not RADIUS_KM_MIN <= radius <= RADIUS_KM_MAX:
        raise InternetFeedSettingsError(
            f"radius_km must be {RADIUS_KM_MIN}..{RADIUS_KM_MAX}",
        )
    return {
        "enabled": enabled,
        "groups": groups,
        "broadcast": broadcast,
        "pos_mode": pos_mode,
        "radius_km": radius,
    }


def suggest_from_registers(registers: Mapping[str, Any]) -> dict[str, Any]:
    """First-run suggestion from the node's BLE registers: its GCB0..GCB5 group
    slots and whether it takes broadcasts (`SN.NOALL` falsy). No data -> no groups,
    broadcast on."""
    groups: list[str] = []
    info = registers.get("I")
    if isinstance(info, Mapping):
        for slot in range(_GCB_SLOTS):
            value = info.get(f"GCB{slot}")
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                continue
            group = _normalise_group(str(value))
            if group is not None:
                groups.append(group)
    sn = registers.get("SN")
    noall = sn.get("NOALL") if isinstance(sn, Mapping) else None
    return {"groups": _dedup(groups), "broadcast": not noall}


def build_internet_feed_router(
    manager: SSEManager,
    allowed_origins: Callable[[], Sequence[str]] = lambda: (),
) -> APIRouter:
    async def _guard(request: Request) -> None:
        host_header = request.headers.get("host")
        if not host_allowed(host_header):
            raise HTTPException(status_code=403, detail="Internet feed is LAN-only")
        if not origin_allowed(request.headers.get("origin"), host_header, allowed_origins()):
            raise HTTPException(status_code=403, detail="Cross-origin request refused")

    router = APIRouter(dependencies=[Depends(_guard)])

    def service() -> InternetFeedApi:
        svc: InternetFeedApi | None = getattr(manager, "internet_feed_service", None)
        if svc is None:
            raise HTTPException(status_code=503, detail="Internet feed not available")
        return svc

    def registers() -> Mapping[str, Any]:
        router_obj = getattr(manager, "message_router", None)
        cached = getattr(router_obj, "cached_ble_registers", None)
        return cached if isinstance(cached, Mapping) else {}

    @router.get("/api/internet-feed")
    async def get_feed() -> dict[str, Any]:
        result = await service().get_settings()
        if not result.get("configured"):
            result = {**result, "suggested": suggest_from_registers(registers())}
        return result

    @router.put("/api/internet-feed")
    async def put_feed(request: Request) -> dict[str, Any]:
        svc = service()
        try:
            validated = validate_settings(await request.json())
        except InternetFeedSettingsError as exc:
            raise HTTPException(status_code=_HTTP_UNPROCESSABLE, detail=str(exc)) from exc
        except ValueError:
            raise HTTPException(status_code=_HTTP_UNPROCESSABLE, detail="invalid JSON") from None
        try:
            return await svc.apply_settings(validated)
        except InternetFeedSettingsError as exc:
            raise HTTPException(status_code=_HTTP_UNPROCESSABLE, detail=str(exc)) from exc

    @router.post("/api/internet-feed/forget")
    async def forget_feed() -> dict[str, Any]:
        return await service().forget()

    return router
