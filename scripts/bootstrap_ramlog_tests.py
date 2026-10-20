"""Startup regression suite for ramlog-board handling in bootstrap/lib/system.sh.

Field evidence: on Orange Pi / Armbian boards ``orangepi-ramlog.service`` (or
``armbian-ramlog.service``) mounts a zram ext4 over /var/log and restores it
from /var/log.hdd. Our fstab ``tmpfs /var/log`` line then competes with it and
the fstab journal bind mount (``/var/lib/mcapp/journal`` -> /var/log/journal)
is shadowed, so the journal is lost on every reboot.

Rules pinned here, against the REAL bash functions with stub binaries on PATH:

1. ``detect_ramlog_service`` prints the enabled ramlog unit, else nothing. A
   unit whose defaults file (``/etc/default/<name>``) sets ``ENABLED=false``
   counts as inactive; a missing defaults file means enabled (upstream default).
2. ``configure_tmpfs`` on a ramlog board never writes a /var/log tmpfs line
   and strips an existing one from the McApp block (keeping /tmp), marking
   reboot-required. Other boards: unchanged.
3. ``configure_journald`` on a ramlog board writes NO bind (no fstab bind line,
   no ``var-log-journal.mount``, no /var/lib/mcapp/journal): the ramlog keeps its
   own persistent journal in /var/log.hdd/journal (armbian-ramlog syncToDisk
   moves a real /var/log/journal dir across filesystems, which a bind breaks).
   Our journald drop-in stays. A ``var-log-journal.mount`` left by an earlier
   run is disabled and removed. Other boards: fstab bind line as before.
4. Everything is idempotent.

Set BOOTSTRAP_RAMLOG_LIB_DIR to point the suite at another lib dir (used to
prove the tests fail against the pre-change system.sh).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_LIB = Path(os.environ.get("BOOTSTRAP_RAMLOG_LIB_DIR", str(_REPO / "bootstrap" / "lib")))
_TEMPLATES = _REPO / "bootstrap" / "templates"
_BASH = shutil.which("bash")

_PRELUDE = """#!/bin/bash
set -u
log_info() { echo "[INFO] $*"; }
log_ok() { echo "[OK] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }
log_step() { echo "==> $*"; }
source "$LIB_DIR/detect.sh"
source "$LIB_DIR/packages.sh"
source "$LIB_DIR/system.sh"
"""

_BASE_FSTAB = "proc /proc proc defaults 0 0\nPARTUUID=5922ef7f-02 / ext4 defaults,noatime 0 1\n"
_VARLOG = "tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,noexec,size=30M 0 0"
_TMP = "tmpfs /tmp tmpfs defaults,noatime,nosuid,mode=1777,size=150M 0 0"
_BIND = "/var/lib/mcapp/journal /var/log/journal none bind 0 0"
_BLOCK_BEGIN = "# McApp tmpfs - begin"
_BLOCK_END = "# McApp tmpfs - end"


def _check(ok: bool, label: str, failures: list[str]) -> None:
    if not ok:
        failures.append(label)


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _board(tmp: Path, name: str, ramlog: str | None) -> tuple[Path, dict[str, str]]:
    """A scratch board: stubs on PATH plus every overridable path redirected."""
    root = tmp / name
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    # `systemctl is-enabled <unit>` answers "enabled" for the ramlog unit only.
    enabled = ramlog or "none.service"
    _write_stub(
        bin_dir,
        "systemctl",
        'echo "systemctl $*" >> "$CALLS"\n'
        f'if [ "$1" = "is-enabled" ]; then\n'
        f'  if [ "$2" = "{enabled}" ]; then echo enabled; exit 0; fi\n'
        '  echo "Failed to get unit file state" >&2; exit 1\n'
        "fi\nexit 0\n",
    )
    for stub, code in (("findmnt", 1), ("mountpoint", 1), ("mount", 0), ("chown", 0)):
        _write_stub(bin_dir, stub, f'echo "{stub} $*" >> "$CALLS"\nexit {code}\n')
    _write_stub(bin_dir, "systemd-tmpfiles", 'echo "tmpfiles $*" >> "$CALLS"\nexit 0\n')
    _write_stub(bin_dir, "date", "echo 20260101\n")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(root),
        "LIB_DIR": str(_LIB),
        "CALLS": str(root / "calls.log"),
        "LANG": "C",
        "MCAPP_FSTAB": str(root / "fstab"),
        "MCAPP_TMPFILES_DIR": str(root / "tmpfiles.d"),
        "MCAPP_SYSTEMD_UNIT_DIR": str(root / "units"),
        "MCAPP_REBOOT_MARKER": str(root / "state" / "reboot-required"),
        "MCAPP_JOURNALD_CONF_DIR": str(root / "journald.d"),
        "MCAPP_JOURNAL_DIR": str(root / "journal"),
        "MCAPP_TEMPLATES_DIR": str(_TEMPLATES),
        "MCAPP_DEFAULTS_DIR": str(root / "default"),
    }
    (root / "default").mkdir()
    return root, env


def _run(root: Path, env: dict[str, str], script: str) -> subprocess.CompletedProcess[str]:
    driver = root / "driver.sh"
    driver.write_text(_PRELUDE + script)
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, str(driver)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _legacy_fstab() -> str:
    return _BASE_FSTAB + f"\n{_BLOCK_BEGIN}\n{_VARLOG}\n{_TMP}\n{_BLOCK_END}\n"


def _test_detect(tmp: Path, failures: list[str]) -> None:
    for unit in ("armbian-ramlog.service", "orangepi-ramlog.service"):
        root, env = _board(tmp, f"detect-{unit}", unit)
        out = _run(root, env, "detect_ramlog_service\n").stdout.strip()
        _check(out == unit, f"detect: {unit} reported (got {out!r})", failures)
    root, env = _board(tmp, "detect-none", None)
    out = _run(root, env, "detect_ramlog_service\n").stdout.strip()
    _check(out == "", f"detect: nothing on a plain board (got {out!r})", failures)
    # The defaults file can switch an enabled unit off (upstream: ENABLED=false).
    for unit, name in (
        ("armbian-ramlog.service", "armbian-ramlog"),
        ("orangepi-ramlog.service", "orangepi-ramlog"),
    ):
        for n, value in enumerate(("false", '"false"', "'false'", '  ENABLED = "false"  # off')):
            root, env = _board(tmp, f"detect-off-{name}-{n}", unit)
            line = value if "ENABLED" in value else f"ENABLED={value}"
            (root / "default" / name).write_text(f"# defaults\n{line}\nSIZE=50\n")
            out = _run(root, env, "detect_ramlog_service\n").stdout.strip()
            _check(
                out == "",
                f"detect: {name} {line.strip()!r} is not detected (got {out!r})",
                failures,
            )
        root, env = _board(tmp, f"detect-on-{name}", unit)
        (root / "default" / name).write_text('ENABLED="true"\nSIZE=50\n')
        out = _run(root, env, "detect_ramlog_service\n").stdout.strip()
        _check(out == unit, f"detect: {name} ENABLED=true still detected (got {out!r})", failures)
        # An ENABLED=false for ANOTHER unit's defaults file does not leak over.
        other = "orangepi-ramlog" if name == "armbian-ramlog" else "armbian-ramlog"
        root, env = _board(tmp, f"detect-other-{name}", unit)
        (root / "default" / other).write_text("ENABLED=false\n")
        out = _run(root, env, "detect_ramlog_service\n").stdout.strip()
        _check(
            out == unit, f"detect: {other} defaults do not disable {name} (got {out!r})", failures
        )


def _test_tmpfs_fresh_ramlog(tmp: Path, failures: list[str]) -> None:
    root, env = _board(tmp, "tmpfs-fresh", "orangepi-ramlog.service")
    fstab = root / "fstab"
    fstab.write_text(_BASE_FSTAB)
    _run(root, env, "configure_tmpfs\n")
    text = fstab.read_text()
    _check("tmpfs /var/log" not in text, "tmpfs fresh ramlog: no /var/log tmpfs line", failures)
    _check(_TMP in text.splitlines(), "tmpfs fresh ramlog: /tmp line added", failures)
    # Never mount /var/log live on a ramlog board.
    calls = (root / "calls.log").read_text() if (root / "calls.log").exists() else ""
    _check("mount /var/log" not in calls, "tmpfs fresh ramlog: /var/log not mounted live", failures)


def _test_tmpfs_strip(tmp: Path, failures: list[str]) -> None:
    root, env = _board(tmp, "tmpfs-strip", "orangepi-ramlog.service")
    fstab = root / "fstab"
    fstab.write_text(_legacy_fstab())
    marker = Path(env["MCAPP_REBOOT_MARKER"])
    _run(root, env, "configure_tmpfs\n")
    lines = fstab.read_text().splitlines()
    _check(_VARLOG not in lines, "tmpfs strip: /var/log line removed", failures)
    _check(_TMP in lines, "tmpfs strip: /tmp line kept", failures)
    _check(
        _BLOCK_BEGIN in lines and _BLOCK_END in lines,
        "tmpfs strip: block markers kept",
        failures,
    )
    _check(marker.exists(), "tmpfs strip: reboot-required written", failures)
    after = fstab.read_text()
    marker.unlink(missing_ok=True)
    _run(root, env, "configure_tmpfs\n")
    _check(fstab.read_text() == after, "tmpfs strip: second run leaves fstab identical", failures)
    _check(not marker.exists(), "tmpfs strip: second run writes no reboot marker", failures)
    # A /var/log tmpfs line OUTSIDE our block belongs to the operator.
    root2, env2 = _board(tmp, "tmpfs-foreign", "orangepi-ramlog.service")
    foreign = _BASE_FSTAB + _VARLOG + "\n"
    (root2 / "fstab").write_text(foreign)
    _run(root2, env2, "configure_tmpfs\n")
    _check(
        _VARLOG in (root2 / "fstab").read_text(), "tmpfs strip: line outside block kept", failures
    )


def _test_tmpfs_plain(tmp: Path, failures: list[str]) -> None:
    root, env = _board(tmp, "tmpfs-plain", None)
    fstab = root / "fstab"
    fstab.write_text(_BASE_FSTAB)
    _run(root, env, "configure_tmpfs\nconfigure_tmpfs\n")
    expected = _BASE_FSTAB + f"\n{_BLOCK_BEGIN}\n{_VARLOG}\n{_TMP}\n{_BLOCK_END}\n"
    _check(fstab.read_text() == expected, "tmpfs plain: block identical to legacy, once", failures)
    _check(
        not Path(env["MCAPP_REBOOT_MARKER"]).exists(),
        "tmpfs plain: no reboot marker",
        failures,
    )


def _test_journal_ramlog(tmp: Path, failures: list[str]) -> None:
    for unit in ("orangepi-ramlog.service", "armbian-ramlog.service"):
        tag = unit.split("-")[0]
        root, env = _board(tmp, f"journal-ramlog-{tag}", unit)
        fstab = root / "fstab"
        fstab.write_text(_legacy_fstab().replace(_VARLOG + "\n", _VARLOG + "\n" + _BIND + "\n"))
        _run(root, env, "configure_journald\n")
        lines = fstab.read_text().splitlines()
        _check(_BIND not in lines, f"journal {tag}: epoch-5 fstab bind line stripped", failures)
        _check(_TMP in lines, f"journal {tag}: other McApp fstab lines kept", failures)
        _check(
            not (root / "units" / "var-log-journal.mount").exists(),
            f"journal {tag}: no var-log-journal.mount written",
            failures,
        )
        _check(
            not (root / "journal").exists(),
            f"journal {tag}: /var/lib/mcapp/journal not created",
            failures,
        )
        conf = root / "journald.d" / "mcapp-journal.conf"
        ctext = conf.read_text() if conf.exists() else ""
        _check(
            "Storage=persistent" in ctext.splitlines() and "SystemMaxUse=16M" in ctext.splitlines(),
            f"journal {tag}: journald drop-in present (persistent, 16M)",
            failures,
        )
        calls = (root / "calls.log").read_text() if (root / "calls.log").exists() else ""
        _check(
            "var-log-journal" not in calls and "mount /var/log/journal" not in calls,
            f"journal {tag}: no mount unit / bind mount touched",
            failures,
        )
        _check(
            _fstab_bind_gone_marker(env),
            f"journal {tag}: fstab bind removal marks reboot-required",
            failures,
        )
        # Second run: nothing changes, no new marker.
        marker = Path(env["MCAPP_REBOOT_MARKER"])
        marker.unlink(missing_ok=True)
        snapshot = (fstab.read_text(), ctext)
        _run(root, env, "configure_journald\n")
        again = (fstab.read_text(), conf.read_text() if conf.exists() else "")
        _check(again == snapshot, f"journal {tag}: second run changes nothing", failures)
        _check(not marker.exists(), f"journal {tag}: second run writes no reboot marker", failures)
        _check(
            not (root / "journal").exists(), f"journal {tag}: second run creates no dir", failures
        )

    # A unit installed by an earlier run of the rejected design is cleaned up.
    unit = "orangepi-ramlog.service"
    root, env = _board(tmp, "journal-cleanup", unit)
    (root / "fstab").write_text(_legacy_fstab())
    units = root / "units"
    units.mkdir()
    stale = units / "var-log-journal.mount"
    stale.write_text("[Mount]\nWhat=/var/lib/mcapp/journal\nWhere=/var/log/journal\n")
    marker = Path(env["MCAPP_REBOOT_MARKER"])
    _run(root, env, "configure_journald\n")
    calls = (root / "calls.log").read_text() if (root / "calls.log").exists() else ""
    _check(not stale.exists(), "journal cleanup: stale unit file removed", failures)
    _check(
        "systemctl disable var-log-journal.mount" in calls,
        "journal cleanup: unit disabled via systemctl",
        failures,
    )
    _check("systemctl daemon-reload" in calls, "journal cleanup: daemon-reload run", failures)
    _check(marker.exists(), "journal cleanup: reboot-required written", failures)
    marker.unlink(missing_ok=True)
    (root / "calls.log").write_text("")
    _run(root, env, "configure_journald\n")
    calls2 = (root / "calls.log").read_text()
    _check(
        "var-log-journal" not in calls2 and "daemon-reload" not in calls2,
        "journal cleanup: second run is a no-op",
        failures,
    )
    _check(not marker.exists(), "journal cleanup: second run writes no reboot marker", failures)

    # ENABLED=false in the defaults file: the board is treated as a plain one.
    root, env = _board(tmp, "journal-ramlog-off", "orangepi-ramlog.service")
    (root / "default" / "orangepi-ramlog").write_text('ENABLED="false"\n')
    (root / "fstab").write_text(_legacy_fstab())
    _run(root, env, "configure_journald\n")
    _check(
        _BIND in (root / "fstab").read_text().splitlines(),
        "journal ramlog disabled in defaults: fstab bind line added",
        failures,
    )


def _fstab_bind_gone_marker(env: dict[str, str]) -> bool:
    return Path(env["MCAPP_REBOOT_MARKER"]).exists()


def _test_journal_plain(tmp: Path, failures: list[str]) -> None:
    root, env = _board(tmp, "journal-plain", None)
    fstab = root / "fstab"
    fstab.write_text(_legacy_fstab())
    _run(root, env, "configure_journald\nconfigure_journald\n")
    lines = fstab.read_text().splitlines()
    _check(lines.count(_BIND) == 1, "journal plain: bind line present once", failures)
    idx = lines.index(_BIND) if _BIND in lines else -1
    _check(
        idx > 0 and lines[idx - 1] == _VARLOG,
        "journal plain: bind line directly after /var/log tmpfs line",
        failures,
    )
    _check(
        not (root / "units" / "var-log-journal.mount").exists(),
        "journal plain: no mount unit written",
        failures,
    )


def run_bootstrap_ramlog_tests() -> bool:
    if _BASH is None:
        print("bootstrap_ramlog: SKIP (no bash)")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-ramlog-") as td:
        tmp = Path(td)
        _test_detect(tmp, failures)
        _test_tmpfs_fresh_ramlog(tmp, failures)
        _test_tmpfs_strip(tmp, failures)
        _test_tmpfs_plain(tmp, failures)
        _test_journal_ramlog(tmp, failures)
        _test_journal_plain(tmp, failures)
    for f in failures:
        print(f"  FAIL: {f}")
    print(f"bootstrap_ramlog: {len(failures)} failure(s) across 6 case groups")
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_ramlog_tests() else 1)
