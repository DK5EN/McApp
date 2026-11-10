# ADR: TLS Remote Access via Caddy Reverse Proxy with DNS-01 Challenge

**Date**: 2026-02-10
**Status**: Accepted

## Context

McApp runs on a Raspberry Pi Zero 2W serving an unencrypted Vue.js webapp (lighttpd:80) with a FastAPI SSE/REST API (:2981). Users want to access their McApp from smartphones over the internet with TLS encryption. The previous approach using mDNS with Fritz!Box TLS certificates doesn't work for public internet access.

Key constraints:
- Raspberry Pi Zero 2W has only 512MB RAM
- Target users are ham radio operators with varying technical expertise
- Port 80 and 443 must work behind consumer routers
- No VPS or external server available
- Solution must be self-contained on the Pi

## Decision

Use **Caddy** as an optional TLS reverse proxy addon with **Let's Encrypt DNS-01 challenge**. Support DuckDNS, Cloudflare, and deSEC.io as DNS providers. Offer **Cloudflare Tunnel** as a zero-port-forwarding alternative.

### Architecture

**Base setup (LAN only — no TLS):**
```
Browser → lighttpd:80 → {
    /webapp/  → static files
    /events   → proxy to FastAPI:2981
    /api/     → proxy to FastAPI:2981
}
```

**TLS addon (internet access):**
```
Browser → Caddy:443 (TLS) → lighttpd:80 → {
    /webapp/  → static files
    /events   → proxy to FastAPI:2981
    /api/     → proxy to FastAPI:2981
}
```

### Key Design Choices

1. **Standalone script, not part of bootstrap** — TLS is optional. The `ssl-tunnel-setup.sh` script runs once after the user confirms their local setup works. It is not called during bootstrap updates/upgrades.

2. **Caddy over nginx** — Caddy handles TLS certificate management, DDNS updates, and reverse proxying in a single binary with minimal configuration. No cron jobs, no certbot, no separate DDNS client.

3. **DNS-01 challenge** — Avoids exposing port 80 to the internet for HTTP-01 challenges. Works behind NAT without port forwarding for the ACME challenge (only port 443 needs forwarding).

4. **lighttpd stays on port 80** — Caddy proxies to lighttpd, not directly to FastAPI. This preserves the existing static file serving and SPA rewrite rules.

5. **WebSocket removal** — All client communication now uses SSE/REST via FastAPI on port 2981, proxied through lighttpd on port 80. The WebSocket handler (`websocket_handler.py`) and the `websockets` Python dependency have been removed.

6. **Same-origin webapp** — The webapp uses relative paths (`/events`, `/api/send`) instead of hardcoded host:port combinations. This works seamlessly with both plain HTTP and HTTPS setups.

## Alternatives Considered

| Alternative | Reason for Rejection |
|---|---|
| **Tailscale Funnel** | Requires ~80MB RAM for the Tailscale daemon, too much for Pi Zero 2W (512MB total) |
| **Self-hosted tunnels** (frp, rathole) | Requires a VPS, adds operational complexity |
| **nginx + acme.sh** | Multiple moving parts (nginx config, certbot/acme.sh cron, separate DDNS client) |
| **Replace lighttpd with Caddy entirely** | Would break the simple base setup; lighttpd is well-tested and minimal |
| **Let's Encrypt HTTP-01** | Requires port 80 open to the internet; doesn't work behind double NAT |

## Consequences

### Positive
- Users get TLS encryption for internet access with minimal setup effort
- DDNS updates are self-contained (Caddy's `dynamic_dns` module)
- Certificate renewal is fully automatic (Caddy manages this internally)
- Two clean deployment modes: plain HTTP (LAN) and HTTPS (internet)
- No additional cron jobs or external dependencies

### Negative
- Caddy binary adds ~50MB disk + ~40MB RAM usage
- Users must set up a DNS provider account (DuckDNS/Cloudflare/deSEC)
- Port 443 must be forwarded on the router (except Cloudflare Tunnel)
- One more service to manage on the Pi

### Risks
- Caddy's Go runtime may consume more memory under load than expected on Pi Zero 2W. Mitigated with `GOMEMLIMIT=256MiB` and `GOGC=50`.
- DNS provider API changes could break DDNS updates. Mitigated by supporting multiple providers.
