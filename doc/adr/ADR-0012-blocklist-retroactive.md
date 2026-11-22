# ADR-0012: Blocklist applies to stored history

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-30 |
| Deciders | Martin Werner |
| Affects | `commands/handler.py` (blocklist fetch), `main.py` (`filter_history_row`), `storage/query.py` (`get_smart_initial_with_summary`, `get_messages_page`), `sse_handler.py` (SSE burst order) |
| Supersedes | the forward-only blocklist (ingest and live broadcast only) |
| Superseded by | - |

## Context

The curated `sperrliste.json` is maintained centrally and lands on boxes the project does not run.
The blocklist ran on ingest and on the live broadcast only. Every row a station had stored before it
was blocked stayed in `messages.db` and was replayed to every client on every reload. A per-host
`DELETE` would not fix that on other boxes.

## Decision

- **Filter on the way out of storage**, not on the way in. `MessageRouter.filter_history_row` is applied
  by `get_smart_initial_with_summary` and `get_messages_page`. That is what makes an entry retroactive.
- **Counts use the same predicate** as the messages. The sidebar badges would otherwise advertise a
  conversation whose messages were removed.
- **`has_more` stays keyed on the raw row count.** A page that filters to empty must not read as the
  start of history.
- **`blocked_callsigns` is emitted before `smart_initial`** in the SSE burst. The client applies the set
  at one ingest point, and anything delivered earlier is admitted against an empty list.
- **Refresh every 15 min with a conditional GET.** Each refresh replaces the curated part, so an upstream
  removal takes effect without a restart. The union with local kickbans runs after the subtraction, so an
  admin's kickban survives.
- **ETag only for a payload that validated.** Storing the tag of a malformed list pins the node to a bad
  list forever.

## Consequences

- A blocked station disappears from history on every client, not only from new traffic.
- The ordering of the SSE burst is load-bearing. A test pins it.
- Offline caches in the webapp are purged on every snapshot. The cache door bypasses the normal ingest path.

## Alternatives considered

- A one-time DELETE per box: rejected, the list is central and boxes are not administered.
- Filter only in the webapp: rejected, the backend would keep serving the rows to every client.

## Evidence

- `../archive/2026-08-30_0930-blocklist-retroactive-plan.md`
- `../reference/architecture-reference.md` (section "Blocklist")
