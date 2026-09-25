# BLE registers IS1 and SN1 — adoption plan

Status: Wave 1 done and committed (2026-09-25); Wave 2 (dev release + live check) open.

## Firmware facts

Upstream PRs by OE1KFR, merged into `fork-neo-test` at `def2dc7e`. Both are a SECOND JSON register
sent right after their parent, because the parent is at the 244-char BLE register limit (same
pattern as `SE`→`S1`, `SW`→`S2`).

| Register | Follows | Emitted by                                                                         | Payload                                                    |
| -------- | ------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| `IS1`    | `I`     | `--info`, connect burst (#1156, `38441f08`)                                        | `{"TYP":"IS1","BDATE":"20260925-193817"}`                  |
| `SN1`    | `SN`    | `--nodeset`, connect burst, every `sendNodeSetting()` caller incl. `--via` (#1155) | `{"TYP":"SN1","VIA":true,"VIACALL":"OE1KBC-24,OE1KFR-12"}` |

- `BDATE` = compiler `__DATE__`/`__TIME__`, `YYYYMMDD-HHMMSS`, build-host LOCAL time, no zone.
  Upstream's successor of the fork-only `I.FWDATE` (integer `YYYYMMDD`, dropped in `v4.35p.08.28`).
- `VIACALL` is `""` after `--via NONE`, max 39 chars, uppercased by the firmware.
- `--via on` / `--via off` / `--via <CALL>` now answer with SN + SN1 JSON instead of a text echo.
- Availability: upstream `dev` has both; local `fork-main` has both since `1b829721` (not yet pushed,
  `origin/fork-main` is `bbc55cea`). mcapp.local's node gets IS1 once it runs that build.

## Impact

MCProxy dropped both with `WARNING Type not found!` (dispatcher allowlist). Allowlists to extend:
`ROUTINE_JSON_TYPS` (`ble_protocol.py`), `BLE_REGISTER_TYPES` (`main.py`),
`_CACHEABLE_REGISTER_TYPS` (`ble_service/src/main.py`). NOT `REQUIRED_BLE_REGISTER_TYPES` — old
firmware never sends them, the reconciler would retry forever. `--info` and `--nodeset` now answer
with two frames and move to `BLE_QUERY_DELAY_MULTIPART`.

Webapp: the Via card (`NodeRadioCard.vue`) was write-only/optimistic waiting for exactly this; the
Identity "Build" row read the dead `I.FWDATE`.

mc-chat: no BLE register surface, no change. No contract/corpus affected.

## Decisions (2026-09-25)

1. Via card: SN1-driven, optimistic fallback when SN1 never arrived.
2. BDATE: Identity "Build" row as `YYYY-MM-DD HH:MM:SS`, no zone suffix; fallback `I.FWDATE`.
3. Unknown future TYPs: keep the explicit allowlist (the WARNING is the discovery signal).
4. Backend use: forward + cache; ALSO expose node firmware (`FWVER`, `BDATE`) in `/api/status`
   and log an INFO line when SN1's `VIA`/`VIACALL` changes.

## Waves

| Wave | Owner        | Scope                                                                                | Status |
| ---- | ------------ | ------------------------------------------------------------------------------------ | ------ |
| 1A   | implementer  | MCProxy: allowlists, sweep delays, `/api/status` fields, via-change log, tests, docs | done   |
| 1B   | implementer  | webapp: bleStore IS1/SN1, register status rows, BDATE formatter, Build row, Via card | done   |
| gate | orchestrator | both repos full gate, advisor pass, commit per repo                                  | done   |
| 2    | orchestrator | dev release + mcapp.local live check (SN1 now; IS1 once the node has #1156)          | open   |

## Follow-ups

- Advisor pass APPROVED. Accepted trade-off: an empty SN1 reset does not clear the Via card's
  refs, so switching (without leaving the page) to a different node on pre-SN1 firmware keeps the
  previous node's via values on screen. Fix if it bites: reset the refs on the BLE disconnect path.
- `doc/ble-state-machine.md` rewritten 2026-09-25 against the current two-process architecture
  (all 10 sweep commands, IS1/SN1); closed.
