# Release History

## v2.0.18 (2026-09-29)

Message details show the right acknowledgements again after a firmware message ID is reused, a
message you sent from another node and got relayed back is one bubble instead of two, and
retried direct messages are recognised on every path. Schema stays at 32 and `SYSTEM_EPOCH` at 5,
so there is no migration and no bootstrap convergence.

### Highlights

- **The right ACKs on the right message.** Firmware message IDs repeat after roughly a thousand
  frames from one node. An older message's "Acknowledged by" list used to show the stations that
  acknowledged the newer message that reused its ID. The list is now anchored on the message
  itself. The older message's own records are already gone by then, so it shows nothing rather
  than the wrong station. An acknowledgement without a sender no longer appears next to the same
  acknowledgement from a named station.
- **One bubble for your own relayed message.** A message you sent from another node and got back
  through the mesh arrived over Bluetooth with the relay as sender and over UDP with you as
  sender, so it appeared twice. The first station in the path is now always the sender; only
  relay positions drop your own callsign.
- **Retried direct messages count once on every path.** Scrolling back through history, the
  offline cache and the send outbox now use the same identity as live messages. A message the
  firmware resent up to three times no longer comes back as separate bubbles there, and a direct
  message you send is no longer always transmitted twice because its echo was not recognised.
- **"Echoed" and "seen on the internet" no longer depend on arrival order.** Your own message
  reads the same whether the node echo or the MeshCom server copy arrives first, and a message
  loaded by scrolling back is marked as echoed by your own node.
- **No beep for a retry of a message that was already pushed.** A resend of a direct message that
  reached you as a push while the app was closed no longer plays the foreground sound with no
  bubble behind it.

### Backend (MCProxy)

- `split_path` in the Bluetooth protocol keeps the first path component as `src`; only relay
  positions drop the own callsign.
- `GET /api/messages/{msg_id}/acks` takes an optional `?since=<ms>` (the message's own timestamp).
  The window is 60 s before to 4 h after it, or 168 h when a held record lies inside the narrow
  window. Without `since` the answer is unchanged, which is what an older webapp gets.
- Release tooling: the GitHub release body is the release's own section plus a footer link, the
  script refuses to start a release whose notes are not on top, and `uv.lock` versions follow the
  bump.
- Dependencies refreshed: `sse-starlette` 3.5.0, `librt` 0.16.0.

### Frontend (webapp)

- The ACK details send the message timestamp as `since`; a backend without the parameter ignores
  it.
- Message identity follows the dedup key on history paging, offline hydration and the outbox
  echo scan; an own message keeps its identity when a foreign one shares its raw ID.
- Echo and internet-sighting flags are set by the same rule on first insert and on duplicates.
- The "What's new" card on the Update page shows only its own release's section. Its Markdown
  sanitizer uses an explicit allowlist, and `marked` and `DOMPurify` load as separate chunks so an
  installed PWA stops downloading them again on every release.
- Dependencies refreshed (`typescript-eslint` 8.71.0, transitive).

### Upgrade notes

- Nothing to configure. A webapp from before this release works against this backend, and this
  webapp works against an older backend; only the ACK details fix needs both halves.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

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
