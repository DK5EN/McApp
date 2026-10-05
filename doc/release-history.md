# Release History

## v2.1.5 (2026-10-05)

The node's time zone is no longer overwritten. Firmware with TZ support keeps a time-zone rule on
the node, and the proxy's time sync used to clear it every time it connected. The proxy now reads
the node's rule first and only ever sets one when the node has none. Schema stays at 34 and
`SYSTEM_EPOCH` at 5.

### Highlights

- **A time-zone rule on the node survives a reconnect.** If the node has a rule, the proxy sends
  only the current UTC time and leaves the offset alone.
- **A node without a rule gets yours, once.** On time-zone firmware with an empty rule the proxy
  sets the rule of the host it runs on (for example `CET-1CEST,M3.5.0,M10.5.0/3`) and confirms
  that the node took it. Set `MCAPP_NODE_TZ_POLICY=never` to opt out, or `MCAPP_NODE_TZ` to use a
  different rule.
- **Older firmware behaves exactly as before:** a fixed UTC offset, then the time.
- **When the proxy cannot tell what the node is, it sends the time only.** UTC is always right;
  a wrong fixed offset could destroy a rule.
- **Overheard acknowledgements no longer mark your own message as delivered.** The browser now
  matches an `:ack` text against the same addressing and one-hour window as the backend, and
  keeps internet-only acknowledgements when the connection reconnects.

### Backend (MCProxy)

- `set_time()` classifies the node from its settings register (`SN1.TZ`) before sending any
  offset, ignores a cached value until the node has answered since the last hello, and reports the
  branch taken (`utcoff`, `settz`, `rule`, `unknown`, `settz_unverified`) in the
  `/api/ble/settime` message.
- The inline `:ack` matching corpus (`ack_match_vectors.json`) is replayed through production
  ingest and shared with the webapp.

### Frontend (webapp)

- The node GPS card shows the node's time-zone rule, offers presets and a rule field with a
  grammar check while the rule is empty, and a "Clear rule" button while one is set. The locate
  button no longer sends a fixed offset to a node that follows a rule.
- The inline `:ack` matcher mirrors the backend rule; the matching corpus runs in the test suite.

### Upgrade notes

- Nothing to do. A node on time-zone firmware with an empty rule receives your host's rule at the
  next time sync; one that already has a rule is left alone.
- Reload the app once (the "Update available" banner) to see the time-zone controls.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

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
