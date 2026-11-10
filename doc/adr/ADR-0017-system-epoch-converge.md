# ADR-0017: System epoch: converge only what changed

| Field | Value |
|---|---|
| Status | Accepted. Epoch in force: 10 (`bootstrap/mcapp.sh`, `src/mcapp/system_converge.py`). |
| Date | 2026-08-08 (commit `6fa1c29`, "system epoch + self-converging updates") |
| Deciders | Martin Werner |
| Affects | `bootstrap/mcapp.sh` (`SYSTEM_EPOCH`), `bootstrap/lib/converge.sh`, `src/mcapp/system_converge.py` (`REQUIRED_SYSTEM_EPOCH`), `scripts/update-runner.py` |
| Supersedes | - |
| Superseded by | - |

## Context

An app update deploys code, but system-level state (packages, firewall, web front door) lives outside
the app. Boxes update through the update runner, and an update from an older runner may skip those
steps. A full bootstrap run re-runs everything, which is slow and touches live services.

## Decision

- One integer, `SYSTEM_EPOCH`, versions system-level state. It is in `bootstrap/mcapp.sh`, mirrored in
  `REQUIRED_SYSTEM_EPOCH`. A startup test fails if the two differ.
- The installed value is marked at `/var/lib/mcapp/system-epoch`.
- `bootstrap/lib/converge.sh` carries a table: each step names the epoch it last changed in. A converge
  runs only the steps whose epoch is above the installed one. `ensure_web_frontend` always runs.
- Every successful update runs `--converge` on the new slot. The app's converge watchdog triggers a
  converge on boxes whose update came from a pre-epoch runner.

## Consequences

- Changing a step means raising its row to the new epoch in the same commit, or existing boxes skip it.
- Every step must still be safe on an installed box, because a full run executes all of them.
- Operators can see the state (`cat /var/lib/mcapp/system-epoch`) and trigger it from the Update page.
  See `../sop/SOP-002-converge-acceptance-test.md` and `../runbook/RB-004-update-runner-and-slots.md`.

## Alternatives considered

- Always run the full bootstrap on update: rejected, touches live services and is slow on a Pi Zero 2 W.
- Version by app version only: rejected, system changes would not reach boxes that already run that version.

## Evidence

- `CLAUDE.md` (section "System Epoch") and `reference/architecture-reference.md`; no separate plan document exists
- `release-history.md` (epoch changes per release, e.g. "SYSTEM_EPOCH stays at 9" in v2.1.26)
