# POL-004: Change guards: holds, pins, operator files

| Field | Value |
|---|---|
| ID | POL-004 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to | the mcapp.local box and every managed box; `bootstrap/`, `scripts/update-runner.py` |
| Sources | `CLAUDE.md` ("Bootstrap Network Safety", "Coexisting with other software", "Node Time Zone", "Memory Footprint"); `doc/archive/2026-11-06_2053-install-coexistence-plan.md`; `doc/archive/2026-09-18_2330-bootstrap-network-safety-plan.md`; `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Running other software on the box"); the network-manager decision is recorded outside this repo in `~/.claude/skills/pi-ops/SKILL.md` (host table, "Other apt holds") and `~/homeautomation/rpizero/aiops/pi-ops/run-log.md` (2026-10-25) |

## 1. Rule

These items are intentional. An automated clean-up, an `apt autoremove`, a "tidy up", or an unreviewed
upgrade must not remove them. Each guard states what it is and how to check it.

### 1.1 wpasupplicant pin on trixie

- **What:** `/etc/apt/preferences.d/mcapp-wpasupplicant`, priority 1001, `wpasupplicant` pinned to `2:2.10-24`.
- **Why:** the `+rpt1` build forces SAE and drops the Zero 2 W off WiFi (ADR-0014).
- **Do not** remove the pin, or upgrade wpasupplicant inline, until the removal criteria in the plan hold.

### 1.2 network-manager hold

- **What:** `apt-mark hold network-manager` on mcapp.local.
- **Status:** the operator's decision (2026-10-25). The bootstrap holds it only during the apt phase and treats
  a pre-existing hold as the operator's.
- **Do not** unhold it. Removal needs the operator.

### 1.3 Operator firewall file

- **What:** `/etc/nftables.conf` with real rules and no McApp marker is the operator's file.
- **Rule:** McApp keeps it byte-identical and applies nothing. An empty deny-all chain counts as a rule.
  Only a rule-free stock file is replaced.
- **Rule:** a marked file is edited, never rewritten. Use `systemctl reload nftables`, **never** `restart`
  (its `ExecStop` flushes every table, including Docker's and fail2ban's).

### 1.4 Config files: back up before overwrite

- **Rule:** every write of a config file is preceded by `backup_config_file`, inside the branch that writes.
  Copies are `0600` because `config.json` and `caddy.env` hold secrets. A new write site needs an entry in
  `bootstrap_config_backup_tests.py`.

### 1.5 Boot configuration

- **`cmdline.txt` stays exactly one line.** A second line makes the bootloader ignore the file.
- **`gpu_mem` lands in `[all]` or before any section header** on a Pi Zero 2 W.
- **Boot-config changes need a reboot** and are marked in `/var/lib/mcapp/reboot-required`. Do not assume a
  change is live until the marker is gone.

### 1.6 Journald and log folders

- **`configure_logdirs` must not re-mount `/var/log` or `/tmp`** over live services on a customised box.
- **Journal bind mount** only on boards without an armbian or orangepi ramlog service.

### 1.7 Other guards

- **`unattended-upgrades.service` disabled, its timers kept.** The timer does the work.
- **rpizero guards** (strongswan mask, wpasupplicant origin pin, `pi` user lock) belong to rpizero, which no longer runs McApp (`CLAUDE.md`, Deployment). They are listed in the pi-ops skill, not here.

## 2. Why

Each guard protects a box that cannot be rescued on site. A lost guard showed up as an outage (2026-09-18),
a lost firewall rule (2026-11-06), or a stopped service (2026-10-25).

## 3. Verify

```bash
ssh mcapp.local 'apt-mark showhold; ls /etc/apt/preferences.d/; grep -A2 wpasupplicant /etc/apt/preferences.d/mcapp-wpasupplicant'
ssh mcapp.local 'test -f /var/lib/mcapp/reboot-required && echo "reboot required" || echo "up to date"'
```

Expected: `network-manager` and `wpasupplicant` in the holds list (`wpasupplicant` for mcapp only), the pin file present,
and `Pin: version 2:2.10-24`.

## 4. Exceptions

- A guard can be changed by a documented decision. Update the source first (ADR or plan), then the guard, then this policy.
- Pi images that ship a different default need a guard review, not a silent override.

## 5. Related

- ADR-0014, ADR-0001, ADR-0017
- `../runbook/RB-005-other-software-on-the-box.md` (how an operator opens a port without breaking the guard)
