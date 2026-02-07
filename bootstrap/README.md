# MCProxy Bootstrap

Unified installer and updater for MCProxy - the MeshCom message proxy for ham radio operators.

> **Installation instructions, CLI options, and service management** are documented in the main [README.md](../README.md). This file covers bootstrap internals and troubleshooting only.

## What Gets Installed

| Component | Purpose |
|-----------|---------|
| lighttpd | Static file server for Vue.js webapp |
| uv | Python package manager (creates venv via `uv sync`) |
| mcproxy.service | systemd service for the proxy |
| nftables/iptables | Firewall rules |

## Migrating from Old Installation

If you previously installed MCProxy using the old scripts (`install_caddy.sh`, `mc-install.sh`, `install_mcproxy.sh`), the bootstrap script will automatically detect and migrate your installation.

**What gets migrated:**
- Your existing `config.json` is preserved (new fields added automatically)
- Webapp and Python scripts are updated in place

**What changes:**
- Python venv moves from `~/venv` to `~/mcproxy` (uv-managed `.venv` inside)
- systemd service is updated to use `uv run mcproxy`
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
| 2980 | TCP | WebSocket (MCProxy) |
| 2981 | TCP | SSE/REST (MCProxy API) |
| 1799 | UDP | MeshCom |
| 5353 | UDP | mDNS (.local) |

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
sudo journalctl -u mcproxy -n 50

# Check config validity
jq '.' /etc/mcadvchat/config.json

# Check Python venv
~/mcproxy/.venv/bin/python -c "import websockets; print('OK')"
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

## File Locations

| Path | Purpose |
|------|---------|
| `/etc/mcadvchat/config.json` | Configuration file |
| `~/mcproxy/` | MCProxy package (pyproject.toml + source) |
| `~/mcproxy/.venv/` | Python virtual environment (uv-managed) |
| `/var/www/html/webapp/` | Vue.js web application |
| `/etc/lighttpd/conf-available/99-mcproxy.conf` | lighttpd SPA rewrite + redirect |

## Uninstallation

```bash
# Stop and disable service
sudo systemctl stop mcproxy
sudo systemctl disable mcproxy

# Remove files
sudo rm -rf /etc/mcadvchat
sudo rm -rf /var/www/html/webapp
sudo rm -rf ~/mcproxy

# Remove systemd service
sudo rm /etc/systemd/system/mcproxy.service
sudo systemctl daemon-reload
```

## License

See the main repository for license information.

## Support

- GitHub Issues: https://github.com/DK5EN/McAdvChat/issues
- MeshCom Community: https://meshcom.org
