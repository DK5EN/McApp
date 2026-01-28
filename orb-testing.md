# OrbStack Testing for MCProxy

This document describes how to use OrbStack on macOS (Apple Silicon) to develop and test MCProxy without needing physical Raspberry Pi hardware for every change.

## Overview

OrbStack provides fast, native ARM64 Linux VMs on Apple Silicon Macs. Since Raspberry Pi also uses ARM64, you get near-native performance for testing MCProxy code changes.

## Quick Start

```bash
# Create a Debian Trixie VM (matches Pi's target environment)
orb create debian:trixie mcproxy-dev

# Shell into the VM
orb shell mcproxy-dev

# Inside the VM, you can run MCProxy with BLE disabled
export MCPROXY_BLE_MODE=disabled
python C2-mc-ws.py
```

## OrbStack Setup

### Prerequisites

1. Install OrbStack from https://orbstack.dev or via Homebrew:
   ```bash
   brew install orbstack
   ```

2. OrbStack requires macOS 12.3+ and Apple Silicon (M1/M2/M3)

### Creating Development VMs

```bash
# Create Debian Trixie (recommended - matches Pi target)
orb create debian:trixie mcproxy-dev

# Alternative: Ubuntu 24.04
orb create ubuntu:24.04 mcproxy-ubuntu
```

### File Sharing

OrbStack automatically mounts your Mac filesystem:

```bash
# Inside VM, access Mac files at:
/mnt/mac/Users/username/WebDev/MCProxy

# Or use the symlink:
cd /mnt/user  # Points to your home directory
```

### Network Access

- VMs can reach localhost services on your Mac via `host.orb.internal`
- VMs get their own hostname: `<vm-name>.orb.local`
- SSH is automatically available: `ssh mcproxy-dev.orb.local`

## BLE Modes for Development

MCProxy supports three BLE modes configured via `BLE_MODE` in config:

| Mode | Use Case | Hardware Required |
|------|----------|-------------------|
| `local` | Production on Pi | Yes (BlueZ/D-Bus) |
| `remote` | Dev on Mac, BLE on Pi | Pi running BLE service |
| `disabled` | Testing non-BLE features | None |

### Mode: disabled

For testing WebSocket, UDP, and command handling without BLE:

```bash
# In config.json or environment
export MCPROXY_BLE_MODE=disabled
```

### Mode: remote

Connect to a BLE service running on a real Pi:

```bash
# On Pi: Start the BLE service
cd ble_service && uvicorn main:app --host 0.0.0.0 --port 8081

# On Mac (OrbStack or native):
export MCPROXY_BLE_MODE=remote
export MCPROXY_BLE_URL=http://pi.local:8081
export MCPROXY_BLE_API_KEY=your-secret-key
python C2-mc-ws.py
```

## What Works vs. Limitations

### Works in OrbStack

| Feature | Status | Notes |
|---------|--------|-------|
| WebSocket server | ✅ | Full functionality |
| UDP handler | ✅ | Can connect to real MeshCom node |
| Command processing | ✅ | All commands work |
| Message storage | ✅ | JSON and SQLite |
| SSE transport | ✅ | Full functionality |
| BLE (remote mode) | ✅ | Via BLE service on Pi |

### Limitations

| Feature | Status | Workaround |
|---------|--------|------------|
| BLE (local) | ❌ | No BlueZ/D-Bus in VM. Use remote mode. |
| GPIO | ❌ | Not available. Mock if needed. |
| Hardware timers | ⚠️ | May have different timing |

## Development Workflow

### 1. Code on Mac, Test in OrbStack

```bash
# Terminal 1: Edit code on Mac
cd ~/WebDev/MCProxy
vim C2-mc-ws.py

# Terminal 2: Run in OrbStack
orb shell mcproxy-dev
cd /mnt/user/WebDev/MCProxy
python C2-mc-ws.py
```

### 2. Snapshot and Restore

OrbStack supports snapshots for quick state management:

```bash
# Create snapshot before testing
orb snapshot create mcproxy-dev pre-test

# Run tests, make changes...

# Restore to clean state
orb snapshot restore mcproxy-dev pre-test

# List snapshots
orb snapshot list mcproxy-dev
```

### 3. Testing with Real MeshCom Node

If you have a MeshCom node on your network:

```bash
# Inside OrbStack VM
# Edit config to point to real node
vi /etc/mcadvchat/config.json
# Set UDP_TARGET to node IP (e.g., 192.168.68.100)

# Run MCProxy
python C2-mc-ws.py
```

### 4. Testing with Remote BLE Service

```bash
# On Raspberry Pi (with Bluetooth)
cd ~/MCProxy/ble_service
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8081

# In OrbStack or on Mac
export MCPROXY_BLE_MODE=remote
export MCPROXY_BLE_URL=http://raspberrypi.local:8081
python C2-mc-ws.py
```

## SD Card Image Testing

For testing the actual install script or creating SD card images:

### Create Clean Test Environment

```bash
# Create fresh VM
orb create debian:trixie mcproxy-install-test

# Shell in and run install script
orb shell mcproxy-install-test
curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash
```

### Extract to SD Card Image

For creating distributable images (advanced):

```bash
# Export VM filesystem
orb export mcproxy-install-test mcproxy-root.tar

# On a Linux system or another VM, create SD card image
# (requires root and dd access)
```

## Troubleshooting

### "Address already in use" errors

```bash
# Inside VM, check what's using the port
lsof -i :2980
# Kill the process or use a different port
```

### Can't reach services on Mac

```bash
# Use host.orb.internal instead of localhost
curl http://host.orb.internal:8080/api/status
```

### Slow file system performance

Mount the project directly in the VM instead of using Mac filesystem:

```bash
# Clone into VM's native filesystem
cd ~
git clone https://github.com/DK5EN/McAdvChat.git
cd McAdvChat
```

### BlueZ/D-Bus errors

These are expected in OrbStack - use `BLE_MODE=disabled` or `BLE_MODE=remote`.

## Comparison: OrbStack vs Real Pi

| Aspect | OrbStack VM | Real Raspberry Pi |
|--------|-------------|-------------------|
| Architecture | ARM64 (native on Apple Silicon) | ARM64 |
| Speed | Fast (native) | Slower (SD card, limited RAM) |
| BLE | No (use remote mode) | Yes |
| GPIO | No | Yes |
| File editing | Instant (shared filesystem) | SSH/SCP |
| Snapshots | Yes, instant | No (SD card backup slow) |
| Cost | One Mac | $50+ per Pi |

## Recommended Development Setup

1. **Primary development**: Mac + OrbStack with `BLE_MODE=disabled`
2. **BLE testing**: Mac + remote BLE service on Pi
3. **Final testing**: Real Pi with full setup
4. **CI/CD**: GitHub Actions with ARM64 runners (when needed)

This workflow gives you fast iteration cycles for most changes while still allowing real hardware testing when necessary.
