"""Startup regression suite for ``bootstrap/lib/deploy.sh``'s ``setup_python_env``.

The deploy ran a plain ``uv sync --all-packages``, which also installs the
default ``dev`` dependency group. Every slot on mcapp.local therefore carried
mypy and its helpers (found in the v2.1.13 production deploy log): nothing
imports them at runtime, they only cost SD-card space and sync time. The sync
must pass ``--no-dev``.

The function is extracted from deploy.sh by name and driven under bash with a
fake ``uv`` that records its argv, and stubs for ``getent``/``sudo`` and the
log helpers, so nothing here touches a real environment.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_BASH = shutil.which("bash")
_REPO = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO / "bootstrap" / "lib" / "deploy.sh"

# getent returns a passwd line whose home is $FAKE_HOME, so uv_bin resolves to
# the fake uv; sudo drops "-u <user>" and runs the rest as the current user.
_DRIVER = """set -eo pipefail
log_info() {{ :; }}
log_ok() {{ :; }}
log_error() {{ printf 'ERR %s\\n' "$*" >&2; }}
getent() {{ printf 'u:x:1:1::%s:/bin/bash\\n' "$FAKE_HOME"; }}
sudo() {{ shift 2; "$@"; }}
{function}
setup_python_env "$1"
"""

_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$UV_ARGV_LOG"
"""


def _extract_function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"deploy.sh no longer defines a top-level `{name}()`")
    return match.group(0)


def _sync_argv(function: str, sudo_user: str | None) -> list[str]:
    """Run setup_python_env against a fake slot; return each recorded uv argv."""
    assert _BASH is not None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        uv = home / ".local" / "bin" / "uv"
        uv.parent.mkdir(parents=True)
        uv.write_text(_FAKE_UV)
        uv.chmod(0o755)
        target = root / "slot"
        target.mkdir()
        (target / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        argv_log = root / "uv-argv.log"
        env = {
            "PATH": "/usr/bin:/bin",
            "FAKE_HOME": str(home),
            "UV_ARGV_LOG": str(argv_log),
            "HOME": str(home),
        }
        if sudo_user is not None:
            env["SUDO_USER"] = sudo_user
        subprocess.run(  # noqa: S603 - fixed argv, absolute binary
            [_BASH, "-c", _DRIVER.format(function=function), "drv", str(target)],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return argv_log.read_text().splitlines() if argv_log.exists() else []


def run_bootstrap_venv_tests() -> bool:
    """Return True if every invariant holds."""
    if _BASH is None or not _DEPLOY_SH.exists():
        print("bootstrap_venv: SKIPPED - NOT VERIFIED (no bash or no bootstrap/lib/deploy.sh)")
        return True

    function = _extract_function(_DEPLOY_SH.read_text(), "setup_python_env")
    passed = 0
    failed = 0

    # Both branches: the real user via sudo (the deploy path) and root itself.
    cases = [("sudo -u <user> branch", "pi-user"), ("root branch", "root")]
    for label, sudo_user in cases:
        try:
            calls = _sync_argv(function, sudo_user)
            assert len(calls) == 1, f"expected one uv call, got {calls!r}"
            args = calls[0].split()
            assert args[:1] == ["sync"], f"not a uv sync: {calls[0]!r}"
            assert "--all-packages" in args, f"workspace members dropped: {calls[0]!r}"
            assert "--no-dev" in args, f"dev group installed into the slot: {calls[0]!r}"
            print(f"PASS | {label}: {calls[0]}")
            passed += 1
        except (AssertionError, subprocess.CalledProcessError) as exc:
            detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else exc
            print(f"FAIL | {label}: {detail}")
            failed += 1

    print(f"bootstrap_venv: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_venv_tests() else 1)
