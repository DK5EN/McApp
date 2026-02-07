# Deployment Testing: McAdvChat on Fresh Raspberry Pi Trixie

## Prerequisites

- Fresh Raspberry Pi with Debian Trixie (64-bit)
- SSH access: `ssh mcapp.local`
- GitHub release: `McAdvChat v1.01.0-dev.1` or later pre-release

## Step 0: Clear stale SSH host key (fresh SD card)

```bash
ssh-keygen -R mcapp.local
```

## Step 1: Pre-create config on Pi

This skips the interactive configuration prompts during bootstrap.

```bash
ssh mcapp.local "sudo mkdir -p /etc/mcadvchat"
ssh mcapp.local "sudo tee /etc/mcadvchat/config.json" <<'EOF'
{
  "UDP_TARGET": "192.168.68.69",
  "UDP_PORT_send": 1799,
  "UDP_PORT_list": 1799,
  "WS_HOST": "127.0.0.1",
  "WS_PORT": 2980,
  "SSE_ENABLED": true,
  "SSE_HOST": "0.0.0.0",
  "SSE_PORT": 2981,
  "CALL_SIGN": "DK5EN-99",
  "LAT": 48.4071,
  "LONG": 11.7389,
  "STAT_NAME": "DK5EN MCProxy",
  "HOSTNAME": "mcapp",
  "STORAGE_BACKEND": "sqlite",
  "DB_PATH": "/var/lib/mcproxy/messages.db",
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

```bash
scp -r bootstrap/ mcapp.local:~/bootstrap/
```

## Step 3: Run bootstrap

```bash
ssh mcapp.local "sudo ~/bootstrap/mcproxy.sh --dev"
```

For re-runs after partial install:

```bash
ssh mcapp.local "sudo ~/bootstrap/mcproxy.sh --dev --force"
```

## Step 4: Verify deployment

```bash
ssh mcapp.local 'bash -c "
echo MCPROXY: $(systemctl is-active mcproxy)
echo LIGHTTPD: $(systemctl is-active lighttpd)
echo WEBAPP: $(curl -s -o /dev/null -w %{http_code} http://localhost/webapp/)
echo UDP: $(ss -ulnp 2>/dev/null | grep -c 1799)
echo CONFIG: $(test -f /etc/mcadvchat/config.json && echo OK || echo MISSING)
echo VENV: $(test -f /home/martin/mcproxy/.venv/bin/python && echo OK || echo MISSING)
echo SQLITE: $(test -f /var/lib/mcproxy/messages.db && echo OK || echo MISSING)
"'
```

Expected output (all OK):

```
MCPROXY: active
LIGHTTPD: active
WEBAPP: 200
UDP: 1
CONFIG: OK
VENV: OK
SQLITE: OK
```

## Step 5: Check logs

```bash
ssh mcapp.local "sudo journalctl -u mcproxy --no-pager -n 30"
```

## Iterative fix cycle

When a step fails:

1. Read the error from ssh output or journal logs
2. Fix the script locally in `bootstrap/`
3. Copy to Pi: `scp -r bootstrap/ mcapp.local:~/bootstrap/`
4. Re-run: `ssh mcapp.local "sudo ~/bootstrap/mcproxy.sh --dev --force"`

For service-only fixes (no full bootstrap re-run needed):

```bash
# Copy updated service template
scp bootstrap/templates/mcproxy.service mcapp.local:~/bootstrap/templates/

# Render template and restart
ssh mcapp.local "sudo bash -c '
  sed -e \"s|{{USER}}|martin|g\" -e \"s|{{HOME}}|/home/martin|g\" \
    /home/martin/bootstrap/templates/mcproxy.service \
    > /etc/systemd/system/mcproxy.service
  systemctl daemon-reload
  systemctl restart mcproxy
'"
```

## Bugs found and fixed during v1.01.0-dev.1 deployment

### 1. Tarball name mismatch (`deploy.sh:115-116`)
- **Symptom:** Download failed — GitHub release has `mcadvchat-*.tar.gz`, script looked for `mcproxy-*.tar.gz`
- **Fix:** Changed tarball prefix from `mcproxy-` to `mcadvchat-` in `download_and_install_release()`

### 2. Python version detection wrong for Trixie (`detect.sh:23`)
- **Symptom:** Bootstrap detected Python 3.14, but Trixie ships Python 3.13.5
- **Fix:** Changed `trixie|sid` case to return `"3.13"` instead of `"3.14"`

### 3. Missing `/var/log/mcproxy` directory (`deploy.sh`)
- **Symptom:** `status=226/NAMESPACE` — systemd `ReadWritePaths` requires all paths to exist
- **Fix:** Added `mkdir -p /var/log/mcproxy && chown` in `download_and_install_release()`

### 4. Missing `/var/lib/mcproxy` in service ReadWritePaths (`mcproxy.service`)
- **Symptom:** SQLite writes would fail (caught before it happened)
- **Fix:** Added `/var/lib/mcproxy` to `ReadWritePaths` line in template

### 5. uv cache not writable (`mcproxy.service`)
- **Symptom:** `status=2` — `Failed to initialize cache at /home/martin/.cache/uv` (read-only filesystem)
- **Fix:** Added `{{HOME}}/.cache/uv` to `ReadWritePaths`

## Locale warning (cosmetic, not blocking)

```
bash: warning: setlocale: LC_ALL: cannot change locale (de_DE.UTF-8): No such file or directory
```

This happens because the Mac's `LC_ALL=de_DE.UTF-8` is forwarded via SSH but not installed on the Pi.
Fix: `ssh mcapp.local "sudo dpkg-reconfigure locales"` (select de_DE.UTF-8), or ignore.
