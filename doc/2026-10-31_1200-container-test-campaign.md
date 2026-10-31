# Container test campaign (CT) - plan and state

**Status:** running, started 2026-10-31. Resume point for the campaign: waves, ownership, environment
and what is done. Concept under test: `doc/2026-10-31_0830-container-deployment-concept.md` (B12),
BLE side `doc/2026-10-31_0830-ble-service-and-lan-discovery-concept.md` (B13).

## Goal

Prove the B12 container premise on real hardware before any production code changes: an Alpine
McApp image on arm64 and amd64, fed by a real MeshCom node over Extern-UDP, reached through
Traefik and Caddy with a login, persisting into a volume, and talking to a BLE service outside the
container on two platforms (Linux BlueZ and macOS CoreBluetooth). Test tooling is kept in
`scripts/container-tests/` so it can later become the CI smoke test.

## Operator decisions (2026-10-31)

- Transmitting on air with DK5EN-1: approved. Flashing DK5EN-1: approved. Managing the node
  (EXT-UDP target, settings): approved.
- BLE: both routes. (a) the real `ble_service` on Linux, (b) a macOS BLE service built from the
  bleak bench framework in the firmware repo, extended to speak the `ble_service` HTTP protocol.
- No outside access. Test hostnames resolve to localhost through `/etc/hosts`; the operator adds
  new entries on request.
- Cookie login: yes (Authelia, same origin).
- OrbStack machines: yes. Test scripts: in the repo, committed and pushed.
- No Pi 4 desktop available: B11's desktop field check stays untested for now.
- (Second round) Browser AI WebAssembly (107 MB of the 228 MB image) is **loaded on demand** by the
  webapp instead of shipped in the bundle: smaller image and smaller bare-metal installs. A webapp
  change, tracked outside this campaign.
- (Second round) Rosetta enabled in OrbStack for the amd64 VM test (U4).
- (Second round) A CoreBluetooth UUID is **never sent as `mac`** in the internet-feed identity;
  Linux MACs are unchanged (macOS BLE design, wave W6).
- (Second round) The macOS BLE service is a **dev/test host only**: documented bench tool, no launchd
  service, no support promise. Linux stays the supported BLE platform.
- (Second round) Test hostnames `mcapp-test` and `auth-test.air-maxx.net` added to `/etc/hosts` by
  the operator; the Caddy snippet added to `~/docker/caddy/Caddyfile` by the orchestrator (backup
  `Caddyfile.bak-mcapp-ct-*` next to it, validated, reloaded).

## Environment (verified 2026-10-31)

| Item                | Value                                                                                                          |
| ------------------- | -------------------------------------------------------------------------------------------------------------- |
| Mac                 | `192.168.68.58` (en0), OrbStack Docker, Rosetta off (amd64 runs under QEMU)                                    |
| DK5EN-1             | Heltec V3, `/dev/cu.usbserial-0001`, firmware 4.40a (build 2026-10-31), WiFi ORBI63, IP `192.168.68.71`        |
| DK5EN-1 start state | `EXTUDP off`, `EXT IP` empty. Restore this at the end of the campaign                                          |
| DK5EN-92            | T-Beam on `/dev/cu.usbserial-573C0005841`. Not part of the campaign; do not touch                              |
| OrbStack machines   | `deb-arm64` (Debian trixie, 192.168.139.96), `deb-amd64` (Debian bookworm, 192.168.139.136)                    |
| Proxy stack         | Caddy `:443` (wildcard `*.air-maxx.net`, `~/docker/caddy`) → Traefik `:80` (`~/docker/traefik`, `traefik-net`) |
| Hosts file          | `*.air-maxx.net` test names → `127.0.0.1`; proposed: `mcapp-test`, `auth-test`                                 |
| Production          | mcapp.local (DK5EN-98) is never touched by this campaign                                                       |

Notes: opening a node's serial port resets the ESP32 (WiFi needs ~7 s to come back). Debian trixie
did not boot as an amd64 OrbStack machine (start timeout); bookworm did, which also covers the
older release.

## Waves

Every wave ends with a gate run by the orchestrator, a commit of its repo files, and an update of
the status table below.

| Wave | Goal                                | Owner / files                                                                                        |
| ---- | ----------------------------------- | ---------------------------------------------------------------------------------------------------- |
| CT0  | Environment                         | orchestrator: node identification, OrbStack machines + Docker, this document                         |
| CT1a | Spike image                         | implementer: `scripts/container-tests/image/` (Dockerfile, launcher shim, build + smoke scripts)     |
| CT1b | Node control                        | implementer: `scripts/container-tests/node/` (serial + net-console helper: info, extudp, send)       |
| CT1c | Proxy + login stack                 | implementer: `scripts/container-tests/proxy/` (compose with Traefik labels, Authelia, Caddy snippet) |
| CT1d | macOS BLE service design            | implementer (analysis): design section in this document, no code                                     |
| CT2  | UDP matrix (hardware, serialized)   | orchestrator only (exclusive: DK5EN-1, its serial port, UDP 1799 on the Mac)                         |
| CT3a | Web, TLS, SSE, Basic auth, Authelia | orchestrator + operator (iPhone checklist)                                                           |
| CT3b | macOS BLE service implementation    | implementer: location decided in CT1d                                                                |
| CT4  | BLE routes end to end               | orchestrator (exclusive: DK5EN-1 BLE link)                                                           |
| CT5  | Persistence, upgrade, migration     | implementer: `scripts/container-tests/persistence/`                                                  |
| CT6  | Report                              | orchestrator: results into the B12/B13 papers and the overview                                       |

**Shared resources.** DK5EN-1 (serial port, BLE link, its settings) belongs to the orchestrator
alone; no writer opens the port or connects over BLE. UDP port 1799 on the Mac and on the OrbStack
machines is used by one test at a time. Image builds use distinct tags per writer. The operator's
`~/docker/caddy` and `~/docker/traefik` configs are changed only by the orchestrator, with a backup
first.

**Spike rule.** CT1a proves the premise without touching `src/mcapp`: the missing B12 wave-1
changes (bind address, converge watchdog off, SPA mount) are applied by a launcher shim inside the
image, kept in `scripts/container-tests/image/`. Each shim item maps to a numbered required code
change in the B12 paper, so the shim shrinks to nothing as wave 1 lands.

## Test matrix (CT2-CT5)

| ID  | Test                                                                        | Expected                                                                                                |
| --- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| U1  | Node → Mac:1799 → container on OrbStack Docker (bridge, `-p 1799:1799/udp`) | learned `udp_target` = `192.168.68.71` (OrbStack's LAN forwarding may rewrite it: that is the question) |
| U2  | same with `network_mode: host` on OrbStack                                  | real source address                                                                                     |
| U3  | Node → Docker inside `deb-arm64` (needs a Mac → VM UDP forward)             | documents what a Pi/NAS with rootful Docker sees                                                        |
| U4  | same on `deb-amd64`                                                         | same as U3, amd64 image                                                                                 |
| U5  | McApp → node (outbound DM over Extern-UDP, transmits on air)                | node prints and keys the message                                                                        |
| W1  | Browser → Caddy TLS → Traefik → McApp, SPA loads                            | 200, SPA served by uvicorn                                                                              |
| W2  | SSE through Caddy and Traefik                                               | events arrive unbuffered, heartbeat every interval                                                      |
| W3  | Traefik `basicauth` in front                                                | 401 without, 200 with; SSE works in a desktop browser                                                   |
| W4  | Authelia same-origin cookie login                                           | redirect to login, cookie, SSE and API work after login                                                 |
| W5  | iPhone PWA checklist (operator)                                             | per the B12 paper's checklist                                                                           |
| B1  | Container → `ble_service` on Linux (route a)                                | connect, register reads, send, notifications via SSE                                                    |
| B2  | Container → macOS BLE service (route b) on the Mac                          | same as B1                                                                                              |
| P1  | Volume persists DB, `runtime.json`, `vapid.json` across recreate            | no data loss                                                                                            |
| P2  | Image tag swap (older → newer)                                              | migrations run, data kept                                                                               |
| P3  | Bare-metal DB copy (backup of 2026-10-20) into the volume                   | starts, history visible, secrets ask for re-entry                                                       |
| P4  | Log rotation per compose settings                                           | json-file capped at 10 MB x 3                                                                           |

## Status

| Wave | State    | Notes                                                                                                                                                           |
| ---- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CT0  | done     | DK5EN-1 = usbserial-0001; deb-arm64 Docker 26.1.5, deb-amd64 Docker 20.10.24                                                                                    |
| CT1  | a-c done | Spike image builds and smokes on arm64 + amd64 (health, SPA, SSE, read-only rootfs); node helper and proxy/Authelia stack written; d (macOS BLE design) running |

**CT1 findings.** Image size 228 MB (arm64) / 225 MB (amd64), not the 60-80 MB target: the Python
venv is 61.5 MB and the webapp bundle 112 MB, of which 107 MB are the Browser AI runtime
(`assets/litertlm_*.wasm`, four variants). Idle RSS 55 MiB on arm64 (amd64 107 MiB under QEMU
emulation, not a baseline). `/api/status` reports `v2.1.23` from package metadata while the SPA
came from the v2.1.22 tarball (B12 change 10). The node IP is not part of `--info`; it is printed
only in the WiFi boot line, so `node.py info` reports it only after a reset.
| CT2 | done (U4 blocked) | U1-U3, U5 run and repeated after the macOS Local Network fix; U4 needs Rosetta |
| CT3a | W1-W4 pass | W5 (iPhone) waits for a router DNS entry; CT3b (macOS BLE) running |
| CT3b | done | macOS BLE backend W1-W6 committed (99de2d0, 01a9416, 99921d3, 4baf50b, f468b7f); two advisor rounds; §7.2 shared contract suite deferred |
| CT5 | done | P1, P3, P4 pass (76a637f) |
| CT4a | done | macOS BLE service end to end against DK5EN-1 pass; two findings fixed with regression tests |
| CT4b | done | Linux `ble_service` (bluez) on rpizero against DK5EN-1 pass; scan gap found and fixed |
| U4 | deferred | dropped on the Mac (amd64 emulation inside an emulated VM does not start containers); retest on real x86 or a NAS |

**CT4a results (2026-10-31), `ble_service/scripts/run-macos.sh` on 127.0.0.1:8091, DK5EN-1 (fw 4.40a):**

- **Pass: health, scan, connect, registers.** `backend: bleak`, `address_kind: uuid`. Scan finds
  `MC-8968-DK5EN-1` (UUID `F4FAC724-...`, RSSI -58) and `MC-41b8-DK5EN-92`. Connect by UUID takes
  ~9 s; 14 registers are cached (I, IS1, SE, S1, SW, S2, SN, SN1, W, G, SA, IO, TM, AN). The
  terminal already held Bluetooth permission; no prompt and no PIN dialog appeared.
- **Pass: DM on air over BLE.** `POST /api/ble/send` to DK5EN-92: the echo
  `DK5EN-1>DK5EN-92:CT4a macOS bleak test{818`, two binary `0x41` peer ACK frames and the
  `:ack818` text from DK5EN-92 all arrive on `/api/ble/notifications`. `settime` answers
  `Time set (rule)`.
- **Pass: reconnect after node reboot.** `node.py reboot`: disconnect detected, auto-reconnect
  attempt 1/4 succeeds ~14 s after the reboot, no manual step.
- **Defined failure: second client.** A second instance (port 8093) does not see the node in a
  scan (a connected node stops advertising); its connect fails after 3 attempts in 32 s with
  `Device not found or not paired`, no hang; the first instance stays connected. The plain
  `/connect` route returns `error_code: null` here, as it does on Linux; codes come only from
  `/ensure_connected`.
- **Fixed: state file never written on macOS.** `~/Library/Application Support/mcapp-ble/` did not
  exist and neither `_save_ble_state` nor `_save_ble_pin` created it, so restart auto-connect could
  not work on a Mac. Both now create the parent directory. Verified live: a restart auto-connects
  on attempt 1.
- **Fixed: `device_name` empty after a connect.** The bleak adapter set `name=""` on connect
  although the connect lookup had the advertised name; `/api/ble/status` and `ble_state.json` now
  carry `MC-8968-DK5EN-1`.
- Not run: Mac sleep/wake, Bluetooth off/on, PIN cases (CoreBluetooth pairs through a system
  dialog; `pair`/`unpair` report unsupported by design).

**CT4b results (2026-10-31), `ble_service` (bluez 5.66) on rpizero, 127.0.0.1:8095:**

- **Setup.** rpizero had `bluetooth.service` masked and `hci0` down; unmasked, started and powered on
  for the test (undo in CT6). uv 0.13.6 and the service live in `~/ct-ble/`. Port 8081 is lighttpd's
  there, and sshd has `AllowTcpForwarding no`, so calls ran as curl on rpizero over ssh instead of a
  tunnel. mc-chat untouched.
- **Pass: health, connect, registers, DM, reboot.** `backend: bluez`, `address_kind: mac`. Connect to
  `48:CA:43:3A:89:69` in ~9 s with the name resolved, the same 14 registers as on the Mac. DM to
  DK5EN-92 on air: echo `{908`, two binary `0x41` peer ACKs and `:ack908` on the stream. Node reboot:
  auto-reconnect attempt 1/4 succeeds ~18 s later.
- **Fixed: the scan missed named devices.** The service reported only devices named in the pre-scan
  `GetManagedObjects` or in an `InterfacesAdded` signal. A stale nameless object never fires that
  signal again, and a name from the scan response arrives later by `PropertiesChanged`, which was
  never read: DK5EN-1 at -43 dBm was missed by every service scan while `bluetoothctl` saw it at once.
  The scan now re-reads the managed objects after discovery. Live after the fix: 8 of 8 scans match
  BlueZ's own device list. This affects production (mcapp.local's device picker), not just the test.
- **Remaining, radio level:** with the cache cleared DK5EN-1 is absent from BlueZ itself in about half
  of the 8 s scans on the Pi Zero 2W (shared WiFi/BT chip); not a service defect.

**CT5 results (2026-10-31), `scripts/container-tests/persistence/persist.sh`, ~45 s:**

- **P1 pass.** A synthetic message, `runtime.json` and `vapid.json` survive removing the container
  and starting a new one on the same volume (sha256 unchanged). The volume holds `messages.db` with
  `-wal`/`-shm`, `runtime.json` and `vapid.json`.
- **P3 pass (covers P2's migration path).** The bare-metal backup DB of 2026-10-20 (6191 messages)
  in a fresh volume: healthy after ~6 s, migrated to schema 39 (`LATEST_SCHEMA_VERSION`). The
  backup was already close to the terminus, so the migration delta was small. `secret.key` is not
  copied on purpose; QRZ, Node Admin and feed credentials need re-entry.
- **P4 pass (config only).** `json-file` with `max-size`/`max-file` is applied; rotation itself
  was not exercised.
- Note: there is no REST endpoint listing messages; the script counts them from the
  `proxy:conversations` connect burst over `/events`.

**CT3a results (2026-10-31), Caddy TLS → Traefik labels → spike container (arm64):**

| ID  | Result                                                                                                                                                                                                                                                               |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1  | Pass. `/` → 307 `/webapp/`, SPA 200, `/api/status` 200 through both proxies                                                                                                                                                                                          |
| W2  | Pass. SSE unbuffered through Caddy and Traefik (`flushinterval=-1`): first events within 20 ms, 26 lines in 0.4 s                                                                                                                                                    |
| W3  | Pass. Traefik `basicauth`: 401 without and with wrong credentials, 200 with; SSE streams with credentials, 401 without                                                                                                                                               |
| W4  | Pass. Authelia 4.39.28: no session → 302 to `auth-test` with `rd=`; login via `/api/firstfactor` sets an HttpOnly cookie for `.air-maxx.net`; API, SPA and SSE work with it; an XHR without session gets 401 (the webapp must answer it with a full-page navigation) |
| W5  | Open. The iPhone needs `mcapp-test` and `auth-test.air-maxx.net` resolved to the Mac (`192.168.68.58`) by the router's DNS; `/etc/hosts` only covers the Mac                                                                                                         |

**CT2 results (2026-10-31).**

| ID  | Setup                                                                                   | Result                                                                                                                                                                               |
| --- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| U1  | OrbStack Docker on the Mac, bridge, `-p 1799:1799/udp`                                  | **Trap confirmed.** First datagram from DK5EN-1 after ~170 s; McApp learned `192.168.215.1` (OrbStack's gateway), not `192.168.68.71`                                                |
| U2  | OrbStack Docker on the Mac, `network_mode: host`                                        | First run: nothing arrived (missing macOS Local Network permission). Repeat: frames arrive from `127.0.0.1`; learning refused (loopback), fails safe                                 |
| U3  | Rootful Docker 26.1.5 in `deb-arm64`, synthetic frame from the Mac                      | **Source kept.** Learned `192.168.139.3` (the real sender), via node identity. One extra datagram from `172.17.0.1` was seen once and ignored by the anti-flap guard; origin open    |
| U4  | Same in `deb-amd64`                                                                     | **Blocked.** Containers fail to start (`runc ... broken pipe`) in an amd64 VM under QEMU; needs Rosetta in OrbStack. The amd64 image itself passed the smoke test in OrbStack Docker |
| U5  | Container → `/api/send` → DK5EN-1 → on air (target pinned to `192.168.68.71` in config) | **Pass after a node reboot**: `[EXT] Inc` on the node, transmitted, and DK5EN-98 answered `:sto800 DK5EN-92` (store-and-forward held it)                                             |

Findings for the papers:

- **The startup warning heuristic in B12 is too narrow.** It looked for an `x.x.x.1` in
  `172.16.0.0/12`; OrbStack's gateway is `192.168.215.1`. The check must compare the source with
  the container's own default gateway (read from `/proc/net/route`).
- **On a Mac with OrbStack the UDP target must be pinned to the node's IP.** Bridge mode also needs
  learning off (B12 change `MCAPP_UDP_LEARN=0`), because it learns the gateway; host mode delivers
  from `127.0.0.1`, which the existing guard already refuses to learn.
- **Firmware: `--extudp on` does not start the EXT receive listener until a reboot.** Sending
  (`[EXT] Out`) works immediately, receiving (`[EXT] Inc`) only after `--reboot`. Worth a firmware
  issue or a note in the node setup docs.
- **Resolved: the missing inbound traffic was macOS, not Docker.** OrbStack had not been granted
  "Local Network" access; after the operator allowed it and OrbStack was restarted (`orb stop`,
  `orb start`), LAN UDP reached the containers again. Before the restart not even a datagram to
  `127.0.0.1:1799` was forwarded. Setup note for Mac users: grant OrbStack (or Docker Desktop)
  Local Network access and restart it.
- **Repeat after the fix:** U1 bridge: node frames (relayed `DF2SI-12,DK5EN-98`) arrive after
  ~110 s from `192.168.215.1`, the same address as a datagram sent from the Mac itself, so the
  rewrite is confirmed. U2 host network: node frames arrive after ~110 s from `127.0.0.1`;
  McApp's existing guard refuses to learn a loopback source and logs that `MESHCOM_IOT_TARGET`
  must be set explicitly.
