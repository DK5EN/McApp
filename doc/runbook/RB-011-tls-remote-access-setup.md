# RB-011 TLS remote access setup

| Field | Value |
|---|---|
| ID | RB-011 |
| Status | DRAFT. Rewritten 2026-11-10 in English. Replaces the German guide, kept as `archive/ssl-tunnel-german-2026-11-10.md`. Verified against the script and templates on that date; not yet run end to end on a box. |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 (or after fixing B20) |
| Applies to (hosts) | `mcapp` (public-TLS boxes only; LAN boxes do not need this) |
| Applies to (variants) | `debian-13-trixie` |
| Sources | `bootstrap/ssl-tunnel-setup.sh` (header, `main()`, `configure_caddy()`, `configure_cloudflared()`, `update_firewall()`, `remove_tls()`, `show_status()`); `bootstrap/templates/caddy/Caddyfile.{duckdns,cloudflare,desec}.tmpl`; `bootstrap/templates/caddy/Caddyfile.mcapp` (LAN front door); `bootstrap/templates/cloudflared/config.yml.tmpl`; `backlog.md` B20; ADR-0002, ADR-0011, ADR-0022 |

## 1. Purpose and scope

Puts McApp on the internet over HTTPS with a publicly trusted certificate. The script replaces the LAN
Caddyfile with one that serves the same webapp and API for a public hostname, and issues certificates
through Let's Encrypt (DNS-01 for DuckDNS, Cloudflare and deSEC; a Cloudflare Tunnel needs no open port).

- Applies when: an operator wants access from outside the LAN with TLS.
- Does **not** apply when: McApp is used on the LAN only. The default LAN front door (Caddy on :80, `bootstrap/templates/caddy/Caddyfile.mcapp`) is correct for that.
- Read `section 2` before you run anything. Two defects are known and one of them stops providers 1 to 3.

## 2. Known defects (read first)

1. **Template lookup is broken (`backlog.md` B20).** `TEMPLATE_DIR` is `${SCRIPT_DIR}/bootstrap/templates`, but
   `SCRIPT_DIR` is already the `bootstrap/` folder. The path resolves to `bootstrap/bootstrap/templates`, which
   does not exist. Providers 1 to 3 then stop with `Template not found: .../caddy/Caddyfile.<provider>.tmpl`
   after the prompts and after Caddy has been downloaded. Provider 4 (Cloudflare Tunnel) does not read this template and is not affected.
   Fix B20 before a run, or run from a copy whose `TEMPLATE_DIR` line is corrected. Do not edit the copy on the box.
2. **Firewall edits ignore the operator's file (POL-004 section 1.3).** `update_firewall()` runs `nft add rule inet filter input
   tcp dport 443 accept` and edits the config with `sed`. It does not check for the McApp marker, and it does
   not use `/etc/mcapp/nftables.d` (see RB-005). Check the result with `nft list ruleset` after the run.
3. **The script installs its own Caddy.** It downloads a custom build with the DNS modules to `/usr/local/bin/caddy`
   and writes `/etc/systemd/system/caddy.service`. The LAN install (bootstrap) uses the distribution package. Both
   cannot be active at once. This conflict is not resolved yet.

## 3. Architecture (as the templates build it)

```
Browser (HTTPS, public name) -> Caddy :443 (certificate from Let's Encrypt)
    /webapp/*                 -> static files from /var/www/html (pre-compressed, served by Caddy)
    /events*, /api/*, /health -> 127.0.0.1:2981   (McApp API, direct; lighttpd is NOT in the path)
    /update-stream            -> 127.0.0.1:2985   (update runner, rewritten to /stream)

Cloudflare Tunnel (provider 4) -> cloudflared -> http://localhost:80 -> the LAN Caddy (Caddyfile.mcapp)
```

lighttpd on 127.0.0.1:8082 stays installed, but no public template proxies to it any more. The script's own
header still says it does; that comment is out of date.

## 4. Inputs

- Access: `ssh` to the box as a user with `sudo`.
- Accounts: one of DuckDNS (subdomain and token), a Cloudflare zone with an API token that can edit DNS, a deSEC
  subdomain and token, or a Cloudflare Tunnel token (`cloudflared tunnel create`).
- Network: outbound internet from the box. Inbound 443 only for providers 1 to 3 and only if the router forwards it.
- Preconditions:
  - McApp is running (`systemctl is-active mcapp`). The preflight asks before continuing if it is not.
  - lighttpd is not required by the templates, but the bootstrap install leaves it on 127.0.0.1:8082.
  - Secrets are typed at the prompts. They are never put on the command line.

## 5. Steps

1. **Check the current state.** `sudo ./bootstrap/ssl-tunnel-setup.sh --status` (path as in section 2.1). It prints the
   Caddy binary and service, the cloudflared state and the TLS settings. Nothing changes.
2. **Confirm the template lookup** before you start, so the run does not fail halfway:
   `ls bootstrap/bootstrap/templates 2>/dev/null && echo BROKEN || echo "check the TEMPLATE_DIR line"`. If the path
   is missing, B20 applies (section 2.1).
3. **Run the setup.** `sudo ./bootstrap/ssl-tunnel-setup.sh`. Choose the provider (1 DuckDNS, 2 Cloudflare, 3 deSEC,
   4 Cloudflare Tunnel) and give the values the prompts ask for. Confirm the summary.
4. **Read the health check.** It requests `https://<hostname>/webapp/` with `--resolve` to 127.0.0.1. HTTP 200 means
   the front door works. A warning "certificate may still be issuing" on the first run is normal (wait a few minutes).
5. **Check from outside the LAN.** Open `https://<hostname>/webapp/` on a phone with mobile data. Expect the webapp.
6. **Check the firewall.** `sudo nft list ruleset | grep 'dport 443'`. Expect one accept rule for 443 (section 2.2).

## 6. Definition of success

- `sudo ./bootstrap/ssl-tunnel-setup.sh --status` shows Caddy running and the hostname.
- `curl -sI https://<hostname>/webapp/` returns `HTTP/2 200` from outside the LAN (for provider 4, from outside the tunnel host's LAN).
- `curl -s https://<hostname>/health` returns the McApp health answer (through the front door, not lighttpd).
- The LAN address (`http://<hostname>.local/`) should not answer after a TLS setup: the public Caddyfile has no LAN site.
  The LAN front door comes back only with the bootstrap (section 7).

## 7. Removing TLS (back to the LAN front door)

`sudo ./bootstrap/ssl-tunnel-setup.sh --remove` stops Caddy and cloudflared, removes their service files, closes 443,
and deletes the `TLS_*` keys from `config.json`. **Afterwards nothing listens on 80 or 443.** The script says so.
To restore the LAN front door, run the bootstrap again: `sudo ~/mcapp-slots/current/bootstrap/mcapp.sh`.
The Caddy binary and `/etc/caddy` are left in place, by design.

## 8. Security notes

- **The McApp API has no authentication.** Behind a public name, anyone can call it. This is a known,
  pre-existing condition (`CLAUDE.md`, "Node Admin"). Node Admin itself refuses any non-LAN `Host`, so it is not
  usable through the public name (ADR-0011).
- **Secrets on disk:** `/etc/mcapp/caddy.env` holds the DNS or tunnel token. The script sets it to `0600` (POL-003).
- **Config backups:** the script backs up `/etc/caddy/Caddyfile` and the env file before it overwrites them
  (POL-004 section 1.4). Backups are `0600`.
- Consider Cloudflare Access or a VPN in front of the name if the API must not be public at all. The project has no
  built-in gate for this yet.

## 9. Troubleshooting

| Symptom (exact message) | Cause | Action |
|---|---|---|
| `Template not found: .../caddy/Caddyfile.<provider>.tmpl` | B20: `TEMPLATE_DIR` resolves to `bootstrap/bootstrap/templates` | fix B20, or run from a copy with the line corrected (section 2.1) |
| `HTTPS returned HTTP 000` in the health check | 443 closed, or the router does not forward it, or DNS does not point to the box yet | check `nft list ruleset`, the router forward, and the DNS record (`dig +short <hostname>`) |
| `HTTPS returned HTTP 5xx` | Caddy is up but the backend is not (2981 down) | RB-001: `systemctl status mcapp`, then `journalctl -u mcapp` |
| certificate errors in `journalctl -u caddy` (`acme`, `dns`) | wrong DNS token, or provider rate limit | recheck the token in `/etc/mcapp/caddy.env`; wait before retrying |
| `caddy validate` error printed by the run | the rendered Caddyfile is invalid | the script validates before it starts Caddy and stops there; read the error, fix the value, run again |
| Port 443 dropped after an update | the firewall rule was not persisted or the file was replaced | re-check section 5.6; see RB-005 for the operator-file rules |

## 10. Feedback and changes

- A step that is wrong when you run it: fix this file the same day, or log it in `../review-log.md`.
- When B20 is fixed, remove section 2.1 and update the Sources line.

## 11. HITL review (owner)

- [ ] Confirmed: the steps match a successful run on a public-TLS box
- [ ] Refuted: see comments
- Comments:
- Reviewer / date:
