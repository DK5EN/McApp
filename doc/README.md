# McApp documentation

Status of this index: restructured 2026-11-10 (verification run, see `review-log.md`). ADRs, policies and SOPs approved by the owner on 2026-11-10. Runbooks are DRAFT.
Language: English for new documents. Older German documents keep their language.

## Layers

Each folder answers one question. A document belongs to one layer and links to the others.

| Layer | Folder | Answers | ID |
|-------|--------|---------|----|
| Architecture decision | [adr/](adr/README.md) | why the system is shaped this way | `ADR-NNNN` |
| Policy | [policy/](policy/README.md) | what must hold, and why | `POL-NNN` |
| SOP | [sop/](sop/) | who does what, in which order, and what success looks like | `SOP-NNN` |
| Runbook | [runbook/](runbook/) | the exact commands and paths for one task | `RB-NNN` |
| Reference | [reference/](reference/) | how the system is built (stable facts) | none |
| Plan | [plans/](plans/) | open work: concepts, campaigns, handovers that are not done | dated name |
| Archive | [archive/](archive/INDEX.md) | closed or superseded work, kept unchanged | dated name |

A decision goes in an ADR. A rule goes in a policy. A procedure goes in an SOP, and the SOP links the runbooks it
uses. A runbook never contains decision logic.

## Start here

- **Operating the box:** [RB-001 health checks](runbook/RB-001-health-checks.md), then the runbook that matches the symptom.
- **Releasing:** [SOP-001 release](sop/SOP-001-release.md).
- **Changing a shared contract:** [SOP-005](sop/SOP-005-wire-contract-change.md).
- **Why something is the way it is:** [adr/README.md](adr/README.md).
- **What is still open:** [backlog.md](backlog.md) (deferred items), [tech-debt.md](tech-debt.md), [plans/](plans/).
- **What changed and what is unverified:** [review-log.md](review-log.md).

## Index

### Decisions (ADR)

[Index with status](adr/README.md). Accepted: 22. Five of them (ADR-0001 to 0005) predate the format; their original text is in `archive/`.

### Policies (POL)

[POL-001 quality gate](policy/POL-001-quality-gate.md) ·
[POL-002 vendored code and corpora](policy/POL-002-vendored-and-corpora.md) ·
[POL-003 secrets](policy/POL-003-secrets.md) ·
[POL-004 change guards](policy/POL-004-change-guards.md) ·
[POL-005 schema and epoch](policy/POL-005-schema-and-epoch.md) ·
[POL-006 production data](policy/POL-006-production-data-safety.md) ·
[POL-007 wire contracts](policy/POL-007-wire-contracts.md)

### Procedures (SOP)

[SOP-001 release](sop/SOP-001-release.md) ·
[SOP-002 converge acceptance test](sop/SOP-002-converge-acceptance-test.md) ·
[SOP-003 TLS maintenance](sop/SOP-003-tls-maintenance.md) ·
[SOP-004 high-hop forensics](sop/SOP-004-high-hop-forensics.md) ·
[SOP-005 wire contract change](sop/SOP-005-wire-contract-change.md) ·
[SOP-006 stall investigation](sop/SOP-006-stall-investigation.md)

### Runbooks (RB)

[RB-001 health checks](runbook/RB-001-health-checks.md) ·
[RB-002 BLE troubleshooting](runbook/RB-002-ble-connection-troubleshooting.md) ·
[RB-003 Bluetooth rfkill](runbook/RB-003-bluetooth-rfkill.md) ·
[RB-004 update runner and slots](runbook/RB-004-update-runner-and-slots.md) ·
[RB-005 other software on the box](runbook/RB-005-other-software-on-the-box.md) ·
[RB-006 Node Admin triage](runbook/RB-006-node-admin-triage.md) ·
[RB-007 stalls: query and replay](runbook/RB-007-stall-query-and-replay.md) ·
[RB-008 standalone BLE service](runbook/RB-008-ble-service-standalone.md) ·
[RB-009 bootstrap install and CLI](runbook/RB-009-bootstrap-install-and-cli.md) ·
[RB-010 remote SD card flashing](runbook/RB-010-remote-sd-card-flashing.md) ·
[RB-011 TLS remote access setup](runbook/RB-011-tls-remote-access-setup.md) (architecture stale, see banner) ·
[RB-012 classifier soak check](runbook/RB-012-classifier-soak-check.md)

### Reference

- [reference/architecture-reference.md](reference/architecture-reference.md): components, ports, message flow, dependencies
- [configuration.md](reference/configuration.md): `config.json` keys, environment overrides, fixed values
- [reference/database-reference.md](reference/database-reference.md): schema v42, tables, retention, queries
- [reference/dataflow.md](reference/dataflow.md): data paths for both transports, signal path, Node Admin flow
- [firewall.md](reference/firewall.md): ports and drops
- [reference/version-logic.md](reference/version-logic.md): version numbers and tags
- [reference/tls-architecture.md](reference/tls-architecture.md): TLS front door
- [reference/telemetry.md](reference/telemetry.md), [reference/MeshCom-ACK.md](reference/MeshCom-ACK.md), [reference/a0-commands.md](reference/a0-commands.md), [reference/ble-state-machine.md](reference/ble-state-machine.md), [reference/ble-pin.md](reference/ble-pin.md)
- [reference/2026-10-04_1500-node-tz-ble-contract.md](reference/2026-10-04_1500-node-tz-ble-contract.md): wire contract, node time zone
- [reference/McApp-Monitor-Help.html](reference/McApp-Monitor-Help.html): user help for the monitor

### Registers (living)

- [backlog.md](backlog.md): deferred work, with due dates or data dependencies
- [tech-debt.md](tech-debt.md): refactoring candidates
- [ops-mcapp-health-log.md](ops-mcapp-health-log.md): production health log (mcapp.local)
- [release-history.md](release-history.md): release notes, newest first (`release.sh` asks for the section)
- [review-log.md](review-log.md): verification runs, fixes, owner questions

## Conventions

- **IDs:** `ADR-NNNN` (4 digits, never reused), `POL-NNN`, `SOP-NNN`, `RB-NNN`. Plans and archive use `YYYY-MM-DD_HHMM-slug.md`.
- **Status:** `DRAFT` (not reviewed), `REVIEWED` (owner confirmed), `DEPRECATED`. ADRs use `Proposed`, `Accepted`, `Superseded by ADR-NNNN`.
- **Dates:** ISO 8601. A fact without a date is not trusted.
- **Sources:** every new document names the code path or the document it was taken from.
- **Secrets:** named, never written (POL-003).
- **Moves:** nothing is deleted. Closed work goes to `archive/` and gets a line in `archive/INDEX.md`.
- **Scripts that read docs:** `scripts/release_notes_tests.py` pins `release-history.md` and the archive copy. Do not move those two.

## Review cycle

- Every policy and every SOP at least every 6 months (critical SOPs: release, converge, TLS, stall investigation).
- Every runbook is checked when the commands it uses change.
- An ADR is not edited to change its decision: supersede it.
- A reader who finds a wrong step fixes the document or logs it in `review-log.md` the same day.
