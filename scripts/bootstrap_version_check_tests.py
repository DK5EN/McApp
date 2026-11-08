"""Startup regression suite for check_versions in bootstrap/lib/health.sh.

F14 (doc/ops-mcapp-health-log.md §59): an update whose release version could not be
resolved left the box on the old build, and the health check still printed
`webapp version: [OK] v2.1.26-dev.5` while v2.1.26 was the target. Inside a deploy,
deploy_app now records the release it was asked to install, and check_versions warns
when the served webapp is a different version. Warn only, by operator decision: the
update runner's own check is the hard gate. A plain health check (no deploy, no
target) prints what it always printed.

Runs the REAL health.sh under bash. The mismatch and unknown-target cases fail
against the pre-change check_versions, which printed [OK] for every version.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "bootstrap" / "lib"
_BASH = shutil.which("bash")

_PRELUDE = """#!/bin/bash
set -u
GREEN=""; YELLOW=""; RED=""; NC=""
log_info() { echo "[INFO] $*"; }
log_ok() { echo "[OK] $*"; }
log_warn() { echo "[WARN] $*"; }
source "$LIB_DIR/health.sh"
"""


def _run(tmp: Path, installed: str, target: str | None) -> subprocess.CompletedProcess[str]:
    install_dir = tmp / "install"
    (install_dir / "webapp").mkdir(parents=True, exist_ok=True)
    (install_dir / "webapp" / "version.html").write_text(installed + "\n", encoding="utf-8")
    driver = tmp / "driver.sh"
    assign = "" if target is None else f'MCAPP_DEPLOY_TARGET_VERSION="{target}"\n'
    driver.write_text(_PRELUDE + assign + "check_versions\necho rc=$?\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LIB_DIR": str(_LIB),
        "INSTALL_DIR": str(install_dir),
        "SCRIPT_VERSION": "9.9.9",
    }
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, str(driver)], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def _version_line(proc: subprocess.CompletedProcess[str]) -> str:
    for line in proc.stdout.splitlines():
        if "webapp version:" in line:
            return line.strip()
    return f"<no webapp version line; stdout={proc.stdout!r} stderr={proc.stderr!r}>"


# (label, installed webapp version, deploy target or None, expected tag, expected text)
_CASES: tuple[tuple[str, str, str | None, str, str], ...] = (
    ("no target", "v2.1.26-dev.5", None, "[OK]", "v2.1.26-dev.5"),
    ("mismatch (F14)", "v2.1.26-dev.5", "v2.1.26", "[WARN]", "v2.1.26-dev.5, expected v2.1.26"),
    ("unknown target", "v2.1.26-dev.5", "unknown", "[WARN]", "target release unknown"),
    ("match", "v2.1.26", "v2.1.26", "[OK]", "v2.1.26"),
    ("match without v", "v2.1.26", "2.1.26", "[OK]", "v2.1.26"),
)


def _test_cases(tmp: Path, failures: list[str]) -> None:
    for label, installed, target, want_tag, want_text in _CASES:
        proc = _run(tmp, installed, target)
        line = _version_line(proc)
        if want_tag not in line or want_text not in line:
            failures.append(f"{label}: expected {want_tag} with {want_text!r}, got {line!r}")
        if "rc=0" not in proc.stdout:
            failures.append(f"{label}: check_versions must return 0, stdout={proc.stdout!r}")


def _test_source(failures: list[str]) -> None:
    """deploy_app must record the target as a GLOBAL after resolving it, or
    check_versions (called later from mcapp.sh) never sees it."""
    text = (_LIB / "deploy.sh").read_text(encoding="utf-8")
    body = text[text.find("deploy_app() {") :]
    resolve = body.find("resolve_target_version")
    assign = body.find('MCAPP_DEPLOY_TARGET_VERSION="$remote_version"')
    if resolve < 0 or assign < 0 or assign < resolve:
        failures.append(
            "deploy.sh: deploy_app must set MCAPP_DEPLOY_TARGET_VERSION after resolving"
        )
    if re.search(r"local[^\n]*MCAPP_DEPLOY_TARGET_VERSION", body):
        failures.append("deploy.sh: MCAPP_DEPLOY_TARGET_VERSION must not be local")


def run_bootstrap_version_check_tests() -> bool:
    if _BASH is None:
        print("bootstrap_version_check: SKIP (no bash)")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-vercheck-") as td:
        _test_cases(Path(td), failures)
    _test_source(failures)
    for line in failures:
        print(f"  bootstrap_version_check: {line}")
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_version_check_tests() else 1)
