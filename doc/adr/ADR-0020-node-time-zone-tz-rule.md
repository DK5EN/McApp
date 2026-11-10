# ADR-0020: Node time zone via the TZ rule, UTC as fallback

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-10-04 (contract `doc/archive/2026-10-04_1500-node-tz-ble-contract.md` in `reference/`; plan archived) |
| Deciders | Martin Werner |
| Affects | `ble_service/src/node_tz.py`, `ble_adapter.py` (`set_time`), `ble_client_remote.py` |
| Supersedes | - |
| Superseded by | - |

## Context

TZ-01 firmware keeps a POSIX TZ rule on the node. The old `--utcoff` command **clears** that rule. A
wrong `--utcoff` can therefore destroy a rule the operator set. The node's register cache survives a
reconnect, so a cached state is not proof of the node's current state.

## Decision

`set_time()` classifies the node before it sends any offset:

- **Rule set** (`0x20`): nothing else is sent.
- **Empty rule** and policy `host_if_unset` (the default): the host rule (TZif footer of
  `/etc/localtime`, or `MCAPP_NODE_TZ`) is pushed once with `--settz` and confirmed through the `SN`
  push. `MCAPP_NODE_TZ_POLICY=never` opts out.
- **Old firmware** without a `TZ` key: the byte-identical `--utcoff`, then `0x20`.
- **Unknown or unverified:** `0x20` only, never `--utcoff`. UTC is always right. An unverified `--settz`
  is not followed by `--utcoff` either, because the node may have accepted it.
- A cached non-RULE classification is trusted only after an `SN` has arrived since hello. Otherwise a
  probe runs.

## Consequences

- A node's rule is never overwritten by a guess.
- The first connect causes a probe that is expected, not a bug.
- MH `DATE` and `TIME` are still converted with the host offset. Deferred: `backlog.md` B7.

## Alternatives considered

- Always send `--utcoff`: rejected, clears the rule.
- Always push the host rule: rejected, overwrites an operator's node rule.

## Evidence

- `../reference/2026-10-04_1500-node-tz-ble-contract.md`
- `../archive/2026-10-04_1600-node-tz-implementation-plan.md`
- `../backlog.md` B7
