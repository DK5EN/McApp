# ADR-0013: Web Push without pywebpush

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-28 (commit `3d960d9`, B4.3) |
| Deciders | Martin Werner |
| Affects | `src/mcapp/push_send.py`, `src/mcapp/push_delivery.py`, `src/mcapp/sse_routes/push.py`, `pyproject.toml` |
| Supersedes | the `pywebpush` dependency (introduced 2026-07-17, commit `2fde5bc`) |
| Superseded by | - |

## Context

Web Push was first built on `pywebpush`. Its module top imports `aiohttp` and `requests`, which added
13.4 MB RSS per process on a Pi with about 144 MB of usable memory. Only the crypto and the POST were
needed.

## Decision

Keep the two cryptographic libraries and replace only the HTTP part:

- `http-ece` for RFC 8291 (aes128gcm payload encryption).
- `py-vapid` for RFC 8292 (VAPID JWT).
- `httpx` for the POST, in `push_send.py`, called through `asyncio.to_thread` with timeouts.

Rules that follow:

- **Do not hand-roll the crypto**, and do not re-add `pywebpush`.
- A 3xx from the push service is a non-pruning failure. `httpx` does not follow redirects, which
  `requests` did.
- Subscriptions are pruned on 401, 403, 404 and 410.
- **The mesh-ingest path never awaits delivery.** A box without internet must not stall the event loop.
- VAPID key: raw base64url 32-byte scalar, not PEM (`py_vapid` cannot read PEM), stored `0600`. The JWT
  `sub` must be a real FQDN. Apple returns 403 `BadJwtToken` otherwise.

## Consequences

- About 13 MB less RSS per process.
- The contract (`push_contract.json`, now v11) is shared with mc-chat and pinned by a sha256. Behaviour
  changes go through mc-chat first.
- The sender is ours to maintain: header and retry behaviour are in `push_send.py`, not in a library.

## Alternatives considered

- Keep `pywebpush`: rejected, 13 MB for code the project did not use.
- Hand-roll RFC 8291 and 8292: rejected, the crypto must not be ours.

## Evidence

- `pyproject.toml` (dependency comment)
- `../reference/architecture-reference.md`
- `../plans/2026-09-19_1200-mention-push-plan.md` (contract v11, mention filter)
