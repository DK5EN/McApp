# Policies (POL)

A policy states **what must hold and why**. It does not give steps (SOP) or commands (runbook), and it
does not explain a design (ADR). Each policy names the rule, the reason, how to verify it, and its
exceptions.

Status of every file here: REVIEWED, approved by the owner on 2026-11-10 (see `../review-log.md`, Q1).

| ID | Policy | Main ADR |
|----|--------|----------|
| [POL-001](POL-001-quality-gate.md) | Quality gate and tooling | ADR-0016 |
| [POL-002](POL-002-vendored-and-corpora.md) | Vendored subtrees and shared corpora | ADR-0018 |
| [POL-003](POL-003-secrets.md) | Secrets and sensitive data | ADR-0011, ADR-0013 |
| [POL-004](POL-004-change-guards.md) | Change guards: holds, pins, operator files | ADR-0014, ADR-0001 |
| [POL-005](POL-005-schema-and-epoch.md) | Schema migrations and system epoch | ADR-0017 |
| [POL-006](POL-006-production-data-safety.md) | Production data and slot activation | - |
| [POL-007](POL-007-wire-contracts.md) | Wire contracts and firmware interoperability | ADR-0003, ADR-0005, ADR-0019 |

## Conventions

- **Language:** English for new documents. Older German documents in `../archive/` and `../plans/` keep their language.
- **Dates:** ISO 8601. A fact without a date is not trusted.
- **Sources:** each policy names the file it was extracted from.
- **Secrets:** named, never written. See POL-003.
- **Review:** every policy at least every 12 months, and whenever an ADR it cites is superseded.
