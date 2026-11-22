# ADR-0001: Single idempotent bootstrap entry point

| Field | Value |
|---|---|
| Status | Accepted (implemented) |
| Date | 2026-01-25 |
| Deciders | Martin Werner |
| Affects | `bootstrap/mcapp.sh`, `bootstrap/lib/*.sh` |
| Supersedes | the three separate install scripts of 2025 |
| Superseded by | - |

Converted 2026-11-10 to the ADR format. Original full text (with the alternatives table and
implementation notes): [archive/2026-01-25_1400-bootstrap-rewrite-ADR.md](../archive/2026-01-25_1400-bootstrap-rewrite-ADR.md).

## Context

The install used three shell scripts and four curl calls, needed a manual edit of `config.json`
between runs, and was not idempotent. It hard-coded a German locale, Austrian coordinates and the
Bookworm Caddy repo, had no SD-card wear protection and no system hardening, and installed Python
dependencies with pip, which drifted.

## Decision

One entry point, `bootstrap/mcapp.sh`, handles every case:

1. Detect the state (fresh, incomplete, migrate, upgrade) before changing anything.
2. Use `uv` for Python, never pip or a hand-made venv.
3. Make every step idempotent through marker-based config management.
4. Put `/tmp` and `/var/log` on tmpfs and make the journal volatile (SD-card protection).

## Consequences

- Re-running the bootstrap is the repair path. `--check`, `--fix`, `--converge` and `--reconfigure`
  are modes of the same script.
- Every config overwrite must back up first. This became a rule later (see `../policy/POL-004`).
- Because the script is one file with many modules, its steps have to stay safe to re-run on a
  box that already runs everything. That constraint shaped ADR-0017.
- Rejected: Ansible and Terraform (about 50 MB Python on a 512 MB Pi, and the operators are not
  DevOps engineers), and Docker (about 100 MB daemon overhead). Revisited and rejected again in
  ADR-0022.

## Alternatives considered

See the archived original. Short list: keep three scripts with a wrapper (rejected: still not
idempotent); Ansible (rejected: RAM and operator skills); containers on the Pi (rejected: RAM).

## Evidence

- `bootstrap/README.md`, `bootstrap/mcapp.sh`
- `archive/2026-01-25_1400-bootstrap-rewrite-ADR.md`
