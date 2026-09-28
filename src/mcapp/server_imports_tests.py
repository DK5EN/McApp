"""Startup regression suite for W2 (backlog B4.2a): guards against `websockets`
and `watchfiles` being pulled in by the production server paths.

Neither service has a WebSocket route (the `websocket_*` names in `main.py`
are internal pub/sub topic names, not ASGI WebSocket handlers) and neither
uses `--reload`, so both imports are dead weight -- `uvicorn[standard]`
pulls both in as extras. `ws="none"` on `uvicorn.Config`/`uvicorn.run` keeps
`Config.load()` from importing `websockets`; depending on bare `uvicorn` plus
explicit `uvloop`/`httptools` instead of the `[standard]` extra is the only
way to stop bare `import uvicorn` from importing `watchfiles`
(`uvicorn/supervisors/__init__.py` imports it unconditionally whenever it is
installed, regardless of `ws=`).

Two cases:

  1. Subprocess check -- a fresh interpreter that imports uvicorn/fastapi and
     builds `uvicorn.Config(FastAPI(), ws="none").load()` must not pull
     `websockets` or `watchfiles` into `sys.modules`. Runs in a SUBPROCESS
     (`sys.executable -c ...`) because `sys.modules` is a process-global
     cache: by the time this suite runs inside `run_startup_tests.py`, some
     earlier suite has almost certainly already imported `uvicorn` (and with
     it `watchfiles`) into THIS interpreter, which would make an in-process
     check pass trivially forever after the first import, everywhere. It
     fails as soon as anything re-adds `watchfiles` to the env (for example
     `uvicorn[standard]` coming back), because `import uvicorn` alone then
     imports it, `ws=` notwithstanding.
  2. In-process check -- the real `SSEManager.start_server()` code path
     (`sse_handler.py`) builds its `uvicorn.Config(...)` call with
     `ws="none"`. `uvicorn.Config`/`uvicorn.Server` are monkeypatched to
     capture the kwargs and avoid actually binding a socket or running a
     server loop; `SSEManager._create_app` is stubbed to a bare `FastAPI()`
     instance to isolate this from full route assembly, which eagerly touches
     the real VAPID keyfile / needs a real storage handler (see
     `storage/uptime_tests.py::_test_route_registered`'s docstring for why
     that concern is kept out of manager-construction-only suites) and is
     already covered by `push_tests.py`/`storage/uptime_tests.py`/
     `sse_format_tests.py`. This drives the actual production call site
     rather than grepping source for `ws="none"`.

Only a source check would be feasible for `ble_service/src/main.py`'s
`uvicorn.run(..., ws="none")` and `bootstrap/templates/mcapp-ble.service`'s
`--ws none` -- the former only runs under `__main__` (never imported by a
test), and the latter is a systemd unit template, not importable Python at
all -- so neither is exercised here; both were reviewed by hand.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI

from .sse_handler import SSEManager

_SUBPROCESS_TIMEOUT_S = 30

_SUBPROCESS_SCRIPT = """
import sys

import uvicorn
from fastapi import FastAPI

config = uvicorn.Config(FastAPI(), ws="none")
config.load()

has_websockets = "websockets" in sys.modules
has_watchfiles = "watchfiles" in sys.modules
print(f"websockets={has_websockets} watchfiles={has_watchfiles}")
sys.exit(1 if (has_websockets or has_watchfiles) else 0)
"""


def _test_no_websocket_or_watchfiles_import(results: list[tuple[str, bool]]) -> None:
    """Case 1: a fresh interpreter loading `Config(ws="none")` must not import
    `websockets` or `watchfiles`. Runs in a subprocess so `sys.modules`
    pollution from other suites in this same process can never mask the
    result (see module docstring).
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, sys.executable, no shell
        [sys.executable, "-c", _SUBPROCESS_SCRIPT],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    ok = proc.returncode == 0
    detail = (proc.stdout.strip() or proc.stderr.strip() or "no output").splitlines()[-1]
    label = f"fresh interpreter: Config(ws='none').load() imports neither package ({detail})"
    results.append((label, ok))


async def _test_production_config_uses_ws_none(results: list[tuple[str, bool]]) -> None:
    """Case 2: the real `SSEManager.start_server()` passes `ws="none"` to its
    `uvicorn.Config(...)` call. `Config`/`Server` are patched to capture
    kwargs and avoid touching a real socket; `_create_app` is stubbed to a
    bare `FastAPI()` to isolate the Config call from full route assembly
    (see module docstring for why that is out of scope here).
    """
    captured: dict[str, Any] = {}

    class _FakeServer:
        """Stand-in for `uvicorn.Server`: no real bind, immediate `serve()`."""

        def __init__(self, config: Any) -> None:
            self.config = config
            self.should_exit = False

        async def serve(self) -> None:
            return None

    def _fake_config(app: Any, **kwargs: Any) -> Any:
        captured.clear()
        captured.update(kwargs)
        return object()

    manager = SSEManager(host="127.0.0.1", port=0, message_router=None)
    # Isolates the Config(...) call under test from full route assembly,
    # which is covered elsewhere -- see module docstring.
    manager._create_app = FastAPI  # type: ignore[method-assign]

    with (
        patch("mcapp.sse_handler.uvicorn.Config", side_effect=_fake_config),
        patch("mcapp.sse_handler.uvicorn.Server", _FakeServer),
    ):
        await manager.start_server()
        try:
            await manager.stop_server()
        finally:
            # Belt-and-braces: stop_server() already awaits (or times out on)
            # _server_task, but a future edit to that method must not leave a
            # dangling task warning in this suite's output.
            task = manager._server_task
            if task is not None and not task.done():
                task.cancel()

    got_ws = captured.get("ws")
    label = f"SSEManager.start_server() passes ws='none' to uvicorn.Config (got {got_ws!r})"
    results.append((label, got_ws == "none"))


async def run_server_imports_tests() -> bool:
    """Run the server-imports regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    _test_no_websocket_or_watchfiles_import(results)
    await _test_production_config_uses_ws_none(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    server_imports: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_server_imports_tests())
