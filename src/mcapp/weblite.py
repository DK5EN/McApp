"""weblite: the slice of FastAPI this repo uses, on Starlette + pydantic only.

FastAPI costs ~11.5 MB RSS per process over Starlette. This module keeps the
call-site surface (routers, Depends, Query/Header, response_model, 422 bodies)
and raises TypeError at REGISTRATION time for anything outside it, so an
unsupported feature fails at import and never silently at request time.
Self-contained on purpose (stdlib, starlette, pydantic): it is copied verbatim
into ble_service. Deliberate deviation from FastAPI: 422 details omit
`input`, `ctx` and `url` (an echoed `input` would leak submitted passwords).
"""

from __future__ import annotations

import copy
import inspect
import json
import types
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

__all__ = [
    "APIRoute",
    "APIRouter",
    "CORSMiddleware",
    "Depends",
    "FastAPI",
    "HTTPException",
    "Header",
    "Query",
    "Request",
    "Response",
    "StreamingResponse",
]

_UNSET: Any = object()
_OK_MIN = 200
_SCALARS: tuple[type, ...] = (int, float, str, bool)


class HTTPException(StarletteHTTPException):
    detail: Any

    def __init__(
        self, status_code: int, detail: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)


class ResponseValidationError(ValueError):
    """A handler's return value violated its response_model (a server bug, 500)."""


class _Info:
    kind = ""

    def __init__(self, default: Any, constraints: dict[str, Any]) -> None:
        self.default = default
        self.constraints = {k: v for k, v in constraints.items() if v is not None}


class _Query(_Info):
    kind = "query"


class _Header(_Info):
    kind = "header"


class _Depends:
    def __init__(self, call: Callable[..., Awaitable[Any]]) -> None:
        self.call = call


def Query(  # noqa: N802, PLR0913 - FastAPI-compatible name and signature
    default: Any = ...,
    *,
    ge: float | None = None,
    le: float | None = None,
    gt: float | None = None,
    lt: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
) -> Any:
    return _Query(
        default,
        {
            "ge": ge,
            "le": le,
            "gt": gt,
            "lt": lt,
            "min_length": min_length,
            "max_length": max_length,
        },
    )


def Header(  # noqa: N802 - FastAPI-compatible name
    default: Any = ...,
    *,
    min_length: int | None = None,
    max_length: int | None = None,
) -> Any:
    return _Header(default, {"min_length": min_length, "max_length": max_length})


def Depends(dependency: Callable[..., Awaitable[Any]]) -> Any:  # noqa: N802 - FastAPI-compatible name
    return _Depends(dependency)


@dataclass
class _Param:
    name: str
    kind: str  # path | query | header | body
    alias: str
    adapter: TypeAdapter[Any]
    required: bool
    default: Any


@dataclass
class _Node:
    call: Callable[..., Awaitable[Any]]
    params: list[_Param] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    deps: list[tuple[str | None, _Node]] = field(default_factory=list)


def _members(ann: Any) -> list[Any]:
    if get_origin(ann) in (typing.Union, types.UnionType):
        return [a for a in get_args(ann) if a is not type(None)]
    return [ann]


def _is_body(ann: Any) -> bool:
    ms = _members(ann)
    if len(ms) != 1:
        return False
    m = ms[0]
    return m is dict or get_origin(m) is dict or (isinstance(m, type) and issubclass(m, BaseModel))


def _kind(name: str, ann: Any, info: Any, path_names: set[str]) -> str:
    if name in path_names:
        return "path" if info is None else ""
    if info is not None:
        return str(info.kind)
    return "body" if _is_body(ann) else "query"


def _make_param(p: inspect.Parameter, ann: Any, meta: list[Any], info: Any, kind: str) -> _Param:
    """Build one validated parameter; `kind` is path, query, header or body."""
    default = p.default if p.default is not p.empty and not isinstance(p.default, _Info) else ...
    if isinstance(info, _Info) and default is ...:
        default = info.default
    if kind == "path":
        default = ...
    if kind != "body" and not all(m in _SCALARS for m in _members(ann)):
        raise TypeError(f"weblite: unsupported type for {p.name!r}: {ann!r}")
    cons = info.constraints if isinstance(info, _Info) else {}
    try:
        adapter: TypeAdapter[Any] = TypeAdapter(Annotated[(ann, *meta, Field(**cons))])
    except Exception as exc:
        raise TypeError(f"weblite: bad parameter {p.name!r}: {exc!r}") from exc
    alias = p.name.replace("_", "-") if kind == "header" else p.name
    return _Param(p.name, kind, alias, adapter, default is ..., default)


def _build(call: Callable[..., Awaitable[Any]], path_names: set[str], *, allow_body: bool) -> _Node:
    where = getattr(call, "__qualname__", repr(call))
    if not inspect.iscoroutinefunction(call):
        raise TypeError(f"weblite: {where} must be `async def`")
    try:
        hints = typing.get_type_hints(call, include_extras=True)
    except Exception as exc:
        raise TypeError(f"weblite: cannot resolve annotations of {where}: {exc!r}") from exc
    node = _Node(call)
    for name, p in inspect.signature(call).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) or name not in hints:
            raise TypeError(f"weblite: {where}: parameter {name!r} needs a plain annotation")
        ann: Any = hints[name]
        meta: list[Any] = []
        if get_origin(ann) is Annotated:
            ann, *meta = get_args(ann)
        infos = [m for m in [p.default, *meta] if isinstance(m, (_Info, _Depends))]
        if len(infos) > 1:
            raise TypeError(f"weblite: {where}: {name!r} has several Query/Header/Depends")
        info = infos[0] if infos else None
        meta = [m for m in meta if not isinstance(m, (_Info, _Depends))]
        if isinstance(info, _Depends):
            node.deps.append((name, _build(info.call, path_names, allow_body=False)))
        elif isinstance(ann, type) and issubclass(ann, Request):
            node.requests.append(name)
        else:
            kind = _kind(name, ann, info, path_names)
            body_ok = allow_body and not any(q.kind == "body" for q in node.params)
            if not kind or (kind == "body" and not body_ok):
                raise TypeError(f"weblite: {where}: unsupported parameter {name!r}: {ann!r}")
            node.params.append(_make_param(p, ann, meta, info, kind))
    return node


def _errors(exc: ValidationError, loc: list[Any]) -> list[dict[str, Any]]:
    return [
        {"type": e["type"], "loc": [*loc, *e["loc"]], "msg": e["msg"]}
        for e in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def _check(
    p: _Param, raw: Any, loc: list[Any], values: dict[str, Any], errors: list[dict[str, Any]]
) -> None:
    if raw is _UNSET:
        if p.required:
            errors.append({"type": "missing", "loc": loc, "msg": "Field required"})
        else:
            values[p.name] = copy.deepcopy(p.default)
        return
    try:
        values[p.name] = p.adapter.validate_python(raw, from_attributes=True)
    except ValidationError as exc:
        errors.extend(_errors(exc, loc))


class _BodyError(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors


async def _read_body(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return _UNSET
    ctype = request.headers.get("content-type")
    if ctype:
        main, _, sub = ctype.split(";")[0].strip().lower().partition("/")
        if main == "application" and (sub == "json" or sub.endswith("+json")):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise _BodyError(
                    [{"type": "json_invalid", "loc": ["body", exc.pos], "msg": "JSON decode error"}]
                ) from exc
            except Exception as exc:
                raise HTTPException(400, "There was an error parsing the body") from exc
    return raw


async def _solve(
    node: _Node, request: Request, cache: dict[Any, Any], body: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    values: dict[str, Any] = dict.fromkeys(node.requests, request)
    errors: list[dict[str, Any]] = []
    for pname, sub in node.deps:
        if sub.call not in cache:
            sub_values, sub_errors = await _solve(sub, request, cache, _UNSET)
            if sub_errors:
                errors.extend(sub_errors)
                continue
            cache[sub.call] = await sub.call(**sub_values)
        if pname is not None:
            values[pname] = cache[sub.call]
    sources: dict[str, Any] = {
        "path": request.path_params,
        "query": request.query_params,
        "header": request.headers,
    }
    for kind, source in sources.items():
        for p in node.params:
            if p.kind == kind:
                _check(p, source.get(p.alias, _UNSET), [kind, p.alias], values, errors)
    for p in node.params:
        if p.kind == "body":
            _check(p, body, ["body"], values, errors)
    return values, errors


def _body_allowed(status_code: int) -> bool:
    return not (status_code < _OK_MIN or status_code in (204, 205, 304))


class APIRoute(Route):
    def __init__(  # noqa: PLR0913 - mirrors FastAPI's APIRoute keywords
        self,
        path: str,
        endpoint: Callable[..., Awaitable[Any]],
        *,
        methods: Sequence[str] | None = None,
        status_code: int | None = None,
        response_model: Any = _UNSET,
        dependencies: Sequence[Any] | None = None,
    ) -> None:
        verbs = {m.upper() for m in (methods or ["GET"])}
        super().__init__(
            path, self._serve, methods=list(verbs), name=getattr(endpoint, "__name__", None)
        )
        self.endpoint = endpoint
        self.methods = verbs  # Route adds HEAD to GET; FastAPI's APIRoute does not
        self.status_code = status_code
        names = set(self.param_convertors)
        if not all(isinstance(d, _Depends) for d in (dependencies or [])):
            raise TypeError("weblite: router dependencies must be Depends(...)")
        self._deps = [_build(d.call, names, allow_body=False) for d in (dependencies or [])]
        self._node = _build(endpoint, names, allow_body=True)
        self._node.deps[:0] = [(None, d) for d in self._deps]
        if response_model is _UNSET:
            response_model = typing.get_type_hints(endpoint, include_extras=True).get("return")
            if response_model is type(None) or (
                inspect.isclass(response_model) and issubclass(response_model, Response)
            ):
                response_model = None
        self._adapter: TypeAdapter[Any] | None = None
        if response_model is not None:
            try:
                self._adapter = TypeAdapter(response_model)
            except Exception as exc:
                raise TypeError(f"weblite: bad response_model {response_model!r}: {exc!r}") from exc

    async def _serve(self, request: Request) -> Response:
        try:
            body = (
                await _read_body(request)
                if any(p.kind == "body" for p in self._node.params)
                else _UNSET
            )
        except _BodyError as exc:
            return JSONResponse({"detail": exc.errors}, status_code=422)
        values, errors = await _solve(self._node, request, {}, body)
        if errors:
            return JSONResponse({"detail": errors}, status_code=422)
        result = await self.endpoint(**values)
        if isinstance(result, Response):
            return result
        status = self.status_code or 200
        if self._adapter is not None:
            try:
                value = self._adapter.validate_python(result, from_attributes=True)
            except ValidationError as exc:
                raise ResponseValidationError(str(exc)) from exc
            response: Response = Response(
                self._adapter.dump_json(value, by_alias=True),
                status_code=status,
                media_type="application/json",
            )
        else:
            response = JSONResponse(to_jsonable_python(result, by_alias=True), status_code=status)
        if not _body_allowed(response.status_code):
            response.body = b""
        return response


class _Verbs:
    _dependencies: Sequence[Any] = ()

    def _sink(self) -> list[BaseRoute]:
        raise NotImplementedError

    def _verb(
        self, method: str, path: str, status_code: int | None, response_model: Any
    ) -> Callable[[Any], Any]:
        def register(fn: Any) -> Any:
            self._sink().append(
                APIRoute(
                    path,
                    fn,
                    methods=[method],
                    status_code=status_code,
                    response_model=response_model,
                    dependencies=self._dependencies,
                )
            )
            return fn

        return register

    def get(
        self, path: str, *, status_code: int | None = None, response_model: Any = _UNSET
    ) -> Callable[[Any], Any]:
        return self._verb("GET", path, status_code, response_model)

    def post(
        self, path: str, *, status_code: int | None = None, response_model: Any = _UNSET
    ) -> Callable[[Any], Any]:
        return self._verb("POST", path, status_code, response_model)

    def put(
        self, path: str, *, status_code: int | None = None, response_model: Any = _UNSET
    ) -> Callable[[Any], Any]:
        return self._verb("PUT", path, status_code, response_model)

    def patch(
        self, path: str, *, status_code: int | None = None, response_model: Any = _UNSET
    ) -> Callable[[Any], Any]:
        return self._verb("PATCH", path, status_code, response_model)

    def delete(
        self, path: str, *, status_code: int | None = None, response_model: Any = _UNSET
    ) -> Callable[[Any], Any]:
        return self._verb("DELETE", path, status_code, response_model)


class APIRouter(_Verbs):
    def __init__(self, *, dependencies: Sequence[Any] | None = None) -> None:
        self._dependencies = list(dependencies or [])
        self.routes: list[BaseRoute] = []

    def _sink(self) -> list[BaseRoute]:
        return self.routes


async def _http_exception(_request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    if not _body_allowed(exc.status_code):
        return Response(status_code=exc.status_code, headers=exc.headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


class FastAPI(_Verbs, Starlette):
    def __init__(
        self,
        *,
        title: str = "",  # noqa: ARG002 - accepted and ignored: no docs/openapi
        version: str = "",  # noqa: ARG002 - accepted and ignored
        description: str = "",  # noqa: ARG002 - accepted and ignored
        lifespan: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__(
            lifespan=lifespan, exception_handlers={StarletteHTTPException: _http_exception}
        )

    def _sink(self) -> list[BaseRoute]:
        return self.router.routes

    def include_router(self, router: APIRouter) -> None:
        self.router.routes.extend(router.routes)
