"""Startup regression suite for ``bootstrap/mcapp.sh``'s ``use_disk_tmpdir``.

/tmp on a McApp box is a 150 MB tmpfs. v2.1.13-dev.1 (browser-AI wasm in the
webapp) is 37 MB packed and 117 MB unpacked, so the bootstrap died in
``tar -xzf`` right after the checksum passed, with the tarball and its tree
both sitting in ``mktemp -d`` on /tmp. ``use_disk_tmpdir`` exports TMPDIR to a
disk-backed directory before any ``mktemp`` runs.

The function is extracted from mcapp.sh by name and driven under bash with a
fake candidate directory, so nothing here touches the real /var/tmp.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_BASH = shutil.which("bash")
_REPO = Path(__file__).resolve().parent.parent
_MCAPP_SH = _REPO / "bootstrap" / "mcapp.sh"

_DRIVER = """set -euo pipefail
{function}
use_disk_tmpdir "$1"
printf '%s' "${{TMPDIR-<unset>}}"
"""


def _extract_function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"mcapp.sh no longer defines a top-level `{name}()`")
    return match.group(0)


def _run(script: str, candidate: str, tmpdir: str | None) -> str:
    assert _BASH is not None
    env = {"PATH": "/usr/bin:/bin"}
    if tmpdir is not None:
        env["TMPDIR"] = tmpdir
    result = subprocess.run(  # noqa: S603 - fixed argv, absolute binary
        [_BASH, "-c", script, "drv", candidate],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout


def run_bootstrap_tmpdir_tests() -> bool:
    """Return True if every invariant holds."""
    if _BASH is None or not _MCAPP_SH.exists():
        print("bootstrap_tmpdir: SKIPPED - NOT VERIFIED (no bash or no bootstrap/mcapp.sh)")
        return True

    passed = 0
    failed = 0

    def record(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"PASS | {label}")
        else:
            failed += 1
            print(f"FAIL | {label} {detail}")

    source = _MCAPP_SH.read_text(encoding="utf-8")
    try:
        script = _DRIVER.format(function=_extract_function(source, "use_disk_tmpdir"))
    except AssertionError as exc:
        record("use_disk_tmpdir extraction", False, str(exc))
        print(f"bootstrap_tmpdir: {passed} passed, {failed} failed")
        return False

    with tempfile.TemporaryDirectory() as raw:
        disk = str(Path(raw) / "disk")
        Path(disk).mkdir()
        missing = str(Path(raw) / "missing")

        got = _run(script, disk, None)
        record("TMPDIR unset -> exported to the disk directory", got == disk, f"(got {got!r})")
        got = _run(script, disk, "/tmp")  # noqa: S108 - the tmpfs value under test, never written to
        record("TMPDIR=/tmp -> moved to the disk directory", got == disk, f"(got {got!r})")
        got = _run(script, disk, "/srv/scratch")
        record("an operator's own TMPDIR is kept", got == "/srv/scratch", f"(got {got!r})")
        got = _run(script, missing, None)
        record("missing candidate -> TMPDIR left alone", got == "<unset>", f"(got {got!r})")

    # The call must sit at top level, before the first mktemp anywhere in the file.
    lines = source.splitlines()
    call = next((i for i, ln in enumerate(lines) if ln == "use_disk_tmpdir"), None)
    # Judge the code part only: a trailing comment must not hide a real mktemp.
    first_mktemp = next(
        (i for i, ln in enumerate(lines) if "mktemp" in ln.split("#", 1)[0]),
        None,
    )
    record(
        "mcapp.sh calls use_disk_tmpdir at top level before any mktemp",
        call is not None and first_mktemp is not None and call < first_mktemp,
        f"(call line {call}, first mktemp line {first_mktemp})",
    )

    print(f"bootstrap_tmpdir: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_bootstrap_tmpdir_tests() else 1)
