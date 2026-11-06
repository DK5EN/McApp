# Coexisting with other software on the same box: campaign plan

**Goal:** a McApp update must not break other services on the box, and operators get a supported way to open their own ports. Field report 2026-11-06: nginx on :8080 (iot4pi, Pi 3B) unreachable after the update to v2.1.24. There was no VPN change; the release only added gzip and an epoch bump.

**Status:** W0 to W5 done. Commits stay on `development`; release and the `mcapp.local` test need separate approval.

## Root cause of the field report

Confirmed from the reporter's Pi (Trixie, epoch 8) and their answers, 2026-11-06. The fix (a rule for 8080 in the current file) brought the page back.

- **The firewall dropped the traffic.** The kernel log showed `[nftables DROP]` with `DPT=8080` from the reporter's PC. nginx was running; its 401 is the page's login.
- **McApp overwrote the operator's firewall file.** The reporter opened 8080 and saved the file, by their account themselves. The backup is an `nft list ruleset` dump: no comments, live counters. A dump loses McApp's marker while keeping all its rules, so a bootstrap run treated it as foreign and replaced it on 2026-11-06 at 14:33.
- **v2.1.24 is not the cause.** It was published at 16:37, and its converge logged `nftables rules already configured`. The reporter updated to v2.1.23 that morning and is unsure they tested the page afterwards; a second card on v2.1.23 times out too. The 14:33 run itself is not attributed to a version with certainty.
- **Ruled out on this box:** a lost temporary rule, and the nginx reboot failure, since `/var/log/nginx` exists there. That failure stays a real defect for other boxes (W0).

## Decisions

- **Scope:** W0 to W5, including the Caddy import.
- **Custom ports:** a documented file, no GUI. Opening ports from an API without login would need at least the Node Admin LAN-only guard.
- **Port warning:** bootstrap log and `health.sh`. A Settings card comes later.
- **Bookworm:** still supported, fixed in this campaign.
- **Rollout:** stop at commits.
- **Config backups (operator request 2026-11-06):** back up every config file before the bootstrap overwrites it, as evidence for later troubleshooting.

## W0 results

Tested 2026-11-06 in OrbStack arm64 VMs (Debian trixie and bookworm), nft 1.1.3, iptables 1.8.9, Caddy 2.11.9. No real Pi was involved.

| Check                                                        | Result                                                                                        |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| nginx, `/var/log` on tmpfs, reboot                           | `/var/log/nginx` gone, `nginx -t` emerg, unit `failed`, nothing on :8080                      |
| Same with a tmpfiles entry for the folder                    | nginx active, HTTP 200                                                                        |
| `/var/log/nginx` in `nginx-common.list`                      | Yes, so dpkg lists can restore packaged folders                                               |
| `include` inside `chain input`, empty glob or missing folder | Valid, loads                                                                                  |
| Custom `tcp dport 8080 accept` through the include           | Sits before the final drop. From the Mac: 8080 HTTP 200, unlisted 8081 times out              |
| Faulty custom file                                           | `nft -c` rc 1, `nft -f` rc 1, live ruleset unchanged                                          |
| `flush ruleset`                                              | Deletes a foreign table                                                                       |
| `destroy table inet filter`                                  | Foreign table survives; reload is idempotent; loads on empty ruleset and over the old file    |
| `nftables.service`                                           | `ExecStop` is `nft flush ruleset`, so `restart` deletes foreign tables whatever the file says |
| `iptables -C \|\| -A` re-apply on bookworm                   | No duplicates, custom rule survives                                                           |
| Caddy `import /etc/caddy/conf.d/*`, missing or empty folder  | Valid                                                                                         |
| Custom Caddy site                                            | Adapted into the config                                                                       |
| Broken custom site, or one claiming `:80`                    | `caddy validate` fails, naming the file or the ambiguous site                                 |

## Design

- **Converge runs only what changed.** Each step has a "last changed in epoch N" constant and runs only if the installed epoch is below N. No new state file. All existing steps start at 8, so boxes on epoch 7 or older behave as today and a box on 8 runs only the steps marked 9. The apt upgrade is gated too. `ensure_web_frontend` stays ungated because it is a no-op when current. A full `mcapp.sh` run still runs everything.
- **A file with real rules and no marker is never replaced.** It belongs to the operator: McApp keeps it byte-identical, applies nothing and warns which ports it needs. A rule-free stock file is still replaced. This reverses the first design, which would have repeated the field report.
- **Trixie firewall.** Existing boxes get one include line through a staged edit, checked with `nft -c -f`, so custom rules stay where they are. `flush ruleset` becomes `table inet filter` plus `delete table inet filter` only if the file defines no other table and has no top-level include; otherwise reloads would duplicate rules. `destroy` is out because it needs kernel 6.3. Existing boxes reload and never restart; a stopped service stays stopped.
- **Faulty custom file at boot.** `nft -f` would fail and leave the box with NO firewall. A drop-in for `nftables.service` retries without the include line, so the box comes up with McApp's own rules and the failure is logged.
- **Bookworm firewall.** Flush only on a real first install: no `[iptables DROP]` rule in `rules.v4` and no epoch marker. After that, `iptables -C || -A`, then an atomic save. An open INPUT chain stays open. Non-22 SSH ports come from `sshd -T`, `ListenAddress` and the session.
- **Port warning.** Custom rules live in `/etc/mcapp/nftables.d/*.nft`. Log and `health.sh` list non-loopback listening ports the firewall blocks and run the `nft -c` check. Runs at converge or health-check time only, so it does not catch a service at the moment it is installed.
- **`/var/log` folders.** A generator on an hourly timer writes `/etc/tmpfiles.d/mcapp-logdirs.conf`, with the priority real folder, then its own earlier line, then package default (`root:adm`, 0755), and writes only on change. It runs before a first-install mount. It has its own step, `configure_logdirs`, at epoch 9, while `configure_tmpfs` stays at 8 because it can re-mount on a customised box. Boards with a ramlog service are skipped. Known limits: an admin-removed folder stays listed until its line is deleted, a gone folder for a non-root writer returns with the default owner, and a gone packaged plain file would read as a folder.
- **Caddy.** `import /etc/caddy/conf.d/*` at the end of all four templates, version bump, and a kept previous-config backup. Accepted risk: a broken custom site stops Caddy at its next restart, so the McApp web UI goes down. It fails closed, the guide has the validate command, and `health.sh` reports it.
- **Config backups.** `backup_config_file` in `bootstrap/lib/detect.sh` copies a file to `/var/lib/mcapp/config-backups/` (folder 0700, files 0600, because `config.json` holds secrets) before a write. It skips a copy identical to the newest one, keeps the newest 5 per file, and warns rather than fails. Existing `.bak` files and the Caddy rollback copy stay.

## Waves

| Wave | Content                                                    | Writers      | Exclusive files                                                                                                                              | State |
| ---- | ---------------------------------------------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| W0   | Real-tool checks                                           | Orchestrator | None in the repo                                                                                                                             | Done  |
| W1A  | Config backups                                             | 1            | `bootstrap/lib/detect.sh`, `system.sh`, `packages.sh`, `config.sh`, `deploy.sh`, new `scripts/bootstrap_config_backup_tests.py`              | Done  |
| W1B  | Per-step converge, epoch 9                                 | 1            | `bootstrap/mcapp.sh`, new `bootstrap/lib/converge.sh`, `scripts/bootstrap_pinning_tests.py`, new `scripts/bootstrap_converge_steps_tests.py` | Done  |
| W2A  | Firewall on both OS paths                                  | 1            | Firewall section of `bootstrap/lib/system.sh`, new `scripts/bootstrap_firewall_tests.py`                                                     | Done  |
| W2B  | Port warning                                               | 1            | `bootstrap/lib/health.sh`, new `scripts/bootstrap_port_warning_tests.py`                                                                     | Done  |
| W3A  | Preserve `/var/log` folders                                | 1            | Tmpfs section of `bootstrap/lib/system.sh`, new timer and service templates, new `scripts/bootstrap_logdirs_tests.py`                        | Done  |
| W3B  | Caddy import                                               | 1            | `configure_caddy` in `bootstrap/lib/packages.sh`, `bootstrap/templates/caddy/*`, `scripts/caddy_config_tests.py`                             | Done  |
| W5   | Operator guide, `CLAUDE.md`, READMEs, operations reference | Orchestrator | Docs                                                                                                                                         | Done  |

W1A and W1B run in parallel, W2A and W2B run in parallel, W3A and W3B run in parallel. Orchestrator only: `scripts/run_startup_tests.py`, `src/mcapp/system_converge.py`, this doc. Implementers run Sonnet medium, the advisor Opus medium.

## Acceptance

- An 8 to 9 converge runs no `apt upgrade` and touches no step that did not change.
- A custom 8080 rule survives a converge on both OS paths and SSH still connects. A faulty custom file leaves the running firewall unchanged, and at boot the box still comes up with McApp's rules.
- A foreign nftables table survives a converge.
- nginx comes up after a reboot with `/var/log` on tmpfs, tested in OrbStack VMs.

## Follow-ups

Moved to `doc/backlog.md`, B18 to B21: the full run ignoring a failed apt phase, lighttpd's bind, `ssl-tunnel-setup.sh`, and small items.

## Limits

- First rollout on `mcapp.local` carries an SSH lockout risk. Changes are additive and pre-checked, but it needs console access at hand.
- A temporary `nft add rule` still disappears on reboot.
- Activating an older slot and running its full `mcapp.sh` brings back the old firewall behaviour.
