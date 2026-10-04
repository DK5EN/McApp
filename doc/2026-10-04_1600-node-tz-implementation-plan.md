# Node time zone over BLE: McApp implementation plan

Date: 2026-10-04. Contract: `doc/2026-10-04_1500-node-tz-ble-contract.md`. Firmware: `fork-dev`
campaign TZ-01, W1-W4 committed and bench-tested 2026-10-04 on DK5EN-1 (ESP32) and DK5EN-90
(nRF52). Status: **W1 committed, W3 hardware check open**. Waves are run with `/orchestrate-waves`.

## BLUF

- Today every fresh BLE connect and every host DST change sends `--utcoff` to the node. Against
  TZ-01 firmware that wipes a configured TZ rule. The fix is one TZ-aware decision inside
  `ble_service`'s `set_time()`, the single choke point both callers already use.
- Old firmware keeps its byte-identical behaviour (`--utcoff` then `0x20`). TZ-01 firmware with a
  rule gets `0x20` only. TZ-01 firmware with no rule gets the host's POSIX rule pushed once
  (`--settz`), verified through the `SN1` push, with `--utcoff` as the fallback.
- When the firmware cannot be classified in time, the fail-safe is to send only `0x20`. UTC is
  always correct; a wrong `--utcoff` can destroy state, a missing one cannot.
- Scope: `ble_service` (one writer), the companion webapp (one writer, separate repo, separate
  commit), docs by the orchestrator. The main app (`src/mcapp`) needs no change: `SN1` already
  passes through generically with all keys.

## 1. Findings that shape the plan

| #   | Finding                                                                                                                                                                                                                                | Source                                                                          |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| F1  | `set_time()` is the only place `--utcoff` is sent. Callers: `POST /api/ble/settime` (main app's `--settime` after a fresh connect, `main.py:_settle_hello_and_sync_time`) and the hourly `_dst_check_loop()`.                          | `ble_service/src/ble_adapter.py:1488`, `:1749`; `src/mcapp/main.py:1101`        |
| F2  | The main app waits `BLE_HELLO_WAIT = 1.0 s` after hello and then posts `settime`. The node's register burst (I, SN+SN1, G, ...) is still in flight during the first second. `SN1` may not be cached yet when `set_time()` runs.        | `src/mcapp/main.py:146`; `ble_service/src/main.py:43` (`POST_CONNECT_SETTLE_S`) |
| F3  | The register cache is `state.register_cache` in `ble_service/src/main.py`, populated by `_cache_register_if_applicable()`; `SN1` is cacheable. The adapter has no handle to it today.                                                  | `ble_service/src/main.py:146-148, 245, 563`                                     |
| F4  | `sendNodeSetting()` in the firmware emits `SN` and `SN1` back to back; `--settz`, `--utcoff` and `--nodeset` all trigger it. `SN1` key order: `TYP VIA VIACALL WSPWD ASYM TZ`. `--settz` validates first and pushes nothing on reject. | firmware `src/command_functions.cpp:695-767, 6720-6731`                         |
| F5  | The `0xA0` path copies the payload verbatim (no lowercasing, no separator stripping), so a rule with `, / < > + - . :` arrives intact.                                                                                                 | firmware `src/phone_commands.cpp:556-587`                                       |
| F6  | `/etc/localtime` on mcapp.local and on the dev Mac both end in `CET-1CEST,M3.5.0,M10.5.0/3`. The TZif footer is a reliable host-rule source.                                                                                           | measured 2026-10-04                                                             |
| F7  | mcapp.local's node runs `4.40 a` built 2026-10-01, before TZ-01. Production stays on the old-firmware path until the node is re-flashed; a live TZ-01 check needs DK5EN-1 or DK5EN-90.                                                 | `GET /api/status` 2026-10-04                                                    |
| F8  | The webapp reads `SN1` into `bleStore` but keeps only `VIA`/`VIACALL`; `NodeGpsCard.vue` displays `SN.UTCOF` and sends `--utcoff` via `fw.setUtcOffset()`. With a rule set, that control would clear the rule.                         | webapp `src/stores/bleStore.ts:342-375`, `NodeGpsCard.vue:33-35, 156`           |
| F9  | `ble_service` tests live inline in `scripts/ble_service_tests.py`, wired into `run_startup_tests.py`. `set_time()` is covered end to end at `:2814`. No config.json or env knob touches time today.                                    | scout audit                                                                     |

## 2. Decisions (for approval)

| ID  | Decision                                                                                                                                                                                                                                                                                                                                                            | Alternative                                                                                                                        |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| D1  | **Policy `host_if_unset` (default).** A TZ-01 node reporting `TZ == ""` gets the host's rule pushed via `--settz`. A non-empty `TZ` is never overwritten and never receives `--utcoff`. Rationale: an updated node starts empty (contract §1); without the push it stays on a fixed offset and McApp would have to keep driving DST, which is what we are removing. | Policy `never`: respect only what is on the node; `""` keeps `--utcoff` as today. Selectable via env `MCAPP_NODE_TZ_POLICY=never`. |
| D2  | **Host rule source** is the TZif footer of `/etc/localtime`, overridable by env `MCAPP_NODE_TZ=<posix rule>`. The rule must pass the node's grammar (39 chars, `M` rules only, no spaces, `Jn`/`n` rejected, DST name needs rules) or it is not sent and `--utcoff` is used instead.                                                                                | none                                                                                                                               |
| D3  | **Firmware classification is tri-state and must be settled before `--utcoff` is sent.** `SN1` cached with `TZ` key → supported; `SN1` cached without `TZ`, or `SN` cached and `SN1` absent after the probe → unsupported (old firmware); neither → unknown. If `SN1` is not cached when `set_time()` runs, send `--nodeset` and wait up to 3 s for the push.        | Trust the cache at call time. Rejected: F2 makes the first connect a race, and losing is a wiped rule.                             |
| D4  | **Unknown → `0x20` only, no `--utcoff`, log a warning.** The DST watchdog then stays idle for this session (`_last_utc_offset` stays `None`); the next connect retries the classification.                                                                                                                                                                          | Send `--utcoff` anyway (today's behaviour). Rejected: the asymmetric cost in the BLUF.                                             |
| D5  | **`--settz` success is confirmed by `SN1.TZ == rule` within 2 s, else one `--nodeset` and compare.** Still different → treat as rejected, log it, fall back to `--utcoff` (the node still has no rule, so that is safe). No retry loop.                                                                                                                             | none                                                                                                                               |
| D6  | **`timestamp_from_date_time()` stays as is** (contract §4). Under D1 the node's offset equals the host's in the common case. Converting MH `DATE`/`TIME` through `SN.UTCOF` is a follow-up, logged in `doc/backlog.md`.                                                                                                                                             | Include it now. Deferred: different code path, different risk.                                                                     |
| D7  | **Webapp wave is in scope** (contract §6: "show the rule in the UI"). Separate repo, separate commit, releasable independently.                                                                                                                                                                                                                                     | Defer the webapp; backend alone already protects the rule.                                                                         |

## 3. Design

New pure module `ble_service/src/node_tz.py` (stdlib only; `ble_service` ships a standalone lock):

- `host_posix_tz(path=Path("/etc/localtime")) -> str | None`: TZif footer per contract §3.
- `validate_node_tz(rule: str) -> str | None`: returns a reason string when the node would reject
  the rule, mirroring firmware `src/tz_rule.cpp` (length 39, names 3+ letters or `<...>`, POSIX
  offset `[+-]h[:mm[:ss]]`, only `,M m.w.d[/time]` rules, DST name requires both rules).
- `classify(register_cache) -> NodeTz`: a small dataclass `NodeTz(state: UNSUPPORTED | EMPTY |
RULE | UNKNOWN, rule: str | None)`.
- `resolve_policy() -> "host_if_unset" | "never"` from `MCAPP_NODE_TZ_POLICY` (invalid → default
  with a warning).

`BLEAdapter` changes (`ble_adapter.py`):

- Constructor takes a `register_lookup: Callable[[], Mapping[str, Mapping[str, Any]]]` (main.py
  passes `lambda: state.register_cache`), plus an `await`-able `wait_for_register(typ, timeout)`
  built on an `asyncio.Event` the cache setter trips. The adapter never imports `main`.
- `set_time()` becomes: classify → if `UNKNOWN`: `--nodeset`, wait ≤ 3 s, reclassify → branch:
  - `UNSUPPORTED`: `--utcoff <host offset>`, sleep 0.3, `0x20`. Byte-identical to today, including
    `_last_utc_offset` bookkeeping.
  - `RULE`: `0x20` only. `_last_utc_offset = None`.
  - `EMPTY` and policy `host_if_unset` and a valid host rule: `--settz <rule>`, verify per D5,
    then `0x20`. On reject: `--utcoff`, then `0x20`.
  - `EMPTY` otherwise: as `UNSUPPORTED`.
  - `UNKNOWN` after the probe: `0x20` only, warning.
- `_dst_check_loop()` is unchanged in shape; it already gates on `_last_utc_offset is not None`,
  which the `RULE` and `UNKNOWN` branches leave `None`. It still calls `set_time()`, which
  re-classifies, so a node that gains a rule mid-session is respected at the next host DST change.
- `/api/ble/settime` response `message` carries the branch taken (`utcoff`, `settz`, `rule`,
  `unknown`) so the main app log and a live check can see which path ran.

Main app: no code change. `SN1` already reaches `cached_ble_registers` and the webapp via
`transform_ble` pass-through, `TZ` included.

Webapp (`/Users/martinwerner/WebDev/webapp`):

- `SN1Register.TZ?: string`; `bleStore.storeMessage` copies it.
- `NodeGpsCard.vue`: when `SN1.TZ` is a non-empty string, show the rule and the derived `UTCOF`,
  and replace the offset picker with "Node follows rule <rule>" plus a "Clear rule" action
  (`--settz none`). When `TZ === ""`, keep the offset picker and add a rule input with the
  contract's examples as presets, client-side grammar check, sent as `--settz <rule>` and
  confirmed by watching `SN1.TZ` (same optimistic-then-reconcile pattern `NodeRadioCard.vue`
  uses for `VIACALL`). When `TZ` is absent, the card is unchanged (old firmware).
- `firmwareCommands.ts`: `setTz(rule)`, `clearTz()`.

## 4. Waves and ownership

| Wave | Writer           | Exclusive files                                                                                                                                                             | Verification by the writer                                                                                                        |
| ---- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| W1-A | implementer      | `ble_service/src/node_tz.py` (new), `ble_service/src/ble_adapter.py`, `ble_service/src/main.py`, `scripts/ble_service_tests.py`                                             | `uvx ruff check` + `format --check` on touched files, `uv run mypy ble_service/src`, `uv run python scripts/ble_service_tests.py` |
| W1-B | implementer      | webapp: `src/stores/bleStore.ts`, `src/types/*` (SN1 type), `src/components/bluetooth/node-settings/NodeGpsCard.vue`, `src/utils/firmwareCommands.ts`, matching `__tests__` | `npm run lint`, `npm run typecheck`, the touched spec files                                                                       |
| gate | orchestrator     | none                                                                                                                                                                        | full gate in both repos, diff read, advisor pass (`/fable-review` on the W1 diffs)                                                |
| W2   | orchestrator     | `CLAUDE.md` (new "Node Time Zone" section), contract doc header + §2 note, `doc/backlog.md` (D6 follow-up), this plan's status                                              | prettier + `uvx ruff format --check .`                                                                                            |
| W3   | operator + orch. | hardware, serialized                                                                                                                                                        | see §6                                                                                                                            |

W1-A and W1-B share no files and no repo; dispatch in one message. No shared build target: the
Python gate and `npm` run in different trees. Both writers: no git, no whole-repo formatters, no
BLE/network transmission (tests use the fake transport), no edits outside the set.

## 5. Tests (all must fail before, pass after, where they pin a behaviour change)

`scripts/ble_service_tests.py`, new cases:

| Case | Pins                                                                                                                                                                            |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1   | Old firmware (`SN1` cached, no `TZ`): `set_time()` writes exactly `--utcoff <host>` then `0x20`, same bytes as before; `_last_utc_offset` set.                                  |
| T2   | Old firmware without `SN1` at all (`SN` cached): same as T1 after the `--nodeset` probe times out; probe is sent once.                                                          |
| T3   | Rule set: only `0x20` on the wire; `_last_utc_offset is None`; the DST loop does not send anything when the host offset changes.                                                |
| T4   | `TZ == ""`, policy `host_if_unset`, fake transport answers `--settz` with an `SN1` push carrying the rule: wire is `--settz`, `0x20`; no `--utcoff`.                            |
| T5   | `TZ == ""`, transport pushes nothing on `--settz` and answers `--nodeset` with `TZ == ""`: falls back to `--utcoff`, then `0x20`; one warning logged.                           |
| T6   | `TZ == ""`, policy `never`: identical to T1.                                                                                                                                    |
| T7   | Cache empty at call time, `SN1` with a rule arrives 0.5 s after `--nodeset`: no `--utcoff` is ever sent (the F2 race).                                                          |
| T8   | Cache empty, nothing arrives in 3 s: `0x20` only, no `--utcoff`.                                                                                                                |
| T9   | `host_posix_tz` on fixture bytes: v2 footer, empty footer → `None`, missing file → `None`.                                                                                      |
| T10  | `validate_node_tz` vectors: the six accepted examples from the contract; rejects `CET-1CEST`, `EST5EDT,J60,J300`, a 40-char rule, a rule with a space, `<+0530>-5:30` accepted. |
| T11  | `MCAPP_NODE_TZ` override wins over the host file; an invalid override is ignored with a warning and the host file is used.                                                      |
| T12  | `/api/ble/settime` response `message` names the branch.                                                                                                                         |

Webapp: spec for `storeMessage` keeping `TZ`, and a component test that the offset picker is not
rendered while `SN1.TZ` is non-empty.

## 6. Hardware verification (W3, after the gate, serialized)

1. **Old firmware, mcapp.local (DK5EN-98, `4.40 a`)**: dev-release, restart `mcapp-ble`, trigger a
   fresh connect. Expect in the `ble_service` journal: `--nodeset` probe is NOT sent (`SN1`
   already cached from the burst) or sent once, then `Syncing UTC offset: --utcoff +2.0`, then
   the `0x20` write. The node's `SN.UTCOF` stays `2.0`. Behaviour unchanged.
2. **TZ-01 firmware, bench node DK5EN-1 or DK5EN-90**: pair the dev box's `ble_service` to it.
   Fresh ESP32 defaults to the CET rule → expect the `rule` branch, no `--utcoff` in the journal,
   node console shows no `utcoff: TZ rule cleared`. Then `--settz none` from the node console,
   reconnect → expect `--settz CET-1CEST,M3.5.0,M10.5.0/3` and an `SN1` push with the rule.
3. Any transmission beyond the paired node's own BLE link is out of scope; no mesh traffic is
   generated by these commands.

## 7. Out of scope / deferred

- D6: MH `DATE`/`TIME` conversion through `SN.UTCOF` (`doc/backlog.md`).
- mc-chat: no BLE, nothing to mirror.
- A `/api/node/tz` REST endpoint: the webapp reaches `--settz` through the existing command path.

## 8. Wave log

| Wave | Status  | Commit         | Notes                                                                                                                                                             |
| ---- | ------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| plan | written |                | 2026-10-04                                                                                                                                                        |
| W1-A | done    | 012ba92        | advisor REWORK folded in: stale-cache freshness gate, `_wait_for_tz`, D5 amended (unverified `--settz` sends 0x20 only, branch `settz_unverified`), tests T13-T15 |
| W1-B | done    | webapp 68d58af | locate sends no `--utcoff` while a rule is set (no offset picker existed)                                                                                         |
| W2   | done    | this commit    | CLAUDE.md section, backlog B7                                                                                                                                     |
| W3   | open    |                | hardware; note set_time runs about 4.4 s after hello, so a probe on first connect is expected                                                                     |
