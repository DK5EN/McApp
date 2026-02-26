# Shadow Mode: Outbound Handler v1/v2 Validation

Shadow mode runs the unified `classify_outbound_v2()` classifier alongside the
existing `_udp_message_handler` and `_ble_message_handler`. The v1 handlers
remain authoritative; the v2 classifier runs as a pure function (no side
effects) and its decision is compared at each exit point.

## How to check for validation errors

### 1. Watch logs on the Pi

```bash
# Live tail — shadow mismatches are logged at WARNING level
sudo journalctl -u mcapp.service -f | grep -i "SHADOW outbound"

# Search recent history (last 24 hours)
sudo journalctl -u mcapp.service --since "24 hours ago" | grep -i "SHADOW outbound"
```

### 2. What mismatch lines look like

```
SHADOW outbound ACTION MISMATCH: proto=udp src=DK5EN dst=OE1ABC msg=!wx target:OE1ABC v1=send v2=suppress
SHADOW outbound REASON MISMATCH: proto=ble src=DK5EN dst=* v1_reason='Invalid destination (*)' v2_reason='Not a command'
```

There are two log patterns (all from `mcapp.commands.shadow`):

| Pattern | Meaning |
|---|---|
| `ACTION MISMATCH` | v1 handler took a different path (suppress/self_message/send) than v2 classified |
| `REASON MISMATCH` | Same action (suppress) but different suppression reason strings |

### 3. How long to run shadow mode

Run for at least **48 hours of normal mesh traffic** on `rpizero.local`. The
goal is zero mismatches. Both UDP outbound (WebSocket → mesh) and BLE outbound
(WebSocket → device) paths must see traffic.

### 4. Checking from your dev machine

```bash
ssh rpizero.local 'sudo journalctl -u mcapp.service --since "48 hours ago"' | grep "SHADOW outbound"
```

If the output is empty, validation passes.

---

## How to remove shadow mode

Once validated, replace both handlers with a unified `_outbound_message_handler`.
All changes are in three files.

### Step 1: `src/mcapp/main.py`

**Remove shadow imports:**

```python
# REMOVE:
from .commands.shadow import compare_outbound_decision, ...
from .outbound import classify_outbound_v2

# KEEP:
from .commands.shadow import normalize_unified
```

**Replace both handlers** with a unified handler:

```python
async def _outbound_message_handler(self, routed_message, protocol_type):
    """Unified outbound handler for both UDP and BLE."""
    message_data = routed_message['data']
    # ... unified normalize + suppress + self-message + send logic
    # ... dispatch to _protocol_send(normalized_data, protocol_type)

async def _protocol_send(self, normalized_data, protocol_type):
    """Dispatch to protocol-specific send."""
    # udp: udp_handler.send_message(normalized_data)
    # ble: client.send_message(msg, dst)

async def _udp_message_handler(self, routed_message):
    await self._outbound_message_handler(routed_message, "udp")

async def _ble_message_handler(self, routed_message):
    await self._outbound_message_handler(routed_message, "ble")
```

### Step 2: `src/mcapp/commands/shadow.py`

**Delete `compare_outbound_decision`** (the entire function). Keep
`compare_parse_command` if parse_command shadow is still active, and keep
`normalize_unified`.

### Step 3: `src/mcapp/outbound.py`

Either **inline** `classify_outbound_v2` into `_outbound_message_handler` (since
the unified handler IS the v2 logic), or **keep** it as a reusable classifier.

### Step 4: Verify

```bash
uvx ruff check src/mcapp/       # Must pass clean
MCAPP_ENV=dev uv run mcapp      # Startup tests must pass
```

### Summary of what changes

| File | What | Action |
|---|---|---|
| `main.py` | `_udp_message_handler` body | Replace with delegation |
| `main.py` | `_ble_message_handler` body | Replace with delegation |
| `main.py` | Shadow imports + comparison calls | Remove |
| `main.py` | New `_outbound_message_handler` + `_protocol_send` | Add |
| `shadow.py` | `compare_outbound_decision()` | Delete |
| `outbound.py` | `classify_outbound_v2` | Inline or keep |

Net result: two ~60-line handlers replaced by one ~40-line unified handler +
~20-line protocol dispatcher. ~40 lines of shadow scaffolding removed.
