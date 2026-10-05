"""Regression suite for the Node Admin router (`sse_routes/node_admin.py`).

Plan: doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md §5 (guard,
routes, redaction) and §6.1 B2. Fully offline: a fake `NodeAdminService`
records calls and raises on demand; the redaction cases run through the REAL
`StallMiddleware` with a fake recorder.

Cases:

  1. The PUT keys route is timed but its body is withheld by the REAL
     `StallMiddleware`. Only an OVERSIZED or NON-JSON body discriminates the
     prefix rule: `redact()` already masks a small JSON body, so that case is
     recorded as a non-discriminating sanity check (1c).
  2. No response body contains the password: not the service's 422, not the
     pydantic-validation 422s (extra field, oversize, non-object body, wrong
     type), not a 422 whose service text echoes the password.
  3. Service absent -> 503 on every route.
  4. Guard: Host allowlist (LAN names, private/loopback literals, never a
     public name), Origin (foreign 403, same-origin OK, none OK, listed OK),
     on every route, with the service never called on a refusal.
  5. Error mapping 422/409/503 and request validation (transport, limit).
  6. PUT/DELETE return 204 with no body; arguments pass through.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx
from fastapi import FastAPI

from .commands.constants import has_console
from .node_admin_types import NodeAdminBusyError, NodeAdminUnavailableError
from .sse_routes.node_admin import build_node_admin_router, host_allowed, origin_allowed
from .stall_middleware import StallMiddleware
from .stalls import redact

SENTINEL = "Pw15CharSecret!"  # exactly 15 characters, one over the firmware's 14

_BASE = "http://localhost"
_EVIL = {"host": "evil.example.com"}


class _FakeService:
    """Records every call; `raise_next` is raised once by the next call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raise_next: BaseException | None = None

    def _hit(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc

    async def list_targets(self) -> list[dict[str, Any]]:
        self._hit("list_targets")
        return [{"target": "DK5EN-90", "has_key": True}]

    async def set_key(self, target: str, password: str, tx_max: int | None) -> None:
        self._hit("set_key", target, password, tx_max)

    async def delete_key(self, target: str) -> None:
        self._hit("delete_key", target)

    async def send(self, target: str, cmd: str, args: str, transport: str) -> dict[str, Any]:
        self._hit("send", target, cmd, args, transport)
        return {"log_id": 7, "ctr": 51, "text": "RM1 51 setout"}

    async def reask(self, log_id: int) -> dict[str, Any]:
        self._hit("reask", log_id)
        return {"log_id": log_id}

    async def sync(self, target: str) -> dict[str, Any]:
        self._hit("sync", target)
        return {"log_id": 9}

    async def history(self, target: str | None, limit: int) -> list[dict[str, Any]]:
        self._hit("history", target, limit)
        return [{"id": 1, "state": "verified"}]

    async def on_reply(self, message: dict[str, Any]) -> None:
        self._hit("on_reply", message)


class _ManagerStub:
    def __init__(self, service: _FakeService | None) -> None:
        self.node_admin_service = service


class _FakeConfig:
    def __init__(self) -> None:
        self.body_cap_bytes = 16  # tiny: an ordinary key body is already "oversized"


class _FakeRecorder:
    """The subset of `StallRecorder` that `StallMiddleware` uses."""

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.records: list[dict[str, Any]] = []

    def severity_for(self, duration_ms: float) -> str | None:
        return "stall"

    def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def _app(
    service: _FakeService | None,
    origins: tuple[str, ...] = (),
    recorder: _FakeRecorder | None = None,
) -> Any:
    app = FastAPI()
    manager: Any = _ManagerStub(service)
    app.include_router(build_node_admin_router(manager, lambda: origins))
    if recorder is not None:
        return StallMiddleware(app, cast(Any, recorder))
    return app


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE)


# (method, path, json body) for every route, with a valid body where one is needed.
_ROUTES: list[tuple[str, str, Any]] = [
    ("PUT", "/api/node-admin/keys/DK5EN-90", {"password": "abc", "tx_max": 15}),
    ("DELETE", "/api/node-admin/keys/DK5EN-90", None),
    ("GET", "/api/node-admin/targets", None),
    ("POST", "/api/node-admin/send", {"target": "DK5EN-90", "cmd": "setout", "args": "a2 on"}),
    ("POST", "/api/node-admin/reask/7", None),
    ("POST", "/api/node-admin/sync/DK5EN-90", None),
    ("GET", "/api/node-admin/history", None),
]


async def _request(
    client: httpx.AsyncClient, method: str, path: str, body: Any, **kw: Any
) -> httpx.Response:
    if body is not None:
        kw.setdefault("json", body)
    return await client.request(method, path, **kw)


async def _test_redaction(record: Any) -> None:
    """1. Withheld request body, through the real StallMiddleware."""
    rec = _FakeRecorder()
    async with _client(_app(_FakeService(), recorder=rec)) as client:
        # Far over the 16-byte capture cap: the tap truncates, the JSON parse
        # fails and the generic path would store the raw prefix text.
        await client.put(
            "/api/node-admin/keys/DK5EN-90",
            content=f'{{"password": "{SENTINEL}", "pad": "{"x" * 200}"}}',
            headers={"content-type": "application/json"},
        )
        # Non-JSON content type: stored as raw text unless withheld.
        await client.put(
            "/api/node-admin/keys/DK5EN-90",
            content=f"password={SENTINEL}",
            headers={"content-type": "text/plain"},
        )
    dumped = [json.dumps(r, default=str) for r in rec.records]
    record(
        "1a. oversized/non-JSON key bodies: recorded, body withheld, sentinel absent",
        len(rec.records) == 2
        and all(r.get("body") is None for r in rec.records)
        and all(r.get("detail", {}).get("body_withheld") for r in rec.records)
        and not any(SENTINEL[:8] in d for d in dumped)
        and all(r.get("path") == "/api/node-admin/keys/DK5EN-90" for r in rec.records),
    )

    # The bare prefix and a trailing-slash variant are withheld as well.
    rec2 = _FakeRecorder()
    async with _client(_app(_FakeService(), recorder=rec2)) as client:
        await client.put("/api/node-admin/keys", content=f"password={SENTINEL}")
        await client.put("/api/node-admin/keys/", content=f"password={SENTINEL}")
        await client.post("/api/node-admin/send", json={"target": "T", "cmd": "setout"})
    paths = [r.get("path") for r in rec2.records]
    record(
        "1b. /keys and /keys/ withheld; /send is NOT (prefix is not a blanket)",
        len(rec2.records) == 3
        and [bool(r.get("detail", {}).get("body_withheld")) for r in rec2.records]
        == [True, True, False]
        and paths[2] == "/api/node-admin/send"
        and rec2.records[2].get("body") == {"target": "T", "cmd": "setout"},
    )

    # Non-discriminating: the real recorder runs `redact()` on a stored body (the
    # fake does not, so it is applied here), which masks a small JSON body even
    # without the withheld rule.
    rec3 = _FakeRecorder()
    rec3.config.body_cap_bytes = 4096
    async with _client(_app(_FakeService(), recorder=rec3)) as client:
        await client.put("/api/node-admin/keys/DK5EN-90", json={"password": SENTINEL})
    record(
        "1c. small JSON key body never stores the sentinel (sanity, non-discriminating)",
        len(rec3.records) == 1
        and SENTINEL[:8] not in json.dumps(redact(rec3.records[0].get("body")), default=str),
    )


async def _test_no_password_in_responses(record: Any) -> None:
    """2. No response body contains the password."""
    svc = _FakeService()
    bodies: list[tuple[str, int, str]] = []
    async with _client(_app(svc)) as client:

        async def put(label: str, **kw: Any) -> None:
            resp = await client.put("/api/node-admin/keys/DK5EN-90", **kw)
            bodies.append((label, resp.status_code, resp.text))

        # Service refuses a 15-char password.
        svc.raise_next = ValueError("password must be 1 to 14 bytes")
        await put("service 422", json={"password": SENTINEL})
        # Defensive: a service text that echoes the password is scrubbed.
        svc.raise_next = ValueError(f"bad password {SENTINEL}")
        await put("service echo 422", json={"password": SENTINEL})
        # Pydantic-level failures that echo `input` in a FastAPI body-model 422.
        await put("extra field", json={"password": "abc", "note": SENTINEL})
        await put("oversize", json={"password": SENTINEL + "x" * 200})
        await put("non-object body", json=[SENTINEL])
        await put("wrong tx_max type", json={"password": SENTINEL, "tx_max": "high"})
        await put("malformed JSON", content=f'{{"password": "{SENTINEL}"', headers={"x": "y"})
        await put("non-JSON body", content=f"password={SENTINEL}")
        await put("oversize raw body", content=SENTINEL * 200)
        resp = await client.put(
            "/api/node-admin/keys/DK5EN-90", json={"password": SENTINEL, "tx_max": 5}
        )
        bodies.append(("accepted", resp.status_code, resp.text))

    leaks = [label for label, _, text in bodies if SENTINEL in text or SENTINEL[:8] in text]
    record(f"2a. no response body contains the password ({', '.join(leaks)})", not leaks)
    by_label = {label: status for label, status, _ in bodies}
    record(
        "2b. every refused key request is a 422, the valid one a 204",
        all(s == 422 for lbl, s in by_label.items() if lbl != "accepted")
        and by_label["accepted"] == 204,
    )
    record(
        "2c. the password reaches the service only on the accepted call",
        [c for c in svc.calls if c[0] == "set_key" and c[1][1] == SENTINEL]
        and sum(1 for c in svc.calls if c[0] == "set_key") == 3,
    )


async def _test_service_absent(record: Any) -> None:
    """3. manager.node_admin_service is None -> 503 on every route."""
    async with _client(_app(None)) as client:
        results = [(m, p, (await _request(client, m, p, b)).status_code) for m, p, b in _ROUTES]
    bad = [(m, p, s) for m, p, s in results if s != 503]
    record("3. service absent -> 503 on every route %s" % (bad or ""), not bad)


def _test_pure_guards(record: Any) -> None:
    hosts_ok = [
        "localhost",
        "localhost:8082",
        "mcapp.local",
        "mcapp.local:80",
        "MCAPP.LOCAL",
        "mcapp",
        "mcapp.fritz.box",
        "mcapp.home.arpa",
        "mcapp.lan",
        "mcapp.home",
        "127.0.0.1",
        "127.0.0.1:2981",
        "192.168.1.20",
        "10.0.0.5:8082",
        "172.16.3.4",
        "[::1]",
        "[::1]:8082",
        "[fe80::1]:80",
        "mypi",
        "mypi.local",
        "mypi.fritz.box:443",
    ]
    hosts_bad = [
        None,
        "",
        "evil.example.com",
        "mcapp.example.com",
        "mypi.example.com",
        "mcapp.local.evil.com",
        "evilmcapp.local",
        "8.8.8.8",
        "93.184.216.34:80",
        "0.0.0.0",  # noqa: S104  # a Host value under test, not a bind address
        "[2606:4700::1]",
        "[::1",
        "localhost.evil.com",
    ]
    record(
        "4a. host_allowed: LAN names and private/loopback literals pass",
        all(host_allowed(h, "mypi") for h in hosts_ok),
    )
    record(
        "4b. host_allowed: public names, public IPs, empty host refuse",
        not any(host_allowed(h, "mypi") for h in hosts_bad),
    )
    record(
        "4c. host_allowed: the machine's own hostname (FQDN input) is accepted",
        host_allowed("pi5.local", "pi5.lan") and host_allowed("pi5", "PI5.example.org"),
    )
    record(
        "4d. origin_allowed: none / same host (port ignored) / listed pass",
        origin_allowed(None, "mcapp.local")
        and origin_allowed("http://mcapp.local", "mcapp.local")
        and origin_allowed("https://mcapp.local:8443", "mcapp.local:80")
        and origin_allowed("http://localhost:5173", "localhost:8082")
        and origin_allowed("http://[::1]:5173", "[::1]:8082")
        and origin_allowed("http://dev.lan:5173", "mcapp.local", ("http://dev.lan:5173",)),
    )
    record(
        "4e. origin_allowed: foreign, null, malformed, unlisted refuse",
        not origin_allowed("http://evil.example.com", "mcapp.local")
        and not origin_allowed("null", "mcapp.local")
        and not origin_allowed("http://mcapp.local.evil.com", "mcapp.local")
        and not origin_allowed("http://evilmcapp.local", "mcapp.local")
        and not origin_allowed("http://x.mcapp.local", "mcapp.local")
        and not origin_allowed("http://mcapp.local@evil.com", "mcapp.local")
        and not origin_allowed("http://dev.lan:5173", "mcapp.local", ("http://other:1",))
        and not origin_allowed("http://dev.lan:5173", None)
        and not origin_allowed("", "mcapp.local")
        and not origin_allowed("http://[bad", "mcapp.local"),
    )


async def _test_guard_over_http(record: Any) -> None:
    """4. The guard on every route; the service is never reached on a refusal."""
    svc = _FakeService()
    async with _client(_app(svc, origins=("http://trusted.lan:5173",))) as client:
        foreign_host = [
            (await _request(client, m, p, b, headers=_EVIL)).status_code for m, p, b in _ROUTES
        ]
        public_host = [
            (await _request(client, m, p, b, headers={"host": "mcapp.example.org"})).status_code
            for m, p, b in _ROUTES
        ]
        foreign_origin = [
            (
                await _request(client, m, p, b, headers={"origin": "http://evil.example.com"})
            ).status_code
            for m, p, b in _ROUTES
        ]
        null_origin = await client.get("/api/node-admin/targets", headers={"origin": "null"})
        refused_calls = len(svc.calls)

        ok_hosts = {}
        for host in ("mcapp.local", "localhost:8082", "192.168.1.20", "10.1.2.3:80"):
            ok_hosts[host] = (
                await client.get("/api/node-admin/targets", headers={"host": host})
            ).status_code
        same_origin = await client.post(
            "/api/node-admin/sync/DK5EN-90",
            headers={"host": "mcapp.local", "origin": "https://mcapp.local"},
        )
        listed_origin = await client.get(
            "/api/node-admin/targets", headers={"origin": "http://trusted.lan:5173"}
        )
        no_origin = await client.get("/api/node-admin/targets")
        evil_put = await client.put(
            "/api/node-admin/keys/DK5EN-90",
            json={"password": SENTINEL},
            headers={"origin": "http://evil.example.com"},
        )
        evil_preflight_like = await client.delete(
            "/api/node-admin/keys/DK5EN-90", headers={"origin": "http://evil.example.com"}
        )

    record("4f. foreign Host -> 403 on every route", set(foreign_host) == {403})
    record("4g. public TLS-style hostname -> 403 on every route", set(public_host) == {403})
    record("4h. foreign Origin -> 403 on every route", set(foreign_origin) == {403})
    record("4i. Origin 'null' -> 403", null_origin.status_code == 403)
    record(
        "4j. the service is never called for any refused request",
        refused_calls == 0
        and evil_put.status_code == 403
        and evil_preflight_like.status_code == 403,
    )
    record(
        "4k. mcapp.local / localhost / private IP literals pass the guard",
        all(s == 200 for s in ok_hosts.values()),
    )
    record("4l. same-origin Origin passes", same_origin.status_code == 200)
    record("4m. an Origin listed in allowed_origins passes", listed_origin.status_code == 200)
    record("4n. a GET with no Origin passes", no_origin.status_code == 200)


async def _test_error_mapping(record: Any) -> None:
    """5. 422/409/503 mapping and request validation."""
    svc = _FakeService()
    mapped: list[tuple[str, int, str]] = []
    errors: list[tuple[str, BaseException]] = [
        ("value", ValueError("unknown command")),
        ("busy", NodeAdminBusyError("command in flight")),
        ("unavailable", NodeAdminUnavailableError("BLE not connected")),
    ]
    async with _client(_app(svc)) as client:
        for method, path, body in _ROUTES:
            for label, exc in errors:
                svc.raise_next = exc
                resp = await _request(client, method, path, body)
                mapped.append((f"{method} {path} {label}", resp.status_code, resp.text))
        svc.raise_next = None

        want = {"value": 422, "busy": 409, "unavailable": 503}
        wrong = [(k, s) for k, s, _ in mapped if s != want[k.rsplit(" ", 1)[1]]]
        record(
            "5a. ValueError 422, Busy 409, Unavailable 503 on every route %s" % (wrong or ""),
            not wrong,
        )
        record(
            "5b. the service's text is the detail",
            all(
                json.loads(t)["detail"]
                == {
                    "value": "unknown command",
                    "busy": "command in flight",
                    "unavailable": "BLE not connected",
                }[k.rsplit(" ", 1)[1]]
                for k, _, t in mapped
            ),
        )

        n = len(svc.calls)
        statuses = [
            (
                await client.post(
                    "/api/node-admin/send",
                    json={"target": "T", "cmd": "setout", "transport": "carrier-pigeon"},
                )
            ).status_code,
            (await client.post("/api/node-admin/send", json={"target": "T"})).status_code,
            (await client.post("/api/node-admin/send", json={"cmd": "setout"})).status_code,
            (await client.post("/api/node-admin/send", content="not json")).status_code,
            (await client.get("/api/node-admin/history?limit=0")).status_code,
            (await client.get("/api/node-admin/history?limit=501")).status_code,
            (await client.get("/api/node-admin/history?limit=abc")).status_code,
            (await client.post("/api/node-admin/reask/abc")).status_code,
        ]
        record(
            "5c. bad transport / missing field / bad JSON / limit bounds / log_id -> 422, no call",
            statuses == [422] * 8 and len(svc.calls) == n,
        )
        ok_edges = [
            (await client.get("/api/node-admin/history?limit=1")).status_code,
            (await client.get("/api/node-admin/history?limit=500")).status_code,
            *[
                (
                    await client.post(
                        "/api/node-admin/send",
                        json={"target": "T", "cmd": "sync", "transport": t},
                    )
                ).status_code
                for t in ("auto", "ble", "udp")
            ],
        ]
        record("5d. limit 1 and 500 and all three transports are accepted", ok_edges == [200] * 5)


async def _test_success_shapes(record: Any) -> None:
    """6. 204s have no body; arguments pass through."""
    svc = _FakeService()
    async with _client(_app(svc)) as client:
        put = await client.put(
            "/api/node-admin/keys/DK5EN-90", json={"password": "pw", "tx_max": 15}
        )
        put_none = await client.put("/api/node-admin/keys/dk5en-91", json={"password": "pw2"})
        delete = await client.delete("/api/node-admin/keys/DK5EN-90")
        targets = await client.get("/api/node-admin/targets")
        send = await client.post(
            "/api/node-admin/send",
            json={"target": "DK5EN-90", "cmd": "setout", "args": "a2 on", "transport": "ble"},
        )
        send_default = await client.post(
            "/api/node-admin/send", json={"target": "DK5EN-90", "cmd": "sync"}
        )
        reask = await client.post("/api/node-admin/reask/42")
        sync = await client.post("/api/node-admin/sync/DK5EN-90")
        hist = await client.get("/api/node-admin/history?target=DK5EN-90&limit=25")
        hist_default = await client.get("/api/node-admin/history")

    record(
        "6a. PUT and DELETE keys -> 204 with an empty body",
        all(r.status_code == 204 and r.content == b"" for r in (put, put_none, delete)),
    )
    record(
        "6b. key arguments pass through (tx_max optional)",
        svc.calls[0] == ("set_key", ("DK5EN-90", "pw", 15))
        and svc.calls[1] == ("set_key", ("dk5en-91", "pw2", None))
        and svc.calls[2] == ("delete_key", ("DK5EN-90",)),
    )
    record(
        "6c. targets / send / reask / sync return the service payload",
        targets.json() == [{"target": "DK5EN-90", "has_key": True}]
        and send.json() == {"log_id": 7, "ctr": 51, "text": "RM1 51 setout"}
        and reask.json() == {"log_id": 42}
        and sync.json() == {"log_id": 9},
    )
    record(
        "6d. send passes args/transport, defaulting to '' and 'auto'",
        svc.calls[4] == ("send", ("DK5EN-90", "setout", "a2 on", "ble"))
        and svc.calls[5] == ("send", ("DK5EN-90", "sync", "", "auto"))
        and send_default.json() == {"log_id": 7, "ctr": 51, "text": "RM1 51 setout"}
        and svc.calls[6] == ("reask", (42,))
        and svc.calls[7] == ("sync", ("DK5EN-90",)),
    )
    record(
        "6e. history passes target and limit through; default limit 100, no target",
        hist.json() == [{"id": 1, "state": "verified"}]
        and svc.calls[8] == ("history", ("DK5EN-90", 25))
        and svc.calls[9] == ("history", (None, 100))
        and hist_default.status_code == 200,
    )


async def run_node_admin_routes_tests() -> bool:
    """Return True iff every Node Admin router case passes."""
    if has_console:
        print("\n🧪 Testing node admin routes:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: Any) -> None:
        results.append((label, bool(ok)))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    await _test_redaction(_record)
    await _test_no_password_in_responses(_record)
    await _test_service_absent(_record)
    _test_pure_guards(_record)
    await _test_guard_over_http(_record)
    await _test_error_mapping(_record)
    await _test_success_shapes(_record)

    passed = sum(1 for _, ok in results if ok)
    if has_console or passed != len(results):
        for label, ok in results:
            if not ok:
                print(f"❌ FAIL | {label}")
        print(f"\n🧪 Node Admin Routes Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    _ok = asyncio.run(run_node_admin_routes_tests())
    print(f"node_admin_routes: {'PASS' if _ok else 'FAIL'}")
    raise SystemExit(0 if _ok else 1)
