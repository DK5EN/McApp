# RB-006 Node Admin: triage and remote view

| Field | Value |
|---|---|
| ID | RB-006 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 327-396; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` (LAN only) |
| Applies to (variants) | `debian-13-trixie` |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Node Admin (RM1 remote admin)"); ADR-0011; POL-003 |

## 1. Purpose and scope

Node Admin sends signed RM1 commands to other MeshCom nodes. The node answers silently for many causes. Use this runbook to find the cause of "no reply" and to remove a node safely.

Remote administration of other MeshCom nodes (reboot, status, toggles, `txpower`, `setout`) from the McApp web UI,
signed with an HMAC over LoRa. Design: `../archive/2026-10-05_1000-node-admin-ui-concept-and-plan.md`.

**Always on, configured in the web GUI.** There is no config switch (operator decision 2026-10-05): open Settings >
Remote nodes, add a node's callsign and its password, then use Node Admin > Remote Management. Until a node is stored the feature is
inert; once a node's password is stored, any device on the LAN can use it through
this API (the API has no authentication, so storing a password is the consent). The API has no authentication, so it is LAN-only by construction: `/api/node-admin/*` refuses any `Host` that is not
a LAN name (`mcapp.local`, the short hostname, private/loopback IPs) and any foreign `Origin`. It is therefore NOT usable
through the public TLS hostname. `GET /api/status` lists `"node_admin"` under `features`; the webapp shows the Remote nodes
tab and the Node Admin > Remote Management sub-tab only when it does (an older backend shows neither; the Node Admin
entry itself is always there because it also hosts the BLE page). A developer-only
`"node_admin": {"allowed_origins": ["http://localhost:5173"]}` in `config.json` extends the Origin check for the Vite dev
server; nothing needs it in normal use, and a leftover `"enabled"` key from an earlier dev build is ignored.

**Prerequisites on every managed node:** `--remotemgmt on` and a non-empty `--passwd` (check `--info`: `RM: on`).
Without both, an `RM1` DM is ordinary acked text and the node never answers. Use a random 14-character password
(Settings > Remote nodes > Generate): the tag lets anyone who captured ONE frame on air guess a short password
offline. Enter the same password in McApp once; it is stored encrypted (`secret.key` + board serial) and never shown.

**CALL_SIGN must carry the attached node's SSID exactly as the node stores it** (`DK5EN-14`, not `DK5EN`). The tag is
bound to the sender's call; a bare call produces a valid-looking frame the node silently ignores. On a BLE-attached box
McApp adopts the call from the node's `I` register; on a UDP-only box set `CALL_SIGN` yourself. The managed node must hear
the attached node over LoRa: a command that reaches a gateway node through the internet first is shown there as plain
text and the later RF copy is dropped, so it is never executed.

**Runbook: "no reply".** The node is silent for every one of these, so check in order: RM off or no password on the
node; wrong stored password (a wrong password never produces a reply, not even `err`); the 10 s rate limit (one accepted
frame per 10 s, per node, from any sender); the 5 min lockout (3 counted rejects in 90 s: bad tag, replayed counter,
blocked command); the node did not hear McApp's node over LoRa; the reply is still on air (20-45 s). `bad tag` in the
view means a reply arrived that McApp could not verify (password changed on the node, or a spoof). Counters:
`ctr = MAX(ctr+1, last_hwm+1, unix time)`, so another SysOp or the firmware web UI using the node does not desync McApp.

**Removing a node.** Settings > Remote nodes > Remove deletes the node from McApp completely (password, counter,
max TX, command history); the node itself is not changed. It is refused for up to 5 minutes after a command that got
no answer, or while a command is in flight; the message names the time it unblocks.

**The remote view (2026-10-06).** Node Admin > Remote Management mirrors the BLE sub-tab next to it: register bar (Sync, Status), Info row,
card grid, Switches (green = on in the last verified answer, amber = uncertain), Restart, and a collapsed Advanced
section (output pin, Re-sync counter, raw command, history). Design: `../archive/2026-10-06_1100-node-admin-remote-view-concept.md`.

- **Connect first.** Selecting a node sends nothing. `Connect` sends `sync`, then `status` (about 30-70 s). Every other
  control stays disabled until a sync was verified in the running McApp process; a restart of McApp needs a new Connect.
  A command that reaches the backend unsynced waits behind an automatic sync and is **dropped** if that sync gets no
  answer ("command dropped: no answer to the counter sync").
- **Cool-down after silence.** After a `no_reply` or `abandoned` row the node gets no new frame for 5 min from that
  frame's hand-off (the node itself resends DMs up to 4 times; a late copy would be a counted replay strike). The page
  shows the wait; the API answers 409 with the remaining seconds.
- **Another station managing the node.** When McApp hears an RM1 frame to or from a stored node that is not its own,
  it sends nothing to that node for 120 s ("Another station is managing ..."). The firmware shares its rate limit, counter
  mark and reply cache between all senders.
- **"The node reports counter mark N, below the last known M."** The node lost its counter mark (re-flashed or erased).
  McApp ignores such a sync reply (it could be a replayed old one). Fix: enter the node's password again in Settings >
  Remote nodes, which resets McApp's stored mark for that node; Connect then works.
- **`GET /api/node-admin/targets/{target}/state`** returns the parsed last known state (concept Appendix A), folded in
  send order from verified replies only. Values carry their age; after a verified Restart they are marked stale.
- **Call blocklist card (2026-10-24).** Needs a fork build with BLK-01 (v4.40a.10.23 or newer); older firmware
  shows "no call blocklist". `Read` fetches the list; `Save list` REPLACES the node's whole list and the node's answer
  is the stored list, which the card compares with what was sent ("Node confirmed the new list." or the difference).
  Refusals, all before anything is sent: an entry that would block this station or the node itself; a list over 39
  characters (one RM1 frame, about 3-4 calls); a write before the list was read. A list the node holds that is longer
  than one remote write (set on the node's own web page or console, up to 16 calls) is read-only here; only
  `Clear all` works. A station on the blocklist is dropped as sender AND as relay, so blocking a relay between McApp
  and the node makes remote replies vanish silently.

**Secrets and logs.** The password is never logged, broadcast or stored in `stall_events` (the `/api/node-admin/keys`
body is withheld by path prefix). An RM1 frame in logs, `send_failed` events and failed monitor captures has its tag
cut (`_redact_rm1`), including the INFO/DEBUG "Processing" lines. A frame that did go out is public on air anyway.

