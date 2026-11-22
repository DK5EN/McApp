# Firewall configuration (reference)

| Field | Value |
|---|---|
| ID | REF-FW |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 651-727; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp`, `debian-13-trixie`, `debian-12-bookworm` |
| Applies to (variants) | see POL-004 §1.3 |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Firewall Configuration"); `bootstrap/lib/system.sh`; `bootstrap/templates/nftables.conf` (unused, see backlog B21) |

## 1. Purpose and scope

Reference for the ports McApp opens and the traffic it drops. Procedures are in RB-005.

McApp uses host-based firewall to protect the Raspberry Pi. The bootstrap script automatically configures:

- **nftables** on Debian Trixie (and newer)
- **iptables** on Debian Bookworm (legacy fallback)

### Allowed Ports

| Port  | Protocol | Service        | Access                                    |
| ----- | -------- | -------------- | ----------------------------------------- |
| 22    | TCP      | SSH            | Rate limited (6/min external, LAN exempt) |
| 80    | TCP      | Caddy          | All traffic (redirect to HTTPS)           |
| 443   | TCP, UDP | Caddy          | All traffic (webapp and API)              |
| 1799  | UDP      | MeshCom        | All traffic (LoRa mesh communication)     |
| 2985  | TCP      | Update Runner  | All traffic (OTA update SSE stream)       |
| 5353  | UDP      | mDNS           | Multicast only (224.0.0.251)              |
| 19532 | TCP      | Journal upload | LAN only, nftables path only              |

**LAN exemption for SSH:** Connections from RFC 1918 private IP ranges (192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12) are NOT rate limited.

### Internal-Only Ports (Not Exposed)

| Port | Protocol | Service          | Why Not Exposed                 |
| ---- | -------- | ---------------- | ------------------------------- |
| 2981 | TCP      | McApp API        | Loopback, proxied by Caddy      |
| 8081 | TCP      | BLE service      | Loopback                        |
| 8082 | TCP      | lighttpd backend | Legacy, blocked by the firewall |

**Note:** Older firewall configurations (before Feb 2026) exposed ports 2980 and 2981 directly. These are now internal-only. The bootstrap script automatically removes them during upgrade.

### Silent Drops (Not Logged)

To prevent log spam, both nftables and iptables silently drop common broadcast/multicast traffic:

- Layer 2 broadcast packets
- Multicast packets (224.0.0.0/4, 239.0.0.0/8)
- Global broadcast (255.255.255.255)
- SSDP/UPnP (port 1900)
- NetBIOS (ports 137, 138)
- LLMNR (port 5355)
- High UDP ports (> 30000)
- IGMP protocol

All other unmatched traffic is **dropped** silently, so a blocked port looks like a timeout in the browser, not a refused connection.

### Firewall Logs

**Log format:**

- nftables: `[nftables DROP] ` prefix (logged before the drop)
- iptables: `[iptables DROP] ` prefix (logged before the drop)

**Rate limiting:** 10 drops per minute are logged (prevents log spam while maintaining visibility)

**View dropped traffic:**

```bash
# Watch firewall drops in real-time
sudo journalctl -kf | grep DROP

# Count drops by source IP (last hour)
sudo journalctl -k --since "1 hour ago" | grep DROP | awk '{print $NF}' | sort | uniq -c | sort -rn | head -20

# Count drops by destination port (last hour)
sudo journalctl -k --since "1 hour ago" | grep DROP | grep -oP 'DPT=\K\d+' | sort | uniq -c | sort -rn | head -20

# Show all firewall logs from today
sudo journalctl -k --since today | grep DROP
```

### Customizing the Firewall

See [Running other software on the box](#running-other-software-on-the-box) for how to open a port so that updates keep it.

**To customize LAN exemption ranges** (e.g., for non-standard subnets), edit the IP ranges in both configurations and apply changes.

