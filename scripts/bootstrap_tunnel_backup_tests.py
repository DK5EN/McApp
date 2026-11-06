"""Startup regression suite: ssl-tunnel-setup.sh backs up what it overwrites.

The script is standalone and cannot be run for real (root, network, systemd),
so this suite does two things instead:

1. Extracts the backup loader block (source lib/detect.sh next to the script,
   else a local ``backup_config_file`` fallback) and drives it in bash against
   a tempdir: copy made 0600 in a 0700 folder, identical copy skipped,
   retention of 5, fallback used when lib/detect.sh is absent, and a failing
   backup (symlinked folder) never aborts under ``set -e``.
2. A static check that every overwrite site (Caddyfile, caddy.env, caddy.service,
   nftables.conf) is preceded, shortly and IN ORDER, by a ``backup_config_file``
   call for that file: the call must come before the first write line.

Set BOOTSTRAP_TUNNEL_SCRIPT to point the suite at another copy of the script
(used to prove the tests fail against the pre-change script).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = Path(
    os.environ.get("BOOTSTRAP_TUNNEL_SCRIPT", str(_REPO / "bootstrap" / "ssl-tunnel-setup.sh"))
)
_BASH = shutil.which("bash")

# (regex of the first write line, backup target). The backup must precede it.
_SITES = (
    (r'cp "\$tmpl" "\$CADDY_CONFIG"', '"$CADDY_CONFIG"'),
    (r'cat >>? "\$env_file"', '"$env_file"'),
    (r'cp "\$\{TEMPLATE_DIR\}/caddy/caddy\.service" "\$CADDY_SERVICE"', '"$CADDY_SERVICE"'),
    (r"sed -i .*\$nft_conf", '"$nft_conf"'),
)
# Lines allowed between the backup call and the write (a case header, a blank).
_MAX_GAP = 6

_failures: list[str] = []


def _check(ok: bool, label: str) -> bool:
    print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")
    if not ok:
        _failures.append(label)
    return ok


def _loader_block(text: str) -> str | None:
    """The backup loader: from the detect.sh sourcing loop to the fallback's closing fi."""
    start = text.find("for _detect in")
    end_marker = text.find("# END local backup_config_file fallback")
    if start < 0 or end_marker < 0:
        return None
    fi = text.find("\nfi\n", end_marker)
    return text[start : fi + 4] if fi >= 0 else None


def _run(driver: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, "-c", driver],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _copies(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_file()) if root.is_dir() else []


def _test_static() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    for write_re, target in _SITES:
        write = next((i for i, ln in enumerate(lines) if re.search(write_re, ln)), -1)
        if write < 0:
            _check(False, f"write site present: {write_re}")
            continue
        backup = max(
            (
                i
                for i, ln in enumerate(lines[:write])
                if re.search(r"backup_config_file\s+" + re.escape(target), ln)
            ),
            default=-1,
        )
        _check(
            backup >= 0 and write - backup <= _MAX_GAP,
            f"backup_config_file {target} comes before the write, within {_MAX_GAP} lines "
            f"(backup line {backup + 1}, write line {write + 1})",
        )
    _check(_loader_block(text) is not None, "backup loader block is extractable")


def _driver(block: str, script_dir: Path, body: str) -> str:
    return (
        "set -euo pipefail\n"
        f'SCRIPT_DIR="{script_dir}"\n'
        'log_warn() { echo "LOGWARN $*" >&2; }\n'
        f"{block}\n"
        f"{body}\n"
    )


def _test_behaviour() -> None:
    if not _check(_BASH is not None, "bash is available"):
        return
    block = _loader_block(_SCRIPT.read_text(encoding="utf-8"))
    if block is None:
        _check(False, "backup loader block present (behaviour tests skipped)")
        return
    with tempfile.TemporaryDirectory() as raw:
        t = Path(raw)
        nolib = t / "nolib"
        nolib.mkdir()
        root = t / "backups"
        src = t / "caddy.env"
        src.write_text("DUCKDNS_TOKEN=secret\n", encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "MCAPP_CONFIG_BACKUP_DIR": str(root)}

        # Fallback (no lib/detect.sh next to the script).
        res = _run(_driver(block, nolib, f'backup_config_file "{src}"\necho done'), env)
        copies = _copies(root)
        _check(res.returncode == 0 and "done" in res.stdout, f"fallback runs ({res.stderr[-200:]})")
        _check(len(copies) == 1, "fallback: one copy made")
        _check(
            len(copies) == 1 and copies[0].read_text() == src.read_text(),
            "fallback: copy holds the file content",
        )
        _check(len(copies) == 1 and _mode(copies[0]) == 0o600, "fallback: copy is mode 0600")
        _check(root.is_dir() and _mode(root) == 0o700, "fallback: folder is mode 0700")

        # Identical to newest: skipped.
        _run(_driver(block, nolib, f'backup_config_file "{src}"'), env)
        _check(len(_copies(root)) == 1, "identical to the newest copy: skipped")

        # Retention: newest 5 kept.
        for i in range(8):
            src.write_text(f"v{i}\n", encoding="utf-8")
            _run(_driver(block, nolib, f'backup_config_file "{src}"'), env)
        kept = _copies(root)
        _check(len(kept) == 5, f"retention keeps the newest 5 (got {len(kept)})")
        _check(
            bool(kept) and kept[-1].read_text() == "v7\n",
            "retention keeps the newest content, drops the oldest",
        )

        # Missing source: nothing happens, rc 0.
        res = _run(_driver(block, nolib, f'backup_config_file "{t}/nope"\necho done'), env)
        _check(res.returncode == 0 and "done" in res.stdout, "missing source is a no-op")

        # Failing backup (symlinked folder) must not abort under set -e.
        link = t / "linkroot"
        link.symlink_to(t)
        env_bad = {**env, "MCAPP_CONFIG_BACKUP_DIR": str(link)}
        res = _run(_driver(block, nolib, f'backup_config_file "{src}"\necho after'), env_bad)
        _check(
            res.returncode == 0 and "after" in res.stdout,
            "fallback: a failing backup does not abort under set -e",
        )
        _check("Could not back up" in res.stderr, "fallback: failure is reported as a warning")

        # lib/detect.sh next to the script wins over the fallback.
        libdir = t / "withlib"
        (libdir / "lib").mkdir(parents=True)
        shutil.copy(_REPO / "bootstrap" / "lib" / "detect.sh", libdir / "lib" / "detect.sh")
        res = _run(_driver(block, libdir, f'backup_config_file "{src}"\necho after'), env_bad)
        _check(
            res.returncode == 0 and "LOGWARN" in res.stderr and "[WARN]" not in res.stderr,
            "lib/detect.sh present: its backup_config_file is used, not the fallback",
        )


def run_bootstrap_tunnel_backup_tests() -> bool:
    """Run the suite. Returns True when every check passed."""
    print("\n🧪 Testing ssl-tunnel-setup.sh config backups:")
    _failures.clear()
    _test_static()
    _test_behaviour()
    if _failures:
        print(f"\n  {len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"    - {failure}")
    return not _failures


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_tunnel_backup_tests() else 1)
