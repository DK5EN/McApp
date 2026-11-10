# ADR-0022: No Ansible and no nginx

| Field | Value |
|---|---|
| Status | Accepted (decided 2026-10-31, `backlog.md` B15) |
| Date | 2026-10-31 |
| Deciders | Martin Werner |
| Affects | `bootstrap/` (stays shell), Caddy and lighttpd roles |
| Supersedes | - |
| Superseded by | - |

## Context

The bootstrap is a large shell tree. Two alternatives were on the table: Ansible, to replace the shell
with declarative roles, and nginx, to replace the Caddy-plus-lighttpd front door. The evaluation is in
`doc/archive/2026-10-31_1100-ansible-and-web-server-evaluation.md`.

## Decision

- **No Ansible rewrite.** The bootstrap stays a shell tree (`bootstrap/mcapp.sh` and `lib/`), with the
  converge table of ADR-0017.
- **No nginx.** Caddy is the front door and serves the webapp. lighttpd stays only as the backend for
  public-TLS Caddyfiles on existing boxes, and it is to be removed as `backlog.md` B15.

## Consequences

- No Python control node and no extra runtime on a 512 MB Pi.
- The shell tree stays hard to test (see ADR-0016 and the bootstrap suites). Rules that keep it safe
  to re-run are in ADR-0017 and `../policy/POL-004-change-guards.md`.
- Two web servers exist until B15 is done. Their roles are in `../reference/architecture-reference.md`.

## Alternatives considered

- Ansible: rejected, Python overhead and an operator base that is not DevOps.
- nginx: rejected, a second TLS stack next to Caddy's DNS-01 setup (ADR-0002).
- Debian package: deferred to the BLE service (`backlog.md` B13), not to the app.

## Evidence

- `../archive/2026-10-31_1100-ansible-and-web-server-evaluation.md`
- `../backlog.md` B12, B13, B15
