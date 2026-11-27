# McApp as a desktop app (Windows `.exe`, macOS `.dmg`) - concept

**Status:** idea, no code. 2026-11-27. Backlog B25. Related: B12 (container), B13 (standalone BLE
service; its macOS bleak backend is the main building block here),
`2026-10-31_1230-ble-service-macos-backend-design.md`.

## BLUF

- **Goal:** one installer per platform (`.dmg` for macOS, `.exe`/`.msi` for Windows 10/11). The app
  is self-contained: the webapp is the display, the backend is baked in, and it uses the desktop's
  internet connection (oevsv.at feed), its network (Extern-UDP 1799 to a MeshCom node) and its
  Bluetooth adapter (BLE to a MeshCom node). A **server mode** instead connects to an existing
  McApp server (`mcapp.local`) and starts no local backend.
- **Verdict: feasible, with Tauri as the shell.** Tauri is a Rust desktop shell that shows the
  existing Vue webapp in the OS web view (WebView2 on Windows, WKWebView on macOS) and runs the
  Python backend as a bundled sidecar process. Estimate 6-10 weeks.
- **Rejected: GPUI, and a Rust rewrite.** GPUI cannot render a browser, so all 88k lines of Vue/TS
  would be rebuilt by hand; it has no web, iOS or Android target, so the iPhone PWA and LAN
  browsers would be lost. A Rust backend would rewrite 57k lines of Python plus 57k lines of tests
  and re-earn every parity corpus shared with mc-chat and the webapp.
- **Biggest open risk:** BLE on Windows. The bleak backend exists and runs on macOS; bleak's WinRT
  path against a real node is unproven. A one-week spike decides go/no-go.

## 1. Options considered

| Option                                 | Frontend reuse | Backend reuse | Phones / LAN browsers | Effort      |
| -------------------------------------- | -------------- | ------------- | --------------------- | ----------- |
| **Tauri shell + Python sidecar**       | 100 %          | 100 %         | kept (PWA unchanged)  | 6-10 weeks  |
| Electron shell + Python sidecar        | 100 %          | 100 %         | kept                  | similar     |
| GPUI frontend, Python backend          | 0 %            | 100 %         | lost for GPUI users   | many months |
| GPUI frontend + Rust backend (rewrite) | 0 %            | 0 %           | lost                  | a year+     |

**Recommendation: Tauri.** Same reuse as Electron with a far smaller installer and RAM footprint
(system web view instead of a bundled Chromium), plus a built-in signed updater.

## 2. Architecture

```text
 Desktop app (Tauri)
 +--------------------------------------------------------------+
 |  Window: OS web view  ->  Vue webapp (unchanged bundle)      |
 |        |  HTTP /api + SSE /events                            |
 |        v                                                     |
 |  Local mode:  127.0.0.1:<port>  McApp backend (sidecar)      |
 |               - UDP 1799  <-> MeshCom node (Extern-UDP)      |
 |               - BLE (bleak) <-> MeshCom node                 |
 |               - HTTPS     -> oevsv.at feed, push, QRZ        |
 |  Server mode: https://mcapp.local  (no local backend)        |
 +--------------------------------------------------------------+
```

- **Display:** the existing webapp bundle, served by the local backend or loaded against the
  remote server. The `/api` + `/events` contract stays as it is.
- **Backend:** McApp and `ble_service` as a standalone Python build (python-build-standalone via
  `uv`, or PyInstaller/Nuitka) shipped as a Tauri sidecar. The shell starts it on a loopback port,
  health-checks it, and stops it when the app quits. One process is preferable to two on a
  desktop (mcapp + ble_service in-process or as two sidecars; decide in the spike).
- **Server mode:** a connection picker (local / `mcapp.local` / IP address). The webapp already
  talks to a backend over HTTP and SSE, so this is a base-URL setting, not a second app.

## 3. What has to change

| Area                     | Today                                                                      | Change                                                                                                                                                                                  |
| ------------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| BLE                      | `dbus_next`/BlueZ on Linux; bleak backend exists for macOS (`BLE_BACKEND`) | Verify bleak on Windows (WinRT). macOS has no MAC addresses (CoreBluetooth UUID, `address_kind`); pair/unpair unsupported there already. `internet_feed` already omits the UUID as MAC. |
| Event loop               | `uvloop` picked by uvicorn                                                 | Not available on Windows; use the default asyncio loop there.                                                                                                                           |
| Paths                    | 10 modules hardcode `/etc/mcapp` or `/var/lib/mcapp`                       | Per-user app-data dirs (`%APPDATA%`, `~/Library/Application Support`) behind one path resolver.                                                                                         |
| `secret_box`             | Key derived from install key + Pi board serial                             | OS keychain (macOS Keychain, Windows Credential Manager).                                                                                                                               |
| VAPID / push             | `/var/lib/mcapp/vapid.json`                                                | Same resolver; push from a desktop app is optional (the OS has native notifications).                                                                                                   |
| Pi-only features         | Bootstrap, system epoch, slots, Update page, Caddy, firewall, boot memory  | Hidden in desktop mode via a runtime mode flag reported in `/api/status` `features`.                                                                                                    |
| Updates                  | Slot runner on the Pi                                                      | Tauri's signed updater.                                                                                                                                                                 |
| `/api/timezone`          | Short-lived child process (`tz_lookup_worker.py`)                          | Works; the child must be spawnable from the frozen build.                                                                                                                               |
| Internet feed (oevsv.at) | `internet_feed.py`, plain HTTPS                                            | Works as is.                                                                                                                                                                            |
| UDP 1799                 | Pi with a stable LAN address                                               | Works; see risks.                                                                                                                                                                       |

## 4. Risks

- **The node's EXT IP has to point at the desktop.** On a laptop with DHCP that address moves and
  UDP then fails silently. Needs a "set EXT IP on the node" action over BLE, or a clear guide and
  a status warning when no UDP frame has arrived.
- **OS permission prompts:** Windows Firewall (UDP 1799 inbound), macOS Bluetooth and local
  network access. Each needs a good usage text (`NSBluetoothAlwaysUsageDescription`,
  `NSLocalNetworkUsageDescription`), or users click "deny" and the app looks broken.
- **Code signing:** Apple Developer ID + notarization (99 USD/year) and a Windows code-signing
  certificate. Without them Gatekeeper blocks the `.dmg` and SmartScreen warns on the `.exe`.
- **Server mode and `mcapp.local`:** Caddy uses its internal CA, which neither web view trusts. The
  shell must pin or import that certificate. `.local` (mDNS) resolution on Windows is less
  reliable, so the picker accepts an IP address.
- **Two runtimes, one codebase.** Desktop McApp and Pi McApp behave differently. Keep one codebase
  with a runtime mode flag, never a fork, or the shared corpora and parity tests split.
- **Gate coverage.** The gate runs on Linux only today. Desktop support needs a macOS and a
  Windows job running at least the backend suites, or portability regresses silently.

## 5. Suggested order

1. **Spike, ~1 week (go/no-go):** bleak on Windows 10/11 and macOS against a real node; receive and
   send one message; UDP 1799 round trip through the Windows firewall.
2. **Portable backend:** path resolver, keychain for `secret_box`, event-loop fallback, desktop
   mode flag; Windows + macOS CI jobs.
3. **Tauri shell:** sidecar start/stop and health check, connection picker, certificate pinning
   for `mcapp.local`.
4. **Ship:** signing, notarization, installers, updater, first-run permission flow.

## 6. Open questions

1. One sidecar process (mcapp with BLE in-process) or two (mcapp + ble_service as today)?
2. Python freezing: python-build-standalone via `uv` vs. PyInstaller vs. Nuitka (size, startup
   time, antivirus false positives on Windows).
3. Is web push needed on the desktop, or are native OS notifications enough?
4. Who holds the signing identities, and does the release pipeline build on GitHub-hosted macOS
   and Windows runners?
