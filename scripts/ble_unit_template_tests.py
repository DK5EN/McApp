#!/usr/bin/env python3
"""Pin the mcapp-ble.service template's pre-start steps.

A hung BlueZ stack made `rfkill unblock bluetooth` block mcapp-ble's start-up
indefinitely on an Orange Pi Zero 2W (2026-10-19): the unit sat in
"activating", the update runner's BLE health check failed, and the slot
activation reported a failed health check. Both privileged pre-start steps
must therefore be bounded by `timeout` and non-fatal (the `-` prefix).
"""

import re
import sys
from pathlib import Path

_TEMPLATE = Path(__file__).resolve().parent.parent / "bootstrap" / "templates" / "mcapp-ble.service"
_TIMEOUT_RE = re.compile(r"/usr/bin/timeout \d+ ")


def check_template(text: str) -> list[str]:
    """Return the failures for one template text (empty list = pass)."""
    failures: list[str] = []
    pre = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("ExecStartPre=")]
    for tool in ("/usr/sbin/rfkill", "/usr/bin/bluetoothctl"):
        lines = [cmd for cmd in pre if tool in cmd]
        if len(lines) != 1:
            failures.append(f"expected exactly one ExecStartPre running {tool}, got {len(lines)}")
            continue
        cmd = lines[0]
        prefix = cmd[: len(cmd) - len(cmd.lstrip("-+@!:"))]
        if "-" not in prefix:
            failures.append(f"{tool} pre-start is fatal (no '-' prefix): {cmd}")
        if "+" not in prefix:
            failures.append(f"{tool} pre-start lost its '+' (root) prefix: {cmd}")
        if not _TIMEOUT_RE.search(cmd):
            failures.append(f"{tool} pre-start is not bounded by /usr/bin/timeout: {cmd}")
    return failures


def run_ble_unit_template_tests() -> bool:
    failures = check_template(_TEMPLATE.read_text(encoding="utf-8"))
    for failure in failures:
        print(f"    FAIL | {failure}")
    if not failures:
        print("    PASS | rfkill/bluetoothctl pre-start steps are bounded and non-fatal")
    return not failures


if __name__ == "__main__":
    sys.exit(0 if run_ble_unit_template_tests() else 1)
