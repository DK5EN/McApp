# Release History

## v2.1.0 (2026-10-02)

McApp 2.1.0 is the release that matches MeshCom node firmware **v4.40a**. Scrolling up to read a
conversation no longer gets pulled back down by new messages, and a store node's `:sto` notice now
shows as "held by …" on your message instead of as a chat bubble. Schema stays at 32 and
`SYSTEM_EPOCH` at 5, so there is no migration and no bootstrap convergence.

### Highlights

- **Your reading position holds.** Reported by DJ8MEH on a tablet: after scrolling up to follow a
  discussion, every incoming message moved the view down again. Two causes, both fixed. Anything
  within 250 px of the bottom (8 to 12 text lines on a tablet) was scrolled to the newest message;
  the band is now 80 px. And while you read inside the newest messages, each new one removed the
  oldest bubble above you, which shifted the text under you. The chat now holds the visible
  messages in place as soon as you scroll up; a "Jump to latest" button appears once newer
  messages exist.
- **Live messages are followed again on a full store.** With 2000 messages held in the browser,
  every new message replaced an old one, the count stayed the same and auto-follow never fired.
  It now reacts to the newest message of the open conversation.
- **The RF Monitor holds its place too.** Scrolled up, live frames no longer remove rows above
  the one you are reading.
- **`:sto` becomes "held by …".** A store node that keeps your direct message for later delivery
  answers with a text like `DK5EN-98 :sto071 DJ8MEH-81`. Behind a node that forwards that text, it
  used to appear as an ordinary message from the store node. It is now matched to your message
  and shown as held by that station, the same state the binary `0x04` frame sets, and the text
  itself is hidden. A notice that matches nothing is hidden as well.

### Backend (MCProxy)

- `store_message` absorbs an inline `:stoNNN` notice: original sender equals the notice's
  destination, echo counter matches, the held destination must match when named, within 1 h. It
  writes `send_success`, rank `held`, a `held` ledger row and the same `msg_status` event as the
  `0x04` frame. Both arriving for the same holder count once.
- The notice row stays in the database and is excluded from history, paging, the initial burst
  and unread counts. It never appears among delivery receipts.
- `ack_predicate_vectors.json` v3 adds `is_sto`, shared with mc-chat and the webapp.
- Dependencies refreshed: `cryptography` 50.0.2, `fastapi` 0.142.2, `uvloop` 0.23.0 (also in the
  standalone BLE service lock).

### Frontend (webapp)

- The chat pins its render window when you scroll up, also after returning from another view.
  Your own send still jumps to the newest message, except while older history is loading.
- The `:sto` notice is hidden in every view and never lights an unread badge. On the direct
  internet feed, which bypasses the backend, it is matched locally. A held status never
  overwrites "acknowledged" or "not delivered", and a repeat keeps the first holder.
- Dependencies refreshed (transitive).

### Upgrade notes

- Matching node firmware: **v4.40a**. Store-and-forward status (`held`, `failed`) and the `:sto`
  handling above come from the firmware's store node; older nodes keep working, they just send
  fewer of these signals.
- Same code as the short-lived v2.0.19, whose GitHub release was removed; v2.1.0 replaces it.
- Nothing to configure. Reload the app once (the "Update available" banner) so the scroll fix
  takes effect. Older `:sto` texts disappear from history, but the messages they refer to are not
  marked held retroactively.

## Earlier releases, in brief

One entry per release. The full notes as published are in
[`doc/archive/release-history-full.md`](https://github.com/DK5EN/McApp/blob/development/doc/archive/release-history-full.md).

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
