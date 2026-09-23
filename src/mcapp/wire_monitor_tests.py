"""Built-in regression suite for `wire_monitor.py` — the MCProxy half of the
RF Monitor wire contract v1 (webapp `docs/rf-monitor-plan.md`).

Harness mirrors `linkcheck_sse_tests.py`/`sse_format_tests.py`: real
`MessageRouter`/`SSEManager`/`WireMonitor` instances wired together the same
way `build_app` (main.py) wires them, driving the real code — never a
reimplementation of the verdict/capture logic. The REST layer
(`sse_routes/monitor.py`) is driven over a real ASGI request/response cycle
(`httpx.ASGITransport`, the same pattern `stall_http_tests.py` uses) so
`Query(ge=..., le=...)` validation is exercised for real, not bypassed by
calling the endpoint function directly.

Cases (see module sections below):

  1. Ring cap (`RING_MAXLEN`) and `seq` monotonicity.
  2. `page()` before/after/limit/has_more edges.
  3. REST: `before` and `after` together -> 422; `limit` out of [1, 2000] ->
     422; the default limit is honoured; no monitor wired -> 503.
  4. Verdict parity: for linkcheck / command-echo / blocklist-drop /
     blocklist-redirect / pass, the monitor's captured verdict matches what
     `SSEManager._broadcast_handler` actually put on a connected SSE
     client's queue — asserted on the queued wire text, not a return value.
     Includes the blocklist-redirect case's dst-leak guard: the envelope's
     `frame` keeps the ORIGINAL dst, never the SPAM_GROUP rewrite.
  5. RX `dir` mapping (`src_type == "node"` -> tx, else rx) and an
     unrecognized `mesh_message` source being skipped rather than guessed.
  6. `ble_notification` gating: a register/config notification (TYP=SN) is
     not captured, an MHeard-beacon mesh frame (type=pos) IS, a
     `source="self"` synthetic echo is skipped (already captured once by
     the TX path), and an unrecognized source is skipped.
  7. `ble_status` -> a SYS frame, `verdict: shown`.
  8. TX: `sent` / `suppressed(local_command)` / `suppressed(self_message)` /
     `failed` each produce exactly one envelope via the single point in
     `MessageRouter._handle_outbound`; a self-addressed send does NOT also
     produce a second envelope through `ble_notification`.
  9. `capture()` deep-copies immediately: mutating the source dict
     afterwards never changes the stored envelope.
  10. A `capture()` failure (an unpicklable frame) is caught and returns
      `{}` rather than raising, and does not stop a later subscriber on the
      same `publish()` call from running.
"""

from __future__ import annotations

import json
import threading
from itertools import pairwise
from types import SimpleNamespace
from typing import Any, cast

import httpx
from fastapi import FastAPI

from .commands.constants import has_console
from .commands.parsing import SPAM_GROUP
from .main import MessageRouter
from .sse_handler import SSEClient, SSEManager
from .sse_routes.monitor import build_monitor_router
from .wire_monitor import RING_MAXLEN, WireMonitor

RecordFn = Any  # (label: str, ok: bool) -> None, kept untyped like sibling suites


def _parse_sse_event(raw: str) -> dict[str, Any]:
    """Extract the JSON payload of one formatted SSE event string."""
    lines = raw.strip("\n").split("\n")
    data_line = next(line for line in lines if line.startswith("data: "))
    return cast("dict[str, Any]", json.loads(data_line.removeprefix("data: ")))


# ── 1. Ring cap + seq monotonicity ──────────────────────────────────────────


async def _test_ring_cap_and_seq(record: RecordFn) -> None:
    monitor = WireMonitor()
    total = RING_MAXLEN + 25
    for i in range(total):
        await monitor.capture("udp", "rx", "shown", None, {"i": i})

    record("ring never grows past RING_MAXLEN", len(monitor._ring) == RING_MAXLEN)
    seqs = [f["seq"] for f in monitor._ring]
    record(
        "oldest surviving envelope has the expected seq (evicted the rest)",
        seqs[0] == total - RING_MAXLEN + 1,
    )
    record("newest envelope has seq == total captures", seqs[-1] == total)
    record(
        "seq is strictly monotonically increasing by 1 across the ring",
        all(b - a == 1 for a, b in pairwise(seqs)),
    )


# ── 2. page() edges ──────────────────────────────────────────────────────────


async def _test_page_edges(record: RecordFn) -> None:
    monitor = WireMonitor()
    for i in range(10):
        await monitor.capture("udp", "rx", "shown", None, {"i": i})
    # seq 1..10 now in the ring, oldest -> newest.

    page = monitor.page()
    record(
        "no before/after: returns everything, oldest->newest, has_more False",
        [f["seq"] for f in page["frames"]] == list(range(1, 11)) and page["has_more"] is False,
    )

    page = monitor.page(limit=3)
    record(
        "no before/after with limit=3: newest 3, has_more True",
        [f["seq"] for f in page["frames"]] == [8, 9, 10] and page["has_more"] is True,
    )

    page = monitor.page(before=5)
    record(
        "before=5, default limit: everything older, has_more False",
        [f["seq"] for f in page["frames"]] == [1, 2, 3, 4] and page["has_more"] is False,
    )

    page = monitor.page(before=5, limit=2)
    record(
        "before=5, limit=2: the 2 closest-to-5 older frames, has_more True",
        [f["seq"] for f in page["frames"]] == [3, 4] and page["has_more"] is True,
    )

    page = monitor.page(after=5)
    record(
        "after=5, default limit: everything newer, has_more False",
        [f["seq"] for f in page["frames"]] == [6, 7, 8, 9, 10] and page["has_more"] is False,
    )

    page = monitor.page(after=5, limit=3)
    record(
        "after=5, limit=3: the 3 closest-to-5 newer frames, has_more True",
        [f["seq"] for f in page["frames"]] == [6, 7, 8] and page["has_more"] is True,
    )

    page = monitor.page(before=1)
    record(
        "before the oldest seq: empty page, has_more False",
        page["frames"] == [] and page["has_more"] is False,
    )

    record("page() always reports the process boot token", monitor.page()["boot"] == monitor.boot)


# ── 3. REST layer ────────────────────────────────────────────────────────────


def _rest_app(monitor: WireMonitor | None) -> FastAPI:
    app = FastAPI()
    manager = SimpleNamespace(wire_monitor=monitor)
    app.include_router(build_monitor_router(cast("SSEManager", manager)))
    return app


async def _test_rest_layer(record: RecordFn) -> None:
    monitor = WireMonitor()
    for i in range(10):
        await monitor.capture("udp", "rx", "shown", None, {"i": i})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_rest_app(monitor)), base_url="http://t"
    ) as client:
        resp = await client.get("/api/monitor/frames", params={"before": 5, "after": 3})
        record("before and after together -> 422", resp.status_code == 422)

        resp = await client.get("/api/monitor/frames", params={"limit": 0})
        record("limit below 1 -> 422", resp.status_code == 422)

        resp = await client.get("/api/monitor/frames", params={"limit": 2001})
        record("limit above 2000 -> 422", resp.status_code == 422)

        resp = await client.get("/api/monitor/frames")
        body = resp.json()
        record(
            "GET with no params: 200, boot + all 10 seeded frames, has_more False",
            resp.status_code == 200
            and body["boot"] == monitor.boot
            and len(body["frames"]) == 10
            and body["has_more"] is False,
        )

    # Default limit (500) is honoured for real, not just accepted:
    big_monitor = WireMonitor()
    for i in range(RING_MAXLEN - 5):
        await big_monitor.capture("udp", "rx", "shown", None, {"i": i})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_rest_app(big_monitor)), base_url="http://t"
    ) as client:
        resp = await client.get("/api/monitor/frames")
        body = resp.json()
        record(
            "default limit is 500 when omitted",
            resp.status_code == 200 and len(body["frames"]) == 500 and body["has_more"] is True,
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_rest_app(None)), base_url="http://t"
    ) as client:
        resp = await client.get("/api/monitor/frames")
        record("no monitor wired -> 503", resp.status_code == 503)


# ── 4. Verdict parity (monitor vs. what SSE actually broadcasts) ────────────


async def _test_verdict_parity(record: RecordFn) -> None:
    router = MessageRouter(None)
    router.register_protocol("commands", SimpleNamespace(blocked_callsigns={"OE9BLK-1"}))
    sse = SSEManager("127.0.0.1", 0, message_router=router)
    monitor = WireMonitor()
    monitor.router = router
    monitor.sse_manager = sse
    router.subscribe("mesh_message", monitor.on_mesh_message)

    client = SSEClient("wire-monitor-test-client")
    sse.clients[client.client_id] = client

    def _drain() -> list[str]:
        """Drain the client's queue, filtering out the monitor's OWN live
        `wire:frame` broadcast — this test client is also "connected" from
        the monitor's point of view (`monitor.sse_manager = sse` above), so
        every capture lands here too. This suite compares against what a
        NORMAL SSE consumer (`mesh:message` etc.) receives, not the
        `/monitor` stream itself, so `wire:frame` events are filtered out
        rather than turning this off by leaving `sse_manager` unwired.
        """
        items: list[str] = []
        while not client.queue.empty():
            raw = client.queue.get_nowait()
            if not raw.startswith("event: wire:frame\n"):
                items.append(raw)
        return items

    async def _publish(payload: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        _drain()
        await router.publish("udp", "mesh_message", payload)
        return _drain(), monitor._ring[-1]

    queued, env = await _publish(
        {
            "src": "DL2JA-2",
            "dst": "DK5EN-98",
            "msg": "{pong}{451010647}",
            "type": "msg",
            "src_type": "lora",
        }
    )
    record("linkcheck: not broadcast to the SSE client", queued == [])
    record(
        "linkcheck: monitor verdict matches (dropped/linkcheck)",
        env["verdict"] == "dropped" and env["reason"] == "linkcheck",
    )

    queued, env = await _publish({"src": "response", "msg": "--ackinfo on", "type": "response"})
    record("command_echo: not broadcast to the SSE client", queued == [])
    record(
        "command_echo: monitor verdict matches (dropped/command_echo)",
        env["verdict"] == "dropped" and env["reason"] == "command_echo",
    )

    queued, env = await _publish({"src": "OE9BLK-1", "dst": "DK5EN-15", "msg": "hi", "type": "msg"})
    record("blocklist drop (personal traffic): not broadcast to the SSE client", queued == [])
    record(
        "blocklist drop: monitor verdict matches (dropped/blocklist)",
        env["verdict"] == "dropped" and env["reason"] == "blocklist",
    )

    queued, env = await _publish({"src": "OE9BLK-1", "dst": "232", "msg": "spam", "type": "msg"})
    sse_payload = _parse_sse_event(queued[0]) if queued else {}
    record("blocklist redirect (group traffic): broadcast exactly once", len(queued) == 1)
    record(
        "blocklist redirect: the SSE copy has dst rewritten to SPAM_GROUP",
        sse_payload.get("dst") == SPAM_GROUP,
    )
    record(
        "blocklist redirect: monitor verdict matches (redirected/blocklist)",
        env["verdict"] == "redirected" and env["reason"] == "blocklist",
    )
    record(
        "blocklist redirect: envelope's frame keeps the ORIGINAL dst, never the rewrite",
        env["frame"].get("dst") == "232",
    )

    chat = {"src": "OE1ABC-1", "dst": "DK5EN-15", "msg": "hello", "type": "msg"}
    queued, env = await _publish(chat)
    sse_payload = _parse_sse_event(queued[0]) if queued else {}
    record("pass: broadcast exactly once, unchanged", len(queued) == 1 and sse_payload == chat)
    record(
        "pass: monitor verdict matches (shown, no reason), frame unchanged",
        env["verdict"] == "shown" and env["reason"] is None and env["frame"] == chat,
    )


# ── 5. RX dir mapping + unmapped mesh_message source ────────────────────────


async def _test_mesh_message_dir_and_source_mapping(record: RecordFn) -> None:
    router = MessageRouter(None)
    monitor = WireMonitor()
    monitor.router = router
    router.subscribe("mesh_message", monitor.on_mesh_message)

    await router.publish(
        "mystery", "mesh_message", {"type": "msg", "src": "X", "dst": "Y", "msg": "z"}
    )
    record(
        "an unrecognized mesh_message source is skipped, not guessed at", len(monitor._ring) == 0
    )

    await router.publish(
        "udp",
        "mesh_message",
        {"src": "DK5EN-98", "dst": "OE1ABC-1", "msg": "echo", "type": "msg", "src_type": "node"},
    )
    record(
        "a src_type='node' frame (our own echoed transmission) captures dir='tx'",
        monitor._ring[-1]["dir"] == "tx" and monitor._ring[-1]["link"] == "udp",
    )

    await router.publish(
        "udp",
        "mesh_message",
        {"src": "OE1ABC-1", "dst": "DK5EN-98", "msg": "hi", "type": "msg", "src_type": "lora"},
    )
    record("an ordinary lora-origin frame captures dir='rx'", monitor._ring[-1]["dir"] == "rx")


# ── 6. ble_notification gating ──────────────────────────────────────────────


async def _test_ble_notification_gating(record: RecordFn) -> None:
    router = MessageRouter(None)
    monitor = WireMonitor()
    monitor.router = router
    router.subscribe("ble_notification", monitor.on_ble_notification)

    await router.publish("ble", "ble_notification", {"TYP": "SN", "src_type": "BLE", "value": "x"})
    record(
        "a BLE register/config notification (TYP=SN, no 'type' key) is NOT captured",
        len(monitor._ring) == 0,
    )

    await router.publish(
        "ble",
        "ble_notification",
        {"type": "pos", "src_type": "ble", "src": "OE1XYZ-9", "rssi": -90, "snr": 5},
    )
    record(
        "an MHeard-beacon mesh frame (transform_mh's type='pos' shape) IS captured",
        len(monitor._ring) == 1,
    )

    await router.publish(
        "self", "ble_notification", {"type": "msg", "src": "DK5EN", "dst": "DK5EN", "msg": "x"}
    )
    record(
        "a source='self' synthetic echo is skipped (avoids a TX-capture double-count)",
        len(monitor._ring) == 1,
    )

    await router.publish(
        "mystery", "ble_notification", {"type": "msg", "src": "X", "dst": "Y", "msg": "z"}
    )
    record(
        "an unrecognized ble_notification source is skipped, not guessed at",
        len(monitor._ring) == 1,
    )


# ── 7. ble_status -> SYS frame ───────────────────────────────────────────────


async def _test_ble_status_sys_frame(record: RecordFn) -> None:
    router = MessageRouter(None)
    monitor = WireMonitor()
    monitor.router = router
    router.subscribe("ble_status", monitor.on_ble_status)

    await router.publish(
        "ble",
        "ble_status",
        {
            "src_type": "BLE",
            "TYP": "blueZ",
            "command": "reconnecting BLE",
            "result": "info",
            "msg": "Reconnecting to Foo (1/4)",
        },
    )
    record("ble_status produces exactly one envelope", len(monitor._ring) == 1)
    env = monitor._ring[-1]
    record(
        "...shaped as a SYS frame: type=sys, msg passthrough, link=ble, dir=rx, verdict=shown",
        env["frame"].get("type") == "sys"
        and env["frame"].get("msg") == "Reconnecting to Foo (1/4)"
        and env["link"] == "ble"
        and env["dir"] == "rx"
        and env["verdict"] == "shown"
        and env["reason"] is None,
    )

    # A status payload that carries its own `type` must still be a SYS frame:
    # otherwise the webapp would render a connectivity notice as mesh traffic.
    await router.publish("ble", "ble_status", {"type": "msg", "msg": "BLE not connected"})
    record(
        "a status payload with its own `type` key is still captured as type=sys",
        monitor._ring[-1]["frame"].get("type") == "sys",
    )


# ── 8. TX single point ───────────────────────────────────────────────────────


async def _test_tx_capture(record: RecordFn) -> None:
    router = MessageRouter(None)
    router.set_callsign("DK5EN")
    monitor = WireMonitor()
    monitor.router = router
    router.wire_monitor = monitor

    async def _send_ok(_data: dict[str, Any]) -> str | None:
        return None

    async def _send_fail(_data: dict[str, Any]) -> str | None:
        return "boom"

    await router._handle_outbound({"data": {"dst": "OE1ABC-1", "msg": "hello"}}, "udp", _send_ok)
    record(
        "a successful send produces exactly one envelope, verdict 'sent', link 'app', dir 'tx'",
        len(monitor._ring) == 1
        and monitor._ring[-1]["verdict"] == "sent"
        and monitor._ring[-1]["link"] == "app"
        and monitor._ring[-1]["dir"] == "tx",
    )

    await router._handle_outbound({"data": {"dst": "", "msg": "!userinfo"}}, "udp", _send_ok)
    record(
        "a locally-suppressed command produces one envelope, verdict 'suppressed'/'local_command'",
        len(monitor._ring) == 2
        and monitor._ring[-1]["verdict"] == "suppressed"
        and monitor._ring[-1]["reason"] == "local_command",
    )

    await router._handle_outbound(
        {"data": {"dst": "DK5EN", "msg": "note to self"}}, "udp", _send_ok
    )
    record(
        "a self-addressed message produces one envelope, verdict 'suppressed'/'self_message'",
        len(monitor._ring) == 3
        and monitor._ring[-1]["verdict"] == "suppressed"
        and monitor._ring[-1]["reason"] == "self_message",
    )

    await router._handle_outbound(
        {"data": {"dst": "OE1ABC-1", "msg": "hello again"}}, "udp", _send_fail
    )
    record(
        "a transport failure produces one envelope, verdict 'failed', reason from the transport",
        len(monitor._ring) == 4
        and monitor._ring[-1]["verdict"] == "failed"
        and monitor._ring[-1]["reason"] == "boom",
    )

    # A self-addressed send must not ALSO surface as a second envelope
    # through the ble_notification RX path (on_ble_notification's
    # source="self" skip exists exactly for this).
    router2 = MessageRouter(None)
    router2.set_callsign("DK5EN")
    monitor2 = WireMonitor()
    monitor2.router = router2
    router2.wire_monitor = monitor2
    router2.subscribe("ble_notification", monitor2.on_ble_notification)
    await router2._handle_outbound(
        {"data": {"dst": "DK5EN", "msg": "note to self"}}, "ble", _send_ok
    )
    record(
        "a self-addressed send stays a SINGLE envelope even when the monitor also"
        " subscribes to ble_notification",
        len(monitor2._ring) == 1,
    )


# ── 9. Deep-copy isolation ───────────────────────────────────────────────────


async def _test_capture_deepcopy_isolation(record: RecordFn) -> None:
    monitor = WireMonitor()
    frame: dict[str, Any] = {"msg": "original", "nested": {"a": 1}}
    envelope = await monitor.capture("udp", "rx", "shown", None, frame)
    frame["msg"] = "MUTATED"
    frame["nested"]["a"] = 999
    record(
        "mutating the source dict after capture() leaves the stored envelope untouched",
        envelope["frame"]["msg"] == "original" and envelope["frame"]["nested"]["a"] == 1,
    )


# ── 10. capture() never raises into the publish path ────────────────────────


async def _test_capture_failure_isolated(record: RecordFn) -> None:
    monitor = WireMonitor()
    unpicklable = {"lock": threading.Lock()}
    envelope = await monitor.capture("udp", "rx", "shown", None, unpicklable)
    record(
        "a capture() failure (deepcopy of an unpicklable frame) is caught, returns {}",
        envelope == {},
    )

    router = MessageRouter(None)
    monitor.router = router
    other_calls: list[dict[str, Any]] = []

    async def _other_subscriber(routed_message: dict[str, Any]) -> None:
        other_calls.append(routed_message)

    router.subscribe("mesh_message", monitor.on_mesh_message)
    router.subscribe("mesh_message", _other_subscriber)
    await router.publish("udp", "mesh_message", {"lock": threading.Lock(), "type": "msg"})
    record(
        "a capture() failure during publish() does not stop the next subscriber from running",
        len(other_calls) == 1,
    )


async def run_wire_monitor_tests() -> bool:
    """Return True iff every WireMonitor regression case passes."""
    if has_console:
        print("\n🧪 Testing WireMonitor (RF Monitor wire contract):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    await _test_ring_cap_and_seq(_record)
    await _test_page_edges(_record)
    await _test_rest_layer(_record)
    await _test_verdict_parity(_record)
    await _test_mesh_message_dir_and_source_mapping(_record)
    await _test_ble_notification_gating(_record)
    await _test_ble_status_sys_frame(_record)
    await _test_tx_capture(_record)
    await _test_capture_deepcopy_isolation(_record)
    await _test_capture_failure_isolated(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 WireMonitor Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    import asyncio

    raise SystemExit(0 if asyncio.run(run_wire_monitor_tests()) else 1)
