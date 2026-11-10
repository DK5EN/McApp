# RB-002 BLE connection troubleshooting

| Field | Value |
|---|---|
| ID | RB-002 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 279-326; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` (BLE service and the Bluetooth stack) |
| Applies to (variants) | `debian-13-trixie` |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("BLE Connection Troubleshooting") |

## 1. Purpose and scope

Use when the webapp shows `Connect failed`, or the node is not found. Run the steps top to bottom and stop at the first that explains the symptom.

Diagnostic flow for "can't connect to the node" reports (webapp shows `Connect failed: ...` in the
Bluetooth Connection dialog). Run top to bottom; stop as soon as a step explains the symptom.

```bash
# 1. Service health — mcapp, mcapp-ble, and the system bluetooth daemon must all be active
ssh mcapp.local "systemctl status mcapp mcapp-ble bluetooth --no-pager -l | grep -E 'Active|●'"

# 2. Adapter state — controller UP, powered on (rfkill is not installed on the Pi image)
ssh mcapp.local "hciconfig -a; bluetoothctl show | grep -E 'Powered|Discoverable|Pairable'"

# 3. Known/paired devices and live scan for the node (MC-xxxx prefix)
ssh mcapp.local "bluetoothctl devices"
K=$(ssh mcapp.local "sudo grep BLE_API_KEY /etc/mcapp/config.json | cut -d'\"' -f4")
ssh mcapp.local "curl -s -H 'X-Api-Key: $K' 'http://127.0.0.1:8081/api/ble/devices?timeout=5.0&prefix=MC-'"

# 4. Recent BLE service log — look for the specific dbus_error string
ssh mcapp.local "sudo journalctl -u mcapp-ble --no-pager -n 60"

# 5. If needed, force a fresh connect attempt directly against the service (bypasses the webapp)
ssh mcapp.local "curl -s -X POST -H 'X-Api-Key: $K' -H 'Content-Type: application/json' \
  -d '{\"device_address\":\"AC:XX:XX:XX:XX:XX\"}' http://127.0.0.1:8081/api/ble/ensure_connected"
```

Note: the header the service reads is `x_api_key` → sent/received as `X-Api-Key` (HTTP headers are
case-insensitive; `operations-reference-pre-split-2026-11-10.md`'s config table above spells it `X-API-Key` — same
header). `ensure_connected` requires a JSON body `{"device_address": "AA:BB:..."}`, not a bare
`address` field.

**Known error signatures** (from `ble_service.src.ble_adapter` log lines):

- `dbus_error=org.bluez.Error.Failed text='le-connection-abort-by-local'` — the Pi's own
  controller aborted while waiting for the connection-complete event; the node was discoverable
  (check RSSI in step 3 — a strong value like -30 to -50 dBm rules out range). Restarting
  `bluetooth.service` and `mcapp-ble.service` does **not** fix this pattern if it persists across
  the restart — that points to the node side, most likely another BLE central already holding the
  connection (e.g. the MeshCom phone app open at the same time; most nodes accept only one central).
  Ask the user to close any other BLE client and retry, or power-cycle the node.
- `dbus_error=InterfaceNotFoundError text='interface not found on this object: org.bluez.Device1'`
  — BlueZ has no cached `Device1` object for that address (typical right after an unpair, or right
  after `bluetooth.service`/`mcapp-ble.service` restarts). A scan (step 3, `GET /api/ble/devices`)
  recreates the object; `ensure_connected` alone will not, because it does not scan first.

Escalation: if the node is not found by the scan at all (0 discovered) and RSSI would otherwise be
strong, the node itself is powered off, out of BLE range, or its firmware BLE stack is wedged —
that requires physical access, which is outside what's reachable over SSH to the Pi.

