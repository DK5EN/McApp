#!/usr/bin/env bash
set -euo pipefail

PI_HOST="mcapp.local"
PI_DIR="mcproxy-test"
REMOTE="$PI_HOST:~/$PI_DIR"

echo "=== Deploying MCProxy to $PI_HOST ==="

# 1. Create remote dirs
ssh "$PI_HOST" "mkdir -p ~/$PI_DIR/src/mcproxy ~/$PI_DIR/ble_service/src"

# 2. Copy MCProxy package
scp src/mcproxy/*.py "$REMOTE/src/mcproxy/"
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

# 5. Copy systemd service files
scp ble_service/mcproxy-ble.service mcproxy.service "$PI_HOST:/tmp/"

# 6. Install uv + deps + systemd service (idempotent)
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

# Install BLE service (only restart if changed)
if ! diff -q /tmp/mcproxy-ble.service /etc/systemd/system/mcproxy-ble.service &>/dev/null; then
    sudo cp /tmp/mcproxy-ble.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable mcproxy-ble
    sudo systemctl restart mcproxy-ble
    echo ">>> mcproxy-ble.service updated and restarted"
else
    echo ">>> mcproxy-ble.service unchanged, skipping restart"
    # Start if not running
    if ! systemctl is-active --quiet mcproxy-ble; then
        sudo systemctl start mcproxy-ble
        echo ">>> mcproxy-ble.service was stopped, started it"
    fi
fi

# Symlink config for dev env
sudo mkdir -p /etc/mcadvchat
sudo ln -sf /home/martin/mcproxy-test/config.json /etc/mcadvchat/config.dev.json

# Install MCProxy service (only restart if changed)
if ! diff -q /tmp/mcproxy.service /etc/systemd/system/mcproxy.service &>/dev/null; then
    sudo cp /tmp/mcproxy.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable mcproxy
    sudo systemctl restart mcproxy
    echo ">>> mcproxy.service updated and restarted"
else
    echo ">>> mcproxy.service unchanged, skipping restart"
    if ! systemctl is-active --quiet mcproxy; then
        sudo systemctl start mcproxy
        echo ">>> mcproxy.service was stopped, started it"
    fi
fi
SETUP

echo "=== Deploy complete ==="
echo ""
echo "Manage services:"
echo "  sudo systemctl status mcproxy-ble mcproxy"
echo "  sudo systemctl restart mcproxy-ble mcproxy"
echo "  sudo journalctl -fu mcproxy-ble"
echo "  sudo journalctl -fu mcproxy"
