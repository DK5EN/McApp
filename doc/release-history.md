# Release History

## v2.0.17 (2026-09-28)

The Update page shows what a release changes before you install it, McApp uses about 16 MB less
memory, and the single ✓ on your own messages now says which signal it rests on. Schema stays at
32 and `SYSTEM_EPOCH` at 5, so there is no migration and no bootstrap convergence.

### Highlights

- **Release notes on the Update page.** Next to the "Update to vX.Y.Z" button a "What's new" card
  shows these notes for the release that button installs. Long notes start collapsed. Links open
  in a new tab; images and any script in the notes are removed before display.
- **16 MB less memory.** Web Push no longer loads `aiohttp` and `requests` for one HTTP request
  per notification, and both services stop loading two web-server extras they never used.
  Measured on mcapp.local at the same uptime: 97 MB instead of 113 MB for the main service.
- **What the ✓ means.** Hover the single check on your own message: "sent" (your node or a gateway
  confirmed it), "seen on the internet" (the message came back from the MeshCom server), or
  "echoed by our own node". "Seen on the internet" used to be hidden behind the echo, and on a
  BLE-connected box it never showed at all. ✓✓ Delivered still means the addressee answered.

### Backend (MCProxy)

- Web Push is sent by `push_send.py`: the same encryption (`http_ece`) and signing (`py_vapid`)
  libraries as before, posted with `httpx`. Headers and signature were checked identical against
  the old sender; a live notification to an iPhone arrived. Subscriptions are still removed on
  401/403/404/410 only.
- An unreachable push service now logs one warning line per attempt instead of a full traceback.
- Both services depend on plain `uvicorn` plus `uvloop` and `httptools`, and run with WebSockets
  off. `websockets`, `watchfiles`, `pyyaml` and `python-dotenv` are no longer installed.
- The deploy health check probes `uvicorn`, `httptools` and `dbus_next`; it used to import
  `websockets`, which would have failed every deploy once that package was gone. A new test
  suite checks that every module the probe imports is installed.

### Frontend (webapp)

- Release notes card on the Update page (Markdown rendered through a sanitizer).
- `msg_www` is split: `msg_echo` for your own node's echo, `msg_www` only for a copy from the
  MeshCom server. Messages cached before this release are read as echoes, so they never claim an
  internet sighting they did not have.
- Dependencies refreshed (`browserslist` 4.29.2, `earcut` 3.2.4, transitive).

### Upgrade notes

- Nothing to configure. Existing push subscriptions keep working; no browser needs to subscribe
  again.
- A box on v2.0.16 or older updates from the old Update page, so it sees the notes card from the
  next release on.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

### v2.0.16 (2026-09-28)

- Direct messages that newer firmware resends up to three times count once: one row, one push,
  one bubble, one unread. Dedup contract v2.
- The ACK details list each station once.

### v2.0.15 (2026-09-26)

- New node registers `IS1` and `SN1` (build date, via state), needs firmware upstream `dev` or a
  DK5EN fork build from 2026-09-25 or later.
- The own station on the map no longer shows a relay's signal.

### v2.0.14 (2026-09-24)

- DBG button: the node's debug console inside the RF Monitor, plus colour and icons.
- Link check works without Extern-UDP pointed at McApp, given firmware that forwards the pong
  over BLE. A password-protected console needs `NODE_CONSOLE_PASSWORD` in the config.

### v2.0.13 (2026-09-23)

- RF Monitor: a live, interpreted view of every frame the node hears or sends.
- Your own messages no longer disappear behind your own spam filters.

### v2.0.12 (2026-09-21)

- A firmware message ID repeats roughly every 1000 frames a node sends; a newer broadcast could
  show the ACK of an older, unrelated message that had reused the same ID. Acknowledgements are
  now bound within a 4 h window, and a reused ID no longer swallows the new message's own ACKs.
- A peer ACK that arrives as text shows who sent it.

### v2.0.11 (2026-09-20)

- An unread `+1` that could never be cleared is fixed; a bare link is no longer hidden as an
  advert.
- `@`-mentions raise a push notification, off by default (enable it in the notification
  settings). **Push contract v11.** First start re-classifies stored messages once in the
  background.

### v2.0.10 (2026-09-19)

- Ingest stalls fixed: they were SD-card fsyncs, not slow queries.
- A bootstrap run can no longer take the box off the network (network packages held and pinned).
  **`SYSTEM_EPOCH` 5.**
- Unread badges for digit-less callsigns such as `WLNK-1` clear again (#11); the Safari update
  banner clears.

### v2.0.9 (2026-09-18)

- Hotfix (#10): over plain `http://` the webapp could not send, scan for devices or show two
  cards. Webapp only.

### v2.0.8 (2026-09-16)

- Stall tracking: every slow call between webapp and API is recorded for reproduction.
- Memory on the Pi Zero: the CMA pool shrinks from 256 to 64 MB and MemTotal rises from 415 to
  473 MB; store-and-forward DM states (held, failed) are shown.
- **Schema v32, push contract v10, `SYSTEM_EPOCH` 4. Reboot the Pi once** after this update for
  the memory settings.

### v2.0.7 (2026-09-11)

- Position beacons carry the comment and node name as separate fields.

### v2.0.6 (2026-09-11)

- The Update page activates any deployed slot instead of "rollback", and never touches the
  database (the old rollback could restore a stale copy).
- BLE position frames follow the firmware's key contract: neighbour counts, QNH, telemetry typed.

### v2.0.5 (2026-09-10)

- BLE protocol audit: messages received while BLE was down keep their real time, a second
  transport copy enriches the message instead of being dropped, no phantom notifications after a
  reconnect. **Push contract v9.**

### v2.0.4 (2026-09-06)

- Unread badges are server-side read cursors, the same on every device.
- An ingest race that stored most messages twice is closed. **Schema v30.**

### v2.0.3 (2026-09-06)

- Who acknowledged a message is shown, not just that it was (needs node firmware
  v4.35s.09.06 or later).
- UDP/443 open for HTTP/3. **Schema v29, `SYSTEM_EPOCH` 2.**

### v2.0.2 (2026-09-01)

- Gateway Availability read 0 % while the link was fine; fixed and the spurious gaps removed.
- The blocklist applies to messages already stored; relayed MHeard beacons credit the sender
  (inert since the upstream 2026-08-28 firmware revert of MH `SRC`/`GW`/`PP` — parser retained).
- The dark map needs an API key. **Schema v28.**

### v2.0.1 (2026-08-22)

- Gateway Availability: a measured record of the node's uplink to the MeshCom server.
  **Schema v25.**

### v2.0.0 (2026-08-21)

- Major release, 765 commits since v1.6.13: Link Check, Web Push, hashtag channels, admin module
  with a backend-authoritative blocklist, self-converging deployments. **Schema v17 → v24, push
  contract v7.**
- Building the webapp needs Node 26 or later; the backend stays on Python 3.11 or later.

### v1.6.13 (2026-06-20)

- Maintenance release: high-frequency UDP/ACK log lines demoted from INFO to DEBUG, dependency
  updates only. No functional changes.
