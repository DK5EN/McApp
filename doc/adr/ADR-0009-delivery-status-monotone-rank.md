# ADR-0009: Store-and-forward delivery status as a monotone rank

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-14 |
| Deciders | Martin Werner |
| Affects | `storage/ingest.py` (`_write_delivery_status`, `_DELIVERY_STATUS_RANK`, `_apply_held_notice`), `messages.delivery_status` |
| Supersedes | - |
| Superseded by | - |

## Context

A store-and-forward node holds a DM and reports `0x03 failed` (all retries exhausted, nobody acked)
or `0x04 held` (the store node took the frame). Binary ACKs and the inline `:ack` text can arrive out
of order, and two transport copies race. The old code set `send_success` on every ack type, so a
`failed` frame marked the message as delivered: the exact inverse of the frame's meaning.

## Decision

- One rank: `sent / node / gateway = 1 < held = 2 < failed = 3 < acked = 4`. NULL is 0.
- The rank is enforced **in the UPDATE's own WHERE clause**, built from `_DELIVERY_STATUS_RANK`.
  A stale read cannot clobber a higher rank.
- Equal rank does not overwrite. `held(A)` then `held(B)` keeps A. Both holders stay in `message_acks`.
- `failed` never sets `send_success`. Its path runs a read-only existence check.
- `held` keeps the write to `send_success`, because a store node did take the frame.
- The `failed` event carries `acked: false`. The webapp renders any event without a `sent` key and
  without `acked === false` as a peer acknowledgement.
- Both "addressee answered" paths (binary `0x02` and inline text) must write the rank, and the inline
  path passes its `row_id` explicitly.

## Consequences

- Out-of-order frames converge on the highest rank with no special cases.
- `failed` is not a send failure. A local send failure is a different event (`send_failed`), emitted
  before a msg_id exists.
- The `:sto` notice text is absorbed (hidden) and treated as `held`, so a held DM shows once.
- No push is sent for `failed` or for `acked` after `held` (see the plan's §6.1).

## Alternatives considered

- Read, compare, write in Python: rejected, a race between two ack frames.
- Fold `failed` into `send_failed`: rejected, `failed` has a msg_id and is a different fact.

## Evidence

- `../archive/2026-09-14_1153-store-forward-dm-status-plan.md`
- `../archive/2026-09-03_2300-delivery-status-rca.md`
- `../reference/MeshCom-ACK.md`
