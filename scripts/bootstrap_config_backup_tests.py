"""Startup regression suite for ``backup_config_file`` (bootstrap/lib/detect.sh).

Before the bootstrap overwrites or edits an existing system config file it keeps
a copy under /var/lib/mcapp/config-backups, so an operator can later see what
changed. Rules pinned here, against the REAL bash functions:

1. Helper: copy at 0600 in a 0700 dir; identical content skipped; changed content
   adds a copy; newest 5 kept; missing source is a no-op; an unwritable backup
   dir still returns 0 under ``set -euo pipefail``; two calls in the same second
   with different content both survive.
2. A real call site (``configure_tmpfs`` through ``MCAPP_FSTAB``) leaves a copy
   of the OLD content.
3. An idempotent rerun creates no new copy.
4. Static: every known write-site function contains a ``backup_config_file`` call.

Set BOOTSTRAP_CONFIG_BACKUP_LIB_DIR to point the suite at another lib dir (used
to prove the tests fail against the pre-change libs).
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
_LIB = Path(os.environ.get("BOOTSTRAP_CONFIG_BACKUP_LIB_DIR", str(_REPO / "bootstrap" / "lib")))
_BASH = shutil.which("bash")

_PRELUDE = """#!/bin/bash
set -euo pipefail
log_info() { echo "[INFO] $*"; }
log_ok() { echo "[OK] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }
log_step() { echo "==> $*"; }
source "$LIB_DIR/detect.sh"
"""

_SITE_PRELUDE = (
    _PRELUDE
    + """source "$LIB_DIR/packages.sh"
source "$LIB_DIR/system.sh"
"""
)

# file -> functions that write an existing system config and must back it up.
_WRITE_SITES: dict[str, tuple[str, ...]] = {
    "detect.sh": ("migrate_old_installation",),
    "system.sh": (
        "configure_locale",
        "configure_tmpfs",
        "_mcapp_configure_config_txt",
        "_mcapp_configure_cmdline_txt",
        "_mcapp_fstab_ensure_line",
        "_mcapp_journal_ramlog_cleanup",
        "configure_journald",
        "configure_wpasupplicant_pin",
        "configure_logrotate",
        "configure_nftables",
        "configure_iptables_legacy",
        "configure_bluetooth",
        "disable_ipv6",
        "configure_unattended_upgrades",
        "configure_fast_ssh_login",
        "configure_ssh_hardening",
    ),
    "packages.sh": (
        "configure_lighttpd_ktls_workaround",
        "configure_lighttpd",
        "configure_caddy_sudo",
        "configure_caddy",
    ),
    "config.sh": ("write_config", "migrate_config"),
    "deploy.sh": ("configure_systemd_service", "deploy_shell_aliases"),
}


def _check(ok: bool, label: str, failures: list[str]) -> None:
    if not ok:
        failures.append(label)


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _env(root: Path) -> dict[str, str]:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    _write_stub(bin_dir, "systemctl", 'echo "systemctl $*" >> "$CALLS"\nexit 1\n')
    for stub, code in (("findmnt", 1), ("mountpoint", 1), ("mount", 0), ("chown", 0)):
        _write_stub(bin_dir, stub, f'echo "{stub} $*" >> "$CALLS"\nexit {code}\n')
    _write_stub(bin_dir, "systemd-tmpfiles", 'echo "tmpfiles $*" >> "$CALLS"\nexit 0\n')
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(root),
        "LIB_DIR": str(_LIB),
        "CALLS": str(root / "calls.log"),
        "LANG": "C",
        "MCAPP_CONFIG_BACKUP_DIR": str(root / "backups"),
        "MCAPP_FSTAB": str(root / "fstab"),
        "MCAPP_TMPFILES_DIR": str(root / "tmpfiles.d"),
        "MCAPP_SYSTEMD_UNIT_DIR": str(root / "units"),
        "MCAPP_REBOOT_MARKER": str(root / "state" / "reboot-required"),
        "MCAPP_DEFAULTS_DIR": str(root / "default"),
    }


def _run(
    root: Path, env: dict[str, str], script: str, prelude: str = _PRELUDE
) -> subprocess.CompletedProcess[str]:
    driver = root / "driver.sh"
    driver.write_text(prelude + script)
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, str(driver)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _copies(env: dict[str, str]) -> list[Path]:
    root = Path(env["MCAPP_CONFIG_BACKUP_DIR"])
    return sorted(root.iterdir()) if root.is_dir() else []


def _copies_of(env: dict[str, str], source: Path) -> list[Path]:
    flat = str(source).replace("/", "__")
    stamp = re.compile(re.escape(flat) + r"\.\d{8}T\d{6}Z-\d{3}$")
    return [p for p in _copies(env) if stamp.fullmatch(p.name)]


def _helper_cases(tmp: Path, failures: list[str]) -> None:
    root = tmp / "helper"
    env = _env(root)
    src = root / "etc" / "thing.conf"
    src.parent.mkdir(parents=True)
    src.write_text("v1\n")
    backup_dir = Path(env["MCAPP_CONFIG_BACKUP_DIR"])

    r = _run(root, env, f'backup_config_file "{src}"\n')
    _check(r.returncode == 0, f"helper first call rc={r.returncode} {r.stderr}", failures)
    copies = _copies_of(env, src)
    _check(len(copies) == 1, f"first call creates one copy, got {len(copies)}", failures)
    if copies:
        _check(copies[0].read_text() == "v1\n", "copy holds the content", failures)
        _check(stat.S_IMODE(copies[0].stat().st_mode) == 0o600, "copy mode 0600", failures)
    _check(
        backup_dir.is_dir() and stat.S_IMODE(backup_dir.stat().st_mode) == 0o700,
        "backup dir mode 0700",
        failures,
    )

    _run(root, env, f'backup_config_file "{src}"\n')
    _check(len(_copies_of(env, src)) == 1, "identical content is skipped", failures)

    src.write_text("v2\n")
    _run(root, env, f'backup_config_file "{src}"\n')
    _check(len(_copies_of(env, src)) == 2, "changed content adds a copy", failures)

    # Retention: 8 more distinct versions, only the newest 5 survive.
    for i in range(3, 11):
        src.write_text(f"v{i}\n")
        _run(root, env, f'backup_config_file "{src}"\n')
    copies = _copies_of(env, src)
    _check(len(copies) == 5, f"retention keeps exactly 5, got {len(copies)}", failures)
    contents = [c.read_text() for c in copies]
    _check(
        contents == [f"v{i}\n" for i in range(6, 11)],
        f"retention keeps the newest, got {contents}",
        failures,
    )

    _edge_cases(tmp, failures)


def _edge_cases(tmp: Path, failures: list[str]) -> None:
    # Missing source: no-op, rc 0, nothing created.
    root2 = tmp / "missing"
    env2 = _env(root2)
    r = _run(root2, env2, f'backup_config_file "{root2}/nope"\necho after\n')
    _check(r.returncode == 0 and "after" in r.stdout, "missing source returns 0", failures)
    _check(_copies(env2) == [], "missing source creates nothing", failures)

    # Unwritable backup dir (parent is a regular file): still rc 0, warns, script goes on.
    root3 = tmp / "unwritable"
    env3 = _env(root3)
    blocker = root3 / "blocker"
    blocker.write_text("not a dir")
    env3["MCAPP_CONFIG_BACKUP_DIR"] = str(blocker / "sub")
    s3 = root3 / "c.conf"
    s3.write_text("x\n")
    r = _run(root3, env3, f'backup_config_file "{s3}"\necho after\n')
    _check(
        r.returncode == 0 and "after" in r.stdout,
        f"unwritable dir returns 0 under set -euo pipefail (rc={r.returncode})",
        failures,
    )
    _check("[WARN]" in r.stderr, "unwritable dir logs a warning", failures)

    # The service user owns /var/lib/mcapp, so it could swap the backup dir for a symlink.
    # Root must not chmod or copy through it.
    root4 = tmp / "edge-symlink"
    root4.mkdir(parents=True)
    env4 = _env(root4)
    target = root4 / "elsewhere"
    target.mkdir(mode=0o755)
    link = root4 / "linked-backups"
    link.symlink_to(target)
    env4["MCAPP_CONFIG_BACKUP_DIR"] = str(link)
    s4 = root4 / "c.conf"
    s4.write_text("x\n")
    r = _run(root4, env4, f'backup_config_file "{s4}"\necho after\n')
    _check(
        r.returncode == 0 and "after" in r.stdout,
        f"symlinked backup dir returns 0 (rc={r.returncode})",
        failures,
    )
    _check(not list(target.iterdir()), "symlinked backup dir: nothing copied through it", failures)
    _check(
        stat.S_IMODE(target.stat().st_mode) == 0o755,
        "symlinked backup dir: target mode untouched",
        failures,
    )

    # Without log_warn defined (detect.sh sourced alone) it still works.
    r = _run(
        root3,
        env3,
        f'backup_config_file "{s3}"\necho after\n',
        prelude='#!/bin/bash\nset -euo pipefail\nsource "$LIB_DIR/detect.sh"\n',
    )
    _check(r.returncode == 0 and "after" in r.stdout, "works without log_warn", failures)

    # Same second, different content: both survive (date stubbed to a constant).
    root4 = tmp / "samesec"
    env4 = _env(root4)
    _write_stub(root4 / "bin", "date", "echo 20260101T000000Z\n")
    s4 = root4 / "d.conf"
    s4.write_text("a\n")
    _run(root4, env4, f'backup_config_file "{s4}"\n')
    s4.write_text("b\n")
    _run(root4, env4, f'backup_config_file "{s4}"\n')
    got = [c.read_text() for c in _copies_of(env4, s4)]
    _check(got == ["a\n", "b\n"], f"same-second calls both survive in order, got {got}", failures)

    # Distinct paths cannot collide after flattening, nor can a name prefix.
    s5a = root4 / "a" / "b_c"
    s5b = root4 / "a_b" / "c"
    for p in (s5a, s5b):
        p.parent.mkdir(parents=True)
        p.write_text("same\n")
        _run(root4, env4, f'backup_config_file "{p}"\n')
    _check(
        len(_copies_of(env4, s5a)) == 1 and len(_copies_of(env4, s5b)) == 1,
        "distinct paths get distinct names",
        failures,
    )


def _site_cases(tmp: Path, failures: list[str]) -> None:
    root = tmp / "site"
    env = _env(root)
    (root / "default").mkdir()
    fstab = Path(env["MCAPP_FSTAB"])
    old = "proc /proc proc defaults 0 0\nPARTUUID=5922ef7f-02 / ext4 defaults,noatime 0 1\n"
    fstab.write_text(old)
    tmpfiles = Path(env["MCAPP_TMPFILES_DIR"]) / "mcapp.conf"
    tmpfiles.parent.mkdir()
    tmpfiles.write_text("# stale tmpfiles\n")

    r = _run(root, env, "configure_tmpfs\n", prelude=_SITE_PRELUDE)
    _check(r.returncode == 0, f"configure_tmpfs rc={r.returncode} {r.stderr}", failures)
    _check("McApp tmpfs - begin" in fstab.read_text(), "fstab was edited", failures)
    fcopies = _copies_of(env, fstab)
    _check(
        len(fcopies) == 1 and fcopies[0].read_text() == old,
        f"fstab copy holds the OLD content ({len(fcopies)} copies)",
        failures,
    )
    tcopies = _copies_of(env, tmpfiles)
    _check(
        len(tcopies) == 1 and tcopies[0].read_text() == "# stale tmpfiles\n",
        f"tmpfiles copy holds the OLD content ({len(tcopies)} copies)",
        failures,
    )

    # Idempotent rerun: nothing new is written, so nothing new is copied. The
    # first rerun may still copy the tmpfiles file once (the first run wrote it
    # unconditionally, and that content was never in a backup); the next must not.
    _run(root, env, "configure_tmpfs\n", prelude=_SITE_PRELUDE)
    before = [p.name for p in _copies(env)]
    r = _run(root, env, "configure_tmpfs\n", prelude=_SITE_PRELUDE)
    _check(r.returncode == 0, f"configure_tmpfs rerun rc={r.returncode} {r.stderr}", failures)
    after = [p.name for p in _copies(env)]
    _check(before == after, f"idempotent rerun adds no copy ({before} -> {after})", failures)


def _function_body(text: str, name: str) -> str | None:
    # Up to the next top-level definition: heredocs may hold "}" at column 0.
    m = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)(?=^[A-Za-z_][A-Za-z0-9_]*\(\) \{{|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    return m.group(1) if m else None


# Functions with several write sites: one surviving call must not hide the loss of the others.
_MIN_CALLS = {
    "configure_tmpfs": 4,
    "configure_locale": 2,
    "_mcapp_fstab_ensure_line": 2,
    "configure_unattended_upgrades": 2,
    "configure_lighttpd": 3,
    "configure_caddy_sudo": 3,
    "configure_systemd_service": 4,
}


def _static_cases(failures: list[str]) -> None:
    for fname, funcs in _WRITE_SITES.items():
        text = (_LIB / fname).read_text()
        for func in funcs:
            body = _function_body(text, func)
            if body is None:
                failures.append(f"{fname}: function {func} not found")
                continue
            count = body.count("backup_config_file")
            if count < _MIN_CALLS.get(func, 1):
                failures.append(f"{fname}: {func} has {count} backup_config_file call(s)")


def run_bootstrap_config_backup_tests() -> bool:
    if _BASH is None:
        print("  SKIP: bash not found")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-cfgbak-") as d:
        tmp = Path(d)
        _helper_cases(tmp, failures)
        _site_cases(tmp, failures)
    _static_cases(failures)
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"Bootstrap config backup tests: {len(failures)} failure(s)")
        return False
    print("Bootstrap config backup tests: all passed")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_config_backup_tests() else 1)
