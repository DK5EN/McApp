# Node Admin remote view: bug fixes and firmware command parity

Approved 2026-10-11 ("go"). Campaign run with `/orchestrate-waves`. This file is the resume point.

## Why

Operator review of v2.1.10 against DK5EN-1 (firmware 4.40a) found:

1. The result box keeps saying "Sent. Waiting for the node to answer" after the reply verified
   (`resultText` in webapp `NodeAdminView.vue` is a one-shot ref).
2. Connect stays enabled while connected (`RemoteConnectHeader.vue`).
3. TX power / Board max read "Not read yet" although `status` answered: firmware `727201de` changed
   `p=<cur>/<max>` to `p=<cur>/<min>/<max>`; `_P_RE` in `node_admin_state.py` is two-part.
4. LED takes `on|off|flash` on the firmware; McApp allowlist, fold and UI know on/off only. The
   status `L` letter is `bRemoteLed`, true for on AND flash, so a status says "on or flash".
5. The node's own web page (signing as DK5EN-98, the same call as McApp) advances the shared
   high-water mark; McApp ignores those replies (`_correlate` finds no row), its counter display
   lags and Observed sessions tags those commands "this McApp".
6. McApp implements the pre-W2 command set (13). Firmware has 23 (`rm_commands.h`): `radio sens txq
mbox maxhop mh name atxt pos chgled` missing. Vendored `remote_cmd_vectors.json` is stale
   (sha `65cbfb36…` vs firmware `8744b368…`).
7. Layout should follow the BLE page card grid.

Firmware references: `MeshCom-Firmware-DEV-Main/src/rm_commands.h`,
`docs/rm-gui/extended-commands-concept.md` §3 (reply grammar per command), §4.3 (remote text is
hostile), `tools/tests/remote_cmd_vectors.json` (canonical corpus).

## Decisions (operator, 2026-10-11)

- Q1 Connect while connected becomes "Re-sync".
- Q2 Connect stays sync + status; every card has its own Read button.
- Q3 Status `L` renders as "on or flash"; the `led` ack names flash. A firmware `led=flash` status
  token is a separate firmware change.
- Q4 A verified reply above McApp's high-water mark raises it (MAX only).
- Q6 Shorter silent cool-down (RM1 no longer has a retry ladder) goes to the backlog.

## Backend -> webapp contract

| State                                          | Source                        | Value                                       |
| ---------------------------------------------- | ----------------------------- | ------------------------------------------- |
| `fields.tx_power` / `tx_power_min` / `_max`    | `status` `p=` (2- and 3-part) | int                                         |
| `switches.led.state`                           | `led` ack / status `L`        | `on` `off` `flash` `on_or_flash` + the rest |
| `fields.chgled`                                | `chgled` ack                  | `auto` `on` `off`                           |
| `fields.radio`                                 | `radio`                       | `{f, sf, cr, bw, p}`                        |
| `fields.name` / `fields.atxt`                  | read or write ack             | string, rendered as plain text              |
| `fields.pos`                                   | `pos`                         | `{lat, lon, alt, src}`                      |
| `fields.sens`                                  | `sens`                        | `{t, h, p, t2}` (`-` -> null)               |
| `fields.txq` / `fields.mbox` / `fields.maxhop` | the reads                     | key/value maps                              |
| `fields.mh`                                    | `mh <row>` pages, `mh <call>` | `{total, next, rows[]}`, `{call: detail}`   |
| Observed exchange `origin` (own sessions only) | ctr in McApp's log or not     | `mcapp` / `other`                           |

## Waves

| Wave | Content                                                                                               | Status |
| ---- | ----------------------------------------------------------------------------------------------------- | ------ |
| 1    | Bugs 1-5: A1 backend parse/allowlist/corpus, A2 counter + observed origin, B1 webapp                  | done   |
| 2    | Command parity: C1 allowlist, C2 fold, D1 APRS/Position, D2 Radio/Sensors/Queues, D3 Heard/Charge LED | done   |
| 3    | Dev release, deploy, live check against DK5EN-1 (transmits: ask first)                                | done   |

### Ownership

| Agent | Files                                                                                                                                                                                                            |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A1    | `remote_cmd.py`, `remote_cmd_tests.py`, `remote_cmd_vectors.json`, `node_admin_state.py`, `node_admin_state_tests.py`                                                                                            |
| A2    | `node_admin_service.py`, `node_admin_service_tests.py`, `node_admin_observed.py`, `node_admin_observed_tests.py`                                                                                                 |
| B1    | webapp `NodeAdminView.vue`, `RemoteConnectHeader.vue`, `RemoteSwitches.vue`, `ObservedSessions.vue`, `observedFormat.ts`, `nodeAdminRemoteState.ts`, `nodeAdminVocabulary.ts`, `types/nodeAdmin.ts`, their specs |
| C1    | `remote_cmd.py`, `remote_cmd_tests.py`                                                                                                                                                                           |
| C2    | `node_admin_state.py`, `node_admin_state_tests.py`                                                                                                                                                               |
| D1-D3 | new card components + specs; `NodeAdminView.vue`, `types/nodeAdmin.ts`, `nodeAdminRemoteState.ts` stay with the orchestrator                                                                                     |

## Wave log

- Wave 1: dispatched 2026-10-11. A1 also carries the full allowlist parity (former C1): syncing
  the corpus makes every new command a failing vector in the same files, so C1 is folded into A1
  and Wave 2 keeps C2 + D1-D3.
- Wave 1 done 2026-10-11. Advisor: approved with rework (LED rule across an unseen reboot via
  `up=`, catch-up hwm pushed to the webapp through `node_admin:observed`, txpower minimum, no
  `origin` on ctr 0, Re-sync honours `sendDisabledReason`), applied and re-approved. Orchestrator
  fixes: `status_tx_power_max` 3-part `p=`, `_handle_then_publish` (observed event after the
  stored catch-up, test p4), `ORIGIN_SCAN = 2000`.
- Open low items from the advisor (Wave 2 or backlog): webapp refetch fires on every own-session
  event once one `other` exchange is in the 24 h window (compare the newest ctr instead); tag
  `other` only on verified exchanges; McApp exchanges read `other` after a Remove + re-add;
  `RemoteConnectHeader` excludes the in-flight reason by its text (check `state.in_flight`).
- Wave 2 dispatched 2026-10-11. Field wire shapes fixed up front in webapp
  `utils/nodeAdminRemoteState.ts` (`RadioValue` .. `MhCallDetail`; `radio`'s `p=` also feeds
  `tx_power*`; `mh` pages accumulate in `{total, next, start, rows}`, lookups in `fields.mh_call`;
  `err unsupported` is `{unsupported: true}`; counters stay verbatim strings). Agents: C2
  (`node_admin_state*`, `node_admin_observed*`: fold + `other` only when verified), D1
  (`RemoteIdentityCard`, new `RemotePositionCard`), D2 (`RemoteRadioCard`, new
  `RemoteSensorsCard`, `RemoteQueuesCard`), D3 (new `RemoteHeardCard`, `RemoteChargeLed`,
  optional `utils/nodeAdminHeard.ts`), E (`stores/nodeAdmin.ts` newest-ctr refetch,
  `RemoteConnectHeader` in-flight check). Orchestrator wires the cards into `NodeAdminView.vue`
  at the gate. Still backlog: `other` after a Remove + re-add.
- Wave 2 done 2026-10-11. Advisor: REWORK, applied by fresh agents, re-check APPROVED. Rework:
  `mh` calls take `?` (firmware turns an illegal byte or a call over 9 chars into `?` and keeps a
  churned row as `?`; one such row used to void the whole page), the observed refetch dedups on
  (ctr, reply present) so the catch-up reply still refetches, Name/APRS text mirror
  `rmTextAllowed` (allowlist; a name takes `,` `/`; atxt refuses only exact `none`), `pos err
hidden` -> `{hidden: true}`, `chgled err unsupported` -> `{unsupported: true}` (blocks writes only
  until a restart makes it stale: a failed PMU init is also reported that way), the pos write
  needs GPS known off and fresh, observed `origin` (`mcapp` too) only on verified exchanges.
- Backlog from the re-check (low, display only): a verified foreign reply whose command copy
  failed verification raises `last_hwm` in the backend but carries no `origin`, so the webapp
  does not refetch the counter. Fix: refetch on a verified reply alone, or publish a
  targets-changed event from the catch-up.
- Wave 3 done 2026-10-11 (operator go for on-air). v2.1.11-dev.1 released (tag parity ok in both
  repos), deployed to mcapp.local slot-2, 14 JSON corpora in the slot, no warnings after restart.
  Live against DK5EN-1 (4.40a) via DK5EN-98 over BLE, log ids 23-33, all `verified`: sync,
  status (`p=2/0/22`, TX power and Board max now render), radio, sens (`err unsupported` -> "No
  sensor on this node"), txq, pos (`nofix`), name, atxt, mbox, maxhop, mh 0 (3 rows, `next -`,
  "Read 3 of 3 · End of the list"). Result box clears to "Done", Connect reads "Re-sync" once
  connected, 10 s spacing and the in-flight refusal held (a second click was refused, one row
  sent). No write commands were sent in this check.
- Found live, open (backlog, all display only):
  - Observed sessions mislabel extended replies: `maxhop t=4 p=2` renders `p=` as "TX power", and
    `mh <call>` renders the detail `s=` as "Switches" (webapp `observedFormat.ts` applies status
    token labels to every reply).
  - Remote card grid leaves empty grey cells beside short cards (Identity, Position); the BLE
    page's grid has none (bug 7 only partly met).
  - BLE badge reads "BLE not connected" on a fresh page load while the backend is connected,
    until the node's `I` register arrives (webapp `bleStore` sets `connected` only from
    `connect BLE result` or `I`; neither is replayed on a new SSE connect). Pre-existing; the
    backend picks the transport itself, so frames still go over BLE.
  - The https origin served the previous bundle from its service worker until "Reload" was
    pressed (known pattern, see memory `project_webapp_sw_stale_verify`).

## Wave 2 resume notes (recon 2026-10-11)

Start here in a fresh session. Wave 1 commits: MCProxy `9bb6fe5`, webapp `1a8f42a`. The
allowlist is already complete (folded into A1); Wave 2 is C2 (fold) + D1-D3 (cards).

- **Reply grammar**: firmware `src/rm_format.h:75-90` and
  `docs/rm-gui/extended-commands-concept.md:132-143`; boundary fixtures in the vendored corpus
  `src/mcapp/remote_cmd_vectors.json` `replies` (no `cmd` field: match by result shape).
  - `radio` `f=433.175 sf=11 cr=5 bw=250 p=<cur>/<min>/<max>` (3-part `p=` here too).
  - `sens` `t= h= p= t2=`, `-` = absent; `err unsupported` without a sensor.
  - `txq` `q=used/cap bp=quiet|qrs|qrt tx= rt= dr= u=%`; `tx`/`dr` per stats interval.
  - `mbox` `m=off|own|list|heard u=used/slots b=bytes a=actions/ceiling st dl ak dr bl nt`;
    `err unsupported` on classic ESP32.
  - `maxhop` `t= p=`. `name` `n=`, `atxt` `a=` (free text, render as plain text).
  - `pos` `<lat> <lon> <alt> gps|nofix|set`; write refused with `err gps` while GPS is on.
  - `mh <row>` `<total> <next|-> CALL min ...` (paged; `0 -` empty);
    `mh <call>` direct `d g m r s la lo di a n x h t`, routed `r h k g m rc=SNR@CALL t v`.
  - Errors in the corpus: `err text|unknown|unsupported|end|gps|hidden|range`.
- **C2 trap**: `_Folder._status` resets `self.fields = {}` on every status. The new read fields
  (radio, sens, name, atxt, pos, txq, mbox, maxhop, mh, chgled) are NOT status-owned and must
  survive a status; reset only the status-owned keys. A reboot marks them stale like the rest.
- **Firmware page layout** (`src/web_functions/web_rm_page.cpp:118-138, 266-328`): Radio (with
  Sensors), Identity (Name, APRS text), Position (Source + lat/lon/alt), Queues (TX queue as a
  bar, Mailbox counters, Max hop), Heard list (Read pages by `next` until `-`/128 rows, "Read N
  of total", Stop; "Other node" lookup, 9 chars, upper-cased), Advanced. Labels: bp quiet / slow
  down / hold; Sent, Retransmitted, Dropped; Mode, Slots used, Bytes, Actions this hour; Stored,
  Delivered, Acknowledged, Dropped, Blocked, Notified; Text messages, Position beacons.
- **Webapp building blocks**: `NodeSettingsGroup` + `EditableField` (editable kinds include
  `name` 19, `atxt` 39, `latitude`/`longitude`/`altitude` number inputs) from
  `components/bluetooth/node-settings/`; `BaseSettingsGrid` is 3/2/1 columns, no spans. BLE page
  order (`BtNodeSettings.vue:48-59`): Identity, Radio, Network, APRS, Sensor, Sensors, Time Zone,
  GPS, Via, WiFi, Weather, PIN. Remote cards emit `{cmd, args, confirm}`; gate with
  `sendDisabledReason`. 10 s spacing: reads are per card, never all on Connect.
