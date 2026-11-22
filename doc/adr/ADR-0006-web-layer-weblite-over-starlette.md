# ADR-0006: Web layer on Starlette (`weblite`), not FastAPI

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-11-05 |
| Deciders | Martin Werner |
| Affects | `src/mcapp/weblite.py`, `ble_service/src/weblite.py`, `src/mcapp/sse_routes/*` |
| Supersedes | the FastAPI web layer (2025 to 2026-11-05) |
| Superseded by | - |

## Context

FastAPI cost about 11.5 MB RSS per process over Starlette, and the Pi Zero 2W has about 144 MB of
usable kernel memory. Routes used only a small part of FastAPI: routing, request bodies, JSON
responses and SSE. The mcapp process and the BLE service each import the web layer.

## Decision

Routes use `src/mcapp/weblite.py`, a small layer on Starlette that implements only what the routes
need. It raises `TypeError` at registration for any unsupported feature, so a route that would fail
on the box fails at startup, in tests. `ble_service/src/weblite.py` is a byte-identical copy, pinned
by `weblite_tests`, because that service cannot import mcapp.

Consequences of the layer's shape:

- A 422 response omits `input`, `ctx` and `url`, because they echo passwords.
- There is no `/docs` and no `/openapi.json`.
- `route_table_tests` builds the real `_create_app()` and asserts that every Node Admin and
  internet-feed route refuses a foreign Host.

## Consequences

- Smaller process (`fastapi` removed from `pyproject.toml`; `starlette`, `uvicorn` and `sse-starlette`
  remain).
- No automatic API documentation. The API is documented in `../reference/architecture-reference.md`.
- Adding a FastAPI feature (dependency injection, OpenAPI) means extending `weblite`, and the copy in
  `ble_service` must follow.

## Alternatives considered

- Keep FastAPI: rejected, the 11.5 MB RSS per process bought nothing the routes used.
- Raw Starlette everywhere: not chosen. `weblite` keeps the same route-decorator style and the
  refusal guard, which raw Starlette does not provide.

## Evidence

- `pyproject.toml` (dependency comment: "Not fastapi: +11.5 MB RSS per process over Starlette")
- `../reference/architecture-reference.md` (section "Web layer")
- Commit `c277047` (2026-11-05) and the campaign `../archive/2026-11-05_2230-ram-and-front-door-campaign.md`
