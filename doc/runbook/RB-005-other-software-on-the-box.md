# RB-005 Running other software on the box

| Field | Value |
|---|---|
| ID | RB-005 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 587-650; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` |
| Applies to (variants) | `debian-13-trixie`, `debian-12-bookworm` (rules differ, marked) |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Running other software on the box"); ADR-0014, POL-004 |

## 1. Purpose and scope

Opening a port, adding a Caddy site or a log folder, and reading the config backups, without McApp overwriting the change at the next update. The rules in POL-004 explain why each step is the way it is.

**McApp's firewall blocks every port it does not know, so another service needs its own rule. Updates keep your rules, your own firewall file and your log folders.**

- **Symptom of a blocked port:** the browser times out. "Connection refused" means the service itself is down.
- **Check:** every bootstrap and update ends with a health check that warns about listening ports the firewall blocks, and about faulty firewall or Caddy config. The kernel log shows `[nftables DROP]` with `DPT=<port>`.
- **Tested on:** OrbStack VMs with Debian Trixie and Bookworm, 2026-11-06, including reboots. Not yet on a real Pi.

### Open a port

On Trixie, put the rule into its own file. This folder is included inside the input chain and updates never touch it:

```bash
echo 'tcp dport 8080 accept' | sudo tee /etc/mcapp/nftables.d/custom.nft
sudo nft -c -f /etc/nftables.conf && sudo systemctl reload nftables
```

- **Use `reload`, not `restart`:** `restart` flushes every table on the box, including those of Docker or fail2ban.
- **A faulty custom file:** a reload refuses it and the running firewall stays. At boot the box falls back to McApp's own rules and logs `McApp: nftables.conf failed`.
- **Do not save a dump over the config:** `nft list ruleset > /etc/nftables.conf` removes McApp's marker, and the file then counts as your own firewall.

On Bookworm, add the rule and save it. Updates add missing McApp rules and keep yours:

```bash
sudo iptables -A INPUT -p tcp --dport 8080 -j ACCEPT
sudo sh -c "iptables-save > /etc/iptables/rules.v4"
```

### Use your own firewall

A `/etc/nftables.conf` with real rules and no `# McApp firewall rules` marker is never replaced. McApp leaves it and warns.

- **What it must allow:** loopback, 22, 80, 443 and 2985/tcp, 1799 and 5353/udp.
- **What you give up:** new McApp rules do not reach this file, and neither the custom folder nor the boot fallback applies.
- **A firewall in another table:** the port check cannot read it and says so.
- **Switched off on purpose:** a stopped `nftables` service, or an open INPUT chain on Bookworm, stays that way.

### Add your own web site

Put each site into `/etc/caddy/conf.d/<name>.caddy`. Every Caddyfile imports that folder at its end, and McApp never edits it:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl restart caddy
```

- **Ports 80 and 443 stay McApp's:** a site claiming them makes the whole config invalid.
- **A broken site:** Caddy will not start on its next restart, which takes the McApp web UI down too. During an upgrade McApp keeps the old config and names the folder.
- **Public-TLS boxes:** the import arrives only when `ssl-tunnel-setup.sh` runs again.

### Log folders

`/var/log` is in RAM, so folders vanish at every reboot. An hourly timer (`mcapp-logdirs.timer`) writes `/etc/tmpfiles.d/mcapp-logdirs.conf`, so they come back with their owner and mode.

- **A new service before the first timer run:** run `sudo /usr/local/sbin/mcapp-logdirs` once.
- **A folder you removed for good:** delete its line from that file, otherwise it returns empty.
- **A folder that was gone before this existed:** it comes back as `root:adm` if a package lists it, and not at all otherwise.
- **Not on Armbian or Orange Pi ramlog boards:** their ramlog service keeps `/var/log` itself.

### Config backups

Before the bootstrap overwrites a config file, it copies it to `/var/lib/mcapp/config-backups/`: readable by root only, newest 5 per file, no copy when nothing changed. Names are the flattened path plus a UTC timestamp, for example `__etc__nftables.conf.20261106T203827Z-000`.

Background: [campaign plan and field report](../archive/2026-11-06_2053-install-coexistence-plan.md).

