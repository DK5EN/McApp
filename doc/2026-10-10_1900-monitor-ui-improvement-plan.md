# Monitor UI improvement campaign (2026-10-10)

Source: operator screenshots 2026-10-10 plus an advisor review of webapp, MCProxy and firmware code.
This file is the resume point; update the wave log after every wave.

## Problems found

1. MHeard register reports (`transform_mh`, `type:"pos"`, no coordinates, no msg_id) render as POS and chain
   into one endless row: the id-less POS merge window slides on `lastTs`, with no cap on sub-lines.
2. Sub-lines print the bare verdict `shown` and never say where the information came from.
3. Node command replies (`src:"response"`, dst `*`) are labelled `redirected · blocklist` only because
   `response` is on `sperrliste.json`.
4. During a DBG session the flag commands (`--loradebug on`, `--txcapture on`) are echoed twice: as DBG
   lines and as `response` MSG rows.
5. No legend or tooltip anywhere in the Monitor.

Firmware facts (verified by the advisor): one live MH frame per neighbour per uptime-minute plus a
connect-time list dump; `RSSI` = last direct frame, `SNR` = running mean over 8 frames; `DATE/TIME` = node
clock minus age, ignored by the Monitor so far.

## Decisions

- New webapp kind **MH** (frontend-derived from `frame.transformer === 'mh'`, no wire-contract change),
  fixed 10-min merge window anchored on `firstTs`, sub-lines capped.
- New webapp kind **CMD** for `src === 'response'`, off by default (chip unselected).
- Backend verdict for `response`: `("redirected", "node_reply")`, same dst rewrite as before. `reason` is an
  open token list in the contract, so this is a documented addition; mc-chat emits nothing like it.
- Flag echoes of a DBG session go through the existing `command_echo` rule via a TTL registry.
- `response` stays in `sperrliste.json` (the webapp's own `blocklistVerdict` still depends on it).
- Review model for the advisor gates is Opus 5.5 (operator decision), not Fable.

## Waves and ownership

| Wave | Agent | Repo    | Exclusive files                                                                                              |
| ---- | ----- | ------- | ------------------------------------------------------------------------------------------------------------ |
| W0   | orch  | webapp  | `src/types/wireFrame.ts` (MH/CMD kinds, `MonitorSource`, `source`/`heardAt`)                                 |
| W1   | A     | MCProxy | `sse_handler.py`, `node_console.py`, `wire_monitor_tests.py`, `node_console_tests.py`, new `command_echo.py` |
| W1   | B     | webapp  | `wireFrameNormalizer.ts`, `monitorStore.ts` + specs                                                          |
| W1   | C     | webapp  | `monitorFormat.ts`, `base.css` (`--mon-*`), `monitorContrast.spec.ts` + specs                                |
| W2   | D     | webapp  | `MonitorLine.vue` + spec (MH row, source label, sub-line cap, verdict wording)                               |
| W2   | E     | webapp  | `MonitorToolbar.vue` (MH/CMD chips), new `MonitorLegend.vue` + specs                                         |
| docs | orch  | both    | `rf-monitor-plan.md`, MCProxy `CLAUDE.md` RF Monitor, help HTML                                              |

## Wave log

| Wave | Status  | Notes                                                                                                                                                                                                                                                                                                                                                                              |
| ---- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W0   | done    | type skeleton in `src/types/wireFrame.ts`                                                                                                                                                                                                                                                                                                                                          |
| W1   | done    | gate green in both repos, Opus 5.5 advisor REWORK applied (heardAt gated and not displayed, MHeard wording hedged to fork vs official firmware, first-anchored merge no longer orphans live rows after backfill, same repoint guard for all kinds, node_reply ordering and eviction pinned). Not pinned: register-before-write in `node_console` (fake console cannot observe it). |
| W2   | pending |                                                                                                                                                                                                                                                                                                                                                                                    |

## Backlog from this campaign

- Pass the firmware `AGE` field through `transform_mh` (new `mh_age_min`), then show "heard N min ago"
  (timezone-free) instead of the node wall time.
- `transform_mh` still gates `GW` on `PLT == 0x40`, but newer firmware sends `GW` on every MH frame as
  the neighbour flag.
- `sperrliste.json` `response` entry is now redundant for the live SSE path (kept: the webapp's own
  `blocklistVerdict` and push filter still use it).
