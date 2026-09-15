"""Pure-ASGI stall-recording middleware for the HTTP surface (§4/§6 of
`doc/2026-09-15_1530-stall-tracking-plan.md`).

Starlette's `BaseHTTPMiddleware` buffers the whole response before the
downstream app's body is visible to it, which breaks a streaming response —
and `/events`/`/update-stream` are exactly that: long-lived SSE streams that
must never be buffered or measured as "one slow request". This middleware is
therefore hand-written at the raw ASGI `(scope, receive, send)` level: it
*taps* the request body and the response as they flow, one chunk at a time,
and never accumulates a chunk it does not need to keep (the response body is
never kept at all — only its total length, for `resp_bytes`).

Everything under `/events`, `/update-stream` and `/health` (prefix match on
`/events*` per the brief) passes through completely untouched: no id minted,
no header added, no timing, no wrapping of `receive`/`send`. This keeps the
SSE hot path exactly as cheap as it was before this module existed.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .stalls import StallRecorder

logger = logging.getLogger(__name__)

# `mcapp.stalls` (§6 contract) owns the canonical `current_request_id`
# ContextVar — "set by the middleware, read by anyone" — so correlation only
# works if this module uses that exact object, not a lookalike of its own.
# At the time this file was written `stalls.py` was still being authored by a
# sibling wave against the same contract, so a static `from .stalls import
# current_request_id` would fail both mypy (module not found) and any
# standalone run of this middleware before that file lands. Resolved by
# string via `importlib` instead: mypy cannot (and does not try to)
# statically resolve a dynamic module name, so this degrades to a local
# fallback ContextVar when `mcapp.stalls` is absent and picks up the real,
# shared one transparently once it exists — no code change needed either way.
_local_request_id_fallback: ContextVar[str | None] = ContextVar(
    "current_request_id_fallback", default=None
)


def _resolve_current_request_id_var() -> ContextVar[str | None]:
    try:
        stalls_mod = importlib.import_module(f"{__package__}.stalls")
    except ModuleNotFoundError:
        return _local_request_id_fallback
    return cast("ContextVar[str | None]", stalls_mod.current_request_id)


# ASGI types are structurally `MutableMapping`/`Callable` — no `asgiref`/
# `starlette.types` dependency needed for a module this small; `MutableMapping`
# (not `dict`) matches the ASGI callable shape httpx's `ASGITransport` itself
# is typed against, so a `StallMiddleware` instance type-checks as its `app`.
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Paths that must never be timed, tapped or given a request id — long-lived
# SSE streams (BaseHTTPMiddleware-hostile) plus the liveness probe.
_PASSTHROUGH_EXACT = frozenset({"/update-stream", "/health"})
_PASSTHROUGH_PREFIX = "/events"


def _passthrough(path: str) -> bool:
    return path in _PASSTHROUGH_EXACT or path.startswith(_PASSTHROUGH_PREFIX)


@dataclass(slots=True)
class _RequestMeta:
    """The identifying facts about one request, fixed before the downstream
    app runs — split out of `_RequestTap` only to keep its `__init__` under
    the lint's argument-count limit."""

    scope: Scope
    headers: dict[bytes, bytes]
    request_id: str
    session_id: str | None
    raw_cap: int


class _RequestTap:
    """Owns everything one request needs across both directions: it taps
    `receive`/`send` as chunks flow (never buffering more than a bounded
    prefix, never withholding a chunk from the downstream app), and carries
    the request identity so `StallMiddleware._maybe_record` needs only this
    one object plus the elapsed duration.
    """

    def __init__(self, receive: Receive, send: Send, meta: _RequestMeta) -> None:
        self._receive = receive
        self._send = send
        self.scope = meta.scope
        self.headers = meta.headers
        self.request_id = meta.request_id
        self.session_id = meta.session_id
        self._raw_cap = meta.raw_cap
        self._request_id_header = meta.request_id.encode("latin-1")
        self.captured_body = bytearray()
        self.body_truncated = False
        self.status = 0
        self.resp_bytes = 0

    async def receive(self) -> Message:
        message = await self._receive()
        if message.get("type") == "http.request":
            chunk = message.get("body", b"") or b""
            if chunk:
                remaining = self._raw_cap - len(self.captured_body)
                if remaining > 0:
                    self.captured_body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        self.body_truncated = True
                else:
                    self.body_truncated = True
        return message

    async def send(self, message: Message) -> None:
        if message.get("type") == "http.response.start":
            self.status = message.get("status", 0)
            id_header = (b"x-request-id", self._request_id_header)
            raw_headers = [*(message.get("headers") or []), id_header]
            message = {**message, "headers": raw_headers}
        elif message.get("type") == "http.response.body":
            self.resp_bytes += len(message.get("body", b"") or b"")
        await self._send(message)


class StallMiddleware:
    """Pure ASGI middleware: mints/echoes `X-Request-Id`, reads
    `X-Session-Id`, taps request/response to record any call whose duration
    crosses `recorder.severity_for()`'s thresholds (or lands in its 1-in-N
    sample).
    """

    def __init__(self, app: ASGIApp, recorder: StallRecorder) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or _passthrough(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        request_id = headers.get(b"x-request-id", b"").decode("latin-1") or uuid.uuid4().hex
        session_id = headers.get(b"x-session-id", b"").decode("latin-1") or None
        meta = _RequestMeta(
            scope=scope,
            headers=headers,
            request_id=request_id,
            session_id=session_id,
            raw_cap=self.recorder.config.body_cap_bytes * 2,
        )
        tap = _RequestTap(receive, send, meta)

        request_id_var = _resolve_current_request_id_var()
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status = 0
        try:
            await self.app(scope, tap.receive, tap.send)
            status = tap.status
        except BaseException:
            status = 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            request_id_var.reset(token)
            try:
                self._maybe_record(tap, status=status, duration_ms=duration_ms)
            except Exception:
                logger.debug("stall middleware: record() failed", exc_info=True)

    def _maybe_record(self, tap: _RequestTap, *, status: int, duration_ms: float) -> None:
        sev = self.recorder.severity_for(duration_ms)
        if sev is None:
            return

        body: Any = None
        if tap.captured_body:
            content_type = tap.headers.get(b"content-type", b"").decode("latin-1")
            text = bytes(tap.captured_body).decode("utf-8", errors="replace")
            if "json" in content_type.lower():
                try:
                    body = json.loads(text)
                except (ValueError, TypeError):
                    body = text
            else:
                body = text

        detail: dict[str, Any] = {"resp_bytes": tap.resp_bytes, "client": tap.scope.get("client")}
        if tap.body_truncated:
            detail["body_truncated"] = True

        self.recorder.record(
            kind="http",
            severity=sev,
            request_id=tap.request_id,
            session_id=tap.session_id,
            method=tap.scope.get("method"),
            path=tap.scope.get("path"),
            query=tap.scope.get("query_string", b"").decode("latin-1"),
            body=body,
            status=status,
            duration_ms=duration_ms,
            detail=detail,
        )
