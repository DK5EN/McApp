"""Startup test suite for the bleak BLE backend (`ble_adapter_bleak.BleakAdapter`).

Wave W5 of doc/2026-10-31_1230-ble-service-macos-backend-design.md (section 7).
Fully offline: bleak is NOT required. The adapter's `scanner_cls` / `client_cls`
constructor seams take the fakes below, so no radio, serial port or network is
touched. Convention matches the other suites: `_test_*` coroutines, a
`run_ble_bleak_tests()` returning a bool, summary printed only with a console.

Mutation notes (verified 2026-10-31 by monkeypatching the method under test to
a broken copy; see `_mutation_check` in the report, not run at startup):
  - `_test_thread_notification_hops_to_loop` fails if `_on_raw_notification`
    drops the `loop.call_soon_threadsafe` branch (calls `_on_notification`
    directly from the foreign thread).
  - `_test_own_disconnect_does_not_fire_callback` fails if
    `_handle_bleak_disconnect` loses its early-return guard line
    (`client is not self._client or state != CONNECTED`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ble_service.src.ble_adapter_bleak import PERMISSION_HELP, BleakAdapter  # noqa: E402 - needs the sys.path bootstrap above
from ble_service.src.ble_backend import create_backend  # noqa: E402 - same
from ble_service.src.ble_types import (  # noqa: E402 - same
    A0_MIN_GAP_S,
    DEVICE_NOT_FOUND_MSG,
    NUS_RX_UUID,
    NUS_TX_UUID,
    BackendUnsupportedError,
    ConnectionState,
)
from mcapp.commands.constants import has_console  # noqa: E402 - same

Record = Callable[[str, bool], None]


class FakeDevice:
    def __init__(self, name: str | None, address: str) -> None:
        self.name = name
        self.address = address


class FakeAdv:
    def __init__(self, rssi: int, local_name: str | None = None) -> None:
        self.rssi = rssi
        self.local_name = local_name


class FakeBleakScanner:
    """Stands in for `bleak.BleakScanner` (used as an object, not instantiated)."""

    def __init__(self, devices: list[tuple[FakeDevice, FakeAdv]]) -> None:
        self.devices = devices

    async def discover(self, timeout: float = 5.0, return_adv: bool = False) -> dict[str, Any]:  # noqa: ASYNC109
        return {d.address: (d, a) for d, a in self.devices}

    async def find_device_by_address(
        self,
        address: str,
        timeout: float = 10.0,  # noqa: ASYNC109 - mirrors bleak
    ) -> FakeDevice | None:
        for d, _ in self.devices:
            if d.address.upper() == address.upper():
                return d
        return None


class FakeBleakClient:
    """Stands in for `bleak.BleakClient`; instances are collected in `instances`."""

    instances: list[FakeBleakClient] = []  # noqa: RUF012 - test fake registry

    def __init__(
        self, device: Any, disconnected_callback: Any = None, timeout: float = 10.0
    ) -> None:
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.is_connected = False
        self.mtu_size = 247
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notify: tuple[str, Callable[[Any, bytearray], None]] | None = None
        self.on_start_notify: Callable[[FakeBleakClient], None] | None = None
        self.on_connect: Callable[[FakeBleakClient], None] | None = None
        self.connect_error: Exception | None = None
        self.write_error: Exception | None = None
        self.write_delay_s = 0.0
        self.disconnect_calls = 0
        FakeBleakClient.instances.append(self)

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.is_connected = True
        if self.on_connect is not None:
            self.on_connect(self)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        self.writes.append((uuid, bytes(data), response))
        if self.write_delay_s:
            await asyncio.sleep(self.write_delay_s)
        if self.write_error is not None:
            raise self.write_error

    async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        if self.on_start_notify is not None:
            self.on_start_notify(self)  # notifications racing start_notify's return
            await asyncio.sleep(0)
        self.notify = (uuid, callback)

    async def stop_notify(self, uuid: str) -> None:
        pass


MAC = "AA:BB:CC:DD:EE:FF"


def _scanner() -> FakeBleakScanner:
    return FakeBleakScanner(
        [
            (FakeDevice("MC-b878-DK5EN-98", "aa:bb:cc:dd:ee:ff"), FakeAdv(-61)),
            (FakeDevice("Headphones", "11:22:33:44:55:66"), FakeAdv(-40)),
            (FakeDevice(None, "77:88:99:AA:BB:CC"), FakeAdv(-70, local_name="MC-advname")),
        ]
    )


def _make(received: list[bytes] | None = None) -> BleakAdapter:
    FakeBleakClient.instances.clear()
    sink = received if received is not None else []
    return BleakAdapter(
        notification_callback=sink.append,
        scanner_cls=_scanner(),
        client_cls=FakeBleakClient,
    )


def _patch_new_client(ad: BleakAdapter, setup: Callable[[FakeBleakClient], None]) -> None:
    """Run `setup` on every client the adapter creates from now on."""
    orig = ad._new_client

    def patched(device: Any) -> Any:
        c = orig(device)
        setup(c)
        return c

    ad._new_client = patched  # type: ignore[method-assign]  # test seam


def _in_thread(fn: Callable[..., object], *args: Any) -> int:
    """Run `fn(*args)` on a foreign thread, join it, return that thread's id."""
    ident: list[int] = []

    def run() -> None:
        ident.append(threading.get_ident())
        fn(*args)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return ident[0]


async def _test_scan_filters_and_maps(record: Record) -> None:
    devs = await _make().scan(timeout=0.1)
    by_name = {d.name: d for d in devs}
    ok = (
        set(by_name) == {"MC-b878-DK5EN-98", "MC-advname"}
        and by_name["MC-b878-DK5EN-98"].address == MAC
        and by_name["MC-b878-DK5EN-98"].rssi == -61
        and by_name["MC-advname"].rssi == -70
        and not by_name["MC-advname"].paired
    )
    record("scan: MC- prefix filter, address/name/rssi mapping", ok)


async def _test_connect_notify_and_writes(record: Record) -> None:
    ad = _make()
    res = await ad.ensure_connected("aa:bb:cc:dd:ee:ff")
    cl = FakeBleakClient.instances[-1]
    wrote = await ad.write(b"\x03\xa0hi")
    ok = (
        res.success
        and ad.is_connected
        and ad._connected_mac == MAC
        and cl.notify is not None
        and cl.notify[0] == NUS_TX_UUID
        and wrote
        and cl.writes == [(NUS_RX_UUID, b"\x03\xa0hi", True)]
    )
    await ad.disconnect()
    record("connect: state connected, notify on NUS TX, writes to NUS RX with response=True", ok)


async def _test_thread_notification_hops_to_loop(record: Record) -> None:
    # Mutation: dropping the call_soon_threadsafe branch of _on_raw_notification
    # makes the delivery thread != loop thread and this test fails.
    got: list[tuple[bytes, int]] = []
    ad = _make()
    ad.notification_callback = lambda b: got.append((b, threading.get_ident()))
    await ad.ensure_connected(MAC)
    cl = FakeBleakClient.instances[-1]
    assert cl.notify is not None
    cb = cl.notify[1]
    t = threading.Thread(target=cb, args=(None, bytearray(b"xyz")))
    t.start()
    t.join()
    await asyncio.sleep(0.05)
    ok = got == [(b"xyz", threading.get_ident())]
    await ad.disconnect()
    record("notify from foreign thread is delivered on the event loop", ok)


async def _test_early_notifications_buffered(record: Record) -> None:
    got: list[bytes] = []
    ad = _make(got)
    orig = ad._new_client

    def patched(device: Any) -> Any:
        c = orig(device)

        def early(_client: FakeBleakClient) -> None:
            ad._on_raw_notification(None, bytearray(b"1"))
            ad._on_raw_notification(None, bytearray(b"2"))

        c.on_start_notify = early
        return c

    ad._new_client = patched  # type: ignore[method-assign]  # test seam
    await ad.ensure_connected(MAC)
    ad._on_raw_notification(None, bytearray(b"3"))
    await asyncio.sleep(0.02)
    ok = got == [b"1", b"2", b"3"]
    await ad.disconnect()
    record("early notifications are buffered then delivered in order", ok)


async def _test_disconnect_callback_once(record: Record) -> None:
    ad = _make()
    calls: list[int] = []
    ad._disconnect_callback = lambda: calls.append(1)
    await ad.ensure_connected(MAC)
    cl = FakeBleakClient.instances[-1]
    assert cl.disconnected_callback is not None
    cl.disconnected_callback(cl)
    cl.disconnected_callback(cl)
    ok = calls == [1] and ad._status.state == ConnectionState.DISCONNECTED and not ad.is_connected
    record("bleak disconnected_callback while connected runs the base path once", ok)


async def _test_own_disconnect_does_not_fire_callback(record: Record) -> None:
    # Mutation: removing the early-return guard line in _handle_bleak_disconnect
    # lets the callback fire when our own disconnect() drops the client.
    ad = _make()
    calls: list[int] = []
    ad._disconnect_callback = lambda: calls.append(1)
    await ad.ensure_connected(MAC)
    cl = FakeBleakClient.instances[-1]

    async def disconnect_and_signal() -> None:
        cl.is_connected = False
        cl.disconnect_calls += 1
        if cl.disconnected_callback is not None:
            cl.disconnected_callback(cl)  # bleak fires it during our disconnect

    cl.disconnect = disconnect_and_signal  # type: ignore[method-assign]  # test fake
    await ad.disconnect()
    await asyncio.sleep(0.02)
    ok = calls == [] and ad._status.state == ConnectionState.DISCONNECTED
    record("own disconnect() does not trigger the disconnect callback", ok)


async def _test_stale_notification_not_delivered_on_new_connection(record: Record) -> None:
    # Regression: a notification queued from bleak's thread by the OLD client's
    # callback can run after the next connection began (notify not ready yet),
    # land in `_early` and be delivered as if the new node had sent it.
    got: list[bytes] = []
    ad = _make(got)
    await ad.ensure_connected(MAC)
    old = FakeBleakClient.instances[-1]
    assert old.notify is not None
    old_cb = old.notify[1]
    await ad.disconnect()

    _patch_new_client(
        ad,
        lambda c: setattr(
            c, "on_start_notify", lambda _c: _in_thread(old_cb, None, bytearray(b"STALE"))
        ),
    )
    await ad.ensure_connected(MAC)
    new = FakeBleakClient.instances[-1]
    assert new.notify is not None
    _in_thread(new.notify[1], None, bytearray(b"fresh"))
    await asyncio.sleep(0.05)
    ok = got == [b"fresh"]
    await ad.disconnect()
    record("stale notification of a discarded client is not delivered on the new connection", ok)


async def _test_disconnect_before_start_notify_runs_on_loop(record: Record) -> None:
    # Regression: `_loop` was only set in start_notify, so a bleak-thread
    # disconnect between connect() and start_notify() ran the base disconnect
    # handler (task cancel, callbacks) on the foreign thread.
    ad = _make()
    seen: list[int] = []
    ad._disconnect_callback = lambda: seen.append(threading.get_ident())
    ok_connect = await ad.connect(MAC, max_retries=1)
    cl = FakeBleakClient.instances[-1]
    assert cl.disconnected_callback is not None
    _in_thread(cl.disconnected_callback, cl)
    await asyncio.sleep(0.05)
    ok = ok_connect and seen == [threading.get_ident()] and not ad.is_connected
    record("disconnect before start_notify is handled on the event loop (connect path)", ok)


async def _test_write_not_connected(record: Record) -> None:
    ad = _make()
    try:
        await ad.write(b"\x03\xa0hi")
    except RuntimeError as e:
        ok = str(e) == "Not connected"
    else:
        ok = False
    record("write when not connected raises RuntimeError('Not connected')", ok)


async def _test_pair_unpair_unsupported(record: Record) -> None:
    ad = _make()
    ok = True
    for coro in (ad.pair(MAC), ad.unpair(MAC)):
        try:
            await coro
            ok = False
        except BackendUnsupportedError:
            pass
    record("pair/unpair raise BackendUnsupportedError", ok)


async def _test_device_not_found_maps(record: Record) -> None:
    # main.py's `_error_code_for_result` keys on DEVICE_NOT_FOUND_MSG in the
    # error text of a failed stage "connect".
    ad = _make()
    res = await ad.ensure_connected("00:00:00:00:00:00")
    ok = (
        not res.success
        and res.stage == "connect"
        and DEVICE_NOT_FOUND_MSG in (res.error_text or "")
        and res.error_name == "ConnectionError"
        and ad._status.state == ConnectionState.ERROR
        and not FakeBleakClient.instances
    )
    record("device not found: stage 'connect' with DEVICE_NOT_FOUND_MSG in error_text", ok)
    ok2 = not await ad.connect("00:00:00:00:00:00", max_retries=1) and (
        ad._status.state == ConnectionState.ERROR
    )
    record("connect(): device not found fails after retries, state ERROR", ok2)


async def _test_write_serialised_with_a0_gap(record: Record) -> None:
    ad = _make()
    await ad.ensure_connected(MAC)
    cl = FakeBleakClient.instances[-1]
    cl.write_delay_s = 0.05
    stamps: list[float] = []
    in_flight = 0
    peak = 0
    orig = cl.write_gatt_char

    async def tracked(uuid: str, data: bytes, response: bool = False) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        stamps.append(time.monotonic())
        try:
            await orig(uuid, data, response)
        finally:
            in_flight -= 1

    cl.write_gatt_char = tracked  # type: ignore[method-assign]  # test fake
    results = await asyncio.gather(ad.write(b"\x03\xa0aa"), ad.write(b"\x03\xa0bb"))
    ok = (
        list(results) == [True, True]
        and peak == 1
        and len(stamps) == 2
        and stamps[1] - stamps[0] >= A0_MIN_GAP_S - 0.02
        and [w[1] for w in cl.writes] == [b"\x03\xa0aa", b"\x03\xa0bb"]
    )
    await ad.disconnect()
    record("concurrent A0 writes are serialised and honour A0_MIN_GAP_S", ok)


async def _test_write_error_fires_disconnect(record: Record) -> None:
    ad = _make()
    calls: list[int] = []
    ad._disconnect_callback = lambda: calls.append(1)
    await ad.ensure_connected(MAC)
    cl = FakeBleakClient.instances[-1]
    cl.write_error = RuntimeError("Not connected")
    wrote = await ad.write(b"\x03\xa0hi")
    ok = (
        wrote is False
        and calls == [1]
        and ad._status.state == ConnectionState.DISCONNECTED
        and ad._client is None
    )
    record("write error 'Not connected' returns False and fires the disconnect path", ok)

    ad2 = _make()
    calls2: list[int] = []
    ad2._disconnect_callback = lambda: calls2.append(1)
    await ad2.ensure_connected(MAC)
    FakeBleakClient.instances[-1].write_error = RuntimeError("GATT busy")
    wrote2 = await ad2.write(b"\x03\xa0hi")
    ok2 = wrote2 is False and calls2 == [] and ad2.is_connected
    await ad2.disconnect()
    record("other write errors return False without dropping the connection", ok2)


async def _test_address_case_insensitive(record: Record) -> None:
    ad = _make()
    uuid_lower = "e1f2a3b4-0000-4c5d-8e9f-0123456789ab"
    uuid_upper = uuid_lower.upper()
    record(
        "normalize_address folds case for CoreBluetooth UUIDs",
        ad.normalize_address(uuid_lower) == uuid_upper == ad.normalize_address(uuid_upper),
    )
    first = await ad.ensure_connected("aa:bb:cc:dd:ee:ff")
    again = await ad.ensure_connected("AA:bb:CC:dd:EE:ff")
    scanned = [d for d in await ad.scan(timeout=0.1) if d.address == MAC]
    ok = (
        first.stage == "connected"
        and again.stage == "already_connected"
        and len(FakeBleakClient.instances) == 1
        and len(scanned) == 1
        and scanned[0].connected
    )
    await ad.disconnect()
    record("mixed-case addresses match: already_connected, scan marks connected", ok)


async def _test_reset_bus_noop(record: Record) -> None:
    ad = _make()
    before = ad._status.state
    ad.reset_bus()
    ok = ad._status.state == before and ad._client is None
    record("reset_bus is a no-op", ok)


class BluetoothNotAvailableError(Exception):
    """Same class-name shape bleak raises when macOS denies Bluetooth."""


async def _test_permission_error_maps_to_connect_failure(record: Record) -> None:
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, rec: logging.LogRecord) -> None:
            records.append(rec)

    log = logging.getLogger("ble_service.src.ble_adapter_bleak")
    handler = _Collect(level=logging.DEBUG)
    log.addHandler(handler)
    try:
        ad = _make()
        _patch_new_client(
            ad, lambda c: setattr(c, "connect_error", BluetoothNotAvailableError("denied"))
        )
        first = await ad.ensure_connected(MAC)
        second = await ad.ensure_connected(MAC)
        ok_connect = await ad.connect(MAC, max_retries=1)
    finally:
        log.removeHandler(handler)
    help_logs = [r for r in records if PERMISSION_HELP in r.getMessage()]
    ok = (
        not first.success
        and first.stage == "connect"
        and first.error_name == "BluetoothNotAvailableError"
        and DEVICE_NOT_FOUND_MSG not in (first.error_text or "")
        and not second.success
        and not ok_connect
        and ad._status.state == ConnectionState.ERROR
        and len(help_logs) == 1
        and help_logs[0].levelno == logging.ERROR
    )
    record("BluetoothNotAvailable maps to a connect failure and logs the permission help once", ok)


async def _test_factory(record: Record) -> None:
    # Real check: a fresh interpreter with dbus_next unimportable. In-process
    # `ble_adapter` (which imports dbus_next) is already loaded, so blocking the
    # module here would prove nothing.
    code = (
        "import sys\n"
        "sys.modules['dbus_next'] = None\n"
        "from ble_service.src.ble_backend import create_backend\n"
        "b = create_backend('bleak')\n"
        "assert b.name == 'bleak', b.name\n"
        "assert 'ble_service.src.ble_adapter' not in sys.modules\n"
    )
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 and has_console:
        print(proc.stderr)
    record(
        "create_backend('bleak') works in a fresh interpreter with dbus_next blocked",
        proc.returncode == 0,
    )

    chosen: list[str] = []
    saved_plat = sys.platform
    saved_env = os.environ.pop("BLE_BACKEND", None)
    try:
        sys.platform = "linux"
        chosen.append(create_backend().name)
        sys.platform = "darwin"
        chosen.append(create_backend().name)
    finally:
        sys.platform = saved_plat
        if saved_env is not None:
            os.environ["BLE_BACKEND"] = saved_env
    record(
        "create_backend default: bluez off darwin, bleak on darwin", chosen == ["bluez", "bleak"]
    )


async def _test_address_semantics(record: Record) -> None:
    ad = _make()
    record(
        "normalize_address strips and upper-cases",
        ad.normalize_address("  aa:bb:cc:dd:ee:ff ") == MAC,
    )
    expected = "uuid" if sys.platform == "darwin" else "mac"
    record(f"address_kind is {expected!r} on {sys.platform}", BleakAdapter.address_kind == expected)


async def run_ble_bleak_tests() -> bool:
    """Run the bleak backend suite. True iff every case passed."""
    if has_console:
        print("\n🧪 Testing ble_bleak:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    cases: list[Callable[[Record], Awaitable[None]]] = [
        _test_scan_filters_and_maps,
        _test_connect_notify_and_writes,
        _test_thread_notification_hops_to_loop,
        _test_early_notifications_buffered,
        _test_disconnect_callback_once,
        _test_own_disconnect_does_not_fire_callback,
        _test_write_not_connected,
        _test_pair_unpair_unsupported,
        _test_stale_notification_not_delivered_on_new_connection,
        _test_disconnect_before_start_notify_runs_on_loop,
        _test_device_not_found_maps,
        _test_write_serialised_with_a0_gap,
        _test_write_error_fires_disconnect,
        _test_address_case_insensitive,
        _test_reset_bus_noop,
        _test_permission_error_maps_to_connect_failure,
        _test_factory,
        _test_address_semantics,
    ]
    for case in cases:
        try:
            await case(_record)
        except Exception as e:  # a crashing case is a failed case, not a dead suite
            _record(f"{case.__name__} raised {type(e).__name__}: {e}", False)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\n🧪 ble_bleak Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_ble_bleak_tests()) else 1)
