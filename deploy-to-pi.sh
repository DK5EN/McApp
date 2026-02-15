#!/usr/bin/env bash
set -euo pipefail

PI_HOST="mcapp.local"
PI_DIR="mcapp"
REMOTE="$PI_HOST:~/$PI_DIR"

echo "=== Deploying McApp to $PI_HOST ==="

# 1. Create remote dirs
ssh "$PI_HOST" "mkdir -p ~/$PI_DIR/src/mcapp/commands ~/$PI_DIR/ble_service/src"

# 2. Copy McApp package
scp src/mcapp/*.py "$REMOTE/src/mcapp/"
scp src/mcapp/commands/*.py "$REMOTE/src/mcapp/commands/"
scp pyproject.toml uv.lock "$REMOTE/"

# 3. Copy BLE service files
scp ble_service/src/__init__.py ble_service/src/main.py \
    ble_service/src/ble_adapter.py \
    "$REMOTE/ble_service/src/"
scp ble_service/pyproject.toml "$REMOTE/ble_service/"

# 4. Config lives in /etc/mcapp/config.json (managed by bootstrap).
#    This script only deploys code, not config.

# 5. Systemd service files contain rendered placeholders ({{USER}} etc.)
#    managed by the bootstrap installer. This script only updates code.
#    To update service files, use: scp bootstrap/ + re-run bootstrap.

# 6. Sync deps + restart service
ssh "$PI_HOST" bash <<'SETUP'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd ~/mcapp

# Sync deps from pyproject.toml (including workspace members)
uv sync --all-packages
echo ">>> Dependencies synced (including workspace members)"

# Restart services
sudo systemctl restart mcapp mcapp-ble
echo ">>> mcapp + mcapp-ble restarted"
SETUP

echo "=== Deploy complete ==="
echo ""
echo "Manage services:"
echo "  sudo systemctl status mcapp"
echo "  sudo journalctl -fu mcapp"
