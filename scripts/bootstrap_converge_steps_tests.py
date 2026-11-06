"""Startup regression suite for step-gated ``mcapp.sh --converge``.

Rules pinned here, against the REAL bash (bootstrap/lib/converge.sh) with stub
step functions:

1. Installed epoch == every step's value: no gated step runs, ensure_web_frontend
   still does (an 8 -> 9 bump with no changed step runs no apt upgrade).
2. Installed 7 and 0: every step runs, in setup_system order, then
   install_apt_deps, install_uv, then ensure_web_frontend.
3. A step raised to 9 runs at installed 8; the others do not.
4. A failing setup step and a failing install_apt_deps both exit non-zero
   (never reaching the code after the call, i.e. write_system_epoch) and the
   later steps do not run.
5. PARITY: every function in setup_system() and install_packages() (real lib
   files) has a row in the table, in order.
6. Source: the --converge branch calls converge_system_steps as a plain command
   and no longer calls setup_system/install_packages; the full run still does.
7. SYSTEM_EPOCH in mcapp.sh >= every step value.

Set BOOTSTRAP_CONVERGE_ROOT to point the suite at another bootstrap dir (used
to prove the tests fail against the pre-change tree).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_ROOT = Path(os.environ.get("BOOTSTRAP_CONVERGE_ROOT", str(_REPO / "bootstrap")))
_LIB = _ROOT / "lib"
_MCAPP = _ROOT / "mcapp.sh"
_BASH = shutil.which("bash")

_PRELUDE = """#!/bin/bash
set -eo pipefail
set -u
log_info() { echo "[INFO] $*"; }
log_error() { echo "[ERROR] $*" >&2; }
source "$LIB_DIR/converge.sh"
for n in $(converge_step_table | cut -d' ' -f1) ensure_web_frontend; do
  eval "$n() { echo $n >> \\"\\$CALLS\\";
    if [ \\"\\${FAIL:-}\\" = $n ]; then return 1; fi; return 0; }"
done
"""


def _function_body(path: Path, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", path.read_text(), re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""


def _calls(body: str) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and re.fullmatch(r"[a-z_][a-z0-9_]*", s):
            out.append(s)
    return out


def _expected_order() -> list[str]:
    return _calls(_function_body(_LIB / "system.sh", "setup_system")) + [
        n
        for n in _calls(_function_body(_LIB / "packages.sh", "install_packages"))
        if n != "ensure_web_frontend"
    ]


def _table_rows() -> list[tuple[str, int]]:
    path = _LIB / "converge.sh"
    if not path.exists():
        return []
    m = re.search(r"<<'TABLE'\n(.*?)^TABLE$", path.read_text(), re.DOTALL | re.MULTILINE)
    rows: list[tuple[str, int]] = []
    for line in (m.group(1) if m else "").splitlines():
        name, _, since = line.partition(" ")
        if name and since.isdigit():
            rows.append((name, int(since)))
    return rows


def _run(
    tmp: Path, installed: int, *, fail: str = "", table: str = ""
) -> tuple[int, list[str], str]:
    calls = tmp / "calls.log"
    calls.write_text("")
    script = (
        _PRELUDE + table + f'converge_system_steps {installed}\necho REACHED_AFTER >> "$CALLS"\n'
    )
    driver = tmp / "driver.sh"
    driver.write_text(script)
    assert _BASH is not None
    env = {"PATH": "/usr/bin:/bin", "LIB_DIR": str(_LIB), "CALLS": str(calls), "FAIL": fail}
    p = subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, str(driver)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return p.returncode, calls.read_text().split(), p.stdout + p.stderr


def _check(ok: bool, label: str, failures: list[str]) -> None:
    if not ok:
        failures.append(label)


def _test_runs(tmp: Path, failures: list[str]) -> None:
    order = _expected_order()
    rc, got, _ = _run(tmp, 8)
    _check(
        rc == 0 and got == ["ensure_web_frontend", "REACHED_AFTER"],
        f"1: installed 8 -> {got}",
        failures,
    )
    full = [*order, "ensure_web_frontend", "REACHED_AFTER"]
    for installed in (7, 0):
        rc, got, _ = _run(tmp, installed)
        _check(rc == 0 and got == full, f"2: installed {installed} order {got}", failures)
    _check(
        order[-2:] == ["install_apt_deps", "install_uv"], f"2: tail of order {order[-3:]}", failures
    )
    override = (
        "converge_step_table() { "
        "for n in $(printf '%s ' " + " ".join(order) + "); do "
        'if [ "$n" = configure_firewall ]; then echo "$n 9"; else echo "$n 8"; fi; done; }\n'
    )
    rc, got, _ = _run(tmp, 8, table=override)
    _check(
        rc == 0 and got == ["configure_firewall", "ensure_web_frontend", "REACHED_AFTER"],
        f"3: one step at 9 -> {got}",
        failures,
    )


def _test_failures(tmp: Path, failures: list[str]) -> None:
    order = _expected_order()
    for step in ("configure_firewall", "install_apt_deps", "install_uv"):
        rc, got, _ = _run(tmp, 0, fail=step)
        idx = order.index(step)
        _check(rc != 0, f"4: {step} failure exits non-zero (rc={rc})", failures)
        _check(got == order[: idx + 1], f"4: {step} failure stops later steps: {got}", failures)
    # The front door is what most epoch bumps exist for: its failure must not advance the epoch.
    rc, got, _ = _run(tmp, 8, fail="ensure_web_frontend")
    _check(
        rc != 0 and got == ["ensure_web_frontend"],
        f"4a: ensure_web_frontend failure (rc={rc}): {got}",
        failures,
    )
    # A package step runs with errexit off, as install_packages did under `if !`: one failing
    # command inside it does not abort on the spot, its return status decides.
    keeps_going = (
        'install_apt_deps() { echo install_apt_deps >> "$CALLS"; false; echo MARK >> "$CALLS"; }\n'
    )
    rc, got, _ = _run(tmp, 0, table=keeps_going)
    _check(rc == 0 and "MARK" in got, f"4c: package step errexit off (rc={rc}): {got}", failures)
    # A step that reads stdin (apt-get, systemd-run, an interactive prompt) must not
    # consume the step table: the later steps would be skipped and the epoch advanced.
    greedy = 'configure_tmpfs() { echo configure_tmpfs >> "$CALLS"; cat > /dev/null; }\n'
    rc, got, _ = _run(tmp, 0, table=greedy)
    full = [*order, "ensure_web_frontend", "REACHED_AFTER"]
    _check(rc == 0 and got == full, f"4b: stdin-reading step ate the table: {got}", failures)


def _test_parity(failures: list[str]) -> None:
    rows = [n for n, _ in _table_rows()]
    order = _expected_order()
    _check(bool(order), "5: parsed no steps from system.sh/packages.sh", failures)
    _check(rows == order, f"5: table {rows} != setup_system+install_packages {order}", failures)


def _test_source(failures: list[str]) -> None:
    text = _MCAPP.read_text()
    start = text.index('if [[ "$CONVERGE_MODE" == "true" ]]; then')
    end = text.index('if [[ "$SKIP_TO_DEPLOY" == "true" ]]; then', start)
    branch, rest = text[start:end], text[end:]
    plain = re.search(r'^\s+converge_system_steps "\$installed_epoch"$', branch, re.MULTILINE)
    _check(
        plain is not None,
        "6: converge branch must call converge_system_steps as a plain command",
        failures,
    )
    _check("setup_system" not in branch, "6: converge branch still calls setup_system", failures)
    _check(
        "install_packages" not in branch,
        "6: converge branch still calls install_packages",
        failures,
    )
    _check(
        plain is not None
        and (w := re.search(r"^\s+write_system_epoch$", branch, re.MULTILINE)) is not None
        and plain.start() < w.start(),
        "6: write_system_epoch must follow the steps",
        failures,
    )
    _check(
        re.search(r"^\s+setup_system$", rest, re.MULTILINE) is not None,
        "6: full run lost setup_system",
        failures,
    )
    # `--tag <older release>` fetches a tree without converge.sh. An unguarded `source` dies under
    # set -e before verify_lib_skew can say "run that release's installer".
    _check(
        re.search(r'if \[\[ -f "\$\{lib_dir\}/converge\.sh" \]\]; then', text) is not None,
        "6: source of converge.sh must tolerate an older tree",
        failures,
    )


def _test_epoch(failures: list[str]) -> None:
    m = re.search(r"^readonly SYSTEM_EPOCH=(\d+)$", _MCAPP.read_text(), re.MULTILINE)
    epoch = int(m.group(1)) if m else -1
    top = max((s for _, s in _table_rows()), default=-1)
    _check(epoch >= top >= 0, f"7: SYSTEM_EPOCH {epoch} < table max {top}", failures)


def run_bootstrap_converge_steps_tests() -> bool:
    if _BASH is None:
        print("bootstrap_converge_steps: SKIP (no bash)")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-converge-") as td:
        tmp = Path(td)
        if (_LIB / "converge.sh").exists():
            _test_runs(tmp, failures)
            _test_failures(tmp, failures)
        else:
            failures.append("1-4: lib/converge.sh missing")
    _test_parity(failures)
    _test_source(failures)
    _test_epoch(failures)
    for f in failures:
        print(f"  FAIL: {f}")
    print(f"bootstrap_converge_steps: {len(failures)} failure(s) across 7 case groups")
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_converge_steps_tests() else 1)
