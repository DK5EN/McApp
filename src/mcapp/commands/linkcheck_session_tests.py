"""Built-in test suite for LinkCheckMixin (src/mcapp/commands/linkcheck.py).

Standalone, no pytest — mirrors the house pattern in `dedup_tests.py`: a
`results: list[tuple[str, bool]]`, `PASS | label` / `FAIL | label` lines, a
`linkcheck: PASS/FAIL` summary, and `return all(...)`.

No real sockets, no DB, no sleeping for real timeouts: `_FakeRouter` stands in
for `MessageRouter` (records `udp_message` sends and `linkcheck_event`
publishes, and can be told to raise `OSError` for a given target to simulate
`udp_handler.send_message()` failing), and every test that needs the driver
loop to actually time out sets `handler.linkcheck_timeout` to a few
hundredths of a second first (mirrors `commands/tests.py`'s
`_test_real_ping_timeout`, which shrinks `handler.ping_timeout` to 0.05).

Run headless:
    uv run python -c "import asyncio, sys; \
from mcapp.commands.linkcheck_session_tests import run_linkcheck_session_tests; \
sys.exit(0 if asyncio.run(run_linkcheck_session_tests()) else 1)"
"""

import asyncio
from typing import Any

from .linkcheck import LinkCheckMixin

# Real captured values, ADR §1.5 run 1: DK5EN-98 -> DL2JA-2, direct (no via
# path), hex echo msg_id 1AE1E057 == decimal pong token 451010647.
_REAL_ECHO_MSG_ID = "1AE1E057"
_REAL_PONG_TOKEN = 451010647
_REAL_RSSI = -117
_REAL_SNR = -7.0

# A real negative pong id from historical traffic (ADR §1.3b / §1.5.1):
# DB0HOB-12's SendPong() formats with signed %i. int("E68641B7", 16) ==
# (-427408969) & 0xFFFFFFFF == 3867558327, so this hex/decimal pair
# correlates under `linkcheck.normalise_id()` exactly like the positive case.
_NEGATIVE_ECHO_MSG_ID = "E68641B7"
_NEGATIVE_PONG_TOKEN = -427408969


class _FakeRouter:
    """Stand-in for MessageRouter: records sends, can simulate OSError."""

    def __init__(self) -> None:
        self.udp_sent: list[dict[str, Any]] = []
        self.ble_sent: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.fail_targets: set[str] = set()
        self.ble_client: Any = None

    def get_protocol(self, name: str) -> Any:
        return self.ble_client if name == "ble_client" else None

    async def publish(self, _source: str, topic: str, data: dict[str, Any]) -> None:
        if topic == "udp_message":
            if data.get("dst") in self.fail_targets:
                raise OSError("simulated send failure")
            self.udp_sent.append(data)
        elif topic == "ble_message":
            self.ble_sent.append(data)
        elif topic == "linkcheck_event":
            self.events.append(data)

    def results_for(self, target: str | None = None) -> list[dict[str, Any]]:
        evs = [e for e in self.events if e["event"] == "linkcheck_result"]
        if target is not None:
            evs = [e for e in evs if e["target"] == target]
        return evs

    def done_for(self, target: str) -> dict[str, Any] | None:
        for e in reversed(self.events):
            if e["event"] == "linkcheck_done" and e["target"] == target:
                return e
        return None


class _Harness(LinkCheckMixin):
    """Minimal concrete LinkCheckMixin instance with a fake router."""

    def __init__(self, my_callsign: str = "DK5EN-98") -> None:
        self.my_callsign = my_callsign
        self.blocked_callsigns: set[str] = set()
        self.message_router = _FakeRouter()
        self._init_linkcheck()


def _make_harness(my_callsign: str = "DK5EN-98") -> _Harness:
    # CommandHandlerBase is a Protocol with dozens of cross-mixin method
    # stubs LinkCheckMixin doesn't implement (they belong to other mixins);
    # _Harness is a deliberate partial test double, same pattern as
    # dedup_tests.py's _DedupTestHarness.
    return _Harness(my_callsign)  # type: ignore[abstract]  # partial test double for CommandHandler mixins


def _echo(dst: str, msg_id: str, my_callsign: str = "DK5EN-98") -> dict[str, Any]:
    # On air the ACK suffix is unterminated ("{ping}{087"); the exact suffix
    # digits don't matter to the parser, only the "{ping}" prefix does.
    return {
        "type": "msg",
        "src": my_callsign,
        "dst": dst,
        "msg": "{ping}{087",
        "msg_id": msg_id,
        "src_type": "node",
        "rssi": 0,
        "snr": 0.0,
    }


def _pong(  # noqa: PLR0913 - test fixture builder, all but src/token are kw-only
    src: str,
    token: int,
    *,
    dst: str = "DK5EN-98",
    rssi: int = -117,
    snr: float = -7.0,
    msg_id: str = "E9FB720A",
    src_type: str = "lora",
) -> dict[str, Any]:
    return {
        "type": "msg",
        "src": src,
        "dst": dst,
        "msg": f"{{pong}}{{{token}}}",
        "msg_id": msg_id,
        "src_type": src_type,
        "rssi": rssi,
        "snr": snr,
    }


async def _await_driver(handler: _Harness, target: str, max_wait: float = 2.0) -> None:
    """Wait for a target's driver task to finish, if it still exists."""
    session = handler.link_sessions.get(target)
    task = session.driver_task if session else None
    if task is not None and not task.done():
        await asyncio.wait_for(task, timeout=max_wait)


async def _test_happy_path() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0  # generous; the pong arrives immediately below

    ok, _msg = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("happy path: start accepted", ok))

    # Let the driver task run up to (and suspend inside) its wait for a pong.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(
        _pong("DL2JA-2", _REAL_PONG_TOKEN, rssi=_REAL_RSSI, snr=_REAL_SNR)
    )
    await _await_driver(h, "DL2JA-2")

    results = h.message_router.results_for("DL2JA-2")
    out.append(("happy path: exactly one result event", len(results) == 1))
    if results:
        r = results[0]
        out.append(("happy path: rssi matches captured value", r["rssi"] == _REAL_RSSI))
        out.append(("happy path: snr matches captured value", r["snr"] == _REAL_SNR))
        out.append(("happy path: hops == 0 (no via path)", r["hops"] == 0))
        out.append(("happy path: not late", r["late"] is False))
        out.append(("happy path: response_ms is set", isinstance(r["response_ms"], int)))

    done = h.message_router.done_for("DL2JA-2")
    out.append(("happy path: done event fired", done is not None))
    if done is not None:
        out.append(("happy path: done status == completed", done["status"] == "completed"))
        out.append(("happy path: done sent == 1", done["sent"] == 1))
        out.append(("happy path: done received == 1", done["received"] == 1))

    out.append(("happy path: session released after completion", "DL2JA-2" not in h.link_sessions))
    return out


async def _test_negative_pong_id() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    ok, _msg = await h.start_link_check("DB0HOB-12", 1, "DK5EN-98")
    out.append(("negative id: start accepted", ok))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DB0HOB-12", _NEGATIVE_ECHO_MSG_ID))
    await h.handle_link_check_frame(_pong("DB0HOB-12", _NEGATIVE_PONG_TOKEN))
    await _await_driver(h, "DB0HOB-12")

    results = h.message_router.results_for("DB0HOB-12")
    out.append(("negative id: correlates correctly (one result)", len(results) == 1))
    return out


async def _test_echo_never_arrives() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05

    ok, _msg = await h.start_link_check("DL9ECH", 1, "DK5EN-98")
    out.append(("no echo: start accepted", ok))
    await _await_driver(h, "DL9ECH", max_wait=2.0)

    done = h.message_router.done_for("DL9ECH")
    out.append(("no echo: session still terminates (done event)", done is not None))
    if done is not None:
        out.append(("no echo: done status == timeout", done["status"] == "timeout"))
        out.append(("no echo: received == 0", done["received"] == 0))
    out.append(("no echo: session released", "DL9ECH" not in h.link_sessions))
    return out


async def _test_duplicate_pong() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    pong = _pong("DL2JA-2", _REAL_PONG_TOKEN)
    await h.handle_link_check_frame(pong)
    await h.handle_link_check_frame(pong)  # duplicate
    await h.handle_link_check_frame(pong)  # duplicate again
    await _await_driver(h, "DL2JA-2")

    results = h.message_router.results_for("DL2JA-2")
    out.append(("duplicate pong: exactly one result despite 3 pongs", len(results) == 1))
    return out


async def _test_unknown_pong_id() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05

    await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    try:
        await h.handle_link_check_frame(_pong("DL2JA-2", 999999999))  # unrelated id
        no_crash = True
    except Exception:
        no_crash = False
    out.append(("unknown pong id: does not raise", no_crash))

    results = h.message_router.results_for("DL2JA-2")
    out.append(("unknown pong id: ignored, no result recorded", len(results) == 0))

    await _await_driver(h, "DL2JA-2", max_wait=2.0)
    done = h.message_router.done_for("DL2JA-2")
    out.append(("unknown pong id: real attempt still times out normally", done is not None))
    if done is not None:
        out.append(("unknown pong id: status == timeout", done["status"] == "timeout"))
    return out


async def _test_concurrent_sessions_no_cross_correlation() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05

    ok_a, _ = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    ok_b, _ = await h.start_link_check("OE3ABC-7", 1, "DK5EN-98")
    out.append(("concurrent: both sessions start", ok_a and ok_b))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Only resolve A; B never gets an echo or a pong.
    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(_pong("DL2JA-2", _REAL_PONG_TOKEN))

    await _await_driver(h, "DL2JA-2")
    await _await_driver(h, "OE3ABC-7", max_wait=2.0)

    a_results = h.message_router.results_for("DL2JA-2")
    b_results = h.message_router.results_for("OE3ABC-7")
    out.append(("concurrent: A resolved exactly once", len(a_results) == 1))
    out.append(("concurrent: B got no result (no cross-correlation)", len(b_results) == 0))

    a_done = h.message_router.done_for("DL2JA-2")
    b_done = h.message_router.done_for("OE3ABC-7")
    out.append(("concurrent: A completed", a_done is not None and a_done["status"] == "completed"))
    out.append(
        ("concurrent: B timed out untouched", b_done is not None and b_done["status"] == "timeout")
    )
    return out


async def _test_second_session_same_target_rejected() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    ok1, _ = await h.start_link_check("DL2JA-2", 2, "DK5EN-98")
    ok2, msg2 = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("dup target: first start accepted", ok1))
    out.append(("dup target: second start rejected", not ok2))
    out.append(("dup target: rejection message non-empty", bool(msg2)))

    await h.stop_link_check("DL2JA-2")
    return out


async def _test_count_not_clamped() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()

    ok, _msg = await h.start_link_check("DL2JA-2", 500, "DK5EN-98")
    out.append(("count 500: rejected, not clamped", not ok))
    out.append(("count 500: no session created", "DL2JA-2" not in h.link_sessions))
    return out


async def _test_self_ping_rejected() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness(my_callsign="DK5EN-98")

    ok, msg = await h.start_link_check("DK5EN-98", 1, "DK5EN-98")
    out.append(("self-ping: rejected", not ok))
    out.append(("self-ping: message explains why", "own" in msg.lower() or "self" in msg.lower()))
    return out


async def _test_blocked_callsign_rejected() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.blocked_callsigns.add("DL3BLK")

    ok, _msg = await h.start_link_check("DL3BLK", 1, "DK5EN-98")
    out.append(("blocked callsign: rejected", not ok))
    return out


async def _test_oserror_releases_target() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0
    h.message_router.fail_targets.add("DL2JA-2")

    ok, _msg = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("OSError: start accepted (fails inside driver)", ok))

    await _await_driver(h, "DL2JA-2", max_wait=2.0)

    out.append(("OSError: target released from link_sessions", "DL2JA-2" not in h.link_sessions))
    done = h.message_router.done_for("DL2JA-2")
    out.append(
        ("OSError: done event with status == error", done is not None and done["status"] == "error")
    )
    return out


async def _test_stop_mid_session_allows_restart() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0  # long enough that stop() interrupts a real wait

    ok, _msg = await h.start_link_check("DL2JA-2", 3, "DK5EN-98")
    out.append(("stop mid-session: start accepted", ok))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    stopped = await h.stop_link_check("DL2JA-2")
    out.append(("stop mid-session: stop() returns True", stopped))
    out.append(("stop mid-session: target released immediately", "DL2JA-2" not in h.link_sessions))

    remaining_tasks = [t for t in h._linkcheck_bg_tasks if not t.done()]
    out.append(("stop mid-session: no pending task left", len(remaining_tasks) == 0))

    ok2, msg2 = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append((f"stop mid-session: immediate restart allowed ({msg2})", ok2))

    await h.stop_link_check("DL2JA-2")
    return out


async def _test_relayed_pong_reports_hops() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    await h.start_link_check("DB0HOB-12", 1, "DK5EN-98")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DB0HOB-12", _NEGATIVE_ECHO_MSG_ID))
    # Relayed: src carries the originator followed by the relay that we
    # actually heard on RF (ADR §1.5.6) — "DB0HOB-12,DB0ED-99" means one hop.
    relayed_pong = _pong("DB0HOB-12,DB0ED-99", _NEGATIVE_PONG_TOKEN, rssi=-90, snr=2.0)
    await h.handle_link_check_frame(relayed_pong)
    await _await_driver(h, "DB0HOB-12")

    results = h.message_router.results_for("DB0HOB-12")
    out.append(("relayed pong: one result", len(results) == 1))
    if results:
        out.append(("relayed pong: hops > 0", results[0]["hops"] == 1))
    return out


async def _test_udp_pong_does_not_resolve() -> list[tuple[str, bool]]:
    """An internet-path (`src_type:"udp"`) pong must never count as RF success.

    ADR §1.5.1: real signal only on `src_type:"lora"`; `udp`/`node` carry the
    `0/0` sentinel. Live evidence 2026-08-17 (mcapp.local): internet-connected
    stations answered in 2-3 s via `src_type:"udp"` before this gate existed,
    and the session wrongly resolved as RF success with sentinel rssi/snr.
    """
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05

    ok, _msg = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("udp pong: start accepted", ok))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(
        _pong("DL2JA-2", _REAL_PONG_TOKEN, src_type="udp", rssi=0, snr=0.0)
    )

    session = h.link_sessions.get("DL2JA-2")
    attempt = session.attempts[-1] if session and session.attempts else None
    out.append(("udp pong: attempt not resolved", attempt is not None and not attempt.resolved))
    out.append(("udp pong: no result event yet", len(h.message_router.results_for("DL2JA-2")) == 0))
    if attempt is not None:
        out.append(("udp pong: rssi stays None", attempt.rssi is None))
        out.append(("udp pong: snr stays None", attempt.snr is None))

    await _await_driver(h, "DL2JA-2", max_wait=2.0)

    out.append(
        (
            "udp pong: still no result event after timeout",
            len(h.message_router.results_for("DL2JA-2")) == 0,
        )
    )
    if attempt is not None:
        out.append(("udp pong: attempt timed_out after driver finishes", attempt.timed_out))
        out.append(("udp pong: attempt internet_reply True", attempt.internet_reply))

    done = h.message_router.done_for("DL2JA-2")
    out.append(
        ("udp pong: done status == timeout", done is not None and done["status"] == "timeout")
    )

    timeout_events = [
        e
        for e in h.message_router.events
        if e["event"] == "linkcheck_timeout" and e["target"] == "DL2JA-2"
    ]
    out.append(("udp pong: exactly one timeout event", len(timeout_events) == 1))
    if timeout_events:
        out.append(
            (
                "udp pong: timeout event carries internet_reply=True",
                timeout_events[0]["internet_reply"] is True,
            )
        )
    return out


async def _test_udp_pong_then_lora_pong_resolves_with_rf_signal() -> list[tuple[str, bool]]:
    """A later `lora` pong for the same attempt still resolves with real RF signal."""
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0

    ok, _msg = await h.start_link_check("DL2JA-2", 1, "DK5EN-98")
    out.append(("udp then lora: start accepted", ok))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(
        _pong("DL2JA-2", _REAL_PONG_TOKEN, src_type="udp", rssi=0, snr=0.0)
    )
    out.append(
        (
            "udp then lora: no result event after udp pong",
            len(h.message_router.results_for("DL2JA-2")) == 0,
        )
    )

    await h.handle_link_check_frame(
        _pong("DL2JA-2", _REAL_PONG_TOKEN, rssi=_REAL_RSSI, snr=_REAL_SNR)
    )
    await _await_driver(h, "DL2JA-2")

    results = h.message_router.results_for("DL2JA-2")
    out.append(("udp then lora: exactly one result event", len(results) == 1))
    if results:
        r = results[0]
        out.append(("udp then lora: rssi from lora frame", r["rssi"] == _REAL_RSSI))
        out.append(("udp then lora: snr from lora frame", r["snr"] == _REAL_SNR))
        out.append(("udp then lora: hops == 0", r["hops"] == 0))
        out.append(
            ("udp then lora: internet_reply True in result event", r["internet_reply"] is True)
        )

    done = h.message_router.done_for("DL2JA-2")
    out.append(
        (
            "udp then lora: done status == completed",
            done is not None and done["status"] == "completed",
        )
    )
    return out


# DM3KS-12 field capture, 2026-09-24: its Extern-UDP pointed at a WebDesk PC,
# so McApp never saw the "node" echo of ping x2EFB0228 and got the answer
# `DM3KS-13>DM3KS-12:{pong}{788202024}` over BLE only. 788202024 == 0x2EFB0228.
# The I-register ID is any value whose low 22 bits name that node (0x0BBEC0).
_DM3KS_GW_ID = 0xF6CBBEC0
_DM3KS_PONG_TOKEN = 788202024
_DK5EN_GW_ID = 0x0406B878  # --info "...ID 0406B878"; echo ids 1AE1E0xx


def _ble_pong(  # noqa: PLR0913 - test fixture builder, all but src/token are kw-only
    src: str,
    token: int,
    *,
    dst: str = "DK5EN-98",
    via: str | None = None,
    msg_server: bool = False,
    msg_id: str = "0D1F90CC",
    src_type: str = "ble_remote",
) -> dict[str, Any]:
    """What reaches the handler over BLE: no rssi/snr, path in `via`, and
    `src_type` restamped "ble_remote" by `ble_client_remote` (live value)."""
    return {
        "type": "msg",
        "src": src,
        "dst": dst,
        "msg": f"{{pong}}{{{token}}}",
        "msg_id": msg_id,
        "src_type": src_type,
        "via": via if via is not None else src,
        "msg_server": msg_server,
    }


def _i_register(gw_id: int, call: str = "DK5EN-98") -> dict[str, Any]:
    return {"TYP": "I", "ID": gw_id, "CALL": call, "src_type": "BLE"}


async def _start(h: _Harness, target: str, count: int = 1) -> bool:
    ok, _msg = await h.start_link_check(target, count, h.my_callsign)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return ok


async def _test_ble_only_pong_resolves_via_node_id() -> list[tuple[str, bool]]:
    """The DM3KS-12 regression: no echo, pong over BLE only -> completed."""
    out: list[tuple[str, bool]] = []
    h = _make_harness("DM3KS-12")
    h.linkcheck_timeout = 2.0
    h.linkcheck_signal_grace = 0.01
    h.note_linkcheck_node_register(_i_register(_DM3KS_GW_ID, "DM3KS-12"))

    out.append(("ble-only: start accepted", await _start(h, "DM3KS-13")))
    await h.handle_link_check_frame(_ble_pong("DM3KS-13", _DM3KS_PONG_TOKEN, dst="DM3KS-12"))
    await _await_driver(h, "DM3KS-13")

    results = h.message_router.results_for("DM3KS-13")
    out.append(("ble-only: exactly one result event", len(results) == 1))
    if results:
        r = results[0]
        out.append(("ble-only: hops == 0", r["hops"] == 0))
        out.append(("ble-only: no rssi/snr (BLE has none)", r["rssi"] is None and r["snr"] is None))
        out.append(("ble-only: not an internet reply", r["internet_reply"] is False))
    done = h.message_router.done_for("DM3KS-13")
    out.append(("ble-only: completed", done is not None and done["status"] == "completed"))
    return out


async def _test_ble_pong_with_echo_resolves() -> list[tuple[str, bool]]:
    """With the echo known, a BLE copy resolves by exact id like a lora one."""
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 2.0
    h.linkcheck_signal_grace = 0.01

    out.append(("ble+echo: start accepted", await _start(h, "DL2JA-2")))
    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(_ble_pong("DL2JA-2", _REAL_PONG_TOKEN))
    session = h.link_sessions.get("DL2JA-2")
    attempt = session.attempts[-1] if session and session.attempts else None
    out.append(("ble+echo: resolved", attempt is not None and attempt.resolved))
    out.append(
        ("ble+echo: correlation echo", attempt is not None and attempt.correlation == "echo")
    )
    await _await_driver(h, "DL2JA-2")
    done = h.message_router.done_for("DL2JA-2")
    out.append(("ble+echo: completed", done is not None and done["status"] == "completed"))
    return out


async def _test_ble_then_lora_adds_signal() -> list[tuple[str, bool]]:
    """BLE first (no signal), the Extern-UDP lora copy inside the grace window
    adds RSSI/SNR and ends the wait at once."""
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 5.0
    h.linkcheck_signal_grace = 3.0

    out.append(("ble then lora: start accepted", await _start(h, "DL2JA-2")))
    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(_ble_pong("DL2JA-2", _REAL_PONG_TOKEN))
    first = list(h.message_router.results_for("DL2JA-2"))
    out.append(("ble then lora: BLE copy reported at once", len(first) == 1))
    out.append(("ble then lora: still waiting inside grace", "DL2JA-2" in h.link_sessions))

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await h.handle_link_check_frame(
        _pong("DL2JA-2", _REAL_PONG_TOKEN, rssi=_REAL_RSSI, snr=_REAL_SNR)
    )
    await _await_driver(h, "DL2JA-2", max_wait=2.0)
    out.append(("ble then lora: lora copy ends the grace early", loop.time() - t0 < 1.0))

    results = h.message_router.results_for("DL2JA-2")
    out.append(("ble then lora: second result event", len(results) == 2))
    if len(results) == 2:
        out.append(("ble then lora: same seq", results[0]["seq"] == results[1]["seq"]))
        out.append(("ble then lora: rssi filled in", results[1]["rssi"] == _REAL_RSSI))
        out.append(("ble then lora: snr filled in", results[1]["snr"] == _REAL_SNR))
    done = h.message_router.done_for("DL2JA-2")
    out.append(("ble then lora: received counted once", done is not None and done["received"] == 1))
    return out


async def _test_lora_then_ble_is_duplicate() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 2.0

    out.append(("lora then ble: start accepted", await _start(h, "DL2JA-2")))
    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    await h.handle_link_check_frame(
        _pong("DL2JA-2", _REAL_PONG_TOKEN, rssi=_REAL_RSSI, snr=_REAL_SNR)
    )
    await h.handle_link_check_frame(_ble_pong("DL2JA-2", _REAL_PONG_TOKEN))
    await _await_driver(h, "DL2JA-2")
    results = h.message_router.results_for("DL2JA-2")
    out.append(("lora then ble: one result only", len(results) == 1))
    out.append(("lora then ble: signal kept", bool(results) and results[0]["rssi"] == _REAL_RSSI))
    return out


async def _test_ble_server_pong_is_internet_reply() -> list[tuple[str, bool]]:
    """A BLE copy carrying the server flag is the internet path, not RF."""
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 0.05
    h.note_linkcheck_node_register(_i_register(_DK5EN_GW_ID))

    out.append(("ble server: start accepted", await _start(h, "DL2JA-2")))
    await h.handle_link_check_frame(_ble_pong("DL2JA-2", _REAL_PONG_TOKEN, msg_server=True))
    session = h.link_sessions.get("DL2JA-2")
    attempt = session.attempts[-1] if session and session.attempts else None
    out.append(("ble server: not resolved", attempt is not None and not attempt.resolved))
    out.append(("ble server: internet_reply", attempt is not None and attempt.internet_reply))
    await _await_driver(h, "DL2JA-2")
    done = h.message_router.done_for("DL2JA-2")
    out.append(("ble server: timeout", done is not None and done["status"] == "timeout"))
    return out


async def _test_node_id_needs_our_prefix_and_target() -> list[tuple[str, bool]]:
    """No echo: a pong minted by ANOTHER node, one from a station we are not
    pinging, and any pong before the I register is known are all ignored."""
    out: list[tuple[str, bool]] = []
    h = _make_harness("DM3KS-12")
    h.linkcheck_timeout = 0.1

    out.append(("node id guards: start accepted", await _start(h, "DM3KS-13")))
    # Prefix still unknown: even the right pong cannot be attributed.
    await h.handle_link_check_frame(_ble_pong("DM3KS-13", _DM3KS_PONG_TOKEN, dst="DM3KS-12"))
    out.append(("node id guards: unknown prefix -> no result", not h.message_router.results_for()))

    h.note_linkcheck_node_register(_i_register(_DK5EN_GW_ID, "DM3KS-12"))  # wrong node
    await h.handle_link_check_frame(_ble_pong("DM3KS-13", _DM3KS_PONG_TOKEN, dst="DM3KS-12"))
    out.append(("node id guards: foreign prefix -> no result", not h.message_router.results_for()))

    h.note_linkcheck_node_register(_i_register(_DM3KS_GW_ID, "DM3KS-12"))
    await h.handle_link_check_frame(_ble_pong("DL9XX-1", _DM3KS_PONG_TOKEN, dst="DM3KS-12"))
    out.append(("node id guards: other origin -> no result", not h.message_router.results_for()))

    await _await_driver(h, "DM3KS-13")
    done = h.message_router.done_for("DM3KS-13")
    out.append(("node id guards: timeout", done is not None and done["status"] == "timeout"))
    return out


async def _test_node_id_token_never_counts_twice() -> list[tuple[str, bool]]:
    """No echo, two attempts: the same pong token arriving again (a copy, or
    an answer to the firmware's retransmission of ping 1) must not resolve
    attempt 2, nor a later session."""
    out: list[tuple[str, bool]] = []
    h = _make_harness("DM3KS-12")
    h.linkcheck_timeout = 0.3
    h.linkcheck_signal_grace = 0.01
    h.note_linkcheck_node_register(_i_register(_DM3KS_GW_ID, "DM3KS-12"))

    out.append(("token reuse: start accepted", await _start(h, "DM3KS-13", count=2)))
    pong = _ble_pong("DM3KS-13", _DM3KS_PONG_TOKEN, dst="DM3KS-12")
    await h.handle_link_check_frame(pong)

    for _ in range(100):  # wait for attempt 2 to be sent
        session = h.link_sessions.get("DM3KS-13")
        if session is not None and len(session.attempts) == 2:
            break
        await asyncio.sleep(0.01)
    await h.handle_link_check_frame(pong)
    await _await_driver(h, "DM3KS-13")
    done = h.message_router.done_for("DM3KS-13")
    out.append(("token reuse: only attempt 1 counted", done is not None and done["received"] == 1))

    h._linkcheck_cooldown_until.clear()
    out.append(("token reuse: second session accepted", await _start(h, "DM3KS-13")))
    await h.handle_link_check_frame(pong)
    session = h.link_sessions.get("DM3KS-13")
    attempt = session.attempts[-1] if session and session.attempts else None
    out.append(
        (
            "token reuse: old token ignored by next session",
            attempt is not None and not attempt.resolved,
        )
    )
    await _await_driver(h, "DM3KS-13")
    return out


async def _test_echo_teaches_node_prefix() -> list[tuple[str, bool]]:
    h = _make_harness()
    await h.handle_link_check_frame(_echo("DL2JA-2", _REAL_ECHO_MSG_ID))
    return [
        (
            "echo teaches node prefix",
            h._linkcheck_node_prefix == (_DK5EN_GW_ID & 0x3FFFFF),
        )
    ]


async def _test_routing_feeds_i_register() -> list[tuple[str, bool]]:
    """The real `_message_handler` hands our node's BLE `I` register to the
    link check — without that hook the node-id path never has a prefix."""
    from .handler import CommandHandler

    h = CommandHandler(message_router=None, storage_handler=None, my_callsign="DM3KS-12")
    await h._message_handler(
        {"source": "ble", "type": "ble_notification", "data": _i_register(_DM3KS_GW_ID, "DM3KS-12")}
    )
    return [("routing: I register teaches node prefix", h._linkcheck_node_prefix == 0x0BBEC0)]


class _BleClientProperty:
    def __init__(self, connected: bool) -> None:
        self.is_connected = connected


class _BleClientMethod:
    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


async def _test_ping_goes_over_ble_when_connected() -> list[tuple[str, bool]]:
    """With EXTUDP off the node never reads UDP, so a connected BLE client
    must carry the ping; without one it stays on UDP."""
    out: list[tuple[str, bool]] = []
    cases: tuple[tuple[str, Any, str], ...] = (
        ("no ble client", None, "udp"),
        ("ble disconnected", _BleClientProperty(False), "udp"),
        ("ble connected (property)", _BleClientProperty(True), "ble"),
        ("ble connected (method)", _BleClientMethod(True), "ble"),
    )
    for label, client, expected in cases:
        h = _make_harness()
        h.linkcheck_timeout = 0.02
        h.message_router.ble_client = client
        await _start(h, "DL2JA-2")
        await _await_driver(h, "DL2JA-2")
        sent_ble = [m for m in h.message_router.ble_sent if m.get("dst") == "DL2JA-2"]
        sent_udp = [m for m in h.message_router.udp_sent if m.get("dst") == "DL2JA-2"]
        got = "ble" if sent_ble and not sent_udp else "udp" if sent_udp and not sent_ble else "?"
        out.append((f"send transport: {label} -> {expected}", got == expected))
        if sent_ble:
            out.append(
                (f"send transport: {label} payload is the ping", sent_ble[0]["msg"] == "{ping}")
            )
    return out


# DK5EN-98 live capture 2026-09-24 (v2.0.14-dev.2, EXTUDP off): the node's BLE
# copy of our own BLE-sent ping, verbatim from /api/monitor/frames.
_BLE_ECHO_FRAME: dict[str, Any] = {
    "app_offline": False,
    "dest": "DK5EN-1",
    "dst": "DK5EN-1",
    "fcs_ok": True,
    "hw_id": 43,
    "message": ":{ping}{487",
    "msg": "{ping}{487",
    "msg_id": "1AE1E1E7",
    "msg_server": False,
    "path": "DK5EN-98>",
    "src": "DK5EN-98",
    "src_type": "ble_remote",
    "transformer": "msg",
    "type": "msg",
    "via": "",
}


async def _test_ble_echo_teaches_ping_id() -> list[tuple[str, bool]]:
    """With EXTUDP off the only echo is the node's BLE copy of our ping."""
    out: list[tuple[str, bool]] = []
    h = _make_harness()
    h.linkcheck_timeout = 2.0
    h.linkcheck_signal_grace = 0.01

    out.append(("ble echo: start accepted", await _start(h, "DK5EN-1")))
    await h.handle_link_check_frame(dict(_BLE_ECHO_FRAME))
    session = h.link_sessions.get("DK5EN-1")
    attempt = session.attempts[-1] if session and session.attempts else None
    out.append(
        ("ble echo: ping id learned exactly", attempt is not None and attempt.ping_id == 0x1AE1E1E7)
    )
    out.append(
        ("ble echo: node prefix learned", h._linkcheck_node_prefix == (_DK5EN_GW_ID & 0x3FFFFF))
    )
    await h.handle_link_check_frame(_ble_pong("DK5EN-1", 0x1AE1E1E7))
    await _await_driver(h, "DK5EN-1")
    out.append(
        ("ble echo: correlated by echo", attempt is not None and attempt.correlation == "echo")
    )
    done = h.message_router.done_for("DK5EN-1")
    out.append(("ble echo: completed", done is not None and done["status"] == "completed"))
    return out


async def _test_foreign_ble_ping_is_not_an_echo() -> list[tuple[str, bool]]:
    """Someone else pinging over BLE must not set our attempt's id."""
    h = _make_harness()
    h.linkcheck_timeout = 0.05
    await _start(h, "DK5EN-1")
    foreign = dict(_BLE_ECHO_FRAME, src="DL9XX-1", path="DL9XX-1,DK5EN-98>", via="DL9XX-1")
    await h.handle_link_check_frame(foreign)
    session = h.link_sessions.get("DK5EN-1")
    attempt = session.attempts[-1] if session and session.attempts else None
    ok = attempt is not None and attempt.ping_id is None
    await _await_driver(h, "DK5EN-1")
    return [("foreign ble ping: not taken as our echo", ok)]


async def _test_plain_ble_src_type_also_counts() -> list[tuple[str, bool]]:
    """`ble_protocol`'s own stamp ("ble") is accepted like "ble_remote"."""
    h = _make_harness("DM3KS-12")
    h.linkcheck_timeout = 2.0
    h.linkcheck_signal_grace = 0.01
    h.note_linkcheck_node_register(_i_register(_DM3KS_GW_ID, "DM3KS-12"))
    await _start(h, "DM3KS-13")
    await h.handle_link_check_frame(
        _ble_pong("DM3KS-13", _DM3KS_PONG_TOKEN, dst="DM3KS-12", src_type="ble")
    )
    await _await_driver(h, "DM3KS-13")
    done = h.message_router.done_for("DM3KS-13")
    return [("src_type ble: completed", done is not None and done["status"] == "completed")]


async def _collect_all() -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    for test_fn in (
        _test_happy_path,
        _test_negative_pong_id,
        _test_echo_never_arrives,
        _test_duplicate_pong,
        _test_unknown_pong_id,
        _test_concurrent_sessions_no_cross_correlation,
        _test_second_session_same_target_rejected,
        _test_count_not_clamped,
        _test_self_ping_rejected,
        _test_blocked_callsign_rejected,
        _test_oserror_releases_target,
        _test_stop_mid_session_allows_restart,
        _test_relayed_pong_reports_hops,
        _test_udp_pong_does_not_resolve,
        _test_udp_pong_then_lora_pong_resolves_with_rf_signal,
        _test_ble_only_pong_resolves_via_node_id,
        _test_ble_pong_with_echo_resolves,
        _test_ble_then_lora_adds_signal,
        _test_lora_then_ble_is_duplicate,
        _test_ble_server_pong_is_internet_reply,
        _test_node_id_needs_our_prefix_and_target,
        _test_node_id_token_never_counts_twice,
        _test_echo_teaches_node_prefix,
        _test_routing_feeds_i_register,
        _test_ping_goes_over_ble_when_connected,
        _test_ble_echo_teaches_ping_id,
        _test_foreign_ble_ping_is_not_an_echo,
        _test_plain_ble_src_type_also_counts,
    ):
        try:
            results.extend(await test_fn())
        except Exception as e:  # pragma: no cover - defensive, surfaced as a failure
            results.append((f"{test_fn.__name__}: raised {e!r}", False))
    return results


async def run_linkcheck_session_tests() -> bool:
    """Run the LinkCheckMixin test suite. Returns True iff every case passes."""
    results = await _collect_all()

    print("Testing LinkCheck Session Logic:")
    print("=" * 50)
    for label, ok in results:
        print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    overall = all(ok for _, ok in results)
    print(f"linkcheck: {'PASS' if overall else 'FAIL'} ({passed}/{total})")
    return overall


if __name__ == "__main__":
    import sys

    sys.exit(0 if asyncio.run(run_linkcheck_session_tests()) else 1)
