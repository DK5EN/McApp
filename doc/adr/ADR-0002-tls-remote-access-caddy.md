# ADR-0002: TLS remote access through Caddy with DNS-01

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-02-10 |
| Deciders | Martin Werner |
| Affects | `bootstrap/ssl-tunnel-setup.sh`, `bootstrap/templates/caddy/`, `bootstrap/templates/cloudflared/` |
| Supersedes | - |
| Superseded by | - |

Converted 2026-11-10 to the ADR format. Original full text: [archive/2026-02-10_1200-tls-remote-access-ADR.md](../archive/2026-02-10_1200-tls-remote-access-ADR.md).
The front door has since changed: Caddy now serves the webapp itself and lighttpd is only a
backend (see ADR-0022 and `backlog.md` B15). The TLS decision below still holds.

## Context

McApp runs on a Pi Zero 2W (512 MB) and serves an unencrypted webapp and API. Users want
smartphone access over the internet with TLS. Port 80 and 443 must work behind consumer routers,
there is no VPS, and the solution must run on the Pi.

## Decision

Caddy is an optional TLS reverse proxy with Let's Encrypt **DNS-01** certificates. Supported DNS
providers: DuckDNS, Cloudflare, deSEC.io. Cloudflare Tunnel is offered for setups without open
ports. The LAN base setup stays plain HTTP and is not changed by the TLS add-on.

## Consequences

- DNS-01 needs no inbound port 80 from the internet and works behind NAT.
- The TLS setup is a separate script (`ssl-tunnel-setup.sh`), not part of the default install.
  Operating it is in `../runbook/RB-011-tls-remote-access-setup.md` and `../sop/SOP-003-tls-maintenance.md`.
- Each provider adds a template to maintain.
- Caddy's memory use is capped in the systemd drop-in (`GOMEMLIMIT`, see `../reference/architecture-reference.md`).

## Alternatives considered

- Port forwarding with Let's Encrypt HTTP-01: needs port 80 open to the internet.
- A VPS relay: needs an external server, which the project does not have.
- Cloudflare Tunnel as the only option: removes the need for open ports, but ties the setup to one
  vendor. Kept as one option among several.

## Evidence

- `archive/2026-02-10_1200-tls-remote-access-ADR.md`
- `reference/tls-architecture.md`
