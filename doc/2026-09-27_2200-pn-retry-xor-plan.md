# PN retry XOR: MCProxy adoption plan and campaign state

Status 2026-09-27. Firmware side: `MeshCom-Firmware-DEV-Main` branch `fork-neo-test` (`f1ebf676`
and later), `docs/pn-retry-mcapp.md` (firmware-authored impact note, German),
`docs/pn-zustellung-dedup.md`, `src/pn_retry.h`.

## 1. What the firmware does

- Every direct message (personal destination, text frame, `{NNN` suffix) retries up to 3 times,
  40 s apart. Retry k XORs msg_id bits 10-11 with k. First send is byte-identical to old
  firmware. The `{NNN` suffix and `:ackNNN` text ack are identical on every copy.
- Groups, `*`, `{ping}`/`{pong}`, `{CET}`/`{MCP}`/`{SET}` are never XOR-retried.
- A node on this firmware drops the copies before Extern-UDP (RX ring, ~70 ids) and, for DMs
  addressed to itself, before BLE (sender + `{NNN` + text). Relaying continues unchanged.
- Every BLE 0x41 ack/status frame (0x00-0x04) for our own DM carries the ORIGINAL msg_id.
  0x41 layout, status bytes and appendix are unchanged since fork-main `150b0a4a`.
- `--dmretry` outbox removed, including the outbox-full nack (never implemented here).
- Extern-UDP `{"type":"ack"}` is now emitted by the firmware, only for 0x01 and 0x02.

## 2. Exposure

- Behind a new-firmware node: none observed. mcapp.local DB check 2026-09-27: 0 multi-id core
  groups in 128 msg rows since the DK5EN-98 flash (17:25) and in 2250 rows over 7 days.
- Behind an old-firmware node (69 % of the fleet): each copy is a new row, a second push, a
  second webapp bubble, a +1 unread, and a resent `!command` passes the msg_id dedup and hits
  the content throttle -> an extra "Command throttled" RF reply.

## 3. Design decisions

- One helper pair in `src/mcapp/util.py`: `msg_core()` (mask `0xFFFFF3FF`) and
  `msg_id_retry_variants()` (the four ids sharing a core, for `msg_id IN (...)` so the index
  stays usable; no SQLite user function).
- Bits 10-11 are the low bits of the 22-bit node id, so the core is not unique across
  stations. Every core-based key carries the sender. The firmware note's msg_id-alone keys for
  push and command dedup are rejected for that reason.
- Stored `messages.msg_id` stays the raw received value.
- `message_acks` ledger rows are written under the resolved message row's own msg_id, not the
  ack frame's, matching the inline `:ackNNN` path.
- `linkcheck.py` untouched (ping/pong never retried).

## 4. Waves

| Wave | Scope                                                                                                                                                                                  | Owner            | Status             |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- | ------------------ |
| 0    | DB check on mcapp.local                                                                                                                                                                | orchestrator     | done, 0 pairs      |
| 0    | `util.msg_core`/`msg_id_retry_variants` + `msg_core_tests.py`, registered in `run_startup_tests.py`                                                                                    | orchestrator     | done               |
| 1a   | `storage/ingest.py`: ingest dedup on core (personal dst only), `_resolve_ack_target` variant fallback (own sent rows), ack ledger key; tests in ingest_dedup / ack_status / conv_dedup | implementer      | done               |
| 1b   | `commands/routing.py`: command dedup key `(sender, core)`; test in routing_tests                                                                                                       | implementer      | done               |
| 2    | `dedup_contract.json` id normalization + sender scoping in mc-chat (canonical), subtree pull + sha pin here, `PushDedup`, webapp `getDedupKey` + contract copy                         | tbd after Wave 1 | open               |
| -    | Firmware: `handleACK()` does not fold a retry msg_id before `checkOwnTx()` (`lora_functions.cpp:410-505`) -> node/gateway heard-ack for a retry copy is dropped                        | firmware side    | reported, not ours |

## 5. Wave 1 advisor round (2026-09-27)

Rework list, all verified in code by the orchestrator:

- R1 (required): the SQL backstop `IN (variants)` branch was never decisive in any test (a mutant
  making it exact-only passed). Restart-shaped test added in `ingest_dedup_tests.py`.
- R2: `_handle_ack` published the ack frame's msg_id in `msg_status`; after a variant-fallback
  bind that id matches no bubble. Now publishes the resolved row's msg_id (byte-identical for
  every exact match).
- R3: `msg_core()` crashed on a non-str msg_id (Extern-UDP admits any JSON scalar), losing the
  command. Now takes `object`; regression tests in `msg_core_tests.py` and `routing_tests.py`.
- R4: a non-str `dst` crashed `dst_kind()` in the ingest dedup and lost the frame. Coerced to
  `""` next to the `msg` coercion.

Declined: running ANALYZE so the `_resolve_ack_target` variant query uses the msg_id index
(3.5 ms/call at 20k rows unanalysed). The query only runs when the exact id has no match, for an
8-hex id, with our own callsign known — rare by construction, and the planner switches to the
index after the first prune's ANALYZE. Revisit if `handler` stall rows ever name `_handle_ack`.
