# Node Admin: human-readable remote view - concept

Date: 2026-10-06. Status: SIGNED OFF 2026-10-06 (draft 2, after `/fable-review` the same day). Implementation in progress, wave log in §12. Review record: §13 and
`doc/2026-10-06_1100-node-admin-remote-view-verdict.md`. Repos: backend `~/WebDev/MCProxy` (branch `development`),
frontend `~/WebDev/webapp` (separate repo, commit independently). Firmware ground truth: `MeshCom-Firmware-DEV-Main`,
branch `fork-dev` (`91d378cb`): `src/remote_cmd.cpp`, `src/rm_runtime.cpp`, `src/rm_sender_policy.h`,
`src/web_functions/web_rm_page.cpp`, `docs/rm-gui/extended-commands-concept.md` (draft 2) and its verdict. Builds on
`doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md` (Node Admin v1, shipped).

## 0. BLUF

The `/nodeadmin` page today is a command composer plus raw reply text (`ok v=4.40a up=417 bat=0 heap=115 s=gtdMwl
p=2/22 led=0`). It becomes a **remote twin of the BLE page and the firmware's own Remote page**: a register bar, an Info
row, the BLE card grid, a Switches grid with green = on, a separate Restart group and a collapsed Advanced section.
Every value comes from a verified RM1 reply, parsed by the backend into a "last known state" with its age.

- **Connect** (the "login") sends `sync` then `status`. It is required once per McApp process before any other
  command, and it fills the required registers. Selecting a node transmits nothing.
- **Reuse** is presentational: `NodeSettingsGroup`/`EditableField` unchanged, one extracted `ToggleTile` (the BLE tile
  button and its CSS), one extracted `SettingsGrid`. The BLE cards are not rewired. Characterisation specs for the BLE
  page come first, because none exist today.
- **Phase A works with today's firmware**: status, six switches, TX power, send position/track, restart, output pin.
- **Phase B** (the firmware's draft-2 commands: radio, name, APRS text, position, sensors, heard list, queue, mailbox,
  max hop) is fully designed here (§3.2, §4, §5). Now: the `RESULT_MAX` raise, a generic `key=value` tokenizer, `rm=`
  detection and a data-driven card registry. Per-command parsers, cards, allowlist entries and the capability gate land
  **when the firmware lands**, against its vectors. Until then nothing about Phase B is visible.
- **The review found four bugs in shipped v1 code** (W0, ship first as a release): re-ask bypasses the lockout guard
  and can repeat past the node's cache window; the second transport copy of a sync reply revives an older failed sync
  row; a reply longer than 63 characters is dropped without a trace and turns a healthy node into two `no_reply` rows
  that trip McApp's own guard; `txpower` is bounded only by the key's `tx_max`, not by the board maximum the node
  reported, so a too-high value is a counted reject.
- **Firmware asks** (operator is the firmware author, §11): exempt `RM1 ` DMs from the DM retry ladder for every origin,
  fix where the `rm=2` token goes, and put compact `status` strings into `remote_cmd_vectors.json`.

## 1. Requirements (operator, 2026-10-06) and how they are met

| #   | Requirement                                                                          | Outcome                                                                                                                                          |
| --- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| R1  | Human-readable instead of raw reply text                                             | Backend parses replies; cards, switches and badges show labelled values with units and age (§3, §5)                                              |
| R2  | Buttons that set and unset things; green = active on the remote node                 | Switches grid from the `s=` letters, tap toggles, green from the last verified state, amber when uncertain (§3.3)                                |
| R3  | "Refresh status" gathers the remote status                                           | `Refresh status` first in the Info row, as on the firmware page                                                                                  |
| R4  | Improve the vocabulary from what the firmware does and offers                        | Firmware page grouping and labels kept (Info, Switches, Restart, Advanced; Light, Track); hints and error sentences from firmware semantics (§5) |
| R5  | Do not reinvent the wheel: reuse the BLE compact view                                | Register bar look, card grid, `NodeSettingsGroup`, tile button reused (§6)                                                                       |
| R6  | "Must" fields on login (sync + status), the other registers swept later              | Connect = sync + status; Read all sweeps the optional registers; each badge readable on its own (§4)                                             |
| R7  | Remote looks different: not all values are available                                 | Only cards an RM1 command can fill; no `--` cards for WiFi, groups, BLE PIN, time zone, via (§3.2)                                               |
| R8  | Focus on what the firmware extended-command concept delivers, ignore its open issues | Phase B designed in full against draft 2; implemented when the firmware lands; draft-2 open items out (§10)                                      |
| R9  | Every answer fits 140 7-bit-safe characters                                          | The node guarantees it; McApp accepts results up to 108 (wire 140) and tests the 108/109 boundary; McApp-sent frames bounded (§7)                |

## 2. Facts that shape the design

### 2.1 Firmware today (fork-dev `91d378cb`)

| Fact                                                                                                                                                                                                                                  | Where                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| 13 commands: `reboot status sendpos sendtrack sync` (no args), `gps track display led gateway mesh` (`on`/`off`), `txpower <n>` (0..`TX_POWER_MAX` of the board), `setout <a0..b7> <on/off>`                                          | `remote_cmd.cpp:130-157`                       |
| `status` reply: `ok v=<ver> up=<min> bat=<%> heap=<kB> s=<letters> p=<cur>/<max>[ led=<0/1>]`, worst case 61-62 of 63 characters                                                                                                      | `rm_sender_policy.h:361-385`                   |
| `s=` letters, fixed order G T D M W L = GPS, Track, Display, Mesh, gateWay, LED. Upper case = on. `L` omitted on boards without an LED. D = display on (`!bDisplayOff`)                                                               | `rm_sender_policy.h:338-354`                   |
| Older status form `gw=<0/1> mesh=<0/1>` (only those two switches plus `led=`)                                                                                                                                                         | `rm_sender_policy.h:389-455`                   |
| Replies: switch `ok <name>=on/off`; `ok txpower=<n>`; `ok <pin>=on/off`; `ok sent`; `ok rebooting` (reboot follows after a short delay); `sync` (ctr 0) `ok ctr=<hwm> v=<ver>`                                                        | `rm_runtime.cpp:165-275, 567-575`              |
| `gps off` also switches Track off; the reply says only `gps=off`. No other switch has a cross-effect                                                                                                                                  | `command_functions.cpp:1965-1975`              |
| LED is RAM only, off after every boot. GPS, Track, Display, Mesh, Gateway and TX power are saved, but boot can override some (gateway off without WiFi credentials, display on at boot on some boards)                                | `loop_functions.cpp:98-102`, settings paths    |
| Tagged `err` replies on air: `failed`, `not output`, `unsupported`, `storage` (`storage` = not executed). None count toward the lockout                                                                                               | `rm_runtime.cpp:213-269, 526`                  |
| Silent rejects. Counted: bad tag, replay, not allowlisted / bad argument shape, format errors in `rmCheck`. Not counted: rate (10 s), lockout, disabled. 3 counted in 90 s lock RM for 5 min                                          | `remote_cmd.cpp:185-202`, `remote_cmd.h:40-44` |
| The node resends every DM it originates, `RM1 ` included, from McApp's BLE and UDP path alike (`{NNN` suffix, up to 4 keyings). A late keying of an old frame is a counted replay strike or a silent rate drop                        | `loop_functions.cpp:4359-4361, 4413-4421`      |
| One high-water mark, one rate stamp, one reject count per node, shared by all senders (McApp, the firmware's own Remote page, a second SysOp)                                                                                         | `remote_cmd.cpp`                               |
| `bat=0` (no measurement) and `bat=100` (T-Beam PMU without a cell) can both mean "no battery reading". `up=` is 32-bit millis in minutes and wraps at 49.7 days                                                                       | `rm_runtime.cpp:176-195`                       |
| Track has an operator-fixed warning: "degraded MeshCom RX performance, packetloss likely"                                                                                                                                             | `track_warning.h:21-22`                        |
| Firmware Remote page order: Info (Refresh status, Send position now, Send track now), Switches (GPS, Track, Display, Light, Mesh, Gateway; caption "State from the last answer of X, N ago"), TX power, Restart (own group), Advanced | `web_rm_page.cpp`                              |

Live sample (DK5EN-1, 2026-10-06): `ok v=4.40a up=417 bat=0 heap=115 s=gtdMwl p=2/22 led=0` = GPS off, Track off,
Display off, Mesh on, Gateway off, Light off, TX 2 of 22 dBm, up 6 h 57 min.

### 2.2 Firmware extended commands (draft 2, not implemented)

Reply wire `RM1 <ctr> <result> <tag>` <= 140 characters, result <= 108 including `ok `/`err `. Reply bytes: space,
letters, digits and `- . / = + _ @ ? ( ) , * #`; anything else arrives as `?`; `:` never appears. Grammar: single
spaces, `key=value`, `-` = absent, free text last. Arguments may contain A-Z. New commands only go to a target that
reports `rm=2`; an unknown command is a counted reject even with the right password. **Where `rm=2` appears is not
decided in draft 2** (`status` plus ` rm=2` is 66-67 characters, over the 63 that draft 2 keeps for old commands).

| Cmd                 | R/W | Reply after `ok `                                                                                     | Error tokens   |
| ------------------- | --- | ----------------------------------------------------------------------------------------------------- | -------------- |
| `radio`             | R   | `f=433.175 sf=11 cr=5 bw=250 p=10/22`                                                                 |                |
| `txpower <dBm>`     | W   | `txpower=10` (unchanged)                                                                              |                |
| `name [text]`       | R/W | `n=Martin` (1..19 chars)                                                                              | `text`         |
| `atxt [text]`       | R/W | `a=MeshCom Garten` (1..39 chars)                                                                      | `text`         |
| `pos [lat lon alt]` | R/W | `48.40812 11.73812 492 gps` (source `gps`, `nofix`, `set`); request args up to 26 chars               | `range`, `gps` |
| `sens`              | R   | `t=21.4 h=45 p=1013.2 t2=-`                                                                           | `unsupported`  |
| `mh <row>`          | R   | `<total> <next row or -> <call> <min> ...` (up to 7 rows)                                             | `end`          |
| `mh <call>`         | R   | direct `d g= m= r= s= la= lo= di= a= n= x= h= t=`; route `r h= k= g= m= rc=<cost>@<call> t= v=<path>` | `unknown`      |
| `txq`               | R   | `q=3/20 bp=quiet tx=.. rt=.. dr=.. u=12`                                                              |                |
| `mbox`              | R   | `m=heard u=12/50 b=1834 a=3/20 st=.. dl=.. ak=.. dr=.. bl=.. nt=..`                                   | `unsupported`  |
| `maxhop`            | R   | `t=4 p=2`                                                                                             |                |

New node-side error tokens: `range text unknown unsupported end` (`range`/`text` do not count toward the lockout).
`busy` is a refusal of the firmware's own sender, never a node reply.

### 2.3 McApp today

| Fact                                                                                                                                                                                                                                                                                                                      | Where                                                                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Allowlist is a hardcoded dict mirroring firmware `allowed()`. `_check_text` runs before it, knows no command, and refuses upper case and args over 23 characters for every command                                                                                                                                        | `remote_cmd.py:62-76, 227-275`                                        |
| `parse_reply` caps the result at `RESULT_MAX = 63`. A longer reply returns `None`, is not stored or logged, the row stays `waiting` and becomes `no_reply`; two of those trip McApp's own 5 min guard                                                                                                                     | `remote_cmd.py:307-330`, `node_admin_service.py`                      |
| No parsed or last-known node state anywhere (no table, no column, no endpoint); schema v35                                                                                                                                                                                                                                | `storage/node_admin.py`                                               |
| `node_admin_log` keeps every row; pruned to 1000 rows per target **only at `start()`**, by id, regardless of command. `history()` and `_broadcast_row` look at the newest 100 rows only                                                                                                                                   | `storage/node_admin.py:349`, `node_admin_service.py`                  |
| SSE `node_admin:reply` fires on hand-off, bad tag and verified reply. `no_reply` and `abandoned` are derived by clock and never pushed                                                                                                                                                                                    | `node_admin_service.py:437-450`                                       |
| Row states `queued send_failed waiting no_reply verified bad_tag abandoned`; one command in flight per target; 10 s spacing; guard after 2 `no_reply` within 5 min. Re-ask does not consult the guard                                                                                                                     | `node_admin_service.py:48-135, 580-608`                               |
| Auto-sync before the first command per process (`_synced`, memory only). On a 90 s sync timeout the held command is sent anyway (v1 U3/D2); the firmware's own sender drops it (`nosync`). `set_key` does not clear `_synced`                                                                                             | `node_admin_service.py:500-530`                                       |
| A ctr-0 (sync) reply binds to the newest unanswered sync row of any age; the second transport copy of a fresh reply binds to an older failed one                                                                                                                                                                          | `storage/node_admin.py:316-322`                                       |
| Webapp: target `<select>`, `NodeAdminComposer`, `NodeAdminAnswer` (raw text, holds the only countdown), `NodeAdminHistory`; allowlist twin `utils/nodeAdminCommands.ts`; store `tick()` driven only by the view's clock                                                                                                   | webapp `views/NodeAdminView.vue:55-57`, `components/nodeadmin/`       |
| BLE page: `NodeSettingsGroup` + `EditableField` props-driven; `Node*Card`s read `useBleStore` and send through the BLE queue; `.toggle-btn` CSS is scoped in `BtQuickActions` and shared by Toggles, Commands, External UDP; **no spec mounts `BtQuickActions`, `BtRegisterStatus`, `BtNodeSettings` or `BluetoothView`** | webapp `components/bluetooth/`, `BtQuickActions.vue:139-205, 223-340` |

## 3. The page

### 3.1 Layout (top to bottom)

1. **Header**: target chooser (keyed targets only, link to Settings > Remote nodes), transport chip (BLE/UDP),
   `Connect` button, one state sentence (§5.3), and "Connected N min ago" once Connect succeeded in this process.
2. **Register bar**: REQUIRED `Sync` `Status`; OPTIONAL badges only for registers the target supports (none until a
   target reports `rm=2`). `Read all` / `Stop` with a progress count when optional registers exist.
3. **Info**: Refresh status, Send position now, Send track now.
4. **Node configuration grid** (`SettingsGrid`, 3/2/1 columns): the remote cards of §3.2. Each card header shows the
   age of its newest value.
5. **Switches**: GPS, Track, Display, Light, Mesh, Gateway, caption "State from the last answer of <call>, <age> ago."
6. **Restart**: own group, danger tile.
7. **Advanced** (collapsed `<details>`): output pin (a0..b7 on/off), Re-sync counter, the raw command composer with its
   "Will send" preview (its confirm modal moved outside the `<details>`), and the sent-commands history.

Until Connect succeeds, everything below the register bar shows the last known state read-only and every send control
is disabled with "Connect first".

### 3.2 Remote cards and their source

| Card (BLE title)          | Fields                                                             | Source                     | Phase | Editable                                                                 |
| ------------------------- | ------------------------------------------------------------------ | -------------------------- | ----- | ------------------------------------------------------------------------ |
| Identity                  | Call (target), Firmware, Uptime, Free memory, Battery              | `status`, `sync` (version) | A     | no                                                                       |
| Radio                     | TX power (current, board max)                                      | `status` `p=`, `txpower`   | A     | TX power select 0..min(board max from `p=`, key `tx_max`), confirm first |
| Radio (cont.)             | Frequency, Spreading factor, Coding rate, Bandwidth                | `radio`                    | B     | no                                                                       |
| APRS                      | Name, Text                                                         | `name`, `atxt`             | B     | yes, 19/39 limits, forbidden-character hint before send, confirm first   |
| GPS / Position            | Latitude, Longitude, Altitude, Source (GPS / no fix / set by hand) | `pos`                      | B     | only while GPS is off (`err gps` otherwise)                              |
| Weather                   | Temperature, Humidity, Pressure, Temperature 2                     | `sens`                     | B     | no                                                                       |
| Network                   | Max hop (text / position), TX queue, Mailbox                       | `maxhop`, `txq`, `mbox`    | B     | no                                                                       |
| Heard by this node (wide) | Call, minutes ago; per-row Details (signal, distance, route)       | `mh <row>`, `mh <call>`    | B     | no                                                                       |

Phase B cards appear only for a target with `rm=2`. TX power uses the confirm-gated select pattern of the BLE
`NodeRadioCard` country field (value reverts on Cancel).

### 3.3 Switches

`buildRemoteToggleActions(state)` returns the BLE `{id, label, icon, active, ...}` shape. Do **not** copy the BLE
Display inversion (`active: !SN.DISP`): the remote `D` letter already means display on.

| Tile state | When                                                                                                                                   | Look                    |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| on / off   | last verified evidence (status snapshot or a newer ack)                                                                                | green / grey            |
| uncertain  | a `no_reply`, `send_failed`, `bad_tag` or `abandoned` row for that switch is newer than the last evidence (it may or may not have run) | amber, "Refresh status" |
| pending    | a row for that switch is `queued`/`waiting`                                                                                            | BLE pending colour      |
| unknown    | no status yet, or the board has no LED                                                                                                 | small On / Off buttons  |

- All send controls of the target are disabled while any of its rows is in flight (the backend allows one).
- Confirm first (danger modal): Mesh off, Gateway off, Track on (shows the firmware warning), Restart, TX power, output
  pin, and every Phase B write.

## 4. Connect, sweep, pacing

### 4.1 Register bar

A badge per register: green = a parsed verified value exists (tooltip: age and raw reply); amber = older than 1 h, or
verified but unparseable (tooltip shows the raw text); outlined = never read; orange outline = required and never read
(BLE colour); pulsing = in flight. Clicking a badge reads that register. Optional badges exist only for an `rm=2`
target.

| Badge    | Command(s) | Required | Phase |
| -------- | ---------- | -------- | ----- |
| Sync     | `sync`     | yes      | A     |
| Status   | `status`   | yes      | A     |
| Radio    | `radio`    | no       | B     |
| Name     | `name`     | no       | B     |
| Text     | `atxt`     | no       | B     |
| Position | `pos`      | no       | B     |
| Sensors  | `sens`     | no       | B     |
| Max hop  | `maxhop`   | no       | B     |
| Queue    | `txq`      | no       | B     |
| Mailbox  | `mbox`     | no       | B     |
| Heard    | `mh 0`,... | no       | B     |

### 4.2 Flows

- **Connect** = `sync`, then `status` once the sync row is verified and the 10 s window is open. Replies take 12-32 s,
  so about 30-70 s with a two-step progress ("1/2 counter in step", "2/2 status"). If the sync succeeds and the status
  fails, Connect counts as done (the counter is in step) and Status stays empty.
- **No command before a verified sync** in this McApp process (backend-enforced, §8). The UI keeps every send control
  disabled until Connect succeeded. A command that still reaches the backend for an unsynced target (raw composer,
  second tab) is held behind v1's automatic sync; if that sync gets no answer, the held command is **dropped** (operator
  decision D2, as the firmware's sender does) instead of being sent anyway. Only a verified sync marks a target synced.
  v1 U3/D2 are amended accordingly; U4 (replies also in the DM conversation) stands.
- **Read all** (Phase B) = the optional registers in table order except Heard; 8 reads take about 3-6 min.
- **Heard** (Phase B) is its own action: `mh 0`, then `mh <next>` until `-`, 7 rows per reply. 20 nodes = 3 replies;
  128 = about 22 (8-15 min, about 100 s of channel time at the default profile, 540 s at the slow one). The list is
  shown only once complete; a stopped load keeps the previous complete list.
- **Stop** on: Stop pressed, target changed, a silent or unverified outcome (`no_reply`, `bad_tag`, `send_failed`,
  `abandoned`, a 409) or `err storage`. Other tagged errors (`unsupported`, `not output`, `failed`, `unknown`, `end`)
  prove the password and count nothing: record and continue. Stop on silence takes up to 120 s (the reply window).
- **Cool-down**: after any silent row (`no_reply`, `abandoned`) the target is held for 180 s before the next frame, so
  a late retry keying of the old frame cannot land behind a new one (§2.1 retry ladder). Re-ask is the exception (same
  ctr, the node answers it from its cache) and stays bounded as in §8.
- **Other senders**: when McApp overhears RM1 frames to or from the target that are not its own, sequences pause for
  120 s and the header says "Another station is managing <call>".
- The sequencer runs client-side in the `nodeAdmin` store with its own timer (not the view's clock). Closing the tab
  stops it, which is the safe direction. A second tab gets a 409 from the one-in-flight rule and stops.
- McApp's lockout guard is a **backstop**, not a floor: it cannot see other senders' strikes or late retry keyings.
  The invariants that keep McApp safe are: one frame in flight, 10 s spacing, Connect first, cool-down after silence,
  re-ask bounded, refusal before allocation of anything the node would count (§8).

### 4.3 State model ("last known state")

Derived on read, no migration, from a dedicated query: the newest **verified** (`verified = 1`, `ok` or `err`) row per
`(cmd, args)` of the target, plus every newer non-verified row (for "uncertain").

- **Order is send order (log `id`, equal to ctr order for ctr > 0), never `reply_at`.** The node runs a command only
  above its high-water mark, so a verified reply for row N describes the state right after N, however late it arrives
  (late status, revived abandoned row, re-ask from the cache). `reply_at` is shown as "as of" only.
- The newest verified `status` is an atomic snapshot: it replaces every status-owned field (a field absent from it is
  unknown, e.g. no `L`). Acks with a higher id apply on top.
- Effects per reply:

| Reply                          | Effect                                                                                    |
| ------------------------------ | ----------------------------------------------------------------------------------------- |
| `status` ok                    | snapshot: Firmware, Uptime, Battery, Free memory, six switches, TX power cur/max, has_led |
| `<switch>=on/off`              | that switch; `gps=off` also sets Track off                                                |
| `txpower=<n>`                  | TX power current                                                                          |
| `<pin>=on/off`                 | that output pin (Advanced only)                                                           |
| `sync` ok                      | Firmware version, capability (if it carries the token), counter in step                   |
| `rebooting`                    | boundary: Light off, Uptime unknown, every other field stale until the next status        |
| `sent`, `err storage`          | no effect                                                                                 |
| `err failed` on a switch       | the switch kept its old value: set it to the opposite of the requested one                |
| `err failed` on txpower/setout | that value unknown                                                                        |
| `err unsupported` on `led`     | has_led = false                                                                           |
| `err not output`               | that pin is not an output                                                                 |

- A lower `up=` than expected from the previous status is only a reboot hint (wrap at 49.7 days), never a boundary.
- **Capability** is sticky per carrier: only a reply type that carries the `rm=` token can set or clear it; absence
  in a reply type that never carries it changes nothing. It resets to 1 on a firmware version change (`v=`), a verified
  `reboot`, or a `no_reply` of a Phase B command.
- Pruning (still only at `start()`) keeps the newest verified row per `(cmd, args)` of every target.
- `/state` returns the Pi's `now_ms` and `as_of_id`; the webapp computes ages from `now_ms`, drops responses for
  another target or with a lower `as_of_id`, and refetches on select, on `node_admin:reply` for the selected target, on
  SSE reconnect (`system:connected`) and when the tab becomes visible. `no_reply` is derived client-side by
  `effectiveState`, as today.

## 5. Vocabulary

Firmware page labels kept; hints from firmware semantics.

### 5.1 Commands and switches

| Raw                   | Label                     | Hint / confirm text                                                                                           |
| --------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `sync`                | Re-sync counter           | "Brings McApp's counter in step with the node. Part of Connect."                                              |
| `status`              | Refresh status            |                                                                                                               |
| `sendpos`             | Send position now         | "The node sends one position beacon."                                                                         |
| `sendtrack`           | Send track now            | "The node sends one track beacon."                                                                            |
| `reboot`              | Restart                   | "The node answers, then restarts a few seconds later. It can take a minute until it answers again."           |
| `gps on/off`          | GPS                       | Off: "Switching GPS off also switches Track off."                                                             |
| `track on/off`        | Track                     | On (confirm): "Track: degraded MeshCom RX performance, packetloss likely."                                    |
| `display on/off`      | Display                   |                                                                                                               |
| `mesh on/off`         | Mesh                      | Off (confirm): "The node stops relaying other stations."                                                      |
| `gateway on/off`      | Gateway                   | Off (confirm): "The node stops forwarding to the MeshCom server and no longer announces itself as a gateway." |
| `led on/off`          | Light                     | "The board LED. It stays as set until switched or the node restarts."                                         |
| `txpower <n>`         | TX power                  | "Set TX power of <call> to <n> dBm?"                                                                          |
| `setout <pin> on/off` | Output pin <pin> on/off   | "Only works if the pin is set as an output on the node."                                                      |
| `name`, `atxt`, `pos` | Name, APRS text, Position | Writes confirm with the exact new value                                                                       |

### 5.2 Status fields

| Token   | Label       | Display                                                                   |
| ------- | ----------- | ------------------------------------------------------------------------- |
| `v=`    | Firmware    | as sent (`4.40a`)                                                         |
| `up=`   | Uptime      | minutes -> `6 h 57 min`, `3 d 4 h`, "as of <age>"                         |
| `bat=`  | Battery     | `87 %`; `0` and `100` add the hint "0 or 100 can mean no battery reading" |
| `heap=` | Free memory | `115 kB`                                                                  |
| `p=a/b` | TX power    | `2 dBm` (max `22 dBm`)                                                    |
| `s=`    | Switches    | §3.3                                                                      |

Phase B labels follow §2.2; counters marked `..` there (queue, mailbox) are labelled from the firmware formatter once
it exists.

### 5.3 Row states and errors

One `STATE_BADGE` source in the vocabulary util, used by header, history and tiles.

| State / token  | Badge            | Sentence                                                                                                                                                                                 |
| -------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `queued`       | Handing to radio | "Handed to the radio. It goes on air in a moment."                                                                                                                                       |
| `waiting`      | Waiting          | "Sent. Waiting for the node to answer, this can take up to a minute."                                                                                                                    |
| `verified` ok  | Done             | per command, e.g. "Done. GPS is now on."                                                                                                                                                 |
| `verified` err | Refused          | per token below                                                                                                                                                                          |
| `no_reply`     | No answer        | "No answer after 2 minutes. Possible causes: wrong password, out of range, remote management off, the node was busy with another station's command. Wait 3 minutes before the next try." |
| `bad_tag`      | Not trusted      | "An answer arrived but could not be verified. Do not trust it."                                                                                                                          |
| `send_failed`  | Not sent         | "McApp could not hand the command to the node: <reason>."                                                                                                                                |
| `abandoned`    | Abandoned        | "McApp restarted before an answer arrived."                                                                                                                                              |
| `failed`       |                  | "The node tried, but the setting did not change."                                                                                                                                        |
| `not output`   |                  | "That pin is not set as an output on the node."                                                                                                                                          |
| `unsupported`  |                  | "This board does not have that feature."                                                                                                                                                 |
| `storage`      |                  | "The node could not save its counter, so it did not run the command."                                                                                                                    |
| `range` (B)    |                  | "The value is out of range for this node."                                                                                                                                               |
| `text` (B)     |                  | "The text contains a character the node does not accept."                                                                                                                                |
| `gps` (B)      |                  | "Switch GPS off before setting the position by hand."                                                                                                                                    |
| `unknown` (B)  |                  | "The node does not know that station."                                                                                                                                                   |
| `end` (B)      |                  | "No more entries."                                                                                                                                                                       |
| any other      |                  | "The node reported an error: <token>."                                                                                                                                                   |

## 6. Reuse of the BLE view

| Piece                                         | Decision                                                                                                                                                                                                                            |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Characterisation specs                        | **First step.** Specs on the unmodified `BtQuickActions`, `BtRegisterStatus`, `BtNodeSettings` (rendered tiles, active/pending/disabled classes, data-test ids, Display inversion), plus a visual check of `/bluetooth` at 3 widths |
| `NodeSettingsGroup`, `EditableField`          | Reused unchanged in Phase A. Phase B adds a counter/hint slot and `step` as additive props                                                                                                                                          |
| `.settings-grid` (scoped, `BtNodeSettings`)   | Extracted into `SettingsGrid.vue`; BLE and remote use it                                                                                                                                                                            |
| `.toggle-btn*` CSS (scoped, `BtQuickActions`) | Extracted into one `ToggleTile.vue` (a button owning all tile states: active, pending, disabled, danger, uncertain); used by every BLE section and the remote page. BLE keeps its layout CSS and its ack code                       |
| `BtRegisterStatus`                            | Not extracted. The remote register bar is its own component reusing the badge look (it needs 6 states and click; BLE has 2 states on spans)                                                                                         |
| `Node*Card`s                                  | Not rewired. Remote cards are small new components that build `SettingsField[]` and render `NodeSettingsGroup`                                                                                                                      |
| `NodeAdminAnswer`                             | Removed; its countdown moves into the header state sentence. History and view specs are rewritten for the new labels                                                                                                                |

Rejected: a data-source abstraction under every BLE card (remote data is a small subset in another shape, BLE editing
is tied to register echoes, and the working BLE page has no specs to catch a regression).

## 7. Limits and the 140-character budget

| Item             | Today                         | Change                                                                                                                           |
| ---------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `RESULT_MAX`     | 63                            | 108 in W0, shipped as a production release before any `rm=2` firmware. Rejected RM1 replies are logged at WARNING                |
| `REPLY_MAX`      | 160                           | stays (140 + 4-character `{NNN` + margin)                                                                                        |
| Sent args        | 23, lower case, every command | per-command (Phase B): `name` 19 and `atxt` 39 with A-Z and the firmware's refuse-never-alter charset, `pos` 26, `mh <call>` A-Z |
| McApp-sent frame | -                             | longest `atxt`: `RM1 <10-digit ctr> atxt <39> <16 hex>` = 76 characters                                                          |

Tests: results of 108 accepted and 109 refused, a 140-character reply wire with a 10-digit counter, and the existing
63-limit tests (`remote_cmd_tests.py:430, 441`) rewritten in the same commit.

## 8. Backend changes (MCProxy)

**W0, v1 fixes** (each with a regression test that fails before):

1. Re-ask consults the lockout guard; the 120 s and 10 min windows count from the first hand-off; one re-ask per row.
2. A ctr-0 reply binds only to a sync row inside its 120 s window with `ctr=<hwm> >= last_hwm`; the second transport
   copy of an already verified reply is dropped.
3. `RESULT_MAX` 108; a refused RM1 reply is logged.
4. `txpower` is bounded by `min(tx_max, board max from the newest verified p=)` before a counter is allocated.
5. `set_key` clears `_synced` for that target; `_broadcast_row` finds the row by id, not in the newest 100.

**Phase A:**

6. `node_admin_state.py` (new, pure): `key=value` tokenizer, `status` parser (compact and `gw=/mesh=` forms), reply
   effect table (§4.3), `fold(rows) -> NodeState`, capability rule. No I/O; unparseable bodies yield "unparsed".
7. Storage: newest verified row per `(cmd, args)` plus newer rows, in one bounded query; prune exemption.
8. `GET /api/node-admin/targets/{target}/state` behind the LAN guard: `{target, now_ms, as_of_id, connected,
in_flight, next_allowed_at, cooldown_until, possible_lockout_until_ms, capability, has_led, fields, switches,
registers}`.
9. Sync gate: a command for a target without a verified sync in this process stays held behind the automatic sync
   (v1) and is dropped with a `node_admin:reply` warning when that sync gets no answer; `_synced` is set only by a
   verified sync, never by the timeout.
10. Cool-down 180 s after a silent row; 120 s pause after foreign RM1 frames to or from the target.
11. Every refusal (Connect, capability, cool-down, txpower bound) happens before counter allocation, in
    `_allocate_and_transmit` so the held-behind-sync path cannot skip it.

**Phase B (W4, with the firmware):** per-command parsers, allowlist entries, per-command args, capability gate, the
firmware's updated `remote_cmd_vectors.json` copy and its new sha256 pin, the webapp's allowlist twin.

## 9. Tests

| Area           | Must include                                                                                                                                                                                                                     |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Status parser  | Strings produced by the firmware (the native-test literals of `test_rm_sender_policy/test_main.cpp:553-633` now, the firmware's vectors once added): live DK5EN-1 line, 5-letter `s=`, `gw=/mesh=` form, 62-character worst case |
| Fold           | late status vs newer ack (id order wins), revived abandoned row, re-ask, `gps=off` clears Track, reboot boundary, atomic snapshot drops `L`, `err failed` on a switch, bad_tag never feeds state, uncertain after no_reply       |
| Capability     | set by carrier, not cleared by a non-carrier, reset on version change / reboot / Phase B no_reply                                                                                                                                |
| Refusals       | Connect precondition, cool-down, txpower bound, capability: 409 with **no row and no counter allocated** (counter unchanged in `node_admin_state`)                                                                               |
| W0 regressions | one per bug, failing before the fix                                                                                                                                                                                              |
| Route          | `/state` behind the LAN guard (foreign Host/Origin 403)                                                                                                                                                                          |
| Webapp         | characterisation specs before extraction; per-letter switch vectors (no Display inversion); sequencer stop/continue table; stale-response drop; one `STATE_BADGE`                                                                |
| Registration   | the new suite is wired into `run_startup_tests.py` `main()`                                                                                                                                                                      |

Mutation targets the suite must catch: fold ordered by `reply_at`; `gps=off` not clearing Track; capability cleared
by a non-carrier; refusal after allocation; `RESULT_MAX` back to 63; Display inverted in the remote builder; sequencer
stopping on `err unsupported`.

## 10. Out of scope

- Draft-2 open items: radio writes, `maxhop` write, the firmware sender policy, the `sync` replay limiter.
- The node's own "Executed / rejected / high-water" counters and its "Last commands executed on this node": no RM1
  command reads them.
- WiFi, groups, BLE PIN, time zone, via, sensor configuration of a remote node.
- Periodic background polling: every read is operator-started (airtime; every RM1 frame is public on the MeshCom
  server and the mcmap archive).

## 11. Decisions (operator, 2026-10-06)

| #   | Question                                                                                           | Decision                                                                                     |
| --- | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| D1  | Connect on the button only, or on selecting a target                                               | Button only                                                                                  |
| D2  | Auto-sync timeout: drop the held command (firmware behaviour) instead of sending it anyway (v1 U3) | Drop                                                                                         |
| D3  | Firmware: where does `rm=2` go                                                                     | Handed to the firmware maintainer (`~/Desktop/2026-10-06_mcapp-rm1-firmware-asks.md`, Ask 1) |
| D4  | Firmware: no DM retry ladder for `RM1 ` DMs from any origin                                        | Handed over (Ask 2); McApp keeps the 180 s cool-down either way                              |
| D5  | Firmware: compact `status` strings into `remote_cmd_vectors.json`                                  | Handed over (Ask 3); until then McApp tests with the firmware's native-test literals         |
| D6  | Raw composer under Advanced                                                                        | Keep                                                                                         |

## 12. Waves

Wave log (one line per wave, updated after each wave):

- W0: done (advisor rework: negative `p=` current power)
- W1a, W1b, W1c: done (advisor rework: silent `gps off` marks Track, `has_led: null` accepted)
- W2a-W2d, W3: pending
- W4: blocked on firmware draft 2

Plan:

| Wave | Repo    | Content                                                                                                       | Depends on        |
| ---- | ------- | ------------------------------------------------------------------------------------------------------------- | ----------------- |
| W0   | MCProxy | v1 fixes 1-5 (§8) with regression tests; production release                                                   | -                 |
| W1a  | MCProxy | `node_admin_state.py` + tests (pure, new files)                                                               | - (parallel W0)   |
| W1b  | webapp  | Characterisation specs, then `ToggleTile` + `SettingsGrid` extraction                                         | -                 |
| W1c  | webapp  | Vocabulary util (`STATE_BADGE`, sentences, formatters), `buildRemoteToggleActions`, sequencer rules (pure)    | -                 |
| W2a  | MCProxy | Storage query, `/state`, Connect precondition, cool-down, foreign pause, refusals in `_allocate_and_transmit` | W0, W1a           |
| W2b  | webapp  | Remote register bar, Info row, cards, Switches, Restart                                                       | W1b, W1c          |
| W2c  | webapp  | Store: `/state` fetch and refetch triggers, own timer, Connect sequencer                                      | W1c, W2a contract |
| W2d  | webapp  | View integration, Advanced section, spec rewrites (orchestrator)                                              | W2b, W2c          |
| W3   | both    | Gates, advisor review, docs, dev release, bench on DK5EN-1                                                    | W2                |
| W4   | both    | Phase B: parsers, cards, allowlist, Read all, Heard, capability gate, firmware vectors                        | firmware draft 2  |

## 13. Review record (2026-10-06)

Seven finders (firmware facts, backend, webapp reuse, protocol/lockout, state model, test audit, requirements/UX),
three adversarial verifiers on the session model (one ran the real service and storage against a temp DB with a fake
clock and transmit). Details and refuted claims: `doc/2026-10-06_1100-node-admin-remote-view-verdict.md`.

What changed from draft 1: fold ordered by id, not `reply_at`; reboot boundary, `gps off` clears Track, atomic status
snapshot, uncertain state; Connect as a precondition and auto-sync drop; cool-down and foreign-sender pause; stop only
on silent outcomes; capability sticky per carrier; four v1 bugs pulled into W0; the guard is a backstop, not a floor;
firmware page order and labels (Light, Track) restored; `busy` removed; Track warning; `ToggleTile` instead of a
toggle-grid extraction, no `RegisterBadgeBar` extraction, characterisation specs first; no new McApp grammar corpus
(firmware strings instead); Phase B split into "now" (tokenizer, `rm=` detection, card registry) and "with the
firmware" (parsers, cards, allowlist, gate).

## Appendix A. `/state` contract (Phase A)

`GET /api/node-admin/targets/{target}/state`, LAN guard as every Node Admin route. Times are epoch ms (Pi clock).
`fold()` in `node_admin_state.py` produces `capability`, `has_led`, `fields`, `switches`, `registers`; the service adds
the rest.

```json
{
  "target": "DK5EN-1",
  "now_ms": 1791268400000,
  "as_of_id": 412,
  "connected": true,
  "in_flight": { "log_id": 412, "cmd": "gps", "args": "on" },
  "next_allowed_at": 1791268410000,
  "cooldown_until": null,
  "foreign_until": null,
  "possible_lockout_until_ms": null,
  "capability": 1,
  "has_led": true,
  "fields": {
    "firmware": {
      "value": "4.40a",
      "at": 1791268069000,
      "log_id": 405,
      "stale": false
    },
    "uptime_min": {
      "value": 417,
      "at": 1791268069000,
      "log_id": 405,
      "stale": false
    },
    "battery_pct": {
      "value": 0,
      "at": 1791268069000,
      "log_id": 405,
      "stale": false
    },
    "heap_kb": {
      "value": 115,
      "at": 1791268069000,
      "log_id": 405,
      "stale": false
    },
    "tx_power": {
      "value": 2,
      "at": 1791268069000,
      "log_id": 405,
      "stale": false
    },
    "tx_power_max": {
      "value": 22,
      "at": 1791268069000,
      "log_id": 405,
      "stale": false
    }
  },
  "switches": {
    "gps": { "state": "pending", "at": 1791268069000, "log_id": 405 },
    "track": { "state": "off", "at": 1791268069000, "log_id": 405 },
    "display": { "state": "off", "at": 1791268069000, "log_id": 405 },
    "mesh": { "state": "on", "at": 1791268069000, "log_id": 405 },
    "gateway": { "state": "off", "at": 1791268069000, "log_id": 405 },
    "led": { "state": "on", "at": 1791268340000, "log_id": 410 }
  },
  "registers": {
    "sync": {
      "state": "ok",
      "at": 1791268029000,
      "log_id": 404,
      "raw": "ok ctr=1791268029 v=4.40a"
    },
    "status": {
      "state": "ok",
      "at": 1791268069000,
      "log_id": 405,
      "raw": "ok v=4.40a up=417 bat=0 heap=115 s=gtdMwl p=2/22 led=0"
    }
  }
}
```

- `fields.*`: absent key = never known. `stale` = a reboot boundary (verified `reboot`) lies after `log_id`.
- `has_led`: `true`/`false` from the newest status (`false` also after `err unsupported` on `led`), `null` until a
  status said so. The Light tile is hidden only on `false`.
- `switches.*`: all six keys always present, each `{state, at, log_id, stale}`; `at`/`log_id` are `null` without
  evidence. A silent or pending `gps off` also marks Track (`gps off` clears Track on the node).
- `as_of_id`: `0` for a target without log rows.
- `switches.*.state`: `on`, `off`, `uncertain` (a silent or unverified row for that switch is newer than the
  evidence), `pending` (a `queued`/`waiting` row for that switch), `unknown` (no evidence; `led` on a board without
  LED is `unknown` with `has_led: false`).
- `registers.*.state`: `never`, `ok` (parsed), `unparsed` (verified but the body did not parse), `pending`.
- `next_allowed_at`: earliest time the service would accept the next frame (10 s spacing, cool-down, foreign pause,
  lockout guard), `null` when now.
- Phase B adds keys to `fields` and `registers`; no existing key changes meaning.
