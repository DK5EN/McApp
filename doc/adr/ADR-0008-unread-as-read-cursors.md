# ADR-0008: Unread state as read cursors, not counts

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-06 |
| Deciders | Martin Werner |
| Affects | `storage/query.py` (`get_conversation_summary`), `storage/suppression.py`, `read_cursors` table (migration 30), `sse_routes/*` for `POST /api/read_cursor` |
| Supersedes | `read_counts` (migration 7): "the total was N when I looked" |
| Superseded by | - |

## Context

Unread badges were stored as a count high-water mark: "the total was N when I last looked". The count
shrinks under retention, the blocklist filter and the webapp's 2000-row cap, so the badge drifted.
The mark was also local to one device, so a second device saw different badges.

## Decision

- A cursor is a **timestamp**: the ingest `timestamp` of the newest message the operator has seen.
  `read_cursors(key, ts)`.
- `unread = COUNT(messages newer than the cursor, not from our own base callsign, not suppressed)`.
- Writes are `MAX(existing, incoming)`. A second device or a delayed retry cannot move a mark backwards.
  The write returns the stored value, and the server broadcasts it with `unread` to every client.
- Keys are `conversation_key` on the wire. The webapp translates at one boundary.
- A **distinct message** is judged by its earliest stored copy. The same message arrives over two
  transports about 100 ms apart, and the webapp dedupes to the first copy. Counting rows leaves the
  second copy "unread" forever.
- Suppression uses a server-side mirror of the webapp's spam and blocklist predicates
  (`storage/suppression.py`, pinned by `suppression_vectors.json`). `count` and `last_ts` stay unfiltered.

## Consequences

- Badges agree across devices and survive retention.
- The aggregate and the candidate query share one dedup subquery. If their predicates diverge the
  subtraction corrupts the count, so the subquery must stay shared.
- Cost: a full-scan summary takes about 990 ms on the Pi, paid once per client connect and off the loop.
- The time fence is anchored on the data, never on a fixed `timestamp / W` bucket. A fixed bucket
  splits a 172 ms transport pair and recreates the stuck `+1`.

## Alternatives considered

- Keep counts and reset them on read: rejected, drifts under retention and per device.
- Per-row count: rejected, counts transport copies as two messages.

## Evidence

- `../archive/2026-09-06_1200-unread-cursor-plan.md`
- `../archive/2026-09-06_1300-unread-cursor-verdict.md`
- `../archive/2026-09-19_2140-unread-suppression-plan.md`
- `../archive/2026-09-19_0006-unread-badge-digitless-callsign-plan.md`
