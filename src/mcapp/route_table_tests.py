"""The real route table assembles, and every guarded route carries its guard.

weblite refuses an unsupported route with a TypeError at registration. Without this suite that
would first surface on the box, in `main()`, because route suites build their own routers and
`server_imports_tests` stubs `_create_app`. Node Admin and the internet feed are LAN-only through
a router-level Host/Origin guard, which is a security boundary worth asserting per route.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from starlette.routing import Route
from starlette.testclient import TestClient

from .main import MessageRouter
from .sqlite_storage import create_sqlite_storage
from .sse_handler import SSEManager

# A floor, not an exact count: a router that silently drops out of `_create_app` lowers it.
_MIN_ROUTES = 74
_FORBIDDEN = 403
_UNPROCESSABLE = 422
_GUARDED_PREFIXES = ("/api/node-admin", "/api/internet-feed")


async def run_route_table_tests() -> bool:
    with (
        tempfile.TemporaryDirectory() as raw,
        patch.dict(os.environ, {"MESHCOM_VAPID_PATH": str(Path(raw) / "vapid.json")}),
    ):
        storage = await create_sqlite_storage(str(Path(raw) / "m.db"))
        router = MessageRouter(storage)
        router.set_callsign("DK5EN-98")
        app = SSEManager("127.0.0.1", 0, message_router=router)._create_app()
        routes = [r for r in app.routes if isinstance(r, Route)]
        results = [(f"at least {_MIN_ROUTES} routes assembled", len(routes) >= _MIN_ROUTES)]

        client = TestClient(app, raise_server_exceptions=False)
        guarded = [r for r in routes if r.path.startswith(_GUARDED_PREFIXES)]
        results.append(("guarded routes found", len(guarded) > 0))
        for route in guarded:
            path = route.path.replace("{target}", "DK5EN-1").replace("{log_id}", "1")
            for method in route.methods or ():
                resp = client.request(method, path, json={}, headers={"host": "evil.example"})
                # A declared JSON body is validated first on FastAPI too, so `send` may
                # answer 422; the handler still never runs.
                allowed = {_FORBIDDEN, _UNPROCESSABLE} if path.endswith("/send") else {_FORBIDDEN}
                results.append(
                    (f"{method} {path} refuses a foreign Host", resp.status_code in allowed)
                )
        await storage.close()

    failed = [name for name, ok in results if not ok]
    for name in failed:
        print("FAIL: " + name)
    print(f"route_table: {len(results) - len(failed)}/{len(results)} passed")
    return not failed
