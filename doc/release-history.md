# Release History

## v2.1.24 (2026-11-06)

**On a slow connection, McApp now connects about 3 times faster and uses 84% less data.** Caddy gzips
the live event stream and the large API responses. On a fast LAN you will barely notice a change.

> Update from the Update page as usual. `SYSTEM_EPOCH` goes to 8, schema stays at 40. A box with public
> TLS keeps its old Caddyfile until `ssl-tunnel-setup.sh` is run again, and works as before.

### Highlights

- **Faster connect on a weak link.** Time until the message history has arrived, measured on the Pi
  Zero 2W with `mcapp.local`'s real data, plain against gzip, median of 5 to 7 runs:
  - 1 Mbit/s: 6.5 s to 2.2 s.
  - 10 Mbit/s: 2.1 s to 1.6 s.
  - LAN: 1.6 s in both cases, because that time is spent on the server, not on the network.
- **Less data on every reconnect.** The burst of data a client gets when it connects shrinks from 631 KB
  to 103 KB. A phone waking from sleep pays this each time.
- **Live messages arrive as fast as before.** Over 200 s, 89 of 89 real events arrived 2.9 ms after a
  direct connection, against 1.8 ms without gzip.
- **No extra memory cost measured.** Caddy peaked at 28 MB with 8 connected clients, with and without
  gzip.

### Backend (MCProxy)

- `encode { gzip 1 }` on `/events*` and `/api/*`, in all four Caddyfile templates. Caddyfile version 5.
- Gzip level 1, because a trial on the Pi showed zstd holding 7 MB more with 3 clients, and level 5
  costing about 50% more CPU for a smaller gain.
- Responses under 512 bytes stay plain, and static files keep their pre-built Brotli and gzip files.
- `/events` compresses only on Caddy 2.11.5 or newer, and stays plain and working on an older one.
- New `scripts/measure_front_door.py` for wire bytes and timing. Full numbers:
  `doc/2026-11-06_1400-runtime-compression-measurements.md`.

### Frontend (webapp)

- No code changes. Transitive dependencies refreshed in `package-lock.json`. Python locks were already
  current.

### Upgrade notes

- No action needed. The update restarts Caddy once to load the new configuration.
- Soak before this release was 67 minutes, on a dev build, with Chrome on a Mac.
- Not tested: iPhone Safari over HTTP/3 with gzip, and a real slow link. The slow links above were
  simulated with a throttling relay.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

### v2.1.23 (2026-11-06)

- Gateway Availability bars show true time and a "Gateway off" state. FastAPI replaced by `weblite`, and Caddy serves the web app pre-compressed.
- Schema 40, `SYSTEM_EPOCH` 7. A slot from before schema 40 can no longer be activated.

### v2.1.22 (2026-10-30)

- QRZ card shows queued lookups, and "Session Timeout" clears itself; no false Caddy warning for a
  site line with an IP. Schema 39, `SYSTEM_EPOCH` 6.

### v2.1.21 (2026-10-25)

- BLE page shows and writes every setting of fork firmware 4.40a.10.25 (Max Hop, MTU, net console,
  KISS, voltage, fitted LEDs); ack latency in the message details. Needs fork firmware 4.40a.10.25
  for the new controls.

### v2.1.20 (2026-10-25)

- Group labels from the ICSSW list in sidebar, picker and header, with your own overrides for
  groups and callsign names; conversation segments. Schema 38 → 39, `SYSTEM_EPOCH` 6.

### v2.1.19 (2026-10-24)

- Call blocklist over RM1 in Remote Management (fork firmware 4.40a.10.23+), guarded against
  self-block, 39-character limit, locally set lists read-only. Schema 38, `SYSTEM_EPOCH` 6.

### v2.1.18 (2026-10-20)

- Classifier on MeshMap's shared rule set: messages carry tags, you hide by tag; custom rules
  retired (backed up to `classifier_rules_retired.json`). Schema 37 → 38, `SYSTEM_EPOCH` 5 → 6
  (reboot once on an Orange Pi with a RAM log).
- Update page refuses to activate a slot whose code is older than the database schema.

### v2.1.17 (2026-10-20)

- Form controls and scrollbars follow the app theme, not the OS. Schema 37, `SYSTEM_EPOCH` 5.

### v2.1.16 (2026-10-18)

- Phone layout fixes (diagram, Settings chips, BLE command row, system cards, tab-bar clearance);
  NF0JN, NF0JN-11 and NF0JN-12 added to the blocklist. Schema 37, `SYSTEM_EPOCH` 5.

### v2.1.15 (2026-10-17)

- Node Admin `net` and `upd` live (Network and Firmware update cards, install via Update now);
  verified replies release the 10 s spacing. Schema 37, `SYSTEM_EPOCH` 5.

### v2.1.14 (2026-10-17)

- Outgoing translation in the compose box (fr, it, es, pl, needs the on-device AI); QRZ subscriber
  QTH on the map; locator in the `!wx` reply. Schema 37, `SYSTEM_EPOCH` 5.

### v2.1.13 (2026-10-16)

- On-device Browser AI (opt-in, Settings > AI): annotations, emergency chip, catch-up summary;
  Q-codes and locators link in chat. Node Admin Max TX ceiling removed. Schema 36,
  `SYSTEM_EPOCH` 5.

### v2.1.12 (2026-10-13)

- Monitor DBG view parses the node's console (packet, `[LOG] STAT`, debug and raw-frame lines)
  with warnings and alarms; a DBG session also sets the node's `setlog` flag and restores it.
  Schema 36, `SYSTEM_EPOCH` 5.

### v2.1.11 (2026-10-11)

- Node Admin remote view at parity with the firmware's RM1 command set (radio, sensors,
  position, queues, heard list, charge LED); full 4.40a status replies. Schema 36,
  `SYSTEM_EPOCH` 5.

### v2.1.10 (2026-10-11)

- Meshmap internet feed (off by default, Settings > Internet) with internet positions on the
  map; RM1 frames out of chat, decoded under Remote Management. Schema 36 (migration 36
  `internet_feed_state`), `SYSTEM_EPOCH` 5.

### v2.1.9 (2026-10-10)

- RF Monitor shows MH and CMD rows with a legend; BLE operator toggles in node settings; Node
  Admin Advanced section. Schema 35, `SYSTEM_EPOCH` 5.

### v2.1.8 (2026-10-10)

- Clear (x) buttons in the destination box and the filter field. The bin that deleted a
  conversation's messages is removed. Schema 35, `SYSTEM_EPOCH` 5.

### v2.1.7 (2026-10-10)

- The status bar's "Internet" button and the "seen on the internet" status are removed; the
  oevsv.at map feed they used was switched off upstream. Schema 35, `SYSTEM_EPOCH` 5.

### v2.1.6 (2026-10-06)

- Remote node administration over LoRa (signed `RM1` messages) in Node Admin > Remote Management;
  nodes and passwords are stored in Settings > Remote nodes. Schema 34 → 35, `SYSTEM_EPOCH` 5.
- New navigation: Node Admin holds BLE and Remote Management; the RF Monitor moved to Settings.

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
