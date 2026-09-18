"""Built-in regression suite for off-loop SSE JSON serialization.

Pins the fix for the stall-tracking finding that `SSEManager.format_sse_event`'s
`json.dumps` ran inline on the event loop for the mheard chart dumps
(7day/monthly/yearly), costing 280-400ms per `loop_lag` sample and stalling the
triggering `POST /api/send` by ~1s (see CLAUDE.md "Stall Tracking"). The fix is
an explicit opt-in (`send_to(..., offload_json=True)`), never a blanket thread
hop, so ordinary small broadcasts stay exactly as fast and exactly as ordered
as before.

Harness mirrors `linkcheck_sse_tests.py`: a real `SSEManager` built with
`message_router=None`, driving the real `send_to`/`format_sse_event` code, not
a reimplementation.
"""

import json
import threading
from types import SimpleNamespace
from typing import Any

from .logging_setup import get_logger
from .main import MessageRouter
from .sse_handler import SSEManager

logger = get_logger(__name__)


def _large_payload() -> dict[str, Any]:
    """A payload shaped like a real mheard chart dump: nested, unicode, large."""
    return {
        "type": "response",
        "msg": "mheard stats",
        "data": {
            "buckets": [{"t": i, "count": i % 7, "rssi": -100 + (i % 30)} for i in range(2000)],
            "note": "Straße Wörld héllo — Zwölf Ölöfen",
            "nested": {"a": [1, 2, {"b": "x", "c": None}], "d": True},
        },
    }


async def _test_offload_byte_identical() -> bool:
    """send_to(..., offload_json=True) queues byte-identical text to the sync path."""
    sse = SSEManager("127.0.0.1", 0, message_router=None)
    client = await sse.register_client("c-identical")
    message = _large_payload()

    ok_sync = await sse.send_to("c-identical", message, offload_json=False)
    text_sync = await client.queue.get()

    ok_offload = await sse.send_to("c-identical", message, offload_json=True)
    text_offload = await client.queue.get()

    return ok_sync and ok_offload and text_sync == text_offload and text_sync.startswith("event:")


async def _test_offload_runs_off_loop() -> bool:
    """offload_json=True runs json.dumps off the loop thread; default runs it on-loop.

    Deterministic: records the calling thread's identity rather than timing.
    """
    sse = SSEManager("127.0.0.1", 0, message_router=None)
    await sse.register_client("c-thread")
    message = {"type": "response", "msg": "mheard stats", "data": {"x": 1}}

    main_thread_id = threading.get_ident()
    recorded: list[int] = []
    original_dumps = json.dumps

    def spy(*args: Any, **kwargs: Any) -> str:
        # Only our payload: another thread (a recorder writer left running by an
        # earlier suite) may call the patched json.dumps in this window.
        if args and args[0] is message:
            recorded.append(threading.get_ident())
        return original_dumps(*args, **kwargs)

    # `json` is a process-wide singleton module, so patching it here also
    # patches the `json.dumps` call inside sse_handler.py's format_sse_event.
    json.dumps = spy  # test seam, restored in finally below
    try:
        recorded.clear()
        await sse.send_to("c-thread", message, offload_json=False)
        on_loop_ident = recorded[-1] if recorded else None

        recorded.clear()
        await sse.send_to("c-thread", message, offload_json=True)
        off_loop_ident = recorded[-1] if recorded else None
    finally:
        json.dumps = original_dumps

    return (
        on_loop_ident == main_thread_id
        and off_loop_ident is not None
        and off_loop_ident != main_thread_id
    )


async def _test_mheard_dump_offloads_only_response() -> bool:
    """_handle_mheard_dump sends its response with offload_json=True and its
    progress updates with the default (False)."""
    router = MessageRouter()

    calls: list[tuple[str, bool]] = []

    async def capture_send_response(
        _websocket: Any,
        payload: dict[str, Any],
        _client_id: str | None = None,
        *,
        offload_json: bool = False,
    ) -> None:
        calls.append((payload.get("type", ""), offload_json))

    router._send_response = capture_send_response  # type: ignore[assignment]  # test seam

    async def fake_dump(progress_callback: Any = None) -> dict[str, Any]:
        if progress_callback is not None:
            await progress_callback("start", "beginning")
            await progress_callback("done", "finished")
        return {"buckets": [1, 2, 3]}

    router.storage_handler = SimpleNamespace(process_mheard_store_parallel=fake_dump)  # type: ignore[assignment]  # test seam

    await router._handle_mheard_dump(None, "client-1", "7day")

    progress_calls = [c for c in calls if c[0] == "progress"]
    response_calls = [c for c in calls if c[0] == "response"]

    return (
        len(progress_calls) == 2
        and all(offload is False for _, offload in progress_calls)
        and len(response_calls) == 1
        and response_calls[0][1] is True
    )


async def run_sse_format_tests() -> bool:
    """Run the off-loop SSE JSON serialization suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    results.append(
        (
            "send_to offload_json=True is byte-identical to the sync path",
            await _test_offload_byte_identical(),
        )
    )
    results.append(
        (
            "offload_json=True runs json.dumps off the loop thread; default runs on it",
            await _test_offload_runs_off_loop(),
        )
    )
    results.append(
        (
            "_handle_mheard_dump offloads only its response, never its progress updates",
            await _test_mheard_dump_offloads_only_response(),
        )
    )

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    sse_format: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
