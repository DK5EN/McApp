"""Regression suite for classifier template overrides (FU1 backend half).

Real SQL (in-memory sqlite) through the real `ClassifierApiMixin` methods and the
real `/api/classifier/templates` router; only the SSE manager is a recorder.

Cases: `user_action_only` returns only overridden rows and ignores `min_count`;
default listing unchanged; PATCH publishes exactly one `proxy:template_override`
(promote and clear) and never `proxy:classifier_template_event`.
"""

from __future__ import annotations

import sqlite3
from typing import Any, cast

import httpx
from fastapi import FastAPI

from .commands.constants import has_console
from .sse_routes.classifier import build_classifier_router
from .storage.classifier_api import ClassifierApiMixin

_BASE = "http://localhost"


class _Storage(ClassifierApiMixin):
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE beacon_templates (template_hash TEXT PRIMARY KEY, example_msg TEXT, "
            "example_src TEXT, srcs TEXT, count INTEGER, first_seen TEXT, last_seen TEXT, "
            "auto_beacon INTEGER DEFAULT 0, user_action TEXT)"
        )
        rows = [
            ("h_big", 50, 0, None),
            ("h_low_promote", 1, 0, "promote"),
            ("h_mid_demote", 4, 1, "demote"),
            ("h_plain", 2, 0, None),
        ]
        for h, c, auto, ua in rows:
            self.db.execute(
                "INSERT INTO beacon_templates VALUES (?, 'm', 's', '[]', ?, 'a', 'b', ?, ?)",
                (h, c, auto, ua),
            )

    async def _query(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(query, params).fetchall()]

    async def _mutate(self, query: str, params: tuple[Any, ...] = ()) -> int:
        return self.db.execute(query, params).rowcount


class _Manager:
    def __init__(self) -> None:
        # The storage base has many abstract members this suite never touches.
        self.storage: _Storage = cast(Any, _Storage)()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def require_storage(self) -> _Storage:
        return self.storage

    async def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


async def run_template_override_tests() -> bool:
    """Return True iff every template override case passes."""
    if has_console:
        print("\n🧪 Testing template overrides:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: Any) -> None:
        results.append((label, bool(ok)))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    mgr = _Manager()
    app = FastAPI()
    app.include_router(build_classifier_router(cast(Any, mgr)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE) as c:
        url = "/api/classifier/templates"
        default = (await c.get(url)).json()
        _record(
            "default listing unchanged (all rows, count DESC)",
            [r["template_hash"] for r in default]
            == ["h_big", "h_mid_demote", "h_plain", "h_low_promote"],
        )
        _record(
            "default min_count still filters",
            {r["template_hash"] for r in (await c.get(url, params={"min_count": 3})).json()}
            == {"h_big", "h_mid_demote"},
        )
        r = await c.get(url, params={"user_action_only": "true", "min_count": 10})
        _record(
            "user_action_only: only overridden rows, min_count ignored",
            r.status_code == 200
            and {x["template_hash"] for x in r.json()} == {"h_low_promote", "h_mid_demote"},
        )

        mgr.events.clear()
        r = await c.patch(f"{url}/h_plain", json={"user_action": "promote"})
        _record(
            "PATCH promote -> one proxy:template_override",
            r.status_code == 200
            and mgr.events
            == [
                ("proxy:template_override", {"template_hash": "h_plain", "user_action": "promote"})
            ],
        )
        mgr.events.clear()
        r = await c.patch(f"{url}/h_plain", json={"user_action": None})
        _record(
            "PATCH clear -> payload user_action None",
            r.status_code == 200
            and mgr.events
            == [("proxy:template_override", {"template_hash": "h_plain", "user_action": None})],
        )
        _record(
            "PATCH never emits proxy:classifier_template_event",
            all(e[0] != "proxy:classifier_template_event" for e in mgr.events),
        )
        mgr.events.clear()
        r = await c.patch(f"{url}/missing", json={"user_action": "promote"})
        _record("PATCH unknown hash -> 404, no event", r.status_code == 404 and not mgr.events)

    passed = sum(1 for _, ok in results if ok)
    if has_console or passed != len(results):
        for label, ok in results:
            if not ok:
                print(f"❌ FAIL | {label}")
        print(f"\n🧪 Template Override Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)
