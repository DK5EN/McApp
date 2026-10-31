# BLE service macOS backend (bleak / CoreBluetooth) - design

**Status:** design, no code. 2026-10-31. Input to campaign waves CT3b (implementation) and CT4
(hardware run), `doc/2026-10-31_1200-container-test-campaign.md`. Related:
`doc/2026-10-31_0830-ble-service-and-lan-discovery-concept.md` (B13). Citations are
`file:line` as of this commit; the bench scripts live in the firmware repo, `tools/bench/`
(read-only here).

## BLUF

- **Put the bleak backend inside `ble_service/`, behind `BLE_BACKEND=bluez|bleak`** (default by
  platform: `darwin` -> bleak, else bluez). One `main.py`, one wire contract, no API drift. A
  separate server built on the bench scripts would re-implement about 40 routes, the register
  cache, the reconnect ladder and the time-sync logic, and would drift.
- **The bench scripts are the proven GATT recipe, not the server.** Their NUS UUIDs, the
  early-notification buffer, find-by-address-then-connect, name-prefix discovery and the
  permission help text are lifted into the new backend. The bench stays as the hardware oracle.
- **Refactor first, in two steps:** (1) pure values and a `BleBackend` Protocol into a dbus-free
  module; (2) the transport-neutral command layer (`write` gate, `send_*`, `set_*`, time sync,
  keepalive, DST loop, register waits) into a base class both adapters inherit. BlueZ behaviour
  stays byte-for-byte; the 6799-line suite is the guard.
- **Addresses stay `device_address` on the wire.** On macOS the value is a CoreBluetooth UUID. An
  additive `address_kind` (`mac` | `uuid`) says which. **One McApp-side hit:**
  `internet_feed.build_identity` forwards the address as `mac` (`src/mcapp/internet_feed.py:113-118`)
  and would send a UUID. Needs a guard (open question 1).
- **Pairing, bond removal and `reset_bus` do not exist on macOS.** `pair`/`unpair` answer HTTP
  200 with `success:false` and `error_code:"unsupported_on_backend"`, which the existing client
  already renders as an ordinary failure with a message. Current firmware needs no pairing; the
  PIN travels in the app-layer hello.
- **Five implementation waves, each <= 25 min, two of them parallel; one hardware wave (CT4).**
  Biggest risks: the shared-layer extraction touching the production Linux path, and an unverified
  CoreBluetooth behaviour list (section 8).

## 1. Where the code lives

| Criterion      | A: bleak backend in `ble_service/`                                                | B: extend bench into a server                                   |
| -------------- | --------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| API drift      | none by construction: same `main.py`, same pydantic models, same SSE              | guaranteed: second implementation of every route and error code |
| Behaviour      | register cache, state file, ladders, `pin_required`, time sync (`node_tz`) reused | all re-implemented, or silently missing                         |
| Tests          | existing suite and `_FakeAdapter` seam (`scripts/ble_service_tests.py:155-198`)   | new harness, outside CI and mypy strict                         |
| Packaging      | one wheel; bleak is a `darwin`-marker dependency, absent on the Pi                | separate artifact, separate release cadence                     |
| Risk           | touches the production Linux adapter (mitigated by the suite)                     | zero risk to production, zero reuse                             |
| Debian package | unchanged: the `.deb` ships the same code, bleak simply not installed             | n/a                                                             |

**Recommendation: A.** macOS runs the same package through `uv` (section 6). The Pi never imports
bleak: the factory imports a backend lazily (section 2).

How the bench patterns carry over either way:

| Bench pattern                                                             | Source                                     | Used in the backend as                                                                                                                                                                           |
| ------------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| NUS write char `6e400002`, write WITH response, notify char `6e400003`    | `ble_golden.py:527`                        | `write_gatt_char(..., response=True)`; UUIDs already equal `NUS_RX_UUID`/`NUS_TX_UUID` (`ble_adapter.py:371-372`, naming is inverted vs. the bench: the service names them from the node's side) |
| Buffer notifications that arrive before the callback is set               | `ble_stress.py:621-651` (`BleakTransport`) | same `_early` buffer, flushed when `start_notify()` wires the callback                                                                                                                           |
| Resolve the device by address first, then connect with the device object  | `ble_cycle.py:183-190`                     | `find_device_by_address(uuid)` then `BleakClient(device, ...)`; macOS name cache makes name lookup unreliable                                                                                    |
| Discover by advertised name prefix incl. `ad.local_name`, NOT by NUS UUID | `ble_golden.py:470-482`                    | `BleakScanner.discover(return_adv=True)` filtered on `MC-`; the node does not advertise NUS                                                                                                      |
| `BleakBluetoothNotAvailableError` detected by type name                   | `ble_stress.py:891`                        | maps to a clear "Bluetooth off / no permission" connect error                                                                                                                                    |
| Permission help: CoreBluetooth grants per host app (Terminal)             | `ble_golden.py:452-463`                    | log text on first denial and in the README                                                                                                                                                       |

The bench's `async with BleakClient(...)` scoping does NOT carry over: the service needs a
long-lived client with explicit connect/disconnect and a `disconnected_callback`.

## 2. Refactor first

Today `main.py` imports pure values from a module that imports `dbus_next` at module top
(`ble_adapter.py:54-59`; `main.py:28-35`), and reaches into a private constant
(`main.py:966`, `# noqa: SLF001`). On macOS the first import already needs dbus-next.

**Step 1 - `ble_service/src/ble_types.py` (dbus-free, pure moves, re-exported from
`ble_adapter` so no importer breaks):**

- Values: `MESHCOM_NAME_PREFIX` (`:155`), `NUS_RX_UUID`/`NUS_TX_UUID` (`:371-372`),
  `build_hello_bytes` (`:375`), `MsgType`, `_frame`, timing constants used by the command layer
  (`A0_MIN_GAP_S :144`, `WRITE_TIMEOUT_S :114`, `KEEPALIVE_INTERVAL_S :123`, time-sync constants).
- Types: `ConnectionState` (`:425`), `BLEDevice` (`:436`), `BLEStatus` (`:449`),
  `EnsureConnectedResult` (`:459`).
- `DEVICE_NOT_FOUND_MSG` made public (was `_DEVICE_NOT_FOUND_MSG`, `:238`); `main.py:966` imports
  it normally and drops the `noqa`.
- `class BackendUnsupportedError(Exception)` for pair/unpair.
- `class BleBackend(Protocol)`, the surface `main.py` really uses:

| Group    | Members                                                                                                                                                                                                                                                   |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| State    | `status`, `is_connected`, `is_busy`, `hello_bytes`, `pairing_passkey`, `last_time_sync_branch`, `_disconnect_callback` (becomes public `on_disconnect`)                                                                                                   |
| Identity | `name: Literal["bluez","bleak"]`, `address_kind: Literal["mac","uuid"]`, `capabilities: Mapping[str,bool]`, `normalize_address(str) -> str`                                                                                                               |
| Link     | `scan(timeout, prefix)`, `connect(addr)`, `ensure_connected(addr, pin, *, user_initiated)`, `disconnect()`, `reset_bus()`, `start_notify()`, `pair(addr)`, `unpair(addr)`                                                                                 |
| Commands | `write`, `send_message`, `send_command`, `send_hello`, `set_time`, `set_callsign`, `set_wifi`, `set_latitude/longitude/altitude`, `set_aprs_symbols`, `save_and_reboot`, `query_extended_registers`, `note_register`, `arm_register`, `wait_for_register` |

`mypy --strict` enforces the Protocol: both adapters are assigned to a `BleBackend` variable in
the tests. `main.py` types `_adapter()` (`:254`) as `BleBackend`.

**Step 2 - `ble_service/src/ble_session.py`: `class BleSessionBase`** holds everything that only
ever calls `write()`: `send_*` (`ble_adapter.py:1449-1514`), register waits (`:1515-1540`),
time sync (`:1542-1700`), the `set_*` family (`:1701-1878`), `query_extended_registers`
(`:1879`), keepalive and DST loops (`:1908-1958`), the write lock and the A0 gap, plus the
transport-neutral half of `_on_disconnect_detected` (`:1411`). It talks to the transport through
three hooks:

- `_gatt_ready() -> bool` (BlueZ: `write_char_iface is not None`; bleak: `client.is_connected`),
- `async _gatt_write(data)` (BlueZ: `call_write_value(data, {})`; bleak: `write_gatt_char(..., response=True)`),
- `_drop_transport_handles()` (BlueZ: the D-Bus proxies, `:1432-1445`; bleak: the client).

`BLEAdapter(BleSessionBase)` keeps scan/connect/pair/agent/stale-bond/`ensure_connected` (the
D-Bus-heavy `:520-1360`, `:1960-2505`). `BleakAdapter(BleSessionBase)` is new (section 3).

**Why not a Protocol alone:** BleakAdapter would copy ~600 lines (time sync alone is the
`node_tz` classify/probe/verify state machine). Two copies drift.

**Test-suite coupling (the risk in step 2):** the suite patches module globals on `ble_adapter`
(`ble_service_tests.py:6070-6072`: `NODE_TZ_PROBE_TIMEOUT_S`, `NODE_TZ_VERIFY_TIMEOUT_S`,
`TIME_SYNC_SETTLE_S`; plus `:1357-1366` patches `ble_adapter.asyncio.sleep`, which is the global
`asyncio` module and survives). Constants whose reader moves must be patched on the new module,
or the patch silently stops applying. `PROPERTY_SET_TIMEOUT_S` and `POST_PAIR_SETTLE_S`
(`:4131`, `:4229`) are BlueZ-side and stay. Step 2 owns those test edits.

**Factory** `ble_service/src/ble_backend.py`: `create_backend(name, **kwargs) -> BleBackend`
imports `ble_adapter` or `ble_adapter_bleak` lazily, so a Pi never imports bleak and macOS never
imports dbus-next (which stays an unconditional dep today, `ble_service/pyproject.toml:21`; make it
`dbus-next>=0.2.3; sys_platform == 'linux'`).

## 3. Per-method mapping

| Surface                    | BlueZ today                                                                    | bleak / macOS                                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scan(timeout, prefix)`    | `StartDiscovery` + ObjectManager under `_operation_lock` (`:794-896`)          | `BleakScanner.discover(timeout, return_adv=True)`; keep entries whose `d.name or ad.local_name` starts with the prefix (case-insensitive); `rssi=ad.rssi`; `paired=False` (CoreBluetooth exposes no bond state); `known` = equals saved state address. Same lock                                                                                                                                         |
| `connect(addr)`            | 3 retries, `Device1.Connect`, services-resolved wait, find chars (`:897-1094`) | normalize addr; `find_device_by_address(addr, timeout)`; none -> raise `ConnectionError(DEVICE_NOT_FOUND_MSG)`; `BleakClient(dev, disconnected_callback=..., timeout=CONNECT_TIMEOUT_S)`; `await connect()`. Same retry count                                                                                                                                                                            |
| `ensure_connected`         | connect, Trusted, GATT-ready, on-demand pair, stale-bond repair (`:1960-2388`) | connect stage, then `start_notify`. Stages produced: `already_connected`, `connected`, `connect`, `gatt`. Never `pair`/`gatt_post_pair`. `error_name` = exception class name (`BleakError`, `TimeoutError`, `BleakBluetoothNotAvailableError`); `device_not_found` code works unchanged via `DEVICE_NOT_FOUND_MSG`. `user_initiated` accepted and ignored                                                |
| Trust / Trusted            | `Device1.Trusted = True`                                                       | no equivalent; skipped                                                                                                                                                                                                                                                                                                                                                                                   |
| `start_notify`             | `StartNotify` + attach PropertiesChanged exactly once (`:1287-1323`)           | `client.start_notify(NUS_TX_UUID, cb)`; `cb` flushes the `_early` buffer, then calls `notification_callback(bytes)`; if `mtu_size` is readable, log it                                                                                                                                                                                                                                                   |
| Notification threading     | dbus-next handler on the loop                                                  | `main.notification_callback` is sync and touches a deque and an `asyncio.Event`: it must run on the loop. bleak is expected to deliver on the loop; the adapter checks the thread and uses `loop.call_soon_threadsafe` if not (unverified which bleak version does what, test with a fake that calls from a thread)                                                                                      |
| `write(bytes)`             | `WriteValue(data, {})` under write lock + A0 gap (`:1362-1410`)                | same base-class gate; `_gatt_write` = `write_gatt_char(NUS_RX_UUID, data, response=True)`; "not connected" error text -> `_on_disconnect_detected`. BlueZ's `{}` options leave the write type to BlueZ; with-response is bench-proven on this firmware, so CT4 compares both                                                                                                                             |
| `disconnect()`             | unsubscribe props, `StopNotify`, `Device1.Disconnect`, drop bus (`:1241-1285`) | state -> `DISCONNECTING`, cancel keepalive/DST, `stop_notify` (best effort), `client.disconnect()` (`cancelPeripheralConnection`; the only way to leave a link on macOS, `ble_cycle.py:35-41`), state -> `DISCONNECTED`. Our own disconnect must not fire `on_disconnect`: the `disconnected_callback` returns early unless state is `CONNECTED`, mirroring BlueZ unsubscribing the props listener first |
| Unexpected drop            | PropertiesChanged `Connected=false` -> `_on_disconnect_detected`               | `disconnected_callback` -> `_on_disconnect_detected` -> `on_disconnect` -> existing reconnect ladder in `main.py:1005`. A node reboot, Mac sleep/wake and Bluetooth-off all arrive here                                                                                                                                                                                                                  |
| `pair(addr)`               | on-demand agent, `Device1.Pair` with `pairing_passkey` (`:2388-2473`)          | raise `BackendUnsupportedError`; route answers 200 `success:false`, `error_code:"unsupported_on_backend"`, message "macOS pairs through a system dialog on first encrypted access"                                                                                                                                                                                                                       |
| `unpair(addr)`             | `Adapter1.RemoveDevice` (`:2474`)                                              | same unsupported result, message "remove the node in System Settings > Bluetooth" (`ble_golden.py:52-55`)                                                                                                                                                                                                                                                                                                |
| `pairing_passkey`          | returned by the D-Bus agent (`:548`)                                           | stored, never used; the PIN still reaches the node through `hello_bytes` (the hello hash), which is backend-neutral                                                                                                                                                                                                                                                                                      |
| `reset_bus()`              | drop the D-Bus connection (`:677`)                                             | no-op                                                                                                                                                                                                                                                                                                                                                                                                    |
| `is_busy`                  | `_operation_lock.locked()`                                                     | same (base class)                                                                                                                                                                                                                                                                                                                                                                                        |
| MTU                        | optional D-Bus `MTU` property, logged only                                     | `client.mtu_size`, logged only; no chunking exists or is added (the bench has none either)                                                                                                                                                                                                                                                                                                               |
| `known` / `paired` in scan | bond list from BlueZ                                                           | `paired=False` always; document it. McApp only displays it (`ble_client_remote.py:344-349`)                                                                                                                                                                                                                                                                                                              |

**Pairing on macOS in detail.** If an older firmware requires a bond, macOS raises its own
passkey dialog during the first encrypted GATT access (`ble_golden.py:52-55`); a headless Mac cannot
answer it, so the connect fails at stage `gatt` and the operator pairs once at the Mac. Whether
bleak's macOS `pair()` is usable is not relied on (from memory it is not implemented there;
unverified).

**Single-connection node.** A node serves one central. With the phone connected, the node stops
advertising, so the bleak scan finds nothing and the result is `device_not_found`, the same
operator hint as on Linux (`ble_stress.py:679-682`).

## 4. Address semantics

- **Wire:** unchanged field names (`ConnectRequest.device_address` `main.py:1363`,
  `StatusResponse.device_address :1415`, `DeviceResponse.address :1429`, state key `device_mac`
  `:709-773`). Additive fields only: `address_kind` on `StatusResponse` and `DeviceResponse`
  (default `"mac"`, so an older service stays correct for a newer McApp).
- **Value on macOS:** the CoreBluetooth peripheral UUID (`8-4-4-4-12`, upper case from bleak).
  It is stable per (Mac, peripheral), not portable between Macs, and changes if the bond or the
  Bluetooth cache is cleared. The real MAC is not available (unverified whether the node's `I`
  register carries one; the McApp feed reads `ID`, not a MAC).
- **Case:** `normalize_address()` per backend: bluez keeps today's behaviour (no normalization
  exists, `grep` finds no `.upper()` on the address); bleak upper-cases UUIDs and compares
  case-insensitively, including `ensure_connected`'s `_connected_mac == mac` check (`:2008`).
- **State file:** key `device_mac` is kept, so the format is unchanged; a UUID is just a string
  there. A file written by one backend is not valid for the other: store `address_kind` next to
  it and ignore a mismatching saved address (log it), instead of failing a reconnect against an
  address of the wrong kind. Default path on macOS: `BLE_STATE_FILE`, else
  `~/Library/Application Support/mcapp-ble/ble_state.json` (the Linux default
  `/var/lib/mcapp/` is not writable there, `main.py:166`).
- **McApp impact (read from `src/mcapp/ble_client_remote.py`):** McApp passes the address through
  (`:396`, `:452`), echoes `device_address` from status into its own status (`:405`, `:685`,
  `:1172`) and compares it only for equality with its previous value (`:800-815`). No MAC format
  check, no regex. Two real consumers: (1) `internet_feed.build_identity` sends it as `"mac"`
  with `idKind:"ble"` (`:113-118`) and builds the identity key from it (`:143`): a UUID would
  reach the mcmap feed. (2) The webapp shows the address string; a UUID is displayed as is.

## 5. `/health`

Additive, unauthenticated, no secrets (consistent with B13's rule, concept lines 113-124):

```text
{"status": "healthy", "ble_connected": false, "timestamp": 1730000000000,
 "backend": "bleak", "platform": "darwin", "address_kind": "uuid",
 "capabilities": {"pair": false, "unpair": false}}
```

`backend` is `bluez` | `bleak`, `platform` is `sys.platform` (`linux` | `darwin`). Together with
B13's `version`, `api_version`, `id` and `auth` they form one dict built in one function. Adding
fields is not wire-incompatible, so `api_version` does not move. McApp may show "BLE host:
macOS (bleak)" and hide pair/unpair when `capabilities` says so; both are optional.

## 6. Runtime on macOS

- **Launch:** the service is a workspace package with a real dependency set, so a PEP 723 script
  is the wrong shape (it cannot import `ble_service.src` as a package). Until B13 wave 1 delivers a
  `main()` entry, run from the repo root as the README already states, with `--loop asyncio`
  (uvloop is installed by marker on macOS too; its interplay with the CoreBluetooth backend is
  unverified, so avoid it):
  `BLE_BACKEND=bleak BLE_SERVICE_API_KEY=<key> uv run --project ble_service uvicorn ble_service.src.main:app --host <bind> --port 8081 --loop asyncio`.
  After B13 wave 1: `uv run --project ble_service mcapp-ble-service` (name per that wave). CT3b
  adds `ble_service/scripts/run-macos.sh` wrapping the above and refusing an unsafe bind.
- **Dependencies:** `bleak>=0.22; sys_platform == 'darwin'` (the bench pins `>=0.22`,
  `ble_golden.py:4`), and bleak also in the `dev` group so `mypy --strict` and the fake-based tests
  resolve on Linux CI. `ble_service/uv.lock` is regenerated outside the workspace (standing rule
  for this lock), by the orchestrator.
- **Bind and key:** the container reaches the Mac, so loopback alone is not enough unless OrbStack
  forwards it. Rule from B13: a non-loopback bind requires `BLE_SERVICE_API_KEY` (>= 16 chars, not
  `disabled`). The macOS wrapper enforces it independently of B13's wave 1. Today's `__main__` binds
  `0.0.0.0` (`main.py:2165-2175`); the CLI `--host` overrides it.
- **Container to host:** under OrbStack the Mac is expected at `host.docker.internal`
  (recalled from OrbStack's documentation, NOT verified here; the Mac's LAN address
  `192.168.68.58` from the campaign doc is the fallback). CT3b step 0: from a throwaway
  container, `wget -qO- http://host.docker.internal:8081/health` with the service bound first to
  `127.0.0.1`, then to the LAN address. Record which bind works; prefer the narrowest.
- **Permission:** CoreBluetooth access is granted to the host app (Terminal, iTerm, or the launchd
  parent), not to python or uv (`ble_golden.py:452-463`). On denial the backend logs that text once
  and returns `connect_failed` with `error_text` naming it. Dismissed prompt: quit the terminal with
  Cmd-Q and relaunch.
- **launchd (later, optional):** a user LaunchAgent (CoreBluetooth needs the user session); its permission prompt attaches to the launcher, so one interactive run comes first. Not in CT3b.

## 7. Tests

1. **`scripts/ble_bleak_tests.py` (new)**, wired into `main()` of
   `scripts/run_startup_tests.py` (a suite not wired there is gated by nothing). No hardware, no
   bleak install needed: `BleakAdapter` takes `scanner_cls`/`client_cls` seams, defaulting to the
   real bleak lazily. Fakes: `FakeScanner` (`discover` returns `(device, adv)` tuples),
   `FakeClient` (`connect`, `disconnect`, `start_notify`, `write_gatt_char`, `is_connected`,
   `mtu_size`, fires `disconnected_callback` on demand, can call notify from a worker thread).
   Cases: prefix filter incl. `ad.local_name`; device-not-found text -> `device_not_found`;
   notification before callback is buffered then delivered once; thread-origin notification lands on
   the loop; write is serialized and A0 gap holds; write error "Not connected" -> disconnect
   callback; own `disconnect()` does not call `on_disconnect`; unexpected drop does; case-insensitive
   UUID compare; `pair`/`unpair` -> unsupported result; `reset_bus` no-op;
   `BleakBluetoothNotAvailableError` -> `connect_failed` text.
2. **Shared-behaviour suite:** the route-level `_FakeAdapter` tests (`:155-198`) already prove the
   routes do not depend on BlueZ; they stay as is. New: a small parametrised "adapter contract"
   (connect -> status, single notification delivery, write serialization, disconnect callback,
   `ensure_connected` stage names) run once with the dbus fakes (`:2206-2357`) and once with the
   bleak fakes, so the base class is exercised through both transports.
3. **Mutation checks** (project rule): remove the early-buffer flush, the own-disconnect guard and
   the thread marshal one at a time; each must fail a named case.
4. **Gate:** `uv run mypy src/mcapp ble_service/src`, `uvx ruff check`, `uvx ruff format --check .`,
   `uv run python scripts/run_startup_tests.py`.

**CT4 hardware checklist (DK5EN-1, orchestrator, exclusive BLE link; phone disconnected):**

1. `GET /health` shows `backend:bleak`, `platform:darwin`; unauthenticated off-loopback refused.
2. `GET /api/ble/devices`: `MC-<id>-DK5EN-1` listed with a UUID; record whether the name is stale.
3. `POST /api/ble/ensure_connected` with the UUID: success, register burst (`I`, `SN`, `G`, ...)
   in `/api/ble/registers` within 10 s; compare with `ble_golden.py` frames from the same node.
4. Send a DM and a group message; confirm on air/UDP monitor; notifications and ACKs on the SSE
   stream (`/api/ble/notifications`), each exactly once.
5. `settime`: branch reported, node time correct.
6. Reconnect after node reboot (power cycle): auto-reconnect ladder recovers; no manual step.
7. Mac sleep/wake and Bluetooth off/on: state returns to `disconnected`, then recovers.
8. Two-client conflict: phone app connected -> `device_not_found`; second McApp/ble_service
   instance while the first is connected -> defined failure, no hang.
9. Wrong PIN -> `pin_required` (disconnect shortly after hello); right PIN -> OK.
10. Container to Mac (campaign B2): the same flow through McApp in the container, SSE included.

## 8. Waves (CT3b)

Each wave: implementer, <= 25 min, exclusive files, gate run by the orchestrator, no git in the
implementer.

| Wave | Content                                                                                                                                                                                                                                 | Exclusive files                                                                                                            |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| W1   | `ble_types.py` (values, types, `BleBackend`, `BackendUnsupportedError`, public `DEVICE_NOT_FOUND_MSG`); re-exports; `main.py` import block and `:966`                                                                                   | `ble_service/src/ble_types.py` (new), `ble_service/src/ble_adapter.py`, `ble_service/src/main.py`                          |
| W2   | `BleSessionBase` extraction with the three hooks; BlueZ adapter inherits; test patch targets moved                                                                                                                                      | `ble_service/src/ble_session.py` (new), `ble_service/src/ble_adapter.py`, `scripts/ble_service_tests.py`                   |
| W3   | `BleakAdapter`, `create_backend` factory, `bleak` seams                                                                                                                                                                                 | `ble_service/src/ble_adapter_bleak.py` (new), `ble_service/src/ble_backend.py` (new)                                       |
| W4   | `main.py` integration: `BLE_BACKEND`, `_adapter()` type, `/health` fields, `address_kind`, pair/unpair unsupported mapping, state-file default and `address_kind` check; `pyproject.toml` markers; README macOS section; wrapper script | `ble_service/src/main.py`, `ble_service/pyproject.toml`, `ble_service/README.md`, `ble_service/scripts/run-macos.sh` (new) |
| W5   | bleak fakes + suite + contract suite + mutation notes; wire into the runner (parallel to W4, needs W3 only)                                                                                                                             | `scripts/ble_bleak_tests.py` (new), `scripts/run_startup_tests.py`                                                         |
| W6   | McApp guard: `build_identity` does not send a UUID as `mac` (depends on open question 1); optional status display                                                                                                                       | `src/mcapp/internet_feed.py`, `src/mcapp/internet_feed_tests.py`                                                           |

Order: W1 -> W2 -> W3 -> (W4 || W5); W6 anytime after the decision. `ble_service/uv.lock`
regeneration and the full gate run belong to the orchestrator after W4. W1/W2 do not need a macOS
host; W3-W5 are testable on Linux with fakes; only CT4 needs the Mac and the node.

## 9. Risks and open questions

**Risks**

- **Extraction regresses Linux.** `ble_adapter.py` is 2505 lines of fixed-by-incident behaviour.
  Mitigation: moves only, hooks named after current code, the full suite after each of W1 and W2,
  W2 not combined with any feature work.
- **Unverified CoreBluetooth/bleak behaviours** (each needs a CT4 line or a fake-based test):
  callback threading; `find_device_by_address` for a UUID not seen since boot; UUID stability after
  a node firmware erase (the bench says the bond must then be removed by hand, `ble_golden.py:52-55`);
  uvloop interplay; behaviour on Mac sleep; `write` with-response vs. BlueZ's default.
- **Stale names.** macOS caches advertised names for hours (`ble_cycle.py:183-190`); the callsign McApp shows comes from the `I` register, not the scan.

**Open questions (need an operator decision)**

1. **Identity `mac` for a UUID host.** Options: (a) omit `mac` when `address_kind == "uuid"`
   (the identity key `(call, idKind, mac)` then changes for such boxes, mcmap §3.1 contract not
   checked here); (b) fall back to `idKind:"pi"`; (c) send the UUID. Recommendation (a), after a
   look at the mcmap contract. A Mac-hosted service is a test setup, so (b) is also acceptable.
2. **Is macOS a supported production host or a test/dev host only?** Decides whether launchd and the
   `.deb`-style installer story are ever needed. Recommendation: dev/test only until asked.
3. **`address_kind` on the wire now, or only in `/health`?** Recommendation: both; McApp stays unaware of `capabilities` in CT3b.
