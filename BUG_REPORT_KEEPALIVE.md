# Bug Report: Keepalive Using Invalid Command in Remote BLE Service

**Priority:** 🔴 CRITICAL
**Date Found:** 2026-02-14
**Status:** OPEN

---

## Summary

The remote BLE service (ble_service/src/ble_adapter.py) keepalive function sends an invalid `--pos info` command every 5 minutes. This command is not recognized by the MeshCom firmware and fails silently, resulting in the keepalive function providing no actual keep-alive functionality for remote BLE connections.

---

## Impact

- **Severity:** HIGH
- **Scope:** Remote BLE mode only (local mode not affected)
- **Effect:** Keepalive commands silently ignored by device
- **Result:** Connection health not validated; stale connections may not be detected

---

## Root Cause

During Phase 1 implementation, the `--pos info` command was fixed to `--pos` in main.py's register query loop. However, the same invalid command remained in the remote BLE service's keepalive function, creating an inconsistency between modes.

---

## Location

**File:** `/Users/martinwerner/WebDev/MCProxy/ble_service/src/ble_adapter.py`
**Line:** 687
**Method:** `_keepalive_loop()`

---

## Code

### Current (BROKEN)
```python
async def _keepalive_loop(self):
    """Send periodic keepalive commands"""
    try:
        while self.is_connected:
            await asyncio.sleep(300)  # 5 minutes
            if self.is_connected:
                logger.debug("Sending keepalive")
                await self.send_command("--pos info")  # 🔴 INVALID COMMAND
    except asyncio.CancelledError:
        pass
```

### Fixed
```python
async def _keepalive_loop(self):
    """Send periodic keepalive commands"""
    try:
        while self.is_connected:
            await asyncio.sleep(300)  # 5 minutes
            if self.is_connected:
                logger.debug("Sending keepalive")
                await self.send_command("--pos")  # ✅ FIXED: removed " info"
    except asyncio.CancelledError:
        pass
```

---

## Why `--pos info` Is Invalid

Per Phase 1 BLE Gap Analysis and MeshCom firmware specification:
- `--pos` is the valid command (returns TYP: G response with position data)
- `--pos info` is not a recognized firmware command
- The firmware silently ignores unrecognized A0 commands
- This was discovered during initial analysis and fixed in `src/mcapp/main.py:594`

---

## Testing

To verify the fix:
1. Run remote BLE service with fixed keepalive
2. Connect to a real device
3. Wait 5+ minutes
4. Check device logs: `--pos` command should execute (returns position data)
5. Verify no "command not recognized" errors in device output

---

## Related Documents

- **Phase 1 Analysis:** doc/ble-challenges.md (Issue #3)
- **Phase 1 Fix:** doc/phase1-fixes-summary.md
- **Verification Report:** VERIFICATION_REPORT_PHASE1_PHASE2.md (Section: Gap Analysis)

---

## Files to Update

1. `ble_service/src/ble_adapter.py` - Line 687

---

## Verification of Fix

After applying the fix, verify:
```bash
# Search for any remaining "--pos info" references
grep -r "--pos info" /Users/martinwerner/WebDev/MCProxy/

# Should NOT find any in executable code (only in docs/comments)
```

---

## Follow-up

After fixing this bug, consider:
1. Adding unit tests for keepalive command format
2. Syncing command validation between local and remote modes
3. Adding integration tests to catch similar inconsistencies
