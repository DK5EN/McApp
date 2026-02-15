# TLS Maintenance — Standard Operating Procedures

## SOP 1: Certificate Troubleshooting

**Symptom:** Browser shows "certificate expired" or "insecure connection"

```bash
# 1. Check Caddy status
sudo systemctl status caddy

# 2. Check Caddy logs for cert errors
sudo journalctl -u caddy --since "1 hour ago" | grep -i -E 'cert|tls|acme'

# 3. Force cert renewal
sudo caddy reload --config /etc/caddy/Caddyfile

# 4. Verify cert from the command line
openssl s_client -connect localhost:443 -servername <hostname> </dev/null 2>/dev/null | openssl x509 -noout -dates

# 5. Check DNS-01 challenge TXT record
dig TXT _acme-challenge.<hostname>

# 6. Check if DNS provider API token is still valid
cat /etc/mcapp/caddy.env  # verify token is present
```

## SOP 2: DDNS Not Updating

**Symptom:** Hostname resolves to old IP after ISP reconnect

```bash
# 1. Check current public IP
curl -s https://api.ipify.org

# 2. Check DNS resolution
dig +short <hostname>

# 3. Check Caddy dynamic_dns logs
sudo journalctl -u caddy | grep -i dynamic_dns

# 4. Force DDNS update by reloading Caddy
sudo caddy reload --config /etc/caddy/Caddyfile

# 5. Check provider dashboard
#    DuckDNS:    https://www.duckdns.org/
#    Cloudflare: https://dash.cloudflare.com/
#    deSEC:      https://desec.io/
```

## SOP 3: Caddy Service Recovery

**Symptom:** Caddy not running or crashing

```bash
# 1. Check status
sudo systemctl status caddy

# 2. Check for port conflicts
sudo ss -tlnp | grep -E ':(80|443)'

# 3. Validate config
caddy validate --config /etc/caddy/Caddyfile

# 4. Check memory (Caddy needs ~40MB)
free -m

# 5. Restart service
sudo systemctl restart caddy

# 6. If persistent crashes, check logs
sudo journalctl -u caddy --no-pager -n 50

# 7. Check if environment file exists and is readable
sudo ls -la /etc/mcapp/caddy.env
```

## SOP 4: Changing DNS Provider

```bash
# Re-run the setup script (it detects existing installations)
sudo ./scripts/ssl-tunnel-setup.sh

# The script will:
# 1. Detect existing setup
# 2. Ask to reconfigure
# 3. Download correct Caddy binary with new DNS module
# 4. Render new Caddyfile
# 5. Update secrets in /etc/mcapp/caddy.env
# 6. Restart Caddy
# Old certs are cleaned up automatically by Caddy
```

## SOP 5: Removing TLS (Reverting to Plain HTTP)

```bash
# Use the built-in remove command
sudo ./scripts/ssl-tunnel-setup.sh --remove

# This will:
# 1. Stop and disable Caddy/cloudflared
# 2. Remove systemd service files
# 3. Close port 443 in firewall
# 4. Remove TLS settings from /etc/mcapp/config.json
# lighttpd continues serving on port 80

# Optional: remove binaries and config
sudo rm -f /usr/local/bin/caddy /usr/local/bin/cloudflared
sudo rm -rf /etc/caddy /var/lib/caddy /etc/cloudflared
```

## SOP 6: Checking TLS Status

```bash
# Quick status overview
sudo ./scripts/ssl-tunnel-setup.sh --status

# Shows:
# - Caddy binary version
# - Service status
# - TLS configuration
# - Certificate validity dates
```
