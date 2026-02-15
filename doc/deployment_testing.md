# Deployment Testing: McApp on Fresh Raspberry Pi

> **Note:** The GitHub repository remains at `github.com/DK5EN/McApp` for
> compatibility reasons (existing links, bookmarks, bootstrap URLs on deployed devices).
> The project itself is called **McApp** — only the repo URL keeps the legacy name.

## Prerequisites

- Fresh Raspberry Pi with Debian Trixie (64-bit) or Bookworm (64-bit)
  - Trixie: Python 3.13, nftables firewall
  - Bookworm: Python 3.11, iptables firewall
- SSH access: `ssh mcapp.local`
- GitHub release: `McApp v1.01.0-dev.1` or later pre-release
- Package manager: `uv` (installed by bootstrap, never pip/venv)

## Step 0: Clear stale SSH host key (fresh SD card)

```bash
ssh-keygen -R mcapp.local
```

## Step 1: Pre-create config on Pi

This skips the interactive configuration prompts during bootstrap.

```bash
ssh mcapp.local "sudo mkdir -p /etc/mcapp"
ssh mcapp.local "sudo tee /etc/mcapp/config.json" <<'EOF'
{
  "UDP_TARGET": "192.168.68.69",
  "UDP_PORT_send": 1799,
  "UDP_PORT_list": 1799,
  "SSE_ENABLED": true,
  "SSE_HOST": "0.0.0.0",
  "SSE_PORT": 2981,
  "CALL_SIGN": "DK5EN-99",
  "USER_INFO_TEXT": "DK5EN-99 Node | Freising, Bavaria",
  "LAT": 48.4071,
  "LONG": 11.7389,
  "STAT_NAME": "DK5EN Freising McApp V 1.0",
  "HOSTNAME": "mcapp",
  "STORAGE_BACKEND": "sqlite",
  "DB_PATH": "/var/lib/mcapp/messages.db",
  "MAX_STORAGE_SIZE_MB": 100,
  "PRUNE_HOURS": 1350,
  "STORE_FILE_NAME": "mcdump.json",
  "WEATHER_SERVICE": "dwd",
  "BLE_MODE": "remote",
  "BLE_REMOTE_URL": "http://127.0.0.1:8081",
  "BLE_API_KEY": "test-dev-key",
  "BLE_DEVICE_NAME": "",
  "BLE_DEVICE_ADDRESS": "",
  "BLE_READ_UUID": "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
  "BLE_WRITE_UUID": "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
  "BLE_HELLO_BYTES": "04102030"
}
EOF
```

## Step 2: Copy bootstrap scripts to Pi

For rapid iteration during development, copy scripts via scp:

```bash
scp -r bootstrap mcapp.local:~
```

> **Note:** Do not add trailing slashes — modern OpenSSH (9.0+) uses SFTP by default,
> which fails with `realpath: No such file` when the remote target doesn't exist yet.

> **Future:** Once the bootstrap is stable on GitHub, install directly via curl:
>
> Dev: `curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash -s -- --dev`
>
> Prod: `curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash`

## Step 3: Run bootstrap

```bash
ssh mcapp.local "sudo ~/bootstrap/mcapp.sh --dev"
```

The bootstrap script will:
- Install system packages and `uv` package manager
- Generate locales (`en_US.UTF-8`, `de_DE.UTF-8`) to suppress SSH locale warnings
- Set up Python environment via `uv sync` (no venv/pip)
- Configure firewall (nftables on Trixie, iptables on Bookworm)
- Open required ports: SSH (22), HTTP (80), UDP (1799), mDNS (5353)
- Install and start systemd services: `mcapp` and `mcapp-ble`

For re-runs after partial install:

```bash
ssh mcapp.local "sudo ~/bootstrap/mcapp.sh --dev --force"
```

## Step 4: Verify deployment

Both `mcapp` (main proxy) and `mcapp-ble` (BLE backend) must be running.

```bash
ssh mcapp.local 'bash -c "
echo MCAPP: $(systemctl is-active mcapp)
echo MCAPP_BLE: $(systemctl is-active mcapp-ble)
echo LIGHTTPD: $(systemctl is-active lighttpd)
echo WEBAPP: $(curl -s -o /dev/null -w %{http_code} http://localhost/webapp/)
echo SSE: $(curl -s -o /dev/null -w %{http_code} http://localhost/health 2>/dev/null || echo CLOSED)
echo UDP: $(ss -ulnp 2>/dev/null | grep -c 1799)
echo CONFIG: $(test -f /etc/mcapp/config.json && echo OK || echo MISSING)
echo UV: $(command -v uv >/dev/null 2>&1 && echo OK || echo MISSING)
echo SQLITE: $(test -f /var/lib/mcapp/messages.db && echo OK || echo MISSING)
echo LOCALE: $(locale -a 2>/dev/null | grep -c de_DE.utf8)
"'
```

Expected output (all OK):

```
MCAPP: active
MCAPP_BLE: active
LIGHTTPD: active
WEBAPP: 200
SSE: 200
UDP: 1
CONFIG: OK
UV: OK
SQLITE: OK
LOCALE: 1
```

### Firewall verification

Confirm that the webapp and API are reachable from the network via lighttpd (port 80):

```bash
# From your Mac (not on the Pi)
curl -s -o /dev/null -w "%{http_code}" http://mcapp.local/webapp/
curl -s -o /dev/null -w "%{http_code}" http://mcapp.local/health
```

## Step 5: Check logs

```bash
# Main proxy
ssh mcapp.local "sudo journalctl -u mcapp --no-pager -n 30"

# BLE backend
ssh mcapp.local "sudo journalctl -u mcapp-ble --no-pager -n 30"
```

## Iterative fix cycle

When a step fails:

1. Read the error from ssh output or journal logs
2. Fix the script locally in `bootstrap/`
3. Copy to Pi: `scp -r bootstrap mcapp.local:~`
4. Re-run: `ssh mcapp.local "sudo ~/bootstrap/mcapp.sh --dev --force"`

For service-only fixes (no full bootstrap re-run needed):

```bash
# Copy updated service template
scp bootstrap/templates/mcapp.service mcapp.local:~/bootstrap/templates/mcapp.service

# Render template and restart
ssh mcapp.local "sudo bash -c '
  sed -e \"s|{{USER}}|martin|g\" -e \"s|{{HOME}}|/home/martin|g\" \
    /home/martin/bootstrap/templates/mcapp.service \
    > /etc/systemd/system/mcapp.service
  systemctl daemon-reload
  systemctl restart mcapp
'"
```

## Bugs found and fixed during v1.01.0-dev.1 deployment

### 1. Tarball name mismatch (`deploy.sh:115-116`)
- **Symptom:** Download failed — tarball name mismatch between release asset and script expectation
- **Fix:** Aligned tarball prefix to `mcapp-` in `download_and_install_release()`

### 2. Python version detection wrong for Trixie (`detect.sh:23`)
- **Symptom:** Bootstrap detected Python 3.14, but Trixie ships Python 3.13.5
- **Fix:** Changed `trixie|sid` case to return `"3.13"` instead of `"3.14"`

### 3. Missing `/var/log/mcapp` directory (`deploy.sh`)
- **Symptom:** `status=226/NAMESPACE` — systemd `ReadWritePaths` requires all paths to exist
- **Fix:** Added `mkdir -p /var/log/mcapp && chown` in `download_and_install_release()`

### 4. Missing `/var/lib/mcapp` in service ReadWritePaths (`mcapp.service`)
- **Symptom:** SQLite writes would fail (caught before it happened)
- **Fix:** Added `/var/lib/mcapp` to `ReadWritePaths` line in template

### 5. uv cache not writable (`mcapp.service`)
- **Symptom:** `status=2` — `Failed to initialize cache at /home/martin/.cache/uv` (read-only filesystem)
- **Fix:** Added `{{HOME}}/.cache/uv` to `ReadWritePaths`

### 6. SSH locale warning (`system.sh`)
- **Symptom:** `bash: warning: setlocale: LC_ALL: cannot change locale (de_DE.UTF-8)`
- **Cause:** Mac forwards `LC_ALL=de_DE.UTF-8` via SSH, but locale not generated on Pi
- **Fix:** Bootstrap now generates `en_US.UTF-8` and `de_DE.UTF-8` locales via `configure_locale()` in `system.sh`

### 7. SSE port not open in firewall (`nftables.conf`)
- **Symptom:** SSE endpoint unreachable from network clients
- **Fix:** Added port 2981/tcp to nftables and iptables firewall rules
- **Note:** Port 2981 is no longer exposed externally — SSE is now proxied through lighttpd on port 80 (`/events`, `/api/`, `/health`)

### 8. SCP fails with SFTP protocol (`deployment_testing.md`)
- **Symptom:** `scp: realpath bootstrap/: No such file` — upload directory fails
- **Cause:** OpenSSH 9.0+ defaults to SFTP protocol, which requires the remote target to exist when trailing slashes are used
- **Fix:** Changed `scp -r bootstrap/ mcapp.local:~/bootstrap/` to `scp -r bootstrap mcapp.local:~` (no trailing slashes)

### 9. `/tmp` tmpfs too small for `uv` download (`system.sh:146,181`)
- **Symptom:** `tar: uv: Wrote only 2048 of 10240 bytes` — uv 0.10.0 binary (~47MB) cannot extract
- **Cause:** Bootstrap configures `/tmp` as 50MB tmpfs for SD card protection, but the `uv` installer downloads and extracts in `/tmp`, exceeding the space limit
- **OS-specific:** Hits on fresh installs where tmpfs is created during the same bootstrap run that installs uv (found on Bookworm, applies to all)
- **Fix:** Increased `/tmp` tmpfs from `size=50M` to `size=150M` in both the fstab entry and the mount command
