"""Offline tests for mcapp.weblite (the FastAPI-compatible slice on Starlette)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from mcapp.weblite import (
    APIRoute,
    APIRouter,
    CORSMiddleware,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    StreamingResponse,
)


class Item(BaseModel):
    name: str
    qty: int = 1


class Out(BaseModel):
    name: str


async def need_key(x_api_key: Annotated[str | None, Header()] = None) -> str:
    if x_api_key != "k":
        raise HTTPException(401, "bad key")
    return x_api_key


calls: list[str] = []


async def guard(request: Request) -> None:
    calls.append("guard")
    if request.query_params.get("deny"):
        raise HTTPException(403, {"why": "denied"})


async def nested(key: str = Depends(need_key), q: int = Query(default=2, ge=1)) -> str:
    return f"{key}{q}"


def _app() -> tuple[FastAPI, APIRouter]:
    router = APIRouter(dependencies=[Depends(guard)])

    @router.get("/q")
    async def q(
        limit: int = Query(default=50, ge=1, le=500),
        s: str | None = None,
        flag: bool = False,
        req: str = Query(...),
        mx: str | None = Query(default=None, max_length=3),
    ) -> dict[str, Any]:
        calls.append("handler")
        return {"limit": limit, "s": s, "flag": flag, "req": req, "mx": mx}

    @router.get("/p/{log_id}")
    async def p(log_id: int) -> dict[str, int]:
        return {"id": log_id}

    @router.get("/h")
    async def h(x_api_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        return {"k": x_api_key}

    @router.get("/hreq")
    async def hreq(x_must: str = Header(...)) -> dict[str, str]:
        return {"m": x_must}

    @router.post("/m")
    async def m(item: Item) -> dict[str, Any]:
        return item.model_dump()

    @router.post("/mopt")
    async def mopt(item: Item | None = None) -> dict[str, Any]:
        return {"item": item.model_dump() if item else None}

    @router.put("/d")
    async def d(body: dict[str, Any]) -> dict[str, Any]:
        return {"body": body}

    @router.delete("/del", status_code=204)
    async def delete() -> None:
        return None

    @router.post("/created", status_code=201)
    async def created() -> dict[str, str]:
        return {"ok": "yes"}

    @router.get("/dep")
    async def dep(v: str = Depends(nested), k: str = Depends(need_key)) -> dict[str, str]:
        return {"v": v, "k": k}

    @router.get("/err")
    async def err() -> dict[str, str]:
        e = HTTPException(409, {"a": ["ü"]})
        raise e

    @router.get("/uni")
    async def uni() -> dict[str, str]:
        return {"s": "äöü€ 😀"}

    @router.get("/rm", response_model=Out)
    async def rm() -> Any:
        return {"name": "n", "secret": "x"}

    @router.get("/rmbad", response_model=Out)
    async def rmbad() -> Any:
        return {"nope": 1}

    @router.get("/passthru")
    async def passthru() -> Response:
        return Response(status_code=204)

    @router.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen() -> Any:
            yield "ab"

        return StreamingResponse(gen(), media_type="text/plain")

    setattr(router, "push_dispatcher", "attr-ok")  # noqa: B010 - arbitrary attributes must work
    app = FastAPI(title="t", version="1", description="d")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
    app.include_router(router)
    return app, router


def _no_input(obj: Any) -> bool:
    if isinstance(obj, dict):
        return not ({"input", "ctx", "url"} & obj.keys()) and all(
            _no_input(v) for v in obj.values()
        )
    if isinstance(obj, list):
        return all(_no_input(v) for v in obj)
    return True


def _raises_type_error(register: Callable[[], Any]) -> bool:
    try:
        register()
    except TypeError:
        return True
    return False


def _fail_loud() -> list[tuple[str, bool]]:
    def sync_handler() -> None:
        r = APIRouter()

        @r.get("/x")
        def h() -> dict[str, int]:
            return {}

    def list_query() -> None:
        r = APIRouter()

        @r.get("/x")
        async def h(ids: list[int]) -> dict[str, int]:
            return {}

    def two_bodies() -> None:
        r = APIRouter()

        @r.post("/x")
        async def h(a: Item, b: Item) -> dict[str, int]:
            return {}

    def response_param() -> None:
        r = APIRouter()

        @r.get("/x")
        async def h(response: Response) -> dict[str, int]:
            return {}

    def unannotated() -> None:
        r = APIRouter()

        @r.get("/x")
        async def h(a) -> dict[str, int]:  # type: ignore[no-untyped-def]  # the point of the test
            return {}

    def unresolvable() -> None:
        r = APIRouter()

        @r.get("/x")
        async def h(a: NoSuchType) -> dict[str, int]:  # type: ignore[name-defined]  # noqa: F821 - the point
            return {}

    def sync_dependency() -> None:
        def dep() -> int:  # sync on purpose
            return 1

        r = APIRouter(dependencies=[Depends(dep)])  # type: ignore[arg-type]  # the point

        @r.get("/x")
        async def h() -> dict[str, int]:
            return {}

    def prefix() -> None:
        FastAPI().include_router(APIRouter(), prefix="/x")  # type: ignore[call-arg]

    def extra_kwarg() -> None:
        APIRouter().get("/x", tags=["a"])  # type: ignore[call-arg]

    def docs_url() -> None:
        FastAPI(docs_url=None)  # type: ignore[call-arg]

    cases = [
        ("sync handler", sync_handler),
        ("list query param", list_query),
        ("two body params", two_bodies),
        ("Response parameter", response_param),
        ("unannotated parameter", unannotated),
        ("unresolvable annotation", unresolvable),
        ("sync dependency", sync_dependency),
        ("include_router prefix", prefix),
        ("decorator tags kwarg", extra_kwarg),
        ("FastAPI docs_url kwarg", docs_url),
    ]
    return [("TypeError: " + name, _raises_type_error(fn)) for name, fn in cases]


def _behaviour() -> list[tuple[str, bool]]:  # noqa: PLR0915 - one flat list of literal expectations
    app, router = _app()
    c = TestClient(app, raise_server_exceptions=False)
    out: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        out.append((name, ok))

    def body(resp: Any) -> bytes:
        return bytes(resp.content)

    r = c.get("/q?req=a&limit=7&s=x&flag=yes")
    check(
        "query binding + bool spelling",
        body(r) == b'{"limit":7,"s":"x","flag":true,"req":"a","mx":null}',
    )
    check(
        "query default",
        body(c.get("/q?req=a")) == b'{"limit":50,"s":null,"flag":false,"req":"a","mx":null}',
    )
    check("json content-type", r.headers["content-type"] == "application/json")
    for spelled in ("1", "on", "true"):
        check("bool " + spelled, c.get("/q?req=a&flag=" + spelled).json()["flag"] is True)

    r = c.get("/q?req=a&limit=501")
    check(
        "constraint -> 422 literal",
        r.status_code == 422
        and body(r)
        == b'{"detail":[{"type":"less_than_equal","loc":["query","limit"],'
        b'"msg":"Input should be less than or equal to 500"}]}',
    )
    r = c.get("/q")
    check(
        "missing required -> 422 literal",
        r.status_code == 422
        and body(r)
        == b'{"detail":[{"type":"missing","loc":["query","req"],"msg":"Field required"}]}',
    )
    r = c.get("/q?req=a&mx=abcd")
    check(
        "max_length -> 422",
        r.status_code == 422 and r.json()["detail"][0]["loc"] == ["query", "mx"],
    )
    r = c.get("/p/abc")
    check(
        "bad path int -> 422",
        r.status_code == 422 and r.json()["detail"][0]["loc"] == ["path", "log_id"],
    )
    check("good path int", c.get("/p/5").json() == {"id": 5})
    r = c.get("/hreq")
    check(
        "missing header -> 422 loc",
        r.status_code == 422 and r.json()["detail"][0]["loc"] == ["header", "x-must"],
    )
    check("header underscore->hyphen", c.get("/h", headers={"X-Api-Key": "z"}).json() == {"k": "z"})
    check("header absent -> default", c.get("/h").json() == {"k": None})

    ct = {"content-type": "application/json"}
    # QRZ and Node Admin bodies carry passwords, and a truncated one is malformed JSON.
    secret = c.post("/m", content=b'{"password":"SECRET-123"', headers=ct)
    out.append(
        (
            "malformed JSON 422 never echoes the body",
            secret.status_code == 422 and "SECRET-123" not in secret.text,
        )
    )
    r = c.post("/m", content=b"{bad", headers=ct)
    check(
        "bad json -> 422", r.status_code == 422 and r.json()["detail"][0]["type"] == "json_invalid"
    )
    r = c.post("/m", json={"name": "a", "qty": "x"})
    check(
        "body field -> loc",
        r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "qty"],
    )
    r = c.post("/m")
    check(
        "empty body required -> missing",
        r.json()["detail"] == [{"type": "missing", "loc": ["body"], "msg": "Field required"}],
    )
    check("empty body optional -> default", c.post("/mopt").json() == {"item": None})
    r = c.post("/m", content=b'{"name":"a"}', headers={"content-type": "text/plain"})
    check(
        "non-json content-type -> 422 not parsed",
        r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body"],
    )
    r = c.post("/m", content=b'{"name":"a"}')
    check("no content-type -> 422 not parsed", r.status_code == 422)
    r = c.post("/m", content=b'{"name":"a"}', headers={"content-type": "application/vnd.x+json"})
    check("+json content-type parsed", r.status_code == 200)
    check("body not embedded", c.post("/m", json={"name": "a"}).json() == {"name": "a", "qty": 1})
    check("dict body", c.put("/d", json={"a": 1}).json() == {"body": {"a": 1}})
    check("dict body wrong type", c.put("/d", json=[1]).status_code == 422)
    all_422 = [
        c.get("/q?req=a&limit=501"), c.get("/q"), c.get("/p/abc"), c.get("/hreq"),
        c.post("/m", content=b"{bad", headers=ct),
        c.post("/m", json={"name": "a", "qty": "x"}), c.post("/m"),
    ]  # fmt: skip
    check("no input/ctx/url in any 422", all(_no_input(x.json()) for x in all_422))

    r = c.delete("/del")
    check(
        "204 empty body",
        r.status_code == 204 and body(r) == b"" and "content-length" not in r.headers,
    )
    check("status_code kwarg", c.post("/created").status_code == 201)
    check("Response passthrough", c.get("/passthru").status_code == 204)
    check("StreamingResponse passthrough", body(c.get("/stream")) == b"ab")
    check("unicode raw utf-8", body(c.get("/uni")) == '{"s":"äöü€ 😀"}'.encode())
    check("response_model filters", body(c.get("/rm")) == b'{"name":"n"}')
    check("response_model violation -> 500", c.get("/rmbad").status_code == 500)

    r = c.get("/err")
    check("dict detail", r.status_code == 409 and body(r) == '{"detail":{"a":["ü"]}}'.encode())
    r = c.get("/nothing")
    check("404 json", r.status_code == 404 and body(r) == b'{"detail":"Not Found"}')
    r = c.post("/p/1")
    check(
        "405 json + Allow",
        r.status_code == 405
        and body(r) == b'{"detail":"Method Not Allowed"}'
        and "allow" in r.headers,
    )
    check("no automatic HEAD", c.head("/q?req=a").status_code == 405)
    r = c.options("/q", headers={"Origin": "http://x", "Access-Control-Request-Method": "GET"})
    check(
        "CORS preflight",
        r.status_code == 200 and r.headers.get("access-control-allow-origin") == "*",
    )

    calls.clear()
    r = c.get("/q?req=a&deny=1")
    check(
        "router dependency before handler",
        r.status_code == 403 and r.json() == {"detail": {"why": "denied"}} and calls == ["guard"],
    )
    check("dependency auth 401", c.get("/dep").json() == {"detail": "bad key"})
    check(
        "nested dep + cache",
        c.get("/dep?q=3", headers={"x-api-key": "k"}).json() == {"v": "k3", "k": "k"},
    )
    check(
        "dep param error -> 422",
        c.get("/dep?q=0", headers={"x-api-key": "k"}).json()["detail"][0]["loc"] == ["query", "q"],
    )

    routes = [x for x in app.routes if isinstance(x, APIRoute)]
    h = next(x for x in routes if x.path == "/p/{log_id}")
    check("route is starlette Route", isinstance(h, Route) and h.methods == {"GET"})
    check(
        "endpoint is the original handler",
        h.endpoint.__name__ == "p" and h.endpoint is not h._serve,
    )
    check(
        "router attrs + routes",
        getattr(router, "push_dispatcher", None) == "attr-ok" and len(router.routes) == len(routes),
    )
    return out


class _Order:
    def __init__(self, app: ASGIApp, tag: str = "") -> None:
        self.app, self.tag = app, tag

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def tagged(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"].append((b"x-order", self.tag.encode()))
            await send(message)

        await self.app(scope, receive, tagged)


def _middleware_order() -> bool:
    app = FastAPI()
    app.add_middleware(_Order, tag="first")
    app.add_middleware(_Order, tag="second")

    @app.get("/x")
    async def x() -> dict[str, int]:
        return {}

    r = TestClient(app).get("/x")
    # Outermost (last added) tags last, exactly as on FastAPI/Starlette.
    return [v for k, v in r.headers.multi_items() if k == "x-order"] == [
        "first",
        "second",
    ]


def _copies_identical() -> bool:
    """ble_service cannot import mcapp, so it carries a copy; drift would split behaviour."""
    here = Path(__file__).resolve()
    copy = here.parents[2] / "ble_service" / "src" / "weblite.py"
    return copy.read_bytes() == here.with_name("weblite.py").read_bytes()


def run_weblite_tests() -> bool:
    results = _fail_loud() + _behaviour() + [("middleware add order", _middleware_order())]
    results.append(("ble_service carries a byte-identical copy", _copies_identical()))
    failed = [name for name, ok in results if not ok]
    for name in failed:
        print("FAIL: " + name)
    print(f"weblite: {len(results) - len(failed)}/{len(results)} passed")
    return not failed


if __name__ == "__main__":
    sys.exit(0 if run_weblite_tests() else 1)
