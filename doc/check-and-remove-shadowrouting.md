# Shadow Mode: parse_command v1/v2 Validation

Shadow mode runs the old (`_parse_command_v1`) and new (`parse_command_v2`) parsers
side-by-side on every incoming command. The old result is used for execution; the new
result is compared and mismatches are logged. Nothing breaks if v2 disagrees.

## How to check for validation errors

### 1. Watch logs on the Pi

```bash
# Live tail — shadow mismatches are logged at WARNING level
sudo journalctl -u mcapp.service -f | grep -i "SHADOW"

# Search recent history (last 2 hours)
sudo journalctl -u mcapp.service --since "2 hours ago" | grep -i "SHADOW"
```

### 2. What mismatch lines look like

```
SHADOW parse_command KWARGS MISMATCH: msg='!ctcping call:dk5en' cmd='ctcping' v1={'call': 'dk5en'} v2={'call': 'DK5EN'}
SHADOW parse_command MISMATCH (None): msg='!foo bar' v1=None v2=('foo', {})
SHADOW parse_command CMD MISMATCH: msg='!wx test' v1_cmd='wx' v2_cmd='weather'
```

There are three log patterns (all from `mcapp.commands.shadow`):

| Pattern | Meaning |
|---|---|
| `MISMATCH (None)` | One parser returned `None`, the other returned a result |
| `CMD MISMATCH` | Both parsed a command but disagree on which command |
| `KWARGS MISMATCH` | Same command, different argument dicts |

### 3. Known expected mismatches

These two were identified during development and are both v1 bugs fixed in v2:

| Input | v1 | v2 | Reason |
|---|---|---|---|
| `!ctcping call:dk5en` | `call: "dk5en"` | `call: "DK5EN"` | v1 skips uppercasing in generic key:value path; v2 always uppercases call |
| `!topic 100 Hello interval:30` | `interval: "30"` (string) | `interval: 30` (int) | v1's generic key:value handler overwrites the parsed int with a string |

Both are harmless because downstream handlers already compensate:
- `ctcping.py:479` does `.upper()` on the call value
- `topic_beacon.py:53` uses `kwargs.get("interval", 30)` directly

### 4. How long to run shadow mode

Run for at least **48 hours of normal mesh traffic** on `rpizero.local`. Ideal:
one full week to cover weekend traffic patterns. The goal is zero unexpected
mismatches (the two known ones above don't count).

### 5. Checking from your dev machine

```bash
ssh rpizero.local 'sudo journalctl -u mcapp.service --since "24 hours ago"' | grep "SHADOW"
```

If the output is empty (or only shows the two known mismatches above), validation
passes.

---

## How to remove shadow mode

Once validated, remove the old v1 code and shadow plumbing. All changes are in three
files.

### Step 1: `src/mcapp/commands/routing.py`

**Replace the shadow-mode `parse_command` + the entire `_parse_command_v1`** with a
simple delegation to v2:

```python
# REMOVE these imports:
#   from .parsing import ..., parse_command_v2
#   from .shadow import compare_parse_command, ...

# CHANGE imports to:
from .parsing import extract_target_callsign, is_group, parse_command_v2
from .shadow import normalize_unified
```

```python
# REPLACE the two methods (parse_command + _parse_command_v1, lines 234-371)
# with this single method:

def parse_command(self, msg_text):
    """Dispatch-based command parser."""
    return parse_command_v2(msg_text)
```

After this, `_parse_command_v1` (lines 241-371) is deleted entirely. The `import re`
at the top of the file can also be removed since nothing else in routing.py uses it.

### Step 2: `src/mcapp/commands/shadow.py`

**Delete `compare_parse_command`** (the entire function, lines 12-42). Keep
`normalize_unified` — it is still used.

Also remove the logger and `get_logger` import if `compare_parse_command` was the only
user:

```python
# REMOVE:
from ..logging_setup import get_logger
logger = get_logger(__name__)
```

The `from __future__ import annotations` can stay or go (no remaining union types after
removing the function).

### Step 3: Verify

```bash
uvx ruff check src/mcapp/       # Must pass clean
MCAPP_ENV=dev uv run python -c "
import asyncio, mcapp.commands.constants as c
c.has_console = True
from mcapp.commands.handler import CommandHandler
async def t():
    h = CommandHandler(my_callsign='DK5EN')
    return await h.run_all_tests()
print('OK' if asyncio.run(t()) else 'FAIL')
"
```

### Summary of what gets deleted

| File | What | Lines removed |
|---|---|---|
| `routing.py` | `_parse_command_v1()` method | ~130 |
| `routing.py` | shadow call in `parse_command()` | ~4 |
| `routing.py` | `import re` | 1 |
| `routing.py` | `compare_parse_command` import | 1 |
| `shadow.py` | `compare_parse_command()` function | ~30 |
| `shadow.py` | logger setup | ~3 |

Total: ~170 lines of dead code removed. The new parser lives in `parsing.py` and is
already the production code path (just indirectly, via shadow wrapper).
