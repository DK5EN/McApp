# ADR-0010: ACK attribution: ledger, msg_id windows, inline match

| Field | Value |
|---|---|
| Status | Accepted (amended 2026-09-21 and 2026-10-04) |
| Date | 2026-09-05 |
| Deciders | Martin Werner |
| Affects | `storage/ingest.py` (`_handle_ack`, `_inline_ack_original`, `_resolve_ack_target`, `_prune_stale_message_acks`), `message_acks` (migration 29), `storage/ack_match_vectors.json` |
| Supersedes | the single `send_success` / `acked` flag as the only record of who acknowledged |
| Superseded by | - |

## Context

The UI showed a single "delivered" tick. It could not say who had acknowledged: the node, a gateway
or the addressee. Two real failures followed. A `!ctcping` to three unanswered targets showed as
delivered (2026-08-19). An unrelated DM reused a node counter and got the tick (2026-09-21, a
`msg_id` reused 24.75 h later).

## Decision

- **Vocabulary:** `node`, `gateway`, `peer` (`ack_kind` on `msg:status`). The inline text match is
  `peer`. The binary frames are `node` and `gateway`.
- **Ledger:** `message_acks` keyed `(msg_id, kind, from_call)`. `from_call` is `''`, never NULL, for an
  unattributed frame, so repeats collapse.
- **Inline `:ackNNN` match** needs three things: the ack is addressed **to** the original's sender, it
  comes **from** the original's target, and it falls inside a 1-hour window. The `echo_id` alone
  identifies nothing, because the counter is per sender and roughly hourly.
- **Held exception:** a message at `delivery_status = 'held'` may match inside 168 h (the firmware's
  store-time maximum). A held DM legitimately waits in a mailbox.
- **msg_id window:** a `msg_id` identifies a message for only 4 hours (`ACK_MSG_ID_WINDOW_MS`). It is
  `(station << 10) | counter`, and the counter wraps at 999. Every binary binding goes through
  `_resolve_ack_target`, which clamps to 4 h.
- **Prune on reuse:** `_prune_stale_message_acks` runs before recording, so a reused msg_id does not
  swallow the new message's acks through `INSERT OR IGNORE`.
- **Read side:** `GET /api/messages/{msg_id}/acks?since=` anchors the window on the bubble's own
  timestamp, not on the newest ack.
- **Parity:** the webapp runs the same inline rule. `ack_match_vectors.json` is canonical here and is
  replayed through `store_message`. Change the rule, the corpus, its sha256 and the webapp copy in one change.

## Consequences

- Attribution is per station and transport. The popover can show who acknowledged and via what.
- Two rows for one message are normal (two transports). Readers must dedupe.
- The window rules are subtle. Each half of the inline match and the 4 h clamp have mutation tests. Do not
  widen a window to "fix" a missing ack: a false match is worse than no match.
- The older message's own ACK rows are pruned when its msg_id is reused, so its popover can show nothing.
  This is an open trade-off, tracked as `backlog.md` B6.

## Alternatives considered

- One flag per message: rejected, it caused both real failures.
- Matching on the padded callsign in the payload: rejected, it truncates at 9 characters.
- A 1-hour window for held messages: rejected, a held DM's real ack arrives days later.

## Evidence

- `../archive/2026-09-05_1545-ack-attribution-plan.md`
- `../archive/2026-10-04_1842-ack-matcher-fix-plan.md`
- `../reference/MeshCom-ACK.md`
- `../backlog.md` B6
