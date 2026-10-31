# Container test campaign - resume point

**Read this first after a context clear.** It carries everything needed to finish the campaign.
Detailed results live in `doc/2026-10-31_1200-container-test-campaign.md` (plan, test matrix,
results per wave). State as of 2026-10-31 ~14:30, branch `development`; CT4a, CT4b, CT4c and U4 closed; next CT3a-W5 (operator) and CT6.

## How to work

- Use `/orchestrate-waves`: waves with exclusive file ownership, gate after each wave (ruff check,
  `uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src`,
  `uv run python scripts/run_startup_tests.py`), Opus advisor for behaviour-changing waves,
  orchestrator commits per wave with explicit paths, push to `development` (operator approved
  commit + push for this campaign). Update this file and the campaign doc at every wave boundary.
- Reports to the operator: BLUF / exec style, no session narrative.
- `script -q /dev/null` does not work in this environment; test summaries print only with a
  console, so judge suites by exit code, or force `has_console` in a one-off scratch runner.
- The operator is present and supports hardware steps (Bluetooth permission, PIN dialogs, iPhone).

## Context documents

| What                                             | Where                                                                                                                                                                                                                              |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Overview (artifact)                              | **McApp Deployment Roadmap**: https://claude.ai/artifact/Jwai6SWp5UBq12T1QUvcVo (slug `914a4b5e-6ac2-4c3c-9b81-4829d33460ca`, live version `1793436264-5a02`, local source `doc/2026-10-31_1000-deployment-roadmap-overview.html`) |
| Campaign plan + results                          | `doc/2026-10-31_1200-container-test-campaign.md`                                                                                                                                                                                   |
| Container concept (B12)                          | `doc/2026-10-31_0830-container-deployment-concept.md`                                                                                                                                                                              |
| BLE + discovery concept (B13)                    | `doc/2026-10-31_0830-ble-service-and-lan-discovery-concept.md`                                                                                                                                                                     |
| macOS BLE backend design                         | `doc/2026-10-31_1230-ble-service-macos-backend-design.md`                                                                                                                                                                          |
| Desktop gate (B11), Ansible/nginx/deb evaluation | `doc/2026-10-31_0830-desktop-image-gate-concept.md`, `doc/2026-10-31_1100-ansible-and-web-server-evaluation.md`                                                                                                                    |
| Backlog                                          | `doc/backlog.md` B11-B15 (B14 belongs to another session: RM1 update channel)                                                                                                                                                      |
| Test tooling                                     | `scripts/container-tests/{image,node,proxy,persistence}/` (each has a README)                                                                                                                                                      |

## Operator decisions (all rounds, 2026-10-31)

- Transmitting on air with DK5EN-1, flashing it, managing its settings: approved.
- No outside access; test hostnames via `/etc/hosts` (done: `mcapp-test`, `auth-test.air-maxx.net`).
- BLE: both routes. Linux `ble_service` (route a) and macOS bleak backend (route b, dev/test host only).
- Cookie login: Authelia, same origin. 64-bit images only. Image on GHCR (later).
- Browser AI WASM (107 MB) is loaded on demand by the webapp (webapp change, not in this campaign).
- Rosetta enabled in OrbStack. A CoreBluetooth UUID is never sent as `mac` (done, `01a9416`).
- Test scripts live in the repo, committed and pushed.

## Done

| Wave | Result                                                                                                                                                                                                                                                                                    | Commits                                               |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| CT0  | DK5EN-1 = `/dev/cu.usbserial-0001` (Heltec V3, fw 4.40a, IP 192.168.68.71); OrbStack VMs `deb-arm64`, `deb-amd64` with Docker                                                                                                                                                             | `ac75efc`                                             |
| CT1  | Spike image arm64 + amd64 (228 MB; 107 MB is Browser AI WASM), node helper, proxy + Authelia stack, macOS BLE design                                                                                                                                                                      | `ac75efc`, `89f36cf`                                  |
| CT2  | U1 OrbStack bridge: source rewritten to `192.168.215.1` (trap confirmed). U2 host network: source `127.0.0.1`, learning refused (fails safe). U3 rootful Docker in VM: source kept. U4 amd64 VM: blocked (QEMU/Rosetta, runc hangs). U5 container → node → on air: pass after node reboot | `067af7d`, `dec39b1`, `34d308e`                       |
| CT3a | W1-W4 pass (SPA, unbuffered SSE through Caddy + Traefik, basicauth, Authelia cookie login, XHR → 401)                                                                                                                                                                                     | `0f1f1dc`                                             |
| CT3b | macOS BLE backend W1-W6, two advisor rounds, `ble_bleak` 23/23, `ble_service` 510/510                                                                                                                                                                                                     | `99de2d0`, `01a9416`, `99921d3`, `4baf50b`, `f468b7f` |
| CT5  | P1 volume survives re-create, P3 bare-metal DB (6191 msgs) migrates to schema 39, P4 log config                                                                                                                                                                                           | `76a637f`                                             |
| CT4a | macOS BLE service vs DK5EN-1: scan, connect by UUID, 14 registers, DM on air + ACKs on SSE, reconnect after reboot, second client fails cleanly. Fixed: state dir not created on macOS, empty `device_name`                                                                               | `85c82d6`                                             |
| CT4b | Linux `ble_service` on rpizero vs DK5EN-1: connect, 14 registers, DM + ACKs, reboot reconnect. Fixed: bluez scan missed devices named during the scan                                                                                                                                     | `9074ee8`                                             |
| CT4c | McApp container → macOS BLE service (`host.docker.internal`, loopback bind suffices): connect via McApp, 14 registers + `ble:status` on SSE, `type: BLE` DM + peer ACK. UDP path: learned target `192.168.97.1` (traefik-net gateway, outside the 172.16/12 heuristic)                    | see campaign doc                                      |
| U4   | Deferred: amd64 rootful Docker cannot start containers on the Mac; retest on real x86/NAS                                                                                                                                                                                                 | -                                                     |

## Remaining waves (in order)

### CT3a-W5 - iPhone PWA - operator

Needs router DNS entries `mcapp-test.air-maxx.net` and `auth-test.air-maxx.net` → `192.168.68.58`
(the Mac); Caddy already serves them. Run the checklist in the B12 paper ("Behind your own reverse
proxy"). Authelia profile: `cd scripts/container-tests/proxy && docker compose --profile authelia up -d`.

### CT6 - Report and paper updates - orchestrator (+ one writer for the papers)

1. B12 paper: replace the gateway heuristic (`x.x.x.1` in `172.16/12`) with "source equals the
   container's default gateway (`/proc/net/route`)" (CT4c: traefik-net gateway `192.168.97.1` slipped
   through and was persisted as the target); note `host.docker.internal` reaches the Mac's loopback
   (BLE service needs no wider bind, API key is the only guard); add the macOS setup note (grant OrbStack/Docker
   Desktop Local Network access, restart with `orb stop` / `orb start`); on a Mac the target must be
   pinned to the node IP (bridge needs learning off, host mode fails safe); image size facts and the
   on-demand Browser AI decision; spike results (SSE through two proxies, Authelia, persistence);
   GPS card 501 retry fix still required.
2. B13 paper: macOS backend exists (link the design), dev/test only; bind guard gap for the
   systemd uvicorn CLI path.
3. Firmware note: `--extudp on` starts the EXT receive listener only after `--reboot` (issue or
   doc in MeshCom-Firmware-DEV-Main, operator decides).
4. Backlog entries for the follow-ups below.
5. Overview artifact: add a "Test campaign results" section to
   `doc/2026-10-31_1000-deployment-roadmap-overview.html` and republish it to the artifact above
   (`mcp__claude-artifacts__publish_files` with slug + base_version from `get_artifact`).
6. Cleanup (below), then final campaign summary to the operator.

## Follow-ups to put into the backlog in CT6

- Webapp: load Browser AI WASM on demand; GPS card stops retrying `/api/timezone` after a failure
  (`skipRecord` for that path); answer 401 with a full-page navigation (Authelia).
- B12 code changes as listed in the concept, plus the corrected gateway check.
- ble_service: shared adapter-contract suite (design §7.2, deferred); bind guard for the systemd
  uvicorn path (needs B13 wave-1 `main()` entry); dev-group bleak without platform marker installs
  bleak + dbus-fast on Linux CI (harmless, comment wording).
- `scripts/container-tests/node/node.py`: node IP only appears in the WiFi boot line; `info`
  shows `null` without a reset.
- Firmware: EXT listener not started by `--extudp on`.
- Unknown single datagram from `172.17.0.1` in U3 (anti-flap guard ignored it).
- Test flake: `node_admin_service_tests.case_e_dropped` (key-deleted iteration) failed once in the full
  runner with `OSError: Directory not empty` in `TemporaryDirectory` cleanup (a DB write outliving
  `svc.stop()` / `storage.close()`, likely an in-flight `to_thread` job); 0/10 failures alone (CT4c gate).

## Cleanup at the end (CT6)

| Item                   | Action                                                                                                                                                               |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DK5EN-1                | `node.py extudp-off`, `node.py extudp-clear`, `node.py reboot`; verify `EXTUDP off`, `EXT IP` empty                                                                  |
| Proxy stack            | `cd scripts/container-tests/proxy && docker compose --profile authelia down -v`                                                                                      |
| Caddy                  | restore `~/docker/caddy/Caddyfile.bak-mcapp-ct-*` (or remove the `@mcappct` block), validate, `docker exec caddy-service caddy reload --config /etc/caddy/Caddyfile` |
| VM containers          | `orb -m deb-arm64 -u root docker rm -f ct`; same for `deb-amd64`                                                                                                     |
| Spike images / volumes | `docker image rm mcapp-spike:arm64 mcapp-spike:amd64`; remove `mcapp-ct-*` volumes                                                                                   |
| rpizero (CT4b)         | stop the `uvicorn ... --port 8095` process; `rm -rf ~/ct-ble`; `sudo systemctl stop bluetooth && sudo systemctl mask bluetooth` (it was masked before CT4b)          |
| macOS BLE service      | stop the `run-macos.sh` process; delete `~/Library/Application Support/mcapp-ble/ble_state.json`                                                                     |
| Ask the operator       | keep or delete OrbStack machines `deb-arm64`/`deb-amd64`; keep Rosetta on; remove the two `/etc/hosts` lines and the router DNS entries                              |

## Current live state (to know what is running)

- DK5EN-1: `EXTUDP on`, `EXT IP 192.168.68.58` (the Mac); BLE-connected to the macOS service.
- rpizero: `ble_service` running on 127.0.0.1:8095 (`~/ct-ble/`, key in `~/ct-ble/key`), disconnected;
  bluetooth.service unmasked and active.
- macOS BLE service running on 127.0.0.1:8091 (restarted in CT4c with a new key in that session's scratchpad,
  regenerate if lost), connected to DK5EN-1; `ble_state.json` saved.
- Docker on the Mac: `mcapp-ct` compose stack, profile `authelia` (Authelia + McApp spike, `CT_BLE_MODE=remote`) up;
  its volume holds the learned UDP target `192.168.97.1`;
  Caddy + Traefik up; another session's MeshCom-Server compose stack up (not ours, port 8081).
- Caddy carries the `@mcappct` block for the two test hostnames.
- OrbStack VMs: `deb-arm64` (idle `ct` container), `deb-amd64` (Docker overlay2, containers do not start).
