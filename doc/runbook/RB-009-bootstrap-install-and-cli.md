# RB-009 Bootstrap: install, repair and CLI

| Field | Value |
|---|---|
| ID | RB-009 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 94-116, 148-160; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp`, `rockpi-4b` (Armbian variant differs, see `bootstrap/README.md`) |
| Applies to (variants) | `debian-13-trixie`, `debian-12-bookworm` |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Deployment", "Bootstrap CLI Options"); `bootstrap/README.md`; ADR-0001 |

## 1. Purpose and scope

Installing, repairing and converging a McApp box with `bootstrap/mcapp.sh`. Full documentation is `bootstrap/README.md`; this runbook gives the commands.

Target: Raspberry Pi Zero 2W running as systemd service (`mcapp.service`)

### New Bootstrap System (Recommended)

```bash
# Single command for install, update, or repair
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash
```

The bootstrap script:

1. Auto-detects state (fresh/migrate/upgrade)
2. Prompts for configuration on first install
3. Creates Python venv in `~/mcapp-venv` using uv
4. Configures SD card protection (tmpfs, volatile journal)
5. Sets up firewall (nftables on Trixie, iptables on Bookworm)
6. Disables IPv6 (fixes mDNS timeouts caused by Happy Eyeballs algorithm)
7. Enables and starts systemd service

See `bootstrap/README.md` for full documentation.


```bash
sudo ./mcapp.sh --check       # Dry-run, show what would change
sudo ./mcapp.sh --force       # Force reinstall everything
sudo ./mcapp.sh --fix         # Repair broken installation
sudo ./mcapp.sh --reconfigure # Re-prompt for config values
sudo ./mcapp.sh --quiet       # Minimal output (for cron)
sudo ./mcapp.sh --converge    # Converge system-level state (packages, firewall, web front
                               # door) to this release's epoch; deploys nothing; no-op when
                               # /var/lib/mcapp/system-epoch is already current
```

