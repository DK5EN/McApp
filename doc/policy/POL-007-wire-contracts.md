# POL-007: Wire contracts and firmware interoperability

| Field | Value |
|---|---|
| ID | POL-007 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to | all parsers of Extern-UDP, BLE registers and binary frames; the push contract; the command contract; the RM1 frames |
| Sources | `CLAUDE.md` ("Extern-UDP wire format", "BLE D{ register", "Link Check", "MHeard Register", "Key Gotchas"); ADR-0003, ADR-0005, ADR-0019 |

## 1. Rule

1. **Never subscript an optional firmware key.** Read it with `.get()`. A firmware version that drops a key
   must not crash the parser.
2. **Detect a field by its presence, not by a version number.** The firmware has no protocol version field.
3. **Do not re-scale values.** RSSI is dBm as-is, SNR is already divided by 4 in firmware.
4. **Only `src_type == "lora"` carries real signal.** `node` and `udp` send a `0/0` sentinel and are excluded
   by an explicit `src_type` check, not by a range check.
5. **Match prefixes, not equality**, for firmware text that carries an unterminated ACK suffix (`{ping}{087`).
6. **Normalise ids.** A pong's id can be a negative decimal; a msg_id is a hex string on one path. Normalise
   with `& 0xFFFFFFFF` or its helper, never with a `\d+` pattern.
7. **Keep the id namespace in mind.** A msg_id is a node-local counter, reused after about 4 hours, and the
   firmware's PN retry flips bits 10 and 11. Every dedup key carries the sender too (ADR-0010).
8. **A `#TAG` destination is a channel**, never a DM (ADR-0021).
9. **Clamped frames are salvaged by whole members only**, never by a coerced partial value.
10. **Storage-side cards are not firmware truth.** An MH register that lacks `SRC`, `GW` or `PP` says nothing
    about the hop count or the gateway. Never read an absent key as a negative claim.

## 2. Why

Firmware versions and forks coexist in the field, and roughly half the fleet emits forms that a strict
parser rejects silently. Each rule above was the cause of a field report.

## 3. Verify

- The vector corpora (`*_vectors.json`) pin the parsers. A new firmware form becomes a new vector first.
- `scripts/run_startup_tests.py` runs the suites that replay them.

## 4. Exceptions

- The `D{` register decode in `ble_service` is strict on purpose (ADR-0019): a decode error is evidence of a clamp.
- The node's own traffic (`src_type` `node`/`udp`) is parsed, but never counted as signal.

## 5. Related

- `../sop/SOP-005-wire-contract-change.md`
- `../reference/MeshCom-ACK.md`, `../reference/telemetry.md`, `../reference/a0-commands.md`
