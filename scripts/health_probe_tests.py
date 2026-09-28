"""Every module the bootstrap health check imports must exist in the venv.

`check_venv` in `bootstrap/lib/health.sh` probes the deployed venv with
`python -c "import a, b"`, and a failed probe fails `health_check`, which makes
`mcapp.sh` exit 1 after every deploy and every `--converge`. Dropping
`uvicorn[standard]` (backlog B4.2a) removed `websockets` while the probe still
imported it — a deploy that works but reports itself as failed. This suite
reads the probe out of `health.sh` and resolves each module in the current
project env, which `uv sync --all-packages` builds from the same lock the Pi
installs.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_HEALTH_SH = _REPO / "bootstrap" / "lib" / "health.sh"
_PROBE_RE = re.compile(r'python"? -c "import ([A-Za-z0-9_., ]+)"')


def run_health_probe_tests() -> bool:
    """Runs the suite. Returns True when every check passed."""
    print("\n🧪 Testing bootstrap health probe imports (health.sh check_venv):")
    results: list[tuple[str, bool]] = []

    if not _HEALTH_SH.is_file():
        results.append((f"bootstrap/lib/health.sh exists at {_HEALTH_SH}", False))
    else:
        probes = _PROBE_RE.findall(_HEALTH_SH.read_text(encoding="utf-8"))
        label = f"health.sh carries at least one import probe (found {len(probes)})"
        results.append((label, bool(probes)))
        for probe in probes:
            for module in (m.strip() for m in probe.split(",")):
                found = importlib.util.find_spec(module) is not None
                results.append((f"health.sh probe module {module!r} is installed", found))

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    health_probe: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


if __name__ == "__main__":
    raise SystemExit(0 if run_health_probe_tests() else 1)
