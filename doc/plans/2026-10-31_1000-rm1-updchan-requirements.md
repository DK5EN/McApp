# RM1 `upd chan`: update channel switch DEV/MAIN in the Remote view (requirements)

Request (operator, 2026-10-31): "dann mach in mcapp RM1 bitte einen Schalter für DEV/MAIN". The
firmware side is done; this file is the work order for McApp backend (this repo) and frontend
(`/Users/martinwerner/WebDev/webapp`). Backlog item: `doc/backlog.md` B14.

Firmware references (repo `MeshCom-Firmware-DEV-Main`, branch `fork-dev`, commit `636e681b`):
`docs/campaign-rm1-updchan.md` (grammar, decisions, bench), `src/remote_cmd.cpp` `argsMatch`
(`RM_ARGS_UPD`), `src/rm_exec_write.cpp` `writeUpd`, `src/rm_au.h` `auSetChannel`, the firmware Remote
page `src/web_functions/web_rm_page.cpp` (reference UI), test vectors `tools/tests/remote_cmd_vectors.json`.

## Decisions (operator, 2026-10-31)

| #   | Topic         | Decision                                                                                                              |
| --- | ------------- | --------------------------------------------------------------------------------------------------------------------- |
| D1  | Wire tokens   | The wire keeps `prod` / `dev` (as the existing `c=` token). "MAIN" / "DEV" are UI labels only.                        |
| D2  | Check result  | A switch clears the node's last check result (`a=-`, `n=0`, `ck=-`); a staged image (`st=`) stays.                    |
| D3  | No auto check | The switch starts no check. The UI offers Check afterwards; the operator taps it (sender spacing 10 s per target).    |
| D4  | Builds        | Only firmware built with `MC_AU_DEV_CHANNEL` (all DK5EN fork builds) accepts the switch; others answer `err blocked`. |
| D5  | Trust         | Same as `upd go`: an authenticated RM1 command (HMAC tag), no strict-mode requirement.                                |

## Contract (firmware, final, benched)

| Command         | Reply                                                                           |
| --------------- | ------------------------------------------------------------------------------- |
| `upd chan prod` | `ok chan prod`                                                                  |
| `upd chan dev`  | `ok chan dev`                                                                   |
| both            | `err aubusy` (a check/download runs or a remote go is armed: `b=1` or `g=1`)    |
| both            | `err unsupported` (boards without auto-update: RAK4631 / nRF52)                 |
| both            | `err blocked` (firmware without the dev channel: the argument shape is refused) |

- Exact argument strings: `chan prod`, `chan dev` (lower case, one space). Anything else (`chan`,
  `chan DEV`, `chan main`, `chan dev x`) is refused by the node.
- Setting the channel that is already set answers `ok chan <x>` and changes nothing.
- The read `upd` is unchanged: `ok m=<off|notify|auto> c=<prod|dev> n=<0|1> a=<tag|-> st=<tag|-> b=<0|1> g=<0|1> ck=<min|-> e=<err|->`.
  After a switch: `c=<new>`, `a=-`, `n=0`, `ck=-`; `st=` keeps a staged tag.
- `ok chan <x>` is a write acknowledgement, not a state reply: it carries no other `upd` tokens.

Bench evidence (2026-10-31, DK5EN-92 -> DK5EN-1 over LoRa, every reply verified):

```
upd check       ok checking
upd             ok m=notify c=dev n=0 a=v4.40a.10.30 st=- b=0 g=0 ck=0 e=-
upd chan prod   ok chan prod
upd             ok m=notify c=prod n=0 a=- st=- b=0 g=0 ck=- e=-
upd chan prod   ok chan prod          (already set: no change)
upd chan dev    ok chan dev
upd             ok m=notify c=dev n=0 a=- st=- b=0 g=0 ck=- e=-
```

## Backend (MCProxy)

1. Argument shape, `src/mcapp/remote_cmd.py` (`_UPD_ARGS_RE`, around line 113; per-command validator
   around line 339): accept `chan prod` and `chan dev` in addition to `check` and `go <tag>`. McApp
   cannot know whether a target was built with the dev channel; it sends, and shows `err blocked` as
   "this node has no channel switch".
2. Test vectors: copy `tools/tests/remote_cmd_vectors.json` from the firmware commit above over
   `src/mcapp/remote_cmd_vectors.json` (it adds `upd chan dev` ctr 104 and `upd chan prod` ctr 105; the
   existing vectors are byte-unchanged). Loaded by `remote_cmd_tests.py:53` and
   `node_admin_service_tests.py:72`.
3. Shape tests, `src/mcapp/remote_cmd_tests.py` (around lines 291 / 303): valid `chan prod`, `chan dev`;
   invalid `chan`, `chan `, `chan DEV`, `chan main`, `chan dev x`.
4. Reply handling, `src/mcapp/node_admin_state.py` (`_parse_upd` around line 446, dispatch around 532,
   action classification around 689): `upd chan <x>` is an action like `upd check`, not a state read.
   On a verified `ok chan <x>`, update the cached `upd` state of that target to `c=<x>`, `a=-`, `n=0`,
   `ck=-` (keep `st`, `m`, `b`, `g`) until the next `upd` read replaces it. Do not parse `ok chan` as an
   `upd` read reply.
5. Install / ask-again classification, `src/mcapp/node_admin_service.py` (around line 833): `upd chan` is
   not an install; it is a settings change. Log it in the observed sessions like other writes.
6. Errors: map `err aubusy`, `err unsupported`, `err blocked` to the existing error texts of the
   update card; `err blocked` for `upd chan` reads "This node has no channel switch."

## Frontend (webapp)

Component `src/components/nodeadmin/remote/RemoteUpdateCard.vue` (CMD `upd`, `check()` around line 91,
guards around lines 38-51); command row detection `src/utils/nodeAdminCommands.ts` (around 358).

1. New row "Update channel" on the update card with two choices: **MAIN (icssw-org)** and **DEV (DK5EN)**.
   The current channel (from `c=` of the last `upd` read) is marked and not clickable.
2. Selecting the other choice asks for confirmation: "Switch the update channel of `<CALL>` to DEV? The
   last check result is cleared and a new check is needed." Then send `upd` with args `chan dev` /
   `chan prod`.
3. Disabled when: no `upd` read yet (channel unknown), the target is locked / rate-limited, or `b=1` or
   `g=1` in the last read (same rule as "Update now").
4. After a verified `ok chan <x>`: show `c=<x>`, no available tag, "Update now" disabled unless `st=`
   holds a staged tag; message "Channel set to DEV. Run Check." Check stays a separate tap.
5. Sent-history label: short, "Update channel DEV" / "Update channel MAIN".
6. `nodeAdminCommands.ts`: recognise `upd chan <x>` rows (label, not an install).
7. Tests (`npm test`, vitest): row rendering and marking, disabled states, confirm then send exactly
   `upd chan dev`, state after `ok chan dev`, `err blocked` text.

## Acceptance criteria

- Backend: shape tests and vector tests pass (`uv run python scripts/run_startup_tests.py`), `uv run mypy
src/mcapp ble_service/src` clean; `upd chan prod|dev` accepted, malformed forms refused before sending.
- Frontend: `npm test` green including the new card tests; type check clean.
- Live check against a fork node (sender any own node, target an own node, test traffic only as a DM to
  an own node, never a broadcast): read `upd`, switch to the other channel, `upd` shows `c=<new> a=-`,
  Check, switch back; the UI matches every reply.

## Known residuals (firmware, accepted)

- A staged image of the old channel stays installable after a switch (D2).
- A console `--updchan` while a check runs now answers `[ERR];updchan;busy` (firmware-local, no McApp
  impact).
