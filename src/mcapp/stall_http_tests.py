"""Built-in regression suite for `StallMiddleware` (pure-ASGI request timing)
and the `/api/stalls*` router (`sse_routes/stalls.py`).

Written against the §6 interface contract in
`doc/2026-09-15_1530-stall-tracking-plan.md`, not against the real
`StallRecorder` (`stalls.py`, owned by a sibling wave and not part of this
file's exclusive set): `_FakeRecorder` below implements exactly the subset of
that contract this suite exercises (`severity_for`, `record`, `config.
body_cap_bytes`, `query`, `summary`, `ingest_client`) so this suite runs
standalone regardless of that module's state.

Cases:

  1. No `X-Request-Id` header -> one is minted (uuid4 hex) and echoed back on
     the response.
  2. A supplied `X-Request-Id`/`X-Session-Id` pair is echoed/recorded
     verbatim, never replaced.
  3. A JSON POST body is captured as a parsed dict in the record, the query
     string is recorded raw, and `status`/`detail["resp_bytes"]` land.
  4. `/events` is a complete passthrough: no header added, nothing recorded.
  5. An exception in the downstream route is recorded with `status=500` and
     still propagates to the caller (ASGITransport's default
     `raise_app_exceptions=True`).
  6. `severity_for` returning `None` records nothing, regardless of duration.
  7. The tap never drops or mutates a byte of the request body before it
     reaches the downstream app, even when the body is far larger than the
     recorder's own capture cap.
  8. The `/api/stalls*` router: rows pass through `query`/`summary`, `limit`
     is clamped into `[1, 2000]`, `POST /api/stalls/client` accepts both a
     single object and an array and rejects a non-JSON body with 400, and a
     manager with no recorder wired answers every route with 503.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import FastAPI, Request

from .commands.constants import has_console
from .sse_routes.stalls import build_stalls_router
from .stall_middleware import StallMiddleware

if TYPE_CHECKING:
    from .sse_handler import SSEManager

_BODY_CAP_BYTES = 16  # deliberately tiny: case 7 needs the tap to under-capture


class _FakeConfig:
    def __init__(self, body_cap_bytes: int = _BODY_CAP_BYTES) -> None:
        self.body_cap_bytes = body_cap_bytes


class _FakeRecorder:
    """Implements exactly the `StallRecorder` subset `StallMiddleware` and
    `build_stalls_router` use (§6 contract) — see module docstring."""

    def __init__(self, *, severity: str | None = "stall") -> None:
        self.config = _FakeConfig()
        self.records: list[dict[str, Any]] = []
        self._severity = severity
        self.query_rows: list[dict[str, Any]] = [{"id": 1, "kind": "http"}]
        self.summary_rows: list[dict[str, Any]] = [{"path": "/ping", "count": 1}]
        self.last_query_kwargs: dict[str, Any] | None = None
        self.ingested: list[list[dict[str, Any]]] = []

    def severity_for(self, duration_ms: float) -> str | None:
        return self._severity

    def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    async def query(
        self,
        *,
        since_ms: int | None = None,
        kind: str | None = None,
        severity: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self.last_query_kwargs = {
            "since_ms": since_ms,
            "kind": kind,
            "severity": severity,
            "limit": limit,
        }
        return self.query_rows

    async def summary(self, *, since_ms: int | None = None) -> list[dict[str, Any]]:
        return self.summary_rows

    async def ingest_client(self, records: list[dict[str, Any]]) -> int:
        self.ingested.append(records)
        return len(records)


class _ManagerStub:
    """`SSEManager` stand-in — `build_stalls_router` only reads `stall_recorder`."""

    def __init__(self, stall_recorder: _FakeRecorder | None) -> None:
        self.stall_recorder = stall_recorder


def _build_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"pong": True}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, Any]:
        raw = await request.body()
        return {"length": len(raw), "text": raw.decode("utf-8", errors="replace")}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/events")
    async def events() -> dict[str, bool]:
        return {"ok": True}

    return app


def _client_for(recorder: _FakeRecorder) -> httpx.AsyncClient:
    wrapped = StallMiddleware(_build_test_app(), cast(Any, recorder))
    transport = httpx.ASGITransport(app=wrapped)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def _test_minted_request_id(record: Any) -> None:
    recorder = _FakeRecorder()
    async with _client_for(recorder) as client:
        resp = await client.get("/ping")
    header_id = resp.headers.get("x-request-id")
    record(
        "1. request id minted when absent and echoed on the response",
        bool(header_id) and len(header_id) == 32,
    )
    record(
        "1. minted request id matches the one recorded",
        len(recorder.records) == 1 and recorder.records[0]["request_id"] == header_id,
    )


async def _test_supplied_ids_echoed(record: Any) -> None:
    recorder = _FakeRecorder()
    async with _client_for(recorder) as client:
        resp = await client.get(
            "/ping", headers={"X-Request-Id": "req-abc", "X-Session-Id": "sess-1"}
        )
    record(
        "2. supplied X-Request-Id echoed verbatim on the response",
        resp.headers.get("x-request-id") == "req-abc",
    )
    record(
        "2. supplied X-Request-Id/X-Session-Id recorded verbatim",
        len(recorder.records) == 1
        and recorder.records[0]["request_id"] == "req-abc"
        and recorder.records[0]["session_id"] == "sess-1",
    )


async def _test_json_body_query_status_recorded(record: Any) -> None:
    recorder = _FakeRecorder()
    async with _client_for(recorder) as client:
        resp = await client.post("/echo", params={"q": "abc"}, json={"a": 1, "b": "x"})
    rec = recorder.records[-1] if recorder.records else {}
    record(
        "3. JSON body captured as a parsed dict",
        rec.get("body") == {"a": 1, "b": "x"},
    )
    record("3. query string recorded raw", rec.get("query") == "q=abc")
    record(
        "3. status and resp_bytes land in the record",
        resp.status_code == 200
        and rec.get("status") == 200
        and rec.get("detail", {}).get("resp_bytes", 0) > 0,
    )


async def _test_events_passthrough(record: Any) -> None:
    recorder = _FakeRecorder()
    async with _client_for(recorder) as client:
        resp = await client.get("/events")
    record("4. /events gets no x-request-id header", "x-request-id" not in resp.headers)
    record("4. /events records nothing", recorder.records == [])


async def _test_exception_recorded_and_propagates(record: Any) -> None:
    # A bare ASGI app, NOT FastAPI: FastAPI's ServerErrorMiddleware sits inside
    # StallMiddleware and would already have turned the exception into a 500
    # response, so the middleware's own except branch would never run here.
    recorder = _FakeRecorder()
    raised: BaseException | None = None

    async def _raising_app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        raise RuntimeError("boom")

    wrapped = StallMiddleware(_raising_app, recorder)  # type: ignore[arg-type]  # fake recorder
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        try:
            await client.get("/boom")
        except Exception as exc:  # capturing whatever ASGITransport re-raises
            raised = exc
    record("5. exception from the route propagates to the caller", raised is not None)
    record(
        "5. exception recorded with status=500",
        len(recorder.records) == 1 and recorder.records[0]["status"] == 500,
    )


async def _test_no_severity_no_record(record: Any) -> None:
    recorder = _FakeRecorder(severity=None)
    async with _client_for(recorder) as client:
        resp = await client.get("/ping")
    record(
        "6. severity_for() -> None means nothing is recorded",
        resp.status_code == 200 and recorder.records == [],
    )


async def _test_body_tap_never_drops_downstream_bytes(record: Any) -> None:
    recorder = _FakeRecorder()  # body_cap_bytes=16, so the recorded copy under-captures
    payload = "X" * 5000
    async with _client_for(recorder) as client:
        resp = await client.post("/echo", content=payload.encode("utf-8"))
    data = resp.json()
    record(
        "7. downstream app receives the FULL request body unchanged",
        data["length"] == len(payload) and data["text"] == payload,
    )
    record(
        "7. the recorded/captured copy is capped (never grows unbounded)",
        len(recorder.records) == 1
        and isinstance(recorder.records[0]["body"], str)
        and len(recorder.records[0]["body"]) <= recorder.config.body_cap_bytes * 2,
    )


async def _test_stalls_router(record: Any) -> None:
    fake = _FakeRecorder()
    app = FastAPI()
    app.include_router(build_stalls_router(cast("SSEManager", _ManagerStub(fake))))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        resp = await client.get("/api/stalls", params={"limit": 50000})
        record(
            "8. GET /api/stalls returns the recorder's rows and clamps limit to 2000",
            resp.status_code == 200
            and resp.json()["rows"] == fake.query_rows
            and fake.last_query_kwargs is not None
            and fake.last_query_kwargs["limit"] == 2000,
        )

        resp = await client.get("/api/stalls", params={"limit": 0})
        record(
            "8. GET /api/stalls clamps a limit below 1 up to 1",
            resp.status_code == 200
            and fake.last_query_kwargs is not None
            and fake.last_query_kwargs["limit"] == 1,
        )

        resp = await client.get("/api/stalls/summary")
        record(
            "8. GET /api/stalls/summary returns the recorder's summary rows",
            resp.status_code == 200 and resp.json()["rows"] == fake.summary_rows,
        )

        resp = await client.post(
            "/api/stalls/client", json=[{"kind": "client_http"}, {"kind": "x"}]
        )
        record(
            "8. POST /api/stalls/client with an array returns accepted=len(array)",
            resp.status_code == 200 and resp.json() == {"accepted": 2},
        )

        resp = await client.post("/api/stalls/client", json={"kind": "client_http"})
        record(
            "8. POST /api/stalls/client with a single object returns accepted=1",
            resp.status_code == 200 and resp.json() == {"accepted": 1},
        )

        resp = await client.post(
            "/api/stalls/client", content=b"not json", headers={"content-type": "text/plain"}
        )
        record("8. POST /api/stalls/client with a non-JSON body -> 400", resp.status_code == 400)

    app_no_recorder = FastAPI()
    app_no_recorder.include_router(build_stalls_router(cast("SSEManager", _ManagerStub(None))))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_no_recorder), base_url="http://t"
    ) as client:
        resp = await client.get("/api/stalls")
        record("8. GET /api/stalls with no recorder wired -> 503", resp.status_code == 503)
        resp = await client.get("/api/stalls/summary")
        record("8. GET /api/stalls/summary with no recorder wired -> 503", resp.status_code == 503)
        resp = await client.post("/api/stalls/client", json={})
        record("8. POST /api/stalls/client with no recorder wired -> 503", resp.status_code == 503)


async def run_stall_http_tests() -> bool:
    """Return True iff every StallMiddleware/router regression case passes."""
    if has_console:
        print("\n🧪 Testing stall HTTP middleware + /api/stalls router:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    await _test_minted_request_id(_record)
    await _test_supplied_ids_echoed(_record)
    await _test_json_body_query_status_recorded(_record)
    await _test_events_passthrough(_record)
    await _test_exception_recorded_and_propagates(_record)
    await _test_no_severity_no_record(_record)
    await _test_body_tap_never_drops_downstream_bytes(_record)
    await _test_stalls_router(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 Stall HTTP Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run_stall_http_tests()) else 1)
