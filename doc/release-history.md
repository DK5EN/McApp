# Release History

## v2.1.6 (2026-10-06)

McApp can now administer other MeshCom nodes over LoRa. Store a node's callsign and its
remote-management password once, then switch GPS, tracking, display, light, mesh and gateway on or
off, change TX power, trigger a position or track beacon, or restart the node, all from the web
page, with the node's last answer shown as green and grey tiles. Node Admin is now also the home of
the BLE page, and the RF Monitor moved into Settings. Schema 34 → 35 (three `node_admin_*` tables,
migrated automatically); `SYSTEM_EPOCH` stays at 5.

### Highlights

- **Remote node administration (Node Admin > Remote Management).** Pick a node and press
  **Connect**: McApp syncs the command counter, then reads the node's status. Switches show the
  last verified answer: green is on, grey off, amber "no answer yet, may or may not have run".
  Restart, "Send position now" / "Send track now", TX power and an Advanced section (output pin,
  re-sync, raw command, history) are on the same page.
- **Settings > Remote nodes** stores each node's callsign, its password (encrypted on the Pi,
  never shown again; Generate offers a random 14-character one) and a per-node TX power limit.
  **Remove** deletes the node from McApp completely after a confirmation.
- **McApp protects the node from itself.** One command in flight per node, 10 s between frames,
  no new frame for 5 minutes after a command that got no answer (the node may still act on a late
  copy), a 2-minute pause while another station is administering the same node, and TX power never
  above the board maximum the node reported. Each of these refusals tells you why and for how long.
- **New navigation.** Node Admin has two sub-tabs, **BLE** (the former BLE page, the default) and
  **Remote Management**, and is shown on every backend. The RF Monitor is now **Settings >
  Monitor**. Old links (`/bluetooth`, `/monitor`, `/nodeadmin`) forward to the new places.
- **QRZ.com lookups no longer get stuck for days.** The count QRZ reports at login is treated as a
  hint, never as a reason for a 24-hour suspension.

### Backend (MCProxy)

- Remote admin uses the firmware's signed `RM1` direct messages (HMAC-SHA-256, monotonic counter,
  sync handshake). Commands: `status`, `sync`, `sendpos`, `sendtrack`, `gps`, `track`, `display`,
  `led`, `gateway`, `mesh`, `txpower`, `setout`, `reboot`. Replies up to 108 characters are
  accepted (firmware draft 2).
- `GET /api/node-admin/targets/{target}/state` folds the verified answers, in send order, into the
  node's last known state; `DELETE /api/node-admin/targets/{target}` removes a node (refused while
  a command is in flight, the post-silence cool-down or a possible lockout runs).
- The feature is always on and inert until a node is stored; `/api/status` lists `node_admin` under
  `features`. The routes are LAN-only: they refuse a foreign `Host` or `Origin`, so they are not
  usable through the public TLS hostname.
- QRZ: the daily budget is `min(50 - ledger, 100 - (Count at login + lookups since))`; a `Count`
  of 100 or more is ignored as implausible, and a persisted server-count suspension is lifted.
- Dependencies refreshed (both `uv.lock` files).

### Frontend (webapp)

- Remote Management reuses the BLE page's register bar, card grid and toggle tiles; the BLE page
  itself is unchanged.
- The QRZ card shows what QRZ reports and why a suspension is running.
- vite-plugin-pwa 2.0.0, vite 8.3.3 and a transitive refresh. TypeScript stays on 6.x.

### Upgrade notes

- Schema 34 → 35 runs automatically at start. Nothing else to do.
- To administer a node it needs remote management enabled (`--remotemgmt on`) and a password
  (`--passwd`). That password is also the node's net-console password, and one captured frame
  allows offline guessing, so use a long random one (Generate).
- Reload the app once (the "Update available" banner) to get the new navigation.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

### v2.1.5 (2026-10-05)

- A time-zone rule on the node survives reconnects; a node with an empty rule gets the host's rule
  once (`MCAPP_NODE_TZ_POLICY=never` opts out, `MCAPP_NODE_TZ` overrides it).
- Overheard acknowledgements no longer mark your own message as delivered.

### v2.1.4 (2026-10-04)

- QRZ.com lookups are no longer limited to 50 per day on a paid subscription; free accounts keep
  every limit.

### v2.1.3 (2026-10-04)

- QRZ.com suspensions record why they were set, so a subscriber account recovers after a password
  re-entry; the settings card shows paid or free. Schema 33 → 34 (`qrz_state.suspend_reason`).

### v2.1.2 (2026-10-04)

- QRZ.com's own lookup counter only pauses lookups on free accounts; on a subscriber account it is
  not a daily count and paused every login.

### v2.1.1 (2026-10-04)

- First name and QTH next to callsigns, looked up on QRZ.com with your own account (Settings →
  QRZ.com Lookup). Schema 32 → 33 (`callsign_info`, `qrz_lookups`, `qrz_state`).

### v2.1.0 (2026-10-02)

- Matches node firmware v4.40a: the reading position holds when you scroll up (chat and RF
  Monitor), and a store node's `:sto` notice shows as "held by …" instead of a chat bubble
  (`ack_predicate_vectors.json` v3).

### v2.0.18 (2026-09-29)

- Message details show the right acknowledgements after a firmware message ID is reused, your own
  relayed message is one bubble, and retried direct messages count once on every path.

### v2.0.17 (2026-09-28)

- The Update page shows the release notes ("What's new") before you install, Web Push is sent
  without `pywebpush` (about 16 MB less memory), and the own-message ✓ tells which signal it rests
  on (`msg_www` split into `msg_echo`).

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
