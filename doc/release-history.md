# Release History

## v2.1.1 (2026-10-04)

McApp now shows the first name and home town (QTH) of a station next to its callsign, looked up on
QRZ.com with your own account ([issue #14](https://github.com/DK5EN/McApp/issues/14), suggested by
dm3ks). The schema goes from 32 to 33 (three new tables, created automatically at startup);
`SYSTEM_EPOCH` stays at 5.

### Highlights

- **Names and QTH next to callsigns.** Chat bubbles and the station cards (Messages panel and
  Positions list) show the first name as a chip and the QTH with a 📍 pin. Names appear live as
  soon as a lookup finds them, without reloading. Callsigns without an entry look as before.
- **Set up in Settings → QRZ.com Lookup.** Enter your QRZ.com username and password; your browser's
  password manager can fill them in. A free QRZ.com account is enough: it delivers name, town and
  country. Two-factor login on the QRZ.com account does not affect this interface.
- **Gentle on your account.** At most 50 lookups per 24 hours and one request every 30 seconds.
  At 50 lookups — or when QRZ.com's own counter for your account reaches 50, for example because a
  logging program uses the same account — lookups pause for 24 hours. Errors back off with growing
  pauses, and a rejected password stops all attempts until you enter new credentials, so the
  account cannot be locked by retries. Chat partners are looked up first; names are refreshed after
  90 days, unknown callsigns are retried after 7 days.
- **The password stays on the proxy, encrypted.** The key is tied to this installation and to the
  Raspberry Pi's board: a copy of the database or of the SD card alone does not reveal it. It is
  never shown again and never appears in logs or stall reports.

### Backend (MCProxy)

- `qrz_service` runs the lookup loop with the limits above, following the QRZ.com XML
  specification's session and error rules (re-login when a session expires, 24 h pause on
  `Connection refused`). Each lookup is counted before its request goes out, so a failed request
  still counts against the daily cap.
- `secret_box`: AES-256-GCM under a key derived (HKDF) from `/var/lib/mcapp/secret.key` (created
  with the first credentials, mode 0600) and the board serial; the ciphertext is bound to the
  username.
- Migration 33: `callsign_info` (cache by base callsign), `qrz_lookups` (cap ledger), `qrz_state`.
- New API: `GET /api/qrz/status`, `PUT`/`DELETE /api/qrz/credentials`, `PUT /api/qrz/enabled`,
  `GET /api/callsign_info`; new SSE event `proxy:callsign_info` (full map on connect, one entry per
  new lookup). The password is write-only.
- Stall tracking never records the body of the credentials request and masks every `*password*`
  key. Without this, a sampled request would have stored the password in `stall_events`.
- Dependencies refreshed (`ast-serialize` 0.12.1).

### Frontend (webapp)

- New "QRZ.com Lookup" settings card: login form, status, lookups used today, next lookup, last
  error, enable switch and removal.
- Name chip and 📍 QTH in chat bubbles and station cards, fed live by `proxy:callsign_info` and kept
  in memory only.
- The client stall reporter drops the credentials request body and masks password keys.
- Dependencies refreshed (`@lucide/vue` 1.51.0, `rollup` 4.64.0, transitive patches).

### Upgrade notes

- Nothing changes until you enter QRZ.com credentials; without them the feature makes no requests.
- Moving the SD card to another Raspberry Pi means entering the QRZ.com password again: the stored
  one cannot be decrypted on a different board, by design.
- QRZ.com delivers the first-name field as registered; a club or repeater entry may read, for
  example, "REPEATER".
- Reload the app once (the "Update available" banner) to get the new display.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

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
