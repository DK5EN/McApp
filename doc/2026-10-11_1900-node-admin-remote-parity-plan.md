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

| Wave | Content                                                                                               | Status  |
| ---- | ----------------------------------------------------------------------------------------------------- | ------- |
| 1    | Bugs 1-5: A1 backend parse/allowlist/corpus, A2 counter + observed origin, B1 webapp                  | running |
| 2    | Command parity: C1 allowlist, C2 fold, D1 APRS/Position, D2 Radio/Sensors/Queues, D3 Heard/Charge LED | open    |
| 3    | Dev release, deploy, live check against DK5EN-1 (transmits: ask first)                                | open    |

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
