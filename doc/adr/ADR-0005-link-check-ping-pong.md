# ADR-0005: Link check over firmware `{ping}` / `{pong}`

| Field | Value |
|---|---|
| Status | Accepted, implemented 2026-08-14 (`v1.6.14-dev.35`). Amended 2026-09-24 (transport-agnostic pong, node-id correlation) and 2026-09-25 (fork firmware fixed). |
| Date | 2026-08-13 |
| Deciders | Martin Werner |
| Affects | `commands/linkcheck.py`, `linkcheck.py`, `sse_routes/linkcheck.py`, `storage/ingest.py`, `commands/routing.py` |
| Supersedes | - |
| Superseded by | - |

Converted 2026-11-10 to the ADR format. Original full text with measurements on air:
[archive/2026-08-13_1500-linkcheck-ping-pong-ADR.md](../archive/2026-08-13_1500-linkcheck-ping-pong-ADR.md).

## Context

Operators want to know whether a station answers on **direct RF**. The firmware (`v4.35p.07.24.2`
and later) can answer a `{ping}` with a `{pong}`. The round trip on air is slow: 21 to 43 s on
official firmware, dominated by the node's TX queue and its 40 s retransmit steps.

## Decision

- Send `{ping}` as a direct message and match the reply by its token. Report **reachability and
  reply RSSI/SNR**, never a round-trip time. Attempts are sequential, with a 90 s timeout.
- Match the ping by **prefix**. `sendMessage()` appends an unterminated ACK suffix, so the sent text
  reads `{ping}{087`.
- Normalise the id with `& 0xFFFFFFFF`. Half the fleet sends the pong's id as a negative decimal.
- The routing hook runs **before** the echo and ACK branches (`commands/routing.py`). Wired after
  them, the ctcping echo handler swallows every reply.
- The ingest guard sits before `_insert_message_row`, not in `_should_filter_message`. The latter
  returns early and would also drop the pong's signal ingestion.

## Consequences

- Only RSSI and SNR of a pong with no via-path (`hops == 0`) belong to the target. Relayed pongs
  carry the last hop's signal.
- Ping costs are real: about four keyings over two minutes on official firmware. Server-side caps
  enforce it; the endpoint has no auth.
- A BLE-sent ping is echoed back by the node with its real msg_id. Live BLE frames carry
  `src_type: "ble_remote"`, and test fixtures must use that value.
- Behaviour depends on the firmware fork. Symptoms of a mismatched node are in `../runbook/` (see the
  BLE and link-check notes in `../reference/architecture-reference.md`).

## Alternatives considered

- Round-trip time as the metric: rejected, the measured delay is the node's queue, not the link.
- Matching on equality: rejected, the ACK suffix makes it fail.

## Evidence

- `archive/2026-08-13_1500-linkcheck-ping-pong-ADR.md` (sections 1.5, 8, 9)
- `archive/2026-08-13_1500-linkcheck-implementation-plan.md`
