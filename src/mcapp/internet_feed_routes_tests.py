"""Regression suite for the internet feed router (`sse_routes/internet_feed.py`).

Design: doc/2026-10-11_1005-internet-feed-concept.md §4.1. Fully offline: a fake
`InternetFeedApi` records calls; the body-withheld cases run through the REAL
`StallMiddleware` with a fake recorder.

Cases: LAN-only guard (Host, Origin), 503 without service, `suggested` only
while unconfigured, `suggest_from_registers`, the `validate_settings` table,
PUT hands the service exactly the validated dict, forget, and the stall
redaction of the settings paths (body withheld; `/api/internet-feedX` is not).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx
from fastapi import FastAPI

from .commands.constants import has_console
from .internet_feed_types import InternetFeedSettingsError
from .sse_routes.internet_feed import (
    build_internet_feed_router,
    suggest_from_registers,
    validate_settings,
)
from .stall_middleware import StallMiddleware

_BASE = "http://localhost"
_EVIL = {"host": "evil.example.com"}
_SECRET = "box-token-secret"


class _FakeService:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.calls: list[tuple[str, Any]] = []

    async def get_settings(self) -> dict[str, Any]:
        self.calls.append(("get", None))
        return {"enabled": True, "configured": self.configured, "groups": ["20"]}

    async def apply_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("apply", settings))
        return {"configured": True, **settings}

    async def forget(self) -> dict[str, Any]:
        self.calls.append(("forget", None))
        return {"registered": False}

    def status(self) -> dict[str, Any]:
        return {"state": "off"}


class _RouterStub:
    def __init__(self, regs: dict[str, Any]) -> None:
        self.cached_ble_registers = regs


class _ManagerStub:
    def __init__(self, service: _FakeService | None, regs: dict[str, Any] | None = None) -> None:
        self.internet_feed_service = service
        self.message_router = _RouterStub(regs or {})


class _FakeConfig:
    body_cap_bytes = 16


class _FakeRecorder:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.records: list[dict[str, Any]] = []

    def severity_for(self, duration_ms: float) -> str | None:
        return "stall"

    def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def _app(
    service: _FakeService | None,
    regs: dict[str, Any] | None = None,
    origins: tuple[str, ...] = (),
    recorder: _FakeRecorder | None = None,
) -> Any:
    app = FastAPI()
    manager: Any = _ManagerStub(service, regs)
    app.include_router(build_internet_feed_router(manager, lambda: origins))
    if recorder is not None:
        return StallMiddleware(app, cast(Any, recorder))
    return app


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE)


def _good() -> dict[str, Any]:
    return {
        "enabled": True,
        "groups": ["20", "#wetter"],
        "broadcast": False,
        "pos_mode": "radius",
        "radius_km": 150,
    }


def _rejects(body: Any) -> bool:
    try:
        validate_settings(body)
    except InternetFeedSettingsError:
        return True
    return False


def _with(**changes: Any) -> dict[str, Any]:
    return {**_good(), **changes}


def _test_validate(rec: Any) -> None:
    ok = validate_settings(_good())
    rec(
        "validate: good body, tag upper-cased, exactly five keys",
        ok["groups"] == ["20", "#WETTER"] and set(ok) == set(_good()),
    )
    rec(
        "validate: dup collapse '#a','#A' keeps first order",
        validate_settings(_with(groups=["20", "#a", "#A", "20"]))["groups"] == ["20", "#A"],
    )
    rec(
        "validate: 10 groups ok",
        len(validate_settings(_with(groups=[str(n) for n in range(1, 11)]))["groups"]) == 10,
    )
    rec(
        "validate: 11 distinct groups rejected",
        _rejects(_with(groups=[str(n) for n in range(1, 12)])),
    )
    rec(
        "validate: 11 entries that dedup to 10 accepted (cap is after dedup)",
        len(validate_settings(_with(groups=[str(n) for n in range(1, 11)] + ["1"]))["groups"])
        == 10,
    )
    for bad in ("0", "020", "100000", "*", "DL1ABC-1", "#a b", "", "x,#A", "#A ", 20):
        rec(f"validate: group {bad!r} rejected", _rejects(_with(groups=[bad])))
    rec(
        "validate: 99999 accepted",
        validate_settings(_with(groups=["99999"]))["groups"] == ["99999"],
    )
    for radius in (9, 2001, True, 10.5, "150"):
        rec(f"validate: radius_km {radius!r} rejected", _rejects(_with(radius_km=radius)))
    for radius in (10, 2000):
        rec(
            f"validate: radius_km {radius} accepted",
            validate_settings(_with(radius_km=radius))["radius_km"] == radius,
        )
    rec("validate: unknown pos_mode rejected", _rejects(_with(pos_mode="everywhere")))
    rec("validate: enabled 'yes' rejected", _rejects(_with(enabled="yes")))
    rec("validate: broadcast 1 rejected", _rejects(_with(broadcast=1)))
    rec("validate: groups not a list rejected", _rejects(_with(groups="20")))
    rec("validate: non-object body rejected", _rejects([1]))
    for key in _good():
        body = _good()
        del body[key]
        rec(f"validate: missing {key} rejected", _rejects(body))
    try:
        validate_settings(_with(groups=["DL1ABC-1"], pos_mode="x"))
        echoed = ""
    except InternetFeedSettingsError as exc:
        echoed = str(exc)
    rec("validate: error echoes only the offending token", "DL1ABC-1" in echoed)


def _test_suggest(rec: Any) -> None:
    regs = {
        "I": {"GCB0": 20, "GCB1": 0, "GCB2": "262", "GCB3": 0, "GCB4": 20, "GCB5": 0},
        "SN": {"NOALL": True},
    }
    out = suggest_from_registers(regs)
    rec(
        "suggest: zeros skipped, ints to str, deduped, NOALL true -> broadcast false",
        out == {"groups": ["20", "262"], "broadcast": False},
    )
    rec(
        "suggest: NOALL false -> broadcast true",
        suggest_from_registers({"SN": {"NOALL": False}})["broadcast"] is True,
    )
    rec(
        "suggest: no registers -> defaults",
        suggest_from_registers({}) == {"groups": [], "broadcast": True},
    )
    rec(
        "suggest: junk slot values skipped",
        suggest_from_registers({"I": {"GCB0": 100000, "GCB1": None, "GCB2": True}})["groups"] == [],
    )


async def _test_routes(rec: Any) -> None:
    svc = _FakeService(configured=False)
    regs = {"I": {"GCB0": 20}, "SN": {"NOALL": False}}
    async with _client(_app(svc, regs)) as c:
        r = await c.get("/api/internet-feed")
        rec(
            "GET unconfigured adds suggested",
            r.status_code == 200 and r.json()["suggested"] == {"groups": ["20"], "broadcast": True},
        )
    svc2 = _FakeService(configured=True)
    async with _client(_app(svc2, regs)) as c:
        r = await c.get("/api/internet-feed")
        rec("GET configured has no suggested", r.status_code == 200 and "suggested" not in r.json())
        r = await c.put("/api/internet-feed", json=_good())
        expected = validate_settings(_good())
        rec(
            "PUT passes exactly the validated dict",
            r.status_code == 200 and svc2.calls[-1] == ("apply", expected),
        )
        n = len(svc2.calls)
        r = await c.put("/api/internet-feed", json=_with(groups=["0"]))
        rec(
            "PUT invalid -> 422, service untouched",
            r.status_code == 422 and len(svc2.calls) == n,
        )
        r = await c.put("/api/internet-feed", content="not json")
        rec("PUT non-JSON -> 422", r.status_code == 422 and len(svc2.calls) == n)
        r = await c.post("/api/internet-feed/forget")
        rec("forget route", r.status_code == 200 and svc2.calls[-1] == ("forget", None))

    async with _client(_app(None)) as c:
        codes = [
            (await c.get("/api/internet-feed")).status_code,
            (await c.put("/api/internet-feed", json=_good())).status_code,
            (await c.post("/api/internet-feed/forget")).status_code,
        ]
        rec("no service -> 503 on every route", codes == [503, 503, 503])


async def _test_guard(rec: Any) -> None:
    svc = _FakeService()
    routes = [
        ("GET", "/api/internet-feed", None),
        ("PUT", "/api/internet-feed", _good()),
        ("POST", "/api/internet-feed/forget", None),
    ]
    async with _client(_app(svc, origins=("https://ok.example",))) as c:
        for method, path, body in routes:
            r = await c.request(method, path, json=body, headers=_EVIL)
            rec(f"guard: foreign Host 403 on {method} {path}", r.status_code == 403)
            r = await c.request(method, path, json=body, headers={"origin": "https://evil.example"})
            rec(f"guard: foreign Origin 403 on {method} {path}", r.status_code == 403)
        rec("guard: service never called on refusal", svc.calls == [])
        r = await c.get("/api/internet-feed")
        rec("guard: LAN host OK", r.status_code == 200)
        r = await c.get("/api/internet-feed", headers={"origin": "http://localhost:5173"})
        rec("guard: same-host Origin OK", r.status_code == 200)
        r = await c.get("/api/internet-feed", headers={"origin": "https://ok.example"})
        rec("guard: listed Origin OK", r.status_code == 200)


async def _test_withheld(rec: Any) -> None:
    recorder = _FakeRecorder()
    pad = "x" * 200
    async with _client(_app(_FakeService(), recorder=recorder)) as c:
        await c.put("/api/internet-feed", json={**_good(), "token": _SECRET, "pad": pad})
        await c.post(
            "/api/internet-feed/forget",
            content=f'{{"token": "{_SECRET}", "pad": "{pad}"}}',
            headers={"content-type": "application/json"},
        )
    leaked = any(_SECRET[:8] in json.dumps(r, default=str) for r in recorder.records)
    rec(
        "stall: /api/internet-feed and /forget are recorded without a body",
        len(recorder.records) == 2
        and all(r.get("body") is None for r in recorder.records)
        and all(r.get("detail", {}).get("body_withheld") for r in recorder.records)
        and not leaked,
    )
    other = _FakeRecorder()
    app = FastAPI()

    @app.put("/api/internet-feedX")
    async def other_path() -> dict[str, bool]:
        return {"ok": True}

    async with _client(StallMiddleware(app, cast(Any, other))) as c:
        await c.put("/api/internet-feedX", json={"a": 1})
    rec(
        "stall: /api/internet-feedX is NOT withheld",
        len(other.records) == 1 and not other.records[0].get("detail", {}).get("body_withheld"),
    )


async def run_internet_feed_routes_tests() -> bool:
    """Return True iff every internet feed router case passes."""
    if has_console:
        print("\n🧪 Testing internet feed routes:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: Any) -> None:
        results.append((label, bool(ok)))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _test_validate(_record)
    _test_suggest(_record)
    await _test_routes(_record)
    await _test_guard(_record)
    await _test_withheld(_record)

    passed = sum(1 for _, ok in results if ok)
    if has_console or passed != len(results):
        for label, ok in results:
            if not ok:
                print(f"❌ FAIL | {label}")
        print(f"\n🧪 Internet Feed Routes Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    _ok = asyncio.run(run_internet_feed_routes_tests())
    print(f"internet_feed_routes: {'PASS' if _ok else 'FAIL'}")
    raise SystemExit(0 if _ok else 1)
