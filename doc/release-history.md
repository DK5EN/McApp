# Release History

## v1.6.10 (2026-05-13)

Adds end-to-end support for the firmware's BLE PIN (`bt_code`) feature. Devices with a PIN set can now be paired and connected from the webapp without disabling the PIN first.

### Backend (MCProxy)

- **[fix]** BlueZ pairing agent now returns the configured PIN as the NimBLE passkey. The firmware's `bt_code` is used both as the app-layer hello hash AND as the BLE pairing passkey; the previous agent always returned `0`, so any device with `bt_code != 0` rejected pairing with `Authentication Failed`.
- **[fix]** `MeshComPairingAgent` reads the current PIN via a getter at call time, so `PATCH /api/ble/pin` takes effect on the next pair attempt without re-registering the D-Bus agent. `RequestPinCode` returns the zero-padded 6-digit string for legacy PIN pairing; `DisplayPasskey` added for Secure Connections numeric display path.
- **[fix]** `BLEAdapter.pairing_passkey` is kept in sync with the persisted `_ble_pin` in both `lifespan` (on startup) and the `PATCH /api/ble/pin` handler.
- **[docs]** New `doc/ble-pin.md` — implementation reference for the firmware PIN protocol (Hello hash format, `BPIN` field, proxy state file layout, frontend integration notes).

### Frontend (webapp)

- **[feat]** PIN input field in the pair view (`BtDeviceSelected.vue`). Empty = open device (no PIN); a filled value must be a 6-digit number between `100000` and `999999`. The webapp `PATCH`es `/api/ble/pin` first so the proxy has the correct passkey and hello hash ready before the pair attempt.
- **[feat]** PIN retry in the error state (`BtErrorState.vue`). When a pair or connect failure looks like an auth failure, the error view offers a PIN input next to the Retry button. Empty input on retry preserves whatever PIN the proxy already has; only a typed value overwrites the stored PIN.
- **[feat]** `useBtConnectionState` orchestrates the PIN flow: `pairBT(pin)`, `connectBT(pin?)`, `retryConnect(pin?)`, and new `retryPair(pin?)` all PATCH `/api/ble/pin` before enqueueing the BLE command. `lastFailedAction` tracks which path (pair or connect) failed so the error-state Retry routes to the correct handler.

