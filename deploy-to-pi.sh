#!/usr/bin/env bash
set -euo pipefail

PI_HOST="mcapp.local"
PI_DIR="mcproxy-test"
REMOTE="$PI_HOST:~/$PI_DIR"

echo "=== Deploying MCProxy to $PI_HOST ==="

# 1. Create remote dirs
ssh "$PI_HOST" "mkdir -p ~/$PI_DIR/src/mcproxy/commands ~/$PI_DIR/ble_service/src"

# 2. Copy MCProxy package
scp src/mcproxy/*.py "$REMOTE/src/mcproxy/"
scp src/mcproxy/commands/*.py "$REMOTE/src/mcproxy/commands/"

# Clean up removed files from refactoring
ssh "$PI_HOST" "rm -f ~/$PI_DIR/src/mcproxy/command_handler.py"
scp pyproject.toml uv.lock "$REMOTE/"

# 3. Copy BLE service files
scp ble_service/src/__init__.py ble_service/src/main.py \
    ble_service/src/ble_adapter.py \
    "$REMOTE/ble_service/src/"
scp ble_service/pyproject.toml "$REMOTE/ble_service/"

# 4. Copy config (only if not exists)
if ssh "$PI_HOST" "test ! -f ~/$PI_DIR/config.json"; then
    scp config.sample.json "$REMOTE/config.json"
    echo ">>> Copied config.sample.json as config.json — EDIT IT on the Pi!"
else
    echo ">>> config.json already exists, skipping"
fi

# 5. Skip systemd service files — templates contain placeholders ({{USER}} etc.)
#    that the bootstrap installer renders. The deploy script only updates code.
#    To update service files, edit them on the Pi directly or use bootstrap/mcproxy.sh.

# 6. Install uv + sync deps + restart services
ssh "$PI_HOST" bash <<'SETUP'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd ~/mcproxy-test

# Install uv if missing
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Sync deps from pyproject.toml
uv sync
echo ">>> Dependencies installed"

# Symlink config for dev env
sudo mkdir -p /etc/mcadvchat
sudo ln -sf /home/martin/mcproxy-test/config.json /etc/mcadvchat/config.dev.json

# Restart services to pick up new code
sudo systemctl restart mcproxy-ble mcproxy
echo ">>> Services restarted"
SETUP

echo "=== Deploy complete ==="
echo ""
echo "Manage services:"
echo "  sudo systemctl status mcproxy-ble mcproxy"
echo "  sudo systemctl restart mcproxy-ble mcproxy"
echo "  sudo journalctl -fu mcproxy-ble"
echo "  sudo journalctl -fu mcproxy"
