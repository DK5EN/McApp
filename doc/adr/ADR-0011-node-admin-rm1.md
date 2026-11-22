# ADR-0011: Node Admin: RM1 remote administration

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-10-05 |
| Deciders | Martin Werner (operator decision 2026-10-05: always on, configured in the web GUI) |
| Affects | `remote_cmd.py`, `node_admin_service.py`, `node_admin_state.py`, `storage/node_admin.py`, `sse_routes/node_admin.py`, `main.py` (`send_node_admin`) |
| Supersedes | - |
| Superseded by | - |

## Context

MeshCom nodes accept administration over LoRa as HMAC-tagged `RM1` direct messages. The node is
slower and stricter than it looks. It accepts one frame per 10 s, shares one high-water mark across
all senders, and locks RM1 for 5 minutes after 3 counted rejects. Every failure is silent: wrong
password, rate limit, lockout, RM off and "never heard over LoRa" look the same on air.

## Decision

- **Always on, no config switch.** Settings > Remote nodes stores a node and its password. Until a
  node exists the feature is inert.
- **LAN-only.** `/api/node-admin/*` refuses a foreign `Host` and a foreign `Origin`. The API has no
  authentication, so this guard is the only protection in the public-TLS modes.
- **Counter** `ctr = MAX(ctr + 1, last_hwm + 1, unix_seconds)`, allocated in one `db_write` transaction.
  Refusals happen before allocation, so they burn no value.
- **One command in flight per target**, 10 s spacing, 5 min cool-down after a silent row, and a 120 s
  pause after another station's RM1 frame to or from the target.
- **A verified reply releases the spacing at once** (operator decision 2026-10-17). Unverified replies
  still hold it.
- **State is derived, never stored**, and folded in send order (log `id`), not arrival order.
- **Key write-only.** The password is never logged, broadcast or put in `stall_events`. The RM1 tag is
  redacted in logs and failed captures.
- **Remove is refused** while a command is in flight or a lockout window runs, because those windows are
  derived from the log that removal deletes.

## Consequences

- Operators get a usable remote view on top of a protocol that otherwise gives no feedback.
- The service carries the protection the node does not: a silent node looks like "no reply", and the
  runbook (`../runbook/RB-006-node-admin-triage.md`) says what to check.
- A second station managing the same node is detected, not prevented.
- Any change to these gates needs a firmware check: the gates mirror firmware behaviour
  (`MeshCom-Firmware-DEV-Main`, `docs/adr-remote-hmac.md`).

## Alternatives considered

- A config switch: rejected by the operator, the GUI is the frontend.
- Per-key operator TX ceiling: dropped 2026-10-16, the board range from `status` is enough.
- Counter as a plain increment: rejected, a re-flashed or second sender would desync it.

## Evidence

- `../archive/2026-10-05_1000-node-admin-ui-concept-and-plan.md`
- `../archive/2026-10-06_1100-node-admin-remote-view-concept.md`
- `../archive/2026-10-11_1900-node-admin-remote-parity-plan.md`
- `../plans/2026-10-06_mcapp-rm1-firmware-asks.md` (open firmware questions)
- `../runbook/RB-006-node-admin-triage.md`
