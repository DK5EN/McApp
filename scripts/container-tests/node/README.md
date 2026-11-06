# node.py

Manage a MeshCom node over its USB serial console for the container test
campaign (`doc/2026-10-31_1200-container-test-campaign.md`).

    uv run scripts/container-tests/node/node.py [--port P] [--dry-run] <cmd>

Commands: `info [--wait S]`, `extudp-set <ip>`, `extudp-on`, `extudp-off`,
`extudp-clear`, `send <dest> <text>`, `raw <command>`,
`monitor --seconds N [--grep RE]`, `reboot`.

- Default port `/dev/cu.usbserial-0001`, 115200 baud, CRLF lines.
- Opening the port resets the ESP32; the tool waits 8 s for WiFi.
- Setting commands re-read `--info` afterwards and exit 1 on mismatch.
- `send` writes `:{DEST} text` (DM or group number); `reboot` writes `--reboot`.
- `--dry-run` prints the bytes and opens nothing.

Tests (no hardware): `uv run scripts/container-tests/node/test_node.py`
