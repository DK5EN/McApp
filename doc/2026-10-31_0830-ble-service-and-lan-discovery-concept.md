# Standalone BLE service and LAN auto-discovery - concept paper

Status: Ideation, backlog, 2026-10-31. No code, no decisions beyond the operator constraints below.

## BLUF

- `ble_service/` can run on any Linux box with a BLE adapter in the LAN; McApp (Pi, NAS or container) talks to it over HTTP. The service is already self-contained (no `mcapp` imports); what is missing is packaging, a safe LAN default, a version contract and a pairing story.
- Recommended packaging: a small `mcapp-ble.sh` installer that runs `uv tool install` from a git tag into a dedicated system user, writes the systemd unit and generates the API key. A `.deb` comes later, only if the audience outgrows the shell installer.
- Security flip: the service must refuse to start on a non-loopback bind without a real API key. Today an empty key or `"disabled"` means auth off, and the standalone entry point already binds `0.0.0.0` with CORS `*`.
- Discovery: DNS-SD (mDNS). The BLE service publishes `_mcapp-ble._tcp` through the host's avahi over D-Bus (dbus-next is already a dependency); McApp browses with python-zeroconf, loaded only while Settings is open. McApp itself publishes `_mcapp._tcp` through a static avahi service file.
- The MeshCom node is only partly discoverable: the `fork-dev` firmware answers to `<CALL>.local` only while its web server runs, and advertises no service. McApp already learns the node's IP passively from Extern-UDP traffic; discovery adds display and a firmware ask, not a new mechanism.
- Security gate: the new routes that change the BLE target are LAN-only (Node Admin Host/Origin guard), because McApp's API has no auth and sends the stored key to whatever URL is configured.
- Operator rule holds throughout: the BLE URL, key and mode are selected and paired in Settings (GUI), discovery fills a picker, manual URL entry stays as the fallback. BLE stays native, never inside the McApp container (see `doc/2026-10-31_0830-container-deployment-concept.md`).

## Operator constraints (decided, not up for debate here)

- Deployment is always LAN. No remote or internet install path, no relay.
- McApp may run on a Pi, a home NAS or any Linux box, natively or in a container.
- BLE is a native service on a host with a Bluetooth adapter; it never moves into the McApp container (no hardware, no BlueZ there).
- No hand-edited `config.json` switches: every setting a feature needs is reachable in the web GUI.

## Current state (verified 2026-10-31)

### BLE service

- Dependencies: fastapi, uvicorn, uvloop, httptools, sse-starlette, dbus-next, pydantic (`ble_service/pyproject.toml:11-23`). BlueZ is reached through dbus-next only; there is no bleak.
- No import of `mcapp` anywhere under `ble_service/src` (grep, empty), so the service is already separable at the code level.
- Console entry is `ble-service = "src.main:app"` (`ble_service/pyproject.toml:33`): that points at an ASGI object, not a callable, and the wheel packages a top-level package named `src` (`pyproject.toml:40-41`). It has never been built as a standalone artifact (unverified that the wheel installs cleanly); a real `main()` and a unique package name are prerequisites for any `uv tool install`. Evidence that no working entry exists today: `__main__` calls `uvicorn.run("main:app", ...)` (`main.py:2169-2170`), which resolves only with cwd `ble_service/src`; the README quick start (`uv run uvicorn ble_service.src.main:app` from `~/mcapp/ble_service`, `README.md:11-19`) does not resolve from the cwd it names either (`ble_service.src` is importable only from the repo root). The Pi unit works because its `WorkingDirectory` is the repo root (`mcapp-ble.service`, `uvicorn ... ble_service.src.main:app`).
- Production unit `bootstrap/templates/mcapp-ble.service`: runs uvicorn on `--host 127.0.0.1 --port 8081` (`:24`), `ProtectSystem=strict`, `NoNewPrivileges`, `ReadWritePaths` including `/var/lib/mcapp`. `rfkill unblock` and `bluetoothctl power on` are bounded and non-fatal `ExecStartPre` (`:17-19`).
- `configure_bluetooth` (`bootstrap/lib/system.sh:950-996`) writes a separate `unblock-bluetooth.service` oneshot; a standalone installer has to reproduce the equivalent.
- State file default is `/var/lib/mcapp/ble_state.json` (`ble_service/src/main.py:166`): a non-McApp host needs its own state dir and unit `ReadWritePaths`.
- `if __name__ == "__main__"` runs uvicorn on `0.0.0.0`, port from `BLE_SERVICE_PORT` (`main.py:2165-2175`, `# noqa: S104` "LAN service binds all interfaces by design"). The README quick start also uses `--host 0.0.0.0`.
- CORS: `BLE_SERVICE_CORS_ORIGINS` defaults to `*` (`main.py:165`).
- A second, stale unit copy lives at `ble_service/mcapp-ble.service` (hard-coded user `martin`, `uv run`, empty key); it should not become the standalone template.
- `/health` returns only `status`, `ble_connected`, `timestamp` (`main.py:2155-2163`); the FastAPI app version is the placeholder `0.1.0` (`main.py:1325`) while `pyproject.toml` carries `2.1.23`. No `api_version` exists.

### macOS backend (CT3b/CT4a, 2026-10-31)

- `ble_service` also has a macOS backend (bleak/CoreBluetooth): `ble_service/scripts/run-macos.sh`,
  `BLE_BACKEND=bleak`. **Dev/test host only**, not a production target. Design:
  `doc/2026-10-31_1230-ble-service-macos-backend-design.md`.
- Verified end to end in CT4a against DK5EN-1 (connect, 14 registers, DM plus ACKs, reconnect after
  node reboot). CoreBluetooth reports a UUID, never a MAC (`address_kind: uuid`); `pair`/`unpair`
  are unsupported by design (the system dialog pairs).
- `run-macos.sh` refuses a non-loopback bind without an API key (at least 16 characters, not
  `disabled`).

### Auth

- Shared secret in header `X-API-Key`, env `BLE_SERVICE_API_KEY` (`main.py:164`), constant-time compare (`main.py:1342-1350`).
- Empty or `"disabled"` means auth OFF (`main.py:1346-1347`); `/health` is unauthenticated; the SSE route checks inline (`main.py:2106`).
- Guarded endpoints include `/api/ble/pair` (`:1884`), `/api/ble/send` (`:1830`), `/api/ble/config/callsign` (`:1951`) and `/api/ble/config/wifi` (`:1971`). With auth off on a LAN bind, any host can rewrite the node's WiFi credentials or transmit on air as the operator.
- The McApp bootstrap already generates a 16-character key (`bootstrap/lib/config.sh:20-24`, `:315-324`) and rotates weak placeholders (`:448+`); that logic is Pi-bootstrap-only and has no standalone equivalent.

### McApp client

- `src/mcapp/ble_client_remote.py`: httpx, key header at `:192` (REST) and `:719-720` (SSE), SSE loop around `:715-781` with exponential backoff (`:181`, `:771-775`).
- URL constant `BLE_SERVICE_URL = "http://127.0.0.1:8081"` (`src/mcapp/config_loader.py:117`); override only by env `MCAPP_BLE_URL`, read at startup (`src/mcapp/main.py:2974`, `:3533`). Mode `remote|disabled` and key come from env `MCAPP_BLE_MODE` / `MCAPP_BLE_API_KEY`, else `config.json` keys `BLE_MODE` / `BLE_API_KEY` (`config_loader.py:131-135`, `:372-379`).
- There is no GUI path for the BLE URL, key or mode today: all three are env or hand-edited `config.json`, which the operator rule forbids for a feature like this.
- The client is built once at startup and captured in several places (see section 5 for the swap cost).
- `_request` retries any 409 (`ble_client_remote.py:240-249`) and the service already uses 409 for "busy" and "not connected" (`ble_service/src/main.py:1512`, `:1627`, `:1835`, `:1892`).
- Recovery timing of the client's own SSE loop: backoff 5 s doubling to a 60 s cap, read timeout 90 s (`ble_client_remote.py:37-39`, `:61`).
- `_headers()` sends `X-API-Key` to whatever `remote_url` is configured (`:188-193`).

### Discovery today

- No mDNS, zeroconf or DNS-SD code in the tree (grep). `avahi-daemon` appears only as the `.local` resolver: kept enabled (`bootstrap/lib/system.sh:121`) and health-checked (`bootstrap/lib/health.sh:274-319`).
- avahi is NOT installed by the bootstrap: the package list (`bootstrap/lib/packages.sh:277-299`) has `bluez`, `bluetooth`, `curl`, `jq` and others but no `avahi-daemon` and no `libnss-mdns`. avahi is assumed preinstalled (true on Raspberry Pi OS; not guaranteed on Armbian, a NAS or a minimal server image). The health check merely warns when it is absent (`health.sh:409-411`).
- The live firewall is the heredoc written to `/etc/nftables.conf` in `bootstrap/lib/system.sh` (`nft_conf` at `:668`, ruleset `:732` onward; iptables fallback `configure_iptables_legacy` at `:854`). `bootstrap/templates/nftables.conf` is DEAD: nothing in `bootstrap/` installs it, so its line numbers must not be cited for behaviour.
- Open inbound ports on a bootstrapped box: 22/tcp (rate limited, LAN exempt, `:767`, `:770`), 80/tcp (`:773`), 443/tcp (`:776`), 443/udp QUIC (`:779`), 2985/tcp update stream (`:782`), 19532/tcp journal cross-shipping, LAN only (`:785`), 1799/udp node (`:788`), plus multicast `224.0.0.251` `udp dport 5353` (`:792`; iptables twin `:904`). The header comment lists 5353/udp mDNS (`:742`).
- The same ruleset later drops `udp sport 5353` and `udp dport 5353` (`:815-816`). The earlier accept (`:792`) wins for multicast, but unicast mDNS replies (QU bit, legacy one-shot queries) to the box are dropped. Browsing from a bootstrapped Pi must be tested against exactly this (unverified).
- Nothing opens 8081, so a BLE service on a bootstrapped McApp Pi is not reachable from the LAN by design.

### The node

- Target: `UDPConfig.target` hostname or callsign (`config_loader.py:124-127`), keys `MESHCOM_IOT_TARGET` / `UDP_TARGET` (`:361-364`), port fixed 1799 (`:113`).
- Correction to the working assumption "nothing reads the node's source IP": `udp_handler` already learns the node's address from inbound datagrams (`_learn_target_from_source`, `src/mcapp/udp_handler.py:868`; `_adopt_target`, `:779`; persisted to `/var/lib/mcapp/runtime.json`, `:837`), restricted to RFC1918 and link-local sources (`:370-381`) and debounced (60 s, `:48-62`). Discovery must feed this logic, never bypass it.
- Firmware (`MeshCom-Firmware-DEV-Main`, checkout is branch `fork-dev`, not official or production firmware; findings below describe that tree):
  - `MDNS.begin(meshcom_settings.node_call)` at `src/web_functions/web_functions.cpp:344`, inside `startWebserver()`, guarded by `bMDNSOK` (`:269`).
  - `startWebserver()` is called from the main loop only `if(bWEBSERVER && iWlanWait == 0)` (`src/esp32/esp32_main.cpp:5224-5228`), and `stopWebserver()` calls `MDNS.end()` (`web_functions.cpp:398`). Settled: with the node's web server off there is NO `<CALL>.local` at all.
  - No `MDNS.addService` on the node: the only `addService` for DNS-SD is the safeboot OTA page, `_http._tcp` (`src/safeboot/main.cpp:366`); the other hits are BLE GATT UUIDs (`src/esp32/esp32_main.cpp:605`, `src/nrf52/nrf52_ble.cpp:186`).
  - The firmware states the convention: the mDNS name equals `node_call` (`src/configuration_global.h:73-75`).
  - Consequence: the node is findable by name, only while its web server runs, and never by browsing a service type.

## Design

### 1. Standalone packaging

| Option                                | Install effort          | Updates               | Fit for a NAS or generic Linux                                       | Verdict                        |
| ------------------------------------- | ----------------------- | --------------------- | -------------------------------------------------------------------- | ------------------------------ |
| `uv tool install git+...@vX.Y.Z`      | one command, needs uv   | re-run with new tag   | good (uv self-contained)                                             | engine of the installer        |
| `pipx install` from git tag           | one command, needs pipx | `pipx upgrade` w/ tag | good, but a second tool                                              | equivalent, no gain over uv    |
| `mcapp-ble.sh` (uv tool + unit + key) | one curl, one prompt    | `mcapp-ble.sh update` | best: handles unit, user, key                                        | recommended                    |
| `.deb` package                        | `apt install`           | apt                   | Debian family only; needs a signed APT repo, built in Actions (nfpm) | **decided 2026-10-31: target** |
| Docker image for the BLE service      | n/a                     | n/a                   | needs host BlueZ socket, privileged mounts                           | rejected by operator rule      |

- **Decision 2026-10-31 (operator): the BLE service becomes the first Debian package**, ahead of McApp itself (which stays on `mcapp.sh` until the container has shipped; `doc/2026-10-31_1100-ansible-and-web-server-evaluation.md`, Question 3). The `mcapp-ble.sh` installer below remains the bootstrap path until the APT repository exists, and the `.deb` moves from "optional" to a planned wave.
- Recommendation: `mcapp-ble.sh`, a single script (`install`, `update`, `show-key`, `status`, `uninstall`) following the style of `bootstrap/mcapp.sh` but with none of its system-wide side effects (no firewall rewrite, no hostname, no Caddy).
- Pinned to a release tag, never `main`, so a LAN box cannot drift ahead of its McApp. The installer takes `--tag`, defaulting to the newest release asset.
- Resulting host layout (all outside any McApp path):
  - system user `mcapp-ble` in group `bluetooth` (no login shell); the stock Debian / Raspberry Pi OS D-Bus policy grants `org.bluez` access to that group, so no custom policy file is needed there;
  - tool env under `/opt/mcapp-ble` (uv tool dir), state in `/var/lib/mcapp-ble`, key in `/etc/mcapp-ble/env` (mode 0600, root-owned, read through `EnvironmentFile=`);
  - unit `mcapp-ble.service` derived from `bootstrap/templates/mcapp-ble.service`: same hardening, same bounded `ExecStartPre` rfkill / power-on, paths and bind from the env file.
- Package changes needed before this works (wave 1): a real `main()` entry reading `BLE_SERVICE_BIND` / `BLE_SERVICE_PORT`; a unique package name instead of top-level `src`; `BLE_STATE_FILE` default moved off `/var/lib/mcapp`. The Pi deploy path (`ble_service.src.main:app`, WorkingDirectory-dependent, per CLAUDE.md memory-footprint notes) must keep working unchanged: the new entry is additive.
- The standalone `ble_service/uv.lock` must be regenerated outside the workspace (inside it `uv lock` defers to the root lock); `scripts/release.sh` already keeps both locks' version entries in step (`release.sh:658-661`).

Host prerequisites the installer checks and reports, never silently fixes:

| Prerequisite                 | Check                                         | Action if missing                                                                   |
| ---------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------- |
| BlueZ + `bluetoothd` running | `systemctl is-active bluetooth`               | print the distro install line                                                       |
| System D-Bus                 | `/run/dbus/system_bus_socket`                 | abort                                                                               |
| Adapter present              | `bluetoothctl list` / `rfkill list bluetooth` | abort with "no adapter" (clearest failure)                                          |
| D-Bus access to `org.bluez`  | try an `org.bluez` call as `mcapp-ble`        | add the user to group `bluetooth`; on a distro without that policy, report and stop |
| `uv`                         | `command -v uv`                               | offer the official installer, ask first                                             |
| Bind address and port free   | `ss -ltn`                                     | abort                                                                               |

- Pairing/bonding to the node is done by the service through BlueZ with the PIN flow in `doc/ble-pin.md`; no change there.
- The installer prints, but does not apply, the firewall line for the chosen port (`ufw allow from <lan> to any port 8081`, nft equivalent), because the host's firewall is not ours to edit.

### 2. Bind address and version contract

- New env `BLE_SERVICE_BIND` (default `127.0.0.1`). The McApp-on-same-Pi path stays loopback. The standalone installer asks "serve other hosts?" and writes the LAN address or `0.0.0.0`.
- `/health` grows `version` (package version, replacing the `0.1.0` placeholder), `api_version` (integer, bumped only on a wire-incompatible change), `id` (random instance id persisted in the state dir), `auth` (`required` or `off`). It stays unauthenticated, therefore carries no secrets, no node MAC, no callsign beyond what mDNS already advertises.
- Compatibility rule (proposal): McApp declares a supported `api_version` range. Inside it, anything goes. Above its maximum, the GUI shows "BLE service is newer, update McApp" and disables connect. Below its minimum, "update the BLE service (`mcapp-ble.sh update`)". Package versions are informational only; the two sides release independently (same tag cadence, but a NAS box may update one without the other).
- A health-based test in `scripts/ble_service_tests.py` pins the field set so it cannot silently disappear.

### 3. Security

- **Bind guard gap (verified 2026-10-31).** The refusal rule lives in `_check_bind_security`, called
  only from the `if __name__ == "__main__"` block of `ble_service/src/main.py` (and mirrored in
  `run-macos.sh`). `bootstrap/templates/mcapp-ble.service` starts `uvicorn ble_service.src.main:app
--host 127.0.0.1 --port 8081` through the uvicorn CLI, so the guard does not apply there. Harmless
  today because the host is hard-coded loopback; it matters as soon as a unit or container binds
  wider. Closing it needs the wave-1 `main()` entry point that runs the check on every start path.

Threat model: LAN is semi-trusted (guest WiFi, IoT devices, a compromised laptop). The BLE service can transmit as the operator and reconfigure the node, so unauthenticated LAN exposure is a real risk, not hygiene.

- Rule: bound to anything other than loopback, the service refuses to start unless `BLE_SERVICE_API_KEY` is set, at least 16 characters, and not `disabled` or a known placeholder. Loopback keeps today's behaviour (the Pi's bootstrap already provisions a key regardless).
- Key lifecycle on a standalone host: generated by the installer (same alphabet and length as `generate_ble_api_key`), stored root-only, shown once by the installer and again on demand by `mcapp-ble.sh show-key` (local root access is the trust anchor, matching the "code running as the service user can decrypt" posture elsewhere in McApp).
- CORS default `*` is removed for the standalone profile: browsers never talk to the BLE service (McApp does, server to server), so the default can be empty.
- No auth on `/health` stays: it carries only what discovery advertises anyway.

Pairing options (McApp learns URL and key):

| Option                               | Flow                                                                                                                                 | Pros                              | Cons                                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------- | ------------------------------------------------------------------------------------------- |
| A. Paste key (v1)                    | Discovery or manual URL, operator pastes key from `show-key`                                                                         | trivial, no new endpoint, no race | typing a 16-char key; fine once                                                             |
| B. One-time pairing code             | `mcapp-ble.sh pair` opens a 5-minute window and prints a 6-digit code; McApp posts the code to `/api/ble/claim` and receives the key | short to type, key never typed    | new unauthenticated-by-design endpoint (rate limited, one active code); more code and tests |
| C. Trust on first discovery, confirm | McApp lists service, operator clicks "pair", service shows a confirmation on its console                                             | no typing                         | needs console interaction on a headless box; weakest model                                  |

- Recommendation: A in v1, B as an additive follow-up (same storage, the claim endpoint only hands out the existing key). C rejected: headless boxes have no console to confirm on.
- TXT records never carry the key or any part of it.
  McApp-side exposure (new routes `PUT /api/ble/service`, `GET /api/discovery/ble`, the `/health` validation and the discovery probes):

- McApp's own API has no authentication and is internet-reachable in the public-TLS modes (CLAUDE.md "Node Admin"). An unauthenticated `PUT /api/ble/service` would let anyone repoint McApp at a host they control; `_headers()` would then send the stored `X-API-Key` to the attacker's host. The `/health` validation call and the discovery probes are an SSRF surface for the same reason.
- Fix, three parts: (1) put these routes behind the Node Admin guard (`_guard` in `src/mcapp/sse_routes/node_admin.py:135`, built on `host_allowed` / `origin_allowed`, `:68`, `:85`), which refuses any foreign `Host` and `Origin`, so they are LAN-only in every mode; (2) any URL change forces key re-entry (the stored key is never re-sent to a new URL), and the validation target must be an RFC1918 / link-local / loopback address or a `.local` / bare LAN name, never a public host; (3) the key is write-only: GET returns `key_set: true`, never the key. The body of the key route is withheld from `stall_events` by path prefix, as for Node Admin.

- TLS on the LAN: out of scope. Reasoning: the key travels in clear on the home LAN, which is the same exposure class as the existing Extern-UDP port 1799 (unauthenticated, plain) and the lighttpd-to-API hop; self-signed certificates would add trust-store prompts on every NAS and break the "one command" install, and the real defences are the mandatory key and LAN-only deployment. Revisit if an operator wants VLAN-crossing installs; a Caddy in front of the BLE service is the documented path (already in `ble_service/README.md` security notes).

Single owner (the BLE link has one controller):

- Today the notification stream is a module-level deque shared across SSE clients: a second consumer "steals events round-robin" (docstring in `main.py:2110-2116`). Two McApps pointed at one BLE service would therefore split the message stream and both appear to work while each misses half the traffic. That is the worst failure shape: silent.
- Proposal: McApp sends `X-Client-Id` (its instance id) on every request. The service records the owner on the first successful SSE attach or `connect`, advertises `owner_id` and `owner_name` (hostname) in `/health` and TXT, and answers a second client's SSE attach or write with `423 Locked` plus the owner's name. NOT 409: 409 already means "busy / not connected" on this wire and the client retries any 409 (`ble_client_remote.py:240-249`), so a 409 for "owned" would be retried in a loop. Alternative is a `reason: "owned"` body, which the client must be changed to treat as terminal; 423 is recommended because it needs no body parsing to avoid the retry. Takeover is explicit: `POST /api/ble/takeover` (key-authenticated), triggered by a GUI button "Take over from `<name>`", after which the old owner's SSE is closed and its client enters its normal backoff, showing "claimed by another McApp".
- Owner state expires with the SSE connection after a grace period that must exceed the owner's OWN recovery: SSE backoff reaches 60 s and the read timeout is 90 s (`ble_client_remote.py:39`, `:61`), so a healthy owner can legitimately be detached for about 150 s. Grace must be > 150 s (proposal 180 s); the 60 s first sketched would hand the link to a second McApp while the first is merely backing off.
- Interaction with the service's own `BLE_AUTO_CONNECT` (`main.py:167`, task started at `:1300`): auto-connect to the last-known device does not need an owner and runs at service start, so a service with no owner can already hold the BLE link. "Owner on first connect" therefore means the first SSE attach or write, not the BLE connect; an ownerless service that is already connected is claimed by the first client that attaches, and an expired grace returns it to ownerless without disconnecting the node.
- Read-only observers (`/health`, discovery) are always allowed.

### 4. Discovery

DNS-SD service types:

| Type              | Publisher        | Port                           | TXT                                                                                                      |
| ----------------- | ---------------- | ------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `_mcapp._tcp`     | McApp host       | 80 (443 where Caddy fronts it) | `v` (version), `id`, `node` (callsign), `path=/`                                                         |
| `_mcapp-ble._tcp` | BLE service host | 8081                           | `v`, `api` (api_version), `id`, `auth=required`, `node` (only if connected), `owner` (hostname or empty) |

- Instance names: `McApp on <hostname>` and `McApp BLE on <hostname>`. `id` disambiguates two services on one host (and two McApps behind NAT'd containers).
- TXT is advisory and unauthenticated: the GUI treats it as a hint, shows the callsign as "reported", and confirms against `/health` after the URL is picked. Nothing in TXT can trigger an action.

Implementation options:

| Option                                                | Publish                                        | Browse   | Memory / deps                                                                                                   | Container story                                                                                               | Verdict                      |
| ----------------------------------------------------- | ---------------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| python-zeroconf in-process                            | yes (dynamic TXT)                              | yes      | pure Python plus ifaddr; a few MB RSS on a 415 MB Pi Zero 2W with a tight budget (CLAUDE.md "Memory Footprint") | needs host network or macvlan; own 5353 socket                                                                | browse side only, lazy       |
| avahi service files `/etc/avahi/services/*.service`   | yes, static TXT only (reload by avahi inotify) | no       | zero RAM, no dependency, but needs avahi-daemon                                                                 | works through host avahi                                                                                      | McApp publish, static fields |
| avahi over D-Bus (`org.freedesktop.Avahi` EntryGroup) | yes, dynamic TXT (callsign, owner)             | possible | zero new deps in ble_service (dbus-next present)                                                                | needs the host D-Bus socket; the BLE host has it by definition                                                | BLE service publish          |
| Own UDP broadcast/multicast beacon                    | yes                                            | yes      | custom protocol, ours to maintain                                                                               | same multicast problem as mDNS; adds a second discovery mechanism that no phone or `avahi-browse` understands | last resort fallback only    |

Recommendation:

- BLE service publishes via avahi over D-Bus (dynamic TXT, no new dependency, no port-5353 conflict with the host daemon). If avahi is absent it logs one line and runs without advertising; manual URL still works.
- McApp publishes `_mcapp._tcp` with a static avahi service file written by bootstrap (and by the container recipe via host avahi, see the container paper). Static TXT is enough: `v`, `id`, `node` rarely change. If avahi is absent, McApp is still reachable by IP; nothing breaks.
- McApp browses with python-zeroconf (`AsyncServiceBrowser`), started on demand when the Settings "Find BLE services" panel opens and stopped after a timeout (e.g. 15 s scan, results cached for the session). It is never resident, which keeps the idle footprint of the Pi Zero unchanged.
- python-zeroconf next to a running avahi means two processes with a 5353 socket (both use `SO_REUSEPORT`), which works but doubles multicast handling on the host. Where D-Bus and avahi exist, browsing through avahi's D-Bus API (`ServiceBrowser`) is the alternative; python-zeroconf stays the choice where there is no avahi (container with host networking, macOS dev).
- Own UDP beacon: not built. If field reports show mDNS blocked broadly (client isolation, VLANs), reconsider.
- Adding `avahi-daemon` to the bootstrap package list is a small separate decision (it is assumed, not installed, today; `packages.sh:277-299`). Needs a system-epoch bump per CLAUDE.md "System Epoch" if done.
- Firewall on a bootstrapped Pi: multicast replies work through `system.sh:792`; unicast replies are dropped by `system.sh:815-816` and need a test. Fix would be a narrow `udp sport 5353` accept for established flows, not removing the drops.

Container caveat (details in `doc/2026-10-31_0830-container-deployment-concept.md`):

- Multicast does not cross Docker's default bridge network. Options for a containerised McApp: `network_mode: host`; a macvlan network (host cannot reach the container directly); or publishing via the host's avahi through a mounted D-Bus socket / service file. Browsing from inside a bridged container finds nothing, so the GUI must say "no services found; enter URL" rather than "none exist".
- Because discovery is a convenience, the container paper should treat manual URL as the baseline and host networking as an upgrade.
- VLANs and a NAS on another segment: multicast does not route. Documented upgrade path: avahi `enable-reflector=yes` (`avahi-daemon.conf`) on a host in both segments, or the router's mDNS reflection / Bonjour gateway. Neither is installed or configured by us; manual URL works across VLANs without any of it, and the BLE service needs only the TCP port reachable.

Discovering the node itself:

- mDNS name: the node publishes `<CALL>.local` (`web_functions.cpp:344`, `configuration_global.h:73-75`) only while its web server runs (`esp32_main.cpp:5224-5228`; `stopWebserver()` calls `MDNS.end()`, `web_functions.cpp:398`). With the node web server off there is no `.local` name.
- McApp does NOT resolve the configured callsign by itself. The default target is the bare `DX0XXX-99` (`config_loader.py:127`) and a bare name goes to the OS resolver with no `.local` appended. `.local` resolution additionally needs `libnss-mdns` (the bootstrap installs neither it nor avahi-daemon) and fails inside bridged containers. Discovery would have to resolve `<CALL>.local` itself (python-zeroconf, `A` query) and offer the address as a suggestion; it never rewrites the target silently.
- Service browsing finds nothing on the `fork-dev` firmware (no `addService`). Safeboot advertises `_http._tcp` (`safeboot/main.cpp:366`) only during OTA, which is a different mode.
- Passive learning (existing, `udp_handler.py:868`) is the reliable path: the node's IP appears the moment it sends one datagram. Proposal: expose the learned address (already persisted in `runtime.json`) and its last-seen time in the Settings node card, so the operator sees what McApp believes without opening a shell.
- Firmware ask (backlog, not ours to decide): `MDNS.addService("meshcom", "udp", 1799)` with TXT `call`, `fw`, so a browse finds the node. Cost to firmware: one line in `web_functions.cpp` after `MDNS.begin`. Until then, node discovery stays name-based.

### 5. GUI flow

Settings, "Bluetooth" section (new), no config-file edit anywhere:

1. State line: current BLE URL, mode, health, `api_version` verdict, owner.
2. "Find BLE services" runs a scan; results are a picker listing name, host, version, `auth`, connected node callsign, owner. Greyed with a reason if `api` is out of range or the service is owned.
3. Picking opens the key dialog (option A: paste; option B later: pairing code). Save validates by calling `/health` and an authenticated `/api/ble/status`; wrong key shows the 401 inline, nothing is stored.
4. "Enter URL manually" is always visible: `http://host:8081` plus key. It is the fallback for blocked mDNS, containers on bridge networks and VLANs.
5. URL, key and a mode toggle (`remote` / `disabled`) are persisted by the backend (runtime state next to `runtime.json`, not `config.json`). Today `BLE_MODE` and `BLE_API_KEY` come only from env or `config.json` (`config_loader.py:372-379`); the GUI becomes the primary path. Precedence for URL, key and mode: env (`MCAPP_BLE_URL`, `MCAPP_BLE_API_KEY`, `MCAPP_BLE_MODE`) > GUI runtime state > `config.json` > default. A value pinned by env is shown read-only with the reason.
   5a. Runtime swap cost. The BLE client is captured at startup in: Node Admin's connected-probe lambda (`main.py:2777`), the internet-feed identity (`:2792`), the stall gauge (`:3018-3019`), `AppContext.ble_client` (`:3028`), and the message router registry (`register_protocol("ble_client", ...)`, resolved per call at 7 sites via `get_protocol`). With `mode=disabled` no client is built at all (`:2972`), so enabling from the GUI has nothing to swap into. Options: (a) a `BleClientHolder` registered once, always present, delegating to the current client (or a disabled stub) and swapping it under a lock, with the captured sites pointed at the holder; (b) accept a McApp service restart on every BLE change. Recommendation: (a), because the GUI must not restart a service that is also serving the operator's chat; fall back to (b) in wave 3 only if the holder proves leaky (start/stop ordering of the SSE task, cached registers).
6. The webapp is a separate repo; the GUI work and the new backend endpoints (`GET /api/discovery/ble`, `PUT /api/ble/service`, both behind the LAN-only guard, section 3) are a contract change shared across both. The key route's body is withheld from `stall_events` the same way as other secret routes (see CLAUDE.md "Stall Tracking" and "QRZ"); regression test with a truncated non-JSON body.

## Phases and file ownership

| Wave | Content                                                                                                                                                              | Files (owner)                                                                                                                                                                        |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1    | BLE service hardening: `main()` entry, unique package name, `BLE_SERVICE_BIND`, refuse non-loopback without key, `/health` fields, owner/`X-Client-Id`, CORS default | `ble_service/src/main.py`, `ble_service/pyproject.toml`, `ble_service/uv.lock` (regenerated outside workspace), `ble_service/README.md`, `scripts/ble_service_tests.py`              |
| 2    | `mcapp-ble.sh` installer and unit template, release tarball/asset wiring                                                                                             | new `bootstrap/mcapp-ble.sh` + `bootstrap/templates/mcapp-ble-standalone.service`, `scripts/release.sh`, `scripts/ble_unit_template_tests.py`, `scripts/webapp_deploy_tests.py` pins |
| 3    | Client: `api_version` negotiation, 423 handling, runtime BLE target (URL, key, mode) via a holder object, LAN-only guard on the new routes                           | `src/mcapp/ble_client_remote.py`, `src/mcapp/config_loader.py`, `src/mcapp/main.py`, new runtime-state module                                                                        |
| 4    | Discovery: avahi D-Bus publisher in BLE service, McApp avahi service file, on-demand zeroconf browser, `/api/discovery/*`                                            | `ble_service/src/` (new module), `src/mcapp/discovery.py` (new), `src/mcapp/sse_routes/` (new route), `bootstrap/lib/`, optional `avahi-daemon` + system epoch bump                  |
| 5    | Webapp Settings panel (separate repo, own waves), contract doc, node card with learned IP                                                                            | webapp repo; `doc/`                                                                                                                                                                  |
| 6    | `.deb` for `mcapp-ble` (decided) plus the signed APT repository; optional pairing code (option B), firmware `addService` request                                     | later                                                                                                                                                                                |

- Waves 1 and 2 are independently useful (a LAN BLE box with manual URL) and ship before any discovery work. Wave 4 is deliberately last: discovery is a convenience over a working manual path.
- Tests: each wave adds suites to `scripts/run_startup_tests.py` (a suite not wired into `main()` is gated by nothing). The auth-refusal rule needs a mutation-pinned test (loopback allowed, LAN refused with empty / `disabled` / short key).

## Risks

- A second McApp silently splitting the notification stream (mitigated by the ownership protocol; until wave 1 lands it is a documented hazard).
- mDNS blocked or flaky (AP isolation, VLANs, bridged containers, Pi firewall dropping unicast replies): mitigated by the manual fallback being first-class, not by retries.
- Version skew on a NAS box updated at a different time than McApp: mitigated by `api_version` and a GUI verdict.
- Key leakage through logs, stall events or TXT: the key route body is withheld from `stall_events`; installer output is the only place the key is printed.
- Memory on the Pi Zero 2W if zeroconf became resident: kept lazy.
- BlueZ variance across distros (older BlueZ on a NAS, `bluetoothd` without experimental features): the installer reports the version; a support matrix is an open question.
- Wheel packaging of a package that has never been built standalone: the first install attempt is the real test, so wave 1 includes a clean-room `uv tool install` check on a scratch Linux box.

## Out of scope

- Remote or internet BLE hosts, relays, port forwarding.
- TLS between McApp and the BLE service (reasoning above).
- Running BLE inside the McApp container (operator decision).
- Several BLE adapters or several nodes per BLE service.
- Discovering other MeshCom nodes on the mesh (that is RF, not LAN).
- Any firmware change (only a documented request).
- Fleet management (a single operator's few boxes, not an inventory).

## Open questions

1. (Researched 2026-10-31) `uv tool install "git+https://github.com/DK5EN/McApp@vX.Y.Z#subdirectory=ble_service"` should work (the `#subdirectory=` URL form is documented for uv dependencies, tag pinning with `@ref` for tools; the combination for `uv tool` is inferred, test once). Three facts: (a) `ble-service = "src.main:app"` points at the FastAPI object, which a console script would call and fail, so wave 1 needs a real `main()` running `uvicorn.run(app, ...)`; (b) the wheel installs a top-level package named `src`, which must become `mcapp_ble` (`[project.scripts] mcapp-ble = "mcapp_ble.main:main"`), and the unit templates' `ble_service.src.main:app` path moves with it; (c) `uv tool install` does NOT honour `ble_service/uv.lock`, so dependency versions float; pin with constraints from `uv export`. With the `.deb` decided, the lockfile matters less: the package build runs `uv sync --frozen` and ships the venv.
2. (Decided 2026-10-31: add them.) `avahi-daemon` and `libnss-mdns` become explicit bootstrap dependencies, with a system-epoch bump. RAM impact on today's fleet is zero: both are already installed and `avahi-daemon` is already running on mcapp.local and rpizero, from the Raspberry Pi OS image. Declaring them only protects images that do not ship them.
3. Runtime re-pointing of the BLE client: holder object (recommended, section 5 item 5a) or restart; the holder's start/stop ordering against the SSE task and cached registers is the thing to prove.
4. Should `owner` be per McApp instance id or per client IP? Instance id survives DHCP changes; IP is not spoofable by a header. Proposal: id, since the key is the trust anchor anyway. Owner grace is fixed at > 150 s (section 3).
5. Minimum supported BlueZ and distro matrix for the standalone installer (Debian 12/13, Ubuntu LTS, Armbian, DSM-class NAS where BlueZ may not be installable at all).
6. Do the unicast mDNS drops in `system.sh:815-816` break browsing on a bootstrapped Pi? Needs a live `avahi-browse` against a test service.
7. (Answered) Node mDNS with the web server off: gone, see "The node". Remaining ask: a firmware option to keep the responder independent of the web server.
8. Pairing option B: worth the extra endpoint, or is paste-the-key adequate for a once-per-install step?
9. Contract with the webapp: one shared paper for `/api/discovery/ble` and `PUT /api/ble/service`, or per-repo docs? Follows the existing pattern of a canonical doc plus mirrored vectors.
