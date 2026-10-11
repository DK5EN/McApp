# Release History

## v2.1.10 (2026-10-11)

McApp can now join the meshmap internet feed. With it, group and broadcast messages and station
positions from meshmap.oevsv.at reach the box over the internet, beside what the node hears on
RF. The feed is off until you enable it in Settings > Internet. RM1 remote-admin traffic is no
longer shown in chat and appears decoded under Remote Management instead. Schema 35 → 36;
`SYSTEM_EPOCH` stays at 5.

### Highlights

- **Internet feed (meshmap).** Settings > Internet turns it on and shows what will be sent.
  You choose up to 10 groups (they can be imported from the node's groups) and whether to
  receive broadcast. The box registers itself with meshmap once. A globe chip in the status bar
  shows the feed state. Messages from the feed are marked in the chat, and the details popover
  names their origin.
- **Internet positions on the map.** Positions can be off, within a radius around the node
  (10 to 2000 km), or worldwide. Stations known only from the internet are drawn as dashed,
  muted markers with their own legend entry. They do not appear in the station list, the chat
  positions panel, counts, hop buckets or Link check. An RF fix always takes precedence over an
  internet one.
- **RM1 frames out of chat.** Remote-admin commands and replies, your own and other stations',
  no longer appear in chat, unread counts, push or history. Node Admin > Remote Management gains
  **Observed sessions**: command/reply pairs per commander and target, decoded, and marked
  "verified" only when every frame checks out against a key stored on this box. The RF Monitor
  still shows the frames.
- **Conversation delete is back** in the Messages filter bar, next to the clear (x), with a
  confirmation. Deleting a group in Settings > Groups now asks first as well.

### Backend (MCProxy)

- New `InternetFeedService`: meshmap registration (token encrypted at rest), the SSE box feed,
  and `GET/PUT /api/internet-feed` + `POST /api/internet-feed/forget`. The routes are LAN-only,
  like Node Admin. `/api/status` `features` gains `internet_feed`.
- Feed messages are stored as `src_type "www"`. A www row never feeds the uptime, ack, RM1 or
  signal paths. When the same message also arrives over RF, the row is upgraded to the RF copy.
  RF never goes the other way.
- Push skips feed messages older than 15 min, and replayed feed messages are not pushed again.
- Feed positions are held in RAM only (5000 rows, 3 h). They never reach `station_positions`,
  and blocked callsigns are dropped. A GPS move over 5 km reconnects, at most once per 10 min.
- RM1 frames get the `rm1` broadcast verdict and are still stored.
  `GET /api/node-admin/observed` serves them decoded. The tag is never exposed.
- Migration 36: `internet_feed_state` (one row, inert until the feed is enabled).
- Dependencies refreshed (numpy 2.5.4).

### Frontend (webapp)

- Settings > Internet card, globe status chip, www message marker and origin in the details
  popover.
- Internet positions on the map: dashed markers, legend entry, "Source: Internet (meshmap)"
  popup. Feed rows are never written to the offline cache.
- Remote Management > Observed sessions. Cached RM1 rows are removed at startup.
- Messages filter bar: delete conversation restored with confirmation. Settings > Groups:
  per-group delete confirms, and the list fills the page height.

### Upgrade notes

- The database migrates to schema 36 on the first start. Nothing else to do on the Pi.
- The internet feed stays off until you enable it in Settings > Internet. Enabling it registers
  the box with meshmap.oevsv.at and needs outbound internet from the Pi.
- Reload the app once (the "Update available" banner) to pick up the new bundle.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

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
