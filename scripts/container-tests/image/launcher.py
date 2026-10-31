"""Spike entrypoint: runtime shims standing in for B12 code changes (no src/mcapp edits).

Change numbers refer to "Required code changes" in
doc/2026-10-31_0830-container-deployment-concept.md.
"""

import importlib
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from mcapp import config_loader, sse_handler, system_converge

# (a) B12 change 1: MCAPP_BIND_HOST replaces SSE_HOST. main.py does
# `from .config_loader import SSE_HOST`, so this must run before mcapp.main is imported.
config_loader.SSE_HOST = os.environ.get("MCAPP_BIND_HOST", config_loader.SSE_HOST)

# (b) B12 change 4: converge watchdog returns early in a container. main.py imports the
# function by name, so patch system_converge before main is imported.


async def _noop_watchdog(_stop_event: Any) -> None:
    return None


system_converge.converge_watchdog = _noop_watchdog  # type: ignore[assignment]  # spike shim

# (c) B12 change 2: static SPA at /webapp (html=True) plus "/" redirect, mounted after all routers.
# Cache headers and SPA fallback to index.html are NOT done here.
_orig_create_app = sse_handler.SSEManager._create_app  # noqa: SLF001 - spike shim


def _create_app_with_webapp(self: Any) -> FastAPI:
    app: FastAPI = _orig_create_app(self)
    webapp_dir = os.environ.get("MCAPP_WEBAPP_DIR")
    if webapp_dir and Path(webapp_dir).is_dir():

        @app.get("/", include_in_schema=False)
        async def _root() -> RedirectResponse:
            return RedirectResponse("/webapp/")

        app.mount("/webapp", StaticFiles(directory=webapp_dir, html=True), name="webapp")
    return app


sse_handler.SSEManager._create_app = _create_app_with_webapp  # type: ignore[method-assign]  # noqa: SLF001 - spike shim

# (d) B12 change 8: /api/timezone is left alone; without timezonefinder it answers 500.

# main must be imported only after every patch above.
main = importlib.import_module("mcapp.main")

if __name__ == "__main__":
    sys.exit(main.run())
