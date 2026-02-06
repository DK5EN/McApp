# MCProxy Bootstrap

Unified installer and updater for MCProxy - the MeshCom message proxy for ham radio operators.

## Quick Start

Run this single command for fresh install, update, or repair:

```bash
curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/bootstrap/mcproxy.sh | sudo bash
```

The script auto-detects its context and does the right thing:
- **Fresh install**: Prompts for configuration, installs everything
- **Update**: Checks versions, updates if newer available
- **Incomplete**: Resumes configuration prompts
- **Migration**: Upgrades from old install scripts (see below)

## Requirements

- Raspberry Pi Zero 2W (or any Pi with ARM Cortex-A53)
- Debian Bookworm (12) or Trixie (13)
- 512MB RAM minimum
- SD card (8GB+ recommended)
- Network connectivity

## What Gets Installed

| Component | Purpose |
|-----------|---------|
| Caddy | HTTPS reverse proxy with auto-TLS |
| lighttpd | Static file server for Vue.js webapp |
| Python venv | Isolated Python environment |
| mcproxy.service | systemd service for the proxy |
| nftables | Firewall rules |

## Configuration

During first install, you'll be prompted for:

| Setting | Example | Description |
|---------|---------|-------------|
| Callsign | `DX9XX-99` | Your ham radio callsign with SSID |
| Node address | `mcapp.local` | MeshCom node hostname or IP |
| Latitude | `48.2082` | Your station latitude |
| Longitude | `16.3738` | Your station longitude |
| Station name | `Vienna` | Name for weather reports |

Configuration is stored in `/etc/mcadvchat/config.json`.

## Command-Line Options

```bash
# Fresh install or update (same command)
sudo ./mcproxy.sh

# Check what would be updated (dry-run)
sudo ./mcproxy.sh --check

# Force reinstall everything
sudo ./mcproxy.sh --force

# Repair broken installation
sudo ./mcproxy.sh --fix

# Re-prompt for configuration
sudo ./mcproxy.sh --reconfigure

# Minimal output (for cron)
sudo ./mcproxy.sh --quiet
```

## Service Management

```bash
# Check status
sudo systemctl status mcproxy

# View logs
sudo journalctl -u mcproxy -f

# Restart service
sudo systemctl restart mcproxy

# Stop service
sudo systemctl stop mcproxy
```

## Access Points

After installation:

| Service | URL |
|---------|-----|
| Web UI | `https://<hostname>.local/webapp` |
| Root Certificate | `https://<hostname>.local/root.crt` |
| WebSocket | `wss://<hostname>.local:2981` |

## First-Time Browser Setup

1. Navigate to `https://<hostname>.local/root.crt`
2. Download and install the root certificate
3. Trust the certificate for website identification
4. Navigate to `https://<hostname>.local/webapp`

## Migrating from Old Installation

If you previously installed MCProxy using the old scripts (`install_caddy.sh`, `mc-install.sh`, `install_mcproxy.sh`), the new bootstrap script will automatically detect and migrate your installation.

**What gets migrated:**
- Your existing `config.json` is preserved (new fields added automatically)
- Webapp and Python scripts are updated in place

**What changes:**
- Python venv moves from `~/venv` to `~/mcproxy-venv`
- systemd service is updated to use the new venv path
- New system hardening (firewall, tmpfs) is applied

**Migration steps:**
```bash
# Simply run the new bootstrap script
curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/bootstrap/mcproxy.sh | sudo bash
```

The script will:
1. Detect the old installation (`~/venv` exists)
2. Stop the mcproxy service
3. Create a new venv at `~/mcproxy-venv`
4. Update the systemd service
5. Restart with the new configuration

**Note:** The old `~/venv` is preserved (not deleted). You can remove it manually after verifying the migration worked:
```bash
rm -rf ~/venv
```

## Automatic Updates

Set up a cron job for automatic updates:

```bash
# /etc/cron.d/mcproxy-update
0 4 * * * root curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/bootstrap/mcproxy.sh | bash --quiet 2>&1 | logger -t mcproxy-update
```

## SD Card Protection

The installer configures:
- tmpfs for `/var/log` and `/tmp` (RAM-based)
- Volatile journal storage for systemd
- Minimized write operations

## Firewall Rules

The following ports are opened:

| Port | Protocol | Service |
|------|----------|---------|
| 22 | TCP | SSH (rate limited) |
| 443 | TCP | HTTPS (Caddy) |
| 2981 | TCP | WebSocket TLS |
| 1799 | UDP | MeshCom |
| 5353 | UDP | mDNS (.local) |

## Debian Version Support

| Debian | Python | Status |
|--------|--------|--------|
| Trixie (13) | 3.14 | Primary target |
| Bookworm (12) | 3.11 | Supported |

The script auto-detects the Debian version and uses appropriate packages.

## Troubleshooting

### Service won't start

```bash
# Check logs
sudo journalctl -u mcproxy -n 50

# Check config validity
jq '.' /etc/mcadvchat/config.json

# Check Python venv
source ~/mcproxy-venv/bin/activate
python -c "import websockets; print('OK')"
```

### Cannot access web UI

```bash
# Check services
sudo systemctl status caddy lighttpd

# Check firewall
sudo nft list ruleset

# Check ports
ss -tlnp | grep -E ':(443|80)\b'
```

### BLE not working

```bash
# Check Bluetooth service
sudo systemctl status bluetooth

# Check BLE adapter
bluetoothctl show
```

## File Locations

| Path | Purpose |
|------|---------|
| `/etc/mcadvchat/config.json` | Configuration file |
| `/usr/local/bin/C2-mc-ws.py` | Main Python script |
| `/var/www/html/webapp/` | Vue.js web application |
| `~/mcproxy-venv/` | Python virtual environment |
| `/etc/caddy/Caddyfile` | Caddy configuration |

## Uninstallation

```bash
# Stop and disable service
sudo systemctl stop mcproxy
sudo systemctl disable mcproxy

# Remove files
sudo rm -rf /etc/mcadvchat
sudo rm -rf /var/www/html/webapp
sudo rm -f /usr/local/bin/C2-mc-ws.py
sudo rm -f /usr/local/bin/mcproxy-version
sudo rm -rf ~/mcproxy-venv

# Remove systemd service
sudo rm /etc/systemd/system/mcproxy.service
sudo systemctl daemon-reload
```

## License

See the main repository for license information.

## Support

- GitHub Issues: https://github.com/DK5EN/McAdvChat/issues
- MeshCom Community: https://meshcom.org
