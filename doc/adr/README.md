# Architecture Decision Records (ADR)

An ADR records one architecture decision: the context, the choice, what it costs, and what was rejected.
ADRs answer **why the system is shaped this way**. They do not give steps (see `../runbook/`, `../sop/`)
or rules that must hold (see `../policy/`).

## Format

Every ADR uses the same sections, in this order:

1. **Header table:** Status, Date, Deciders, Affects, Supersedes, Superseded by.
2. **Context:** the forces at work. Facts, with dates.
3. **Decision:** the choice, in one or two sentences first, then the rules that follow from it.
4. **Consequences:** what gets easier, what gets harder, what must be watched.
5. **Alternatives considered:** what was rejected and why. Empty only if none existed.
6. **Evidence:** links to the plan, incident, measurement or commit that carried the decision.

File name: `ADR-NNNN-<short-slug>.md`, numbers are never reused.

Links are relative to the file. Paths in backticks inside an Evidence section are relative to `doc/`.

Status values:

- `Proposed`: under discussion, not in force.
- `Accepted`: in force. Implemented or not yet, say which in Consequences.
- `Superseded by ADR-NNNN`: kept for history, do not follow.
- `Deprecated`: no longer applies, no replacement.

An accepted ADR is not edited to change its decision. Write a new ADR that supersedes it. Small
clarifications go in an `Amendment (date)` section at the end.

## Index

| ID | Title | Status | Date |
|----|-------|--------|------|
| [ADR-0001](ADR-0001-bootstrap-architecture.md) | Single idempotent bootstrap entry point | Accepted (implemented) | 2026-01-25 |
| [ADR-0002](ADR-0002-tls-remote-access-caddy.md) | TLS remote access through Caddy with DNS-01 | Accepted | 2026-02-10 |
| [ADR-0003](ADR-0003-position-signal-data-architecture.md) | Positions, signal and MHeard data architecture | Accepted (amended 2026-07-05) | 2026-02-11 |
| [ADR-0004](ADR-0004-altitude-normalized-to-meters.md) | Altitude normalized to meters at ingestion | Accepted (implemented) | 2026-02-14 |
| [ADR-0005](ADR-0005-link-check-ping-pong.md) | Link check over firmware `{ping}` / `{pong}` | Accepted (amended 2026-09-25) | 2026-08-13 |
| [ADR-0006](ADR-0006-web-layer-weblite-over-starlette.md) | Web layer on Starlette (`weblite`), not FastAPI | Accepted | 2026-11-05 |
| [ADR-0007](ADR-0007-stall-capture-pure-asgi.md) | Stall capture: pure ASGI middleware and an own writer thread | Accepted | 2026-09-15 |
| [ADR-0008](ADR-0008-unread-as-read-cursors.md) | Unread state as read cursors, not counts | Accepted | 2026-09-06 |
| [ADR-0009](ADR-0009-delivery-status-monotone-rank.md) | Store-and-forward delivery status as a monotone rank | Accepted | 2026-09-14 |
| [ADR-0010](ADR-0010-ack-attribution-ledger.md) | ACK attribution: ledger, msg_id windows, inline match | Accepted | 2026-09-05 |
| [ADR-0011](ADR-0011-node-admin-rm1.md) | Node Admin: RM1 remote administration | Accepted | 2026-10-05 |
| [ADR-0012](ADR-0012-blocklist-retroactive.md) | Blocklist applies to stored history | Accepted | 2026-08-30 |
| [ADR-0013](ADR-0013-web-push-without-pywebpush.md) | Web Push without pywebpush | Accepted | 2026-09-28 |
| [ADR-0014](ADR-0014-network-safety-wpasupplicant-pin.md) | Network safety: wpasupplicant pin, held network packages | Accepted | 2026-09-18 |
| [ADR-0015](ADR-0015-gateway-uptime-ledger.md) | Gateway uptime as a ledger of gap and dark runs | Accepted (amended 2026-11-06) | 2026-08-21 |
| [ADR-0016](ADR-0016-homegrown-test-runner.md) | Homegrown test runner instead of pytest | Accepted (see review-log Q3) | 2026-07-12 |
| [ADR-0017](ADR-0017-system-epoch-converge.md) | System epoch: converge only what changed | Accepted | 2026-08-08 |
| [ADR-0018](ADR-0018-vendored-subtrees-and-corpora.md) | Vendored subtrees and hand-copied corpora | Accepted | 2026-05-08 |
| [ADR-0019](ADR-0019-inbound-charset-blacklist.md) | Inbound text: CP1252 fallback and blacklist filter | Accepted | 2026-09-09 |
| [ADR-0020](ADR-0020-node-time-zone-tz-rule.md) | Node time zone via the TZ rule, UTC as fallback | Accepted | 2026-10-04 |
| [ADR-0021](ADR-0021-hashtag-destinations.md) | `#TAG` destinations are channels, not callsigns | Accepted | 2026-08-20 |
| [ADR-0022](ADR-0022-no-ansible-no-nginx.md) | No Ansible and no nginx | Accepted | 2026-10-31 |

All 22 ADRs were approved by the owner on 2026-11-10. The five ADRs 0001-0005 were written before this format. Their original text is in
`../archive/` and linked from each file.
