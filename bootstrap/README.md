# McApp Bootstrap

> **Note:** The GitHub repo is `DK5EN/McAdvChat` (legacy name kept for compatibility).
> The project is called **McApp**.

Unified installer and updater for McApp - the MeshCom message proxy for ham radio operators.

> **Installation instructions, CLI options, and service management** are documented in the main [README.md](../README.md). This file covers bootstrap internals and troubleshooting only.

## What Gets Installed

| Component | Purpose |
|-----------|---------|
| lighttpd | Static file server for Vue.js webapp |
| uv | Python package manager (creates venv via `uv sync`) |
| mcapp.service | systemd service for the proxy |
| nftables/iptables | Firewall rules |

## Migrating from Old Installation

If you previously installed McApp using the old scripts (`install_caddy.sh`, `mc-install.sh`, `install_mcapp.sh`), the bootstrap script will automatically detect and migrate your installation.

**What gets migrated:**
- Your existing `config.json` is preserved (new fields added automatically)
- Webapp and Python scripts are updated in place

**What changes:**
- Python venv moves from `~/venv` to `~/mcapp` (uv-managed `.venv` inside)
- systemd service is updated to use `uv run mcapp`
- New system hardening (firewall, tmpfs) is applied

**Note:** The old `~/venv` is preserved (not deleted). You can remove it manually after verifying the migration worked:
```bash
rm -rf ~/venv
```

## SD Card Protection

The installer configures:
- tmpfs for `/var/log` and `/tmp` (RAM-based, no SD writes)
- Volatile journal storage for systemd
- Reduced logrotate retention (2 rotations)
- Disabled unused services (ModemManager, cloud-init, etc.)

## Firewall Rules

The following ports are opened:

| Port | Protocol | Service |
|------|----------|---------|
| 22 | TCP | SSH (rate limited) |
| 80 | TCP | HTTP (lighttpd webapp) |
| 2980 | TCP | WebSocket (McApp) |
| 2981 | TCP | SSE/REST (McApp API) |
| 1799 | UDP | MeshCom |
| 5353 | UDP | mDNS (.local) |

**IPv6 is disabled** to eliminate connection timeouts caused by the Happy Eyeballs algorithm when the IPv6 firewall is closed. This fixes mDNS advertisements and improves connection reliability.

## Debian Version Support

| Debian | Python | Firewall | Status |
|--------|--------|----------|--------|
| Trixie (13) | 3.13 | nftables | Primary target |
| Bookworm (12) | 3.11 | iptables | Supported |

The script auto-detects the Debian version and uses appropriate packages.

## Troubleshooting

### Service won't start

```bash
# Check logs
sudo journalctl -u mcapp -n 50

# Check config validity
jq '.' /etc/mcapp/config.json

# Check Python venv
~/mcapp/.venv/bin/python -c "import websockets; print('OK')"
```

### Cannot access web UI

```bash
# Check lighttpd
sudo systemctl status lighttpd

# Check firewall
sudo nft list ruleset    # Trixie
sudo iptables -L -n      # Bookworm

# Check port
ss -tlnp | grep ':80\b'
```

### BLE not working

```bash
# Check Bluetooth service
sudo systemctl status bluetooth

# Check rfkill
rfkill list bluetooth

# Check BLE adapter
bluetoothctl show
```

### View deployment history

The bootstrap script logs deployment events to the systemd journal:

```bash
# View all deployment events
sudo journalctl -u mcapp.service | grep BOOTSTRAP

# View recent deployments with version info
sudo journalctl -u mcapp.service --since "7 days ago" | grep BOOTSTRAP

# Check last deployment
sudo journalctl -u mcapp.service | grep BOOTSTRAP | tail -5
```

Example output:
```
Feb 15 09:30:14 mcapp mcapp[38295]: [BOOTSTRAP] Stopping service for maintenance and deployment
Feb 15 09:30:14 mcapp mcapp[38295]: [BOOTSTRAP] Current version: v1.01.0
Feb 15 09:30:18 mcapp mcapp[38295]: [BOOTSTRAP] Deployment complete - new version: v1.01.1
Feb 15 09:30:18 mcapp mcapp[38295]: [BOOTSTRAP] Upgraded from v1.01.0 to v1.01.1
```

See [DEPLOYMENT_LOGGING.md](DEPLOYMENT_LOGGING.md) for details.

## File Locations

| Path | Purpose |
|------|---------|
| `/etc/mcapp/config.json` | Configuration file |
| `~/mcapp/` | McApp package (pyproject.toml + source) |
| `~/mcapp/.venv/` | Python virtual environment (uv-managed) |
| `/var/www/html/webapp/` | Vue.js web application |
| `/etc/lighttpd/conf-available/99-mcapp.conf` | lighttpd SPA rewrite + redirect |

## Uninstallation

```bash
# Stop and disable service
sudo systemctl stop mcapp
sudo systemctl disable mcapp

# Remove files
sudo rm -rf /etc/mcapp
sudo rm -rf /var/www/html/webapp
sudo rm -rf ~/mcapp

# Remove systemd service
sudo rm /etc/systemd/system/mcapp.service
sudo systemctl daemon-reload
```

## License

See the main repository for license information.

## Support

- GitHub Issues: https://github.com/DK5EN/McAdvChat/issues
- MeshCom Community: https://meshcom.org
