#!/usr/bin/env bash
# Run the BLE service on macOS (CoreBluetooth via bleak). Dev/test host only.
# Usage (from anywhere): BLE_SERVICE_API_KEY=... ble_service/scripts/run-macos.sh
#   BLE_HOST  bind address (default 127.0.0.1)
#   BLE_PORT  port (default 8091; 8081 is often taken by another stack)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
host="${BLE_HOST:-127.0.0.1}"
port="${BLE_PORT:-8091}"

case "$host" in
127.0.0.1 | localhost | ::1) ;;
*)
  key="${BLE_SERVICE_API_KEY:-}"
  if [ -z "$key" ] || [ "$key" = "disabled" ] || [ "${#key}" -lt 16 ]; then
    echo "Refusing to bind $host: set BLE_SERVICE_API_KEY (>= 16 chars, not 'disabled')." >&2
    exit 1
  fi
  ;;
esac

cat >&2 <<MSG
BLE service (bleak/CoreBluetooth) on $host:$port
Bluetooth permission: macOS grants it to the terminal app (Terminal, iTerm, ...),
not to python/uv. If the prompt was dismissed, quit the terminal with Cmd-Q and relaunch.
MSG

cd "$root"
export BLE_BACKEND=bleak
exec uv run --project ble_service uvicorn src.main:app --app-dir ble_service \
  --loop asyncio --host "$host" --port "$port" --ws none
