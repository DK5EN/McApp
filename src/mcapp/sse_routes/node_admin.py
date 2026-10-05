"""Node Admin (RM1 remote admin) endpoints.

Plan: doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md §5 ("Guard and
security", "Routes"). The password is write-only: no endpoint returns it.

Every route sits behind `_guard`, a Host + Origin check that makes the feature
LAN-only in every mode (no public TLS hostname is allowlisted) and closes CSRF
from any LAN browser page and DNS rebinding. A custom header would protect
nothing here: CORS runs with `allow_headers=["*"]`.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError

from ..node_admin_types import NodeAdminBusyError, NodeAdminError, NodeAdminUnavailableError
from ..schemas import NodeAdminKeyRequest, NodeAdminSendRequest

if TYPE_CHECKING:
    from ..node_admin_types import NodeAdminService
    from ..sse_handler import SSEManager

T = TypeVar("T")

# Name suffixes `Caddyfile.mcapp` serves a LAN box under (`@@HOST@@.<suffix>`).
_LAN_SUFFIXES = ("local", "fritz.box", "home.arpa", "lan", "home")
_ALWAYS_NAMES = ("localhost", "mcapp")
_KEY_BODY_MAX_BYTES = 1024
_DETAIL_MAX_CHARS = 200
_KEY_REQUEST_INVALID = "invalid key request"
_HTTP_UNPROCESSABLE = 422


def _split_host(host_header: str | None) -> str | None:
    """Host header -> lower-cased host without port (and without IPv6 brackets)."""
    if not host_header:
        return None
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        host = host[1:end]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.rstrip(".")
    return host or None


def _lan_names(hostname: str) -> set[str]:
    names: set[str] = set()
    for base in (*_ALWAYS_NAMES, hostname):
        if not base:
            continue
        names.add(base)
        names.update(f"{base}.{suffix}" for suffix in _LAN_SUFFIXES)
    return names


def host_allowed(host_header: str | None, hostname: str | None = None) -> bool:
    """True for LAN-only names: localhost, this box under the Caddyfile names,
    or a loopback/private IP literal. Any other name, a public TLS hostname
    included, is refused on purpose."""
    host = _split_host(host_header)
    if host is None:
        return False
    short = (hostname if hostname is not None else socket.gethostname()).split(".")[0].lower()
    if host in _lan_names(short):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (ip.is_loopback or ip.is_private) and not ip.is_unspecified


def origin_allowed(origin: str | None, host_header: str | None, extra: Sequence[str] = ()) -> bool:
    """No Origin (same-origin GET, curl) passes. A present Origin must name the
    Host's host (port ignored) or be listed verbatim in `extra`."""
    if origin is None:
        return True
    if origin in extra:
        return True
    host = _split_host(host_header)
    if host is None:
        return False
    try:
        origin_host = urlsplit(origin).hostname
    except ValueError:
        return False
    return origin_host is not None and origin_host.rstrip(".").lower() == host


def _parse_key_body(raw: bytes) -> NodeAdminKeyRequest:
    """Parse a key request; the failure text never contains any of the input."""
    invalid = HTTPException(status_code=_HTTP_UNPROCESSABLE, detail=_KEY_REQUEST_INVALID)
    if len(raw) > _KEY_BODY_MAX_BYTES:
        raise invalid
    try:
        return NodeAdminKeyRequest.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        raise invalid from None


def _short(exc: Exception) -> str:
    return str(exc)[:_DETAIL_MAX_CHARS]


async def _run(call: Awaitable[T]) -> T:
    """Await a service call and map its refusals to HTTP statuses."""
    try:
        return await call
    except ValueError as exc:
        raise HTTPException(status_code=_HTTP_UNPROCESSABLE, detail=_short(exc)) from exc
    except NodeAdminBusyError as exc:
        raise HTTPException(status_code=409, detail=_short(exc)) from exc
    except NodeAdminUnavailableError as exc:
        raise HTTPException(status_code=503, detail=_short(exc)) from exc
    except NodeAdminError as exc:
        raise HTTPException(status_code=409, detail=_short(exc)) from exc


def build_node_admin_router(
    manager: SSEManager,
    allowed_origins: Callable[[], Sequence[str]] = lambda: (),
) -> APIRouter:
    async def _guard(request: Request) -> None:
        host_header = request.headers.get("host")
        if not host_allowed(host_header):
            raise HTTPException(status_code=403, detail="Node admin is LAN-only")
        if not origin_allowed(request.headers.get("origin"), host_header, allowed_origins()):
            raise HTTPException(status_code=403, detail="Cross-origin request refused")

    router = APIRouter(dependencies=[Depends(_guard)])

    def service() -> NodeAdminService:
        if manager.node_admin_service is None:
            raise HTTPException(status_code=503, detail="Node admin not available")
        return manager.node_admin_service

    @router.put("/api/node-admin/keys/{target}", status_code=204)
    async def put_key(target: str, request: Request) -> Response:
        svc = service()
        # Hand-validated, never a body-model parameter: FastAPI's 422 would
        # echo the plaintext password in `input`.
        body = _parse_key_body(await request.body())
        password = body.password.get_secret_value()
        try:
            await _run(svc.set_key(target, password, body.tx_max))
        except HTTPException as exc:
            if exc.status_code == _HTTP_UNPROCESSABLE and password in str(exc.detail):
                raise HTTPException(
                    status_code=_HTTP_UNPROCESSABLE, detail=_KEY_REQUEST_INVALID
                ) from None
            raise
        return Response(status_code=204)

    @router.delete("/api/node-admin/keys/{target}", status_code=204)
    async def delete_key(target: str) -> Response:
        await _run(service().delete_key(target))
        return Response(status_code=204)

    @router.get("/api/node-admin/targets")
    async def get_targets() -> list[dict[str, Any]]:
        return await _run(service().list_targets())

    @router.post("/api/node-admin/send")
    async def post_send(body: NodeAdminSendRequest) -> dict[str, Any]:
        return await _run(service().send(body.target, body.cmd, body.args, body.transport))

    @router.post("/api/node-admin/reask/{log_id}")
    async def post_reask(log_id: int) -> dict[str, Any]:
        return await _run(service().reask(log_id))

    @router.post("/api/node-admin/sync/{target}")
    async def post_sync(target: str) -> dict[str, Any]:
        return await _run(service().sync(target))

    @router.get("/api/node-admin/history")
    async def get_history(
        target: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return await _run(service().history(target, limit))

    return router


__all__ = ["build_node_admin_router", "host_allowed", "origin_allowed"]
