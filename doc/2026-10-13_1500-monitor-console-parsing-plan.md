# Monitor console parsing: gap analysis and wave plan (2026-10-13)

Source: `~/Desktop/how-to-read-meshcom-logs.md` (firmware 4.40a log reference, its section 8 = the
original gap list G1-G9). Wire contract: webapp `docs/rf-monitor-plan.md`, section "Parsed console
lines" and the status shape under "REST + SSE for the session itself". This document is the resume
point for the campaign.

## Decisions (user, 2026-10-13)

- Parse firmware console lines in the BACKEND (MCProxy); the webapp renders `frame.parsed`.
- A DBG session ALWAYS sets `--setlog on` (third managed flag, restored like the other two).
- All three waves; commit per wave, no release.
- `doc/McApp-Monitor-Help.html` (MCProxy, untracked) and webapp `docs/monitor-help.html` are kept in
  sync: wave 3 updates both.

## Verified gaps (code state webapp 4c2d64d, MCProxy a86148f)

| ID  | Gap                                                                                                         | Fix wave |
| --- | ----------------------------------------------------------------------------------------------------------- | -------- |
| G1  | No structural decoding of any firmware line                                                                 | 1, 2     |
| G2  | `LEADING_TAG_RE` matches only at column 0; packet and `[LOG]` lines start with `HH:MM:SS`                   | 1        |
| G3  | Tokenizer misses `x<8hex>`, `RSSI:`/`SNR:`, `rssi=`/`snr=`, `H03`                                           | 1        |
| G4  | False positives: `rx=`/`tx=` as direction, 8-digit decimals as msg ids, `FCS:06DB` as dB                    | 1        |
| G5  | 500-row DBG cap, no pause, no per-family mute                                                               | 2        |
| G6  | DBG time column in seconds although the envelope `ts` is ms                                                 | 2        |
| G7  | Flags and platform invisible; DBG never set setlog                                                          | 2, 3     |
| G8  | `MonitorRow`/`MonitorCopy` drop `max_hop`, `lora_mod`, flags, `fcs_ok`, `last_hw_id`                        | 1, 2     |
| G9  | Tests use a line the firmware never prints                                                                  | 1        |
| N1  | Corrects the guide: BLE frames already carry `max_hop`, `lora_mod`, `last_hw_id`, `fcs_ok`, `msg_server`... | 1, 2     |
| N2  | Corrects the guide: HEY reaches the Monitor over BLE as MH rows (`transform_mh`), not over Extern-UDP       | -        |
| N3  | Partial console line idle-flushes after 1 s as its own row (`IDLE_FLUSH_S`): one line can become two rows   | open     |
| N5  | DBG rows also count against the shared 5000-row cap                                                         | 2        |
| N6  | Flag state only as pre-session `prior_flags`; board never detected                                          | 2        |

## Waves and file ownership

Wave 0 (orchestrator): contract in webapp `docs/rf-monitor-plan.md`; this document.

| Wave | Agent | Repo    | Exclusive files                                                                                            |
| ---- | ----- | ------- | ---------------------------------------------------------------------------------------------------------- |
| 1    | A     | MCProxy | new `src/mcapp/console_parse.py`, new `src/mcapp/console_parse_tests.py`                                   |
| 1    | B     | MCProxy | new `src/mcapp/frame_decode.py`, new `src/mcapp/frame_decode_tests.py`                                     |
| 1    | C     | webapp  | `src/components/monitor/monitorFormat.ts` (tokenizer only), `__tests__/monitorFormat.spec.ts`              |
| 1    | D     | webapp  | `src/types/wireFrame.ts`, `src/services/wireFrameNormalizer.ts` + its spec                                 |
| 1    | (me)  | MCProxy | `scripts/run_startup_tests.py` registration (hotspot)                                                      |
| 2    | E     | MCProxy | `src/mcapp/node_console.py`, `src/mcapp/node_console_tests.py`                                             |
| 2    | F     | webapp  | `MonitorLine.vue`, new `MonitorFrameDecode.vue`, new `consoleGlossary.ts`, `__tests__/MonitorLine.spec.ts` |
| 2    | G     | webapp  | `src/stores/monitorStore.ts`, `MonitorToolbar.vue` + their specs                                           |
| 3    | H     | both    | webapp `docs/monitor-help.html`, MCProxy `doc/McApp-Monitor-Help.html`                                     |
| 3    | (me)  | MCProxy | `CLAUDE.md` RF Monitor section                                                                             |

Writers: Sonnet 5.5 implementer, medium. Advisor (Opus 5.5, medium) before each behavioural commit.

Out of scope: firmware doc errors (guide appendix A), `[NBR]` field parsing, DBG on nRF52 (needs a
firmware console), N3 line splitting.

## Wave status log

- W0: contract and plan written (2026-10-13).
