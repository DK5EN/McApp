"""Startup regression suite for ``configure_ssh_hardening`` (bootstrap/lib/system.sh).

Field finding 2026-11-08: mcapp.local's drop-in carried
``KexAlgorithms curve25519-sha256,...`` only, which shut out the post-quantum
hybrids OpenSSH 10 would otherwise negotiate, and the function never touched an
existing file, so no rerun could fix it. Rules pinned here, against the REAL bash
functions with stubbed ``ssh``/``sshd``/``systemctl``:

1. An existing epoch-8 drop-in is rewritten: mlkem768 first, sntrup761, curve25519
   as fallback, ``HostKeyAlgorithms ssh-ed25519``, ``RequiredRSASize 3072``; the old
   content is backed up and sshd restarted.
2. A Bookworm-era OpenSSH (sntrup761@openssh.com only, no mlkem) gets only what it
   knows; a pre-9.1 build gets no ``RequiredRSASize`` line.
3. A rerun with nothing changed writes nothing, backs up nothing, restarts nothing.
4. ``sshd -t`` failing restores the previous file, or removes a newly created one,
   and never restarts sshd.
5. A ``10-pi-harden.conf`` box is left alone.
6. The converge row for ``configure_ssh_hardening`` is >= 10, so existing boxes
   re-run it.

Set BOOTSTRAP_SSH_HARDENING_ROOT to point the suite at another bootstrap dir (used
to prove the tests fail against the pre-change tree).
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
_ROOT = Path(os.environ.get("BOOTSTRAP_SSH_HARDENING_ROOT", str(_REPO / "bootstrap")))
_LIB = _ROOT / "lib"
_BASH = shutil.which("bash")

_PRELUDE = """#!/bin/bash
set -euo pipefail
log_info() { echo "[INFO] $*"; }
log_ok() { echo "[OK] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }
log_step() { echo "==> $*"; }
source "$LIB_DIR/detect.sh"
source "$LIB_DIR/packages.sh"
source "$LIB_DIR/system.sh"
configure_ssh_hardening
"""

# What epoch 8 wrote, byte for byte (live on mcapp.local until 2026-11-08).
_EPOCH8 = """# McApp SSH hardening — crypto + access control
HostKey /etc/ssh/ssh_host_ed25519_key
Ciphers aes256-gcm@openssh.com,chacha20-poly1305@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org
UseDNS no
X11Forwarding no
PermitRootLogin no
MaxAuthTries 3
MaxSessions 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
"""

_KEX_TRIXIE = (
    "curve25519-sha256\ncurve25519-sha256@libssh.org\nsntrup761x25519-sha512\n"
    "sntrup761x25519-sha512@openssh.com\nmlkem768x25519-sha256\n"
)
_KEX_BOOKWORM = (
    "curve25519-sha256\ncurve25519-sha256@libssh.org\nsntrup761x25519-sha512@openssh.com\n"
)


def _check(ok: bool, label: str, failures: list[str]) -> None:
    if not ok:
        failures.append(label)


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _Box:
    """One fake box: a sshd_config.d dir, stubbed tools, a call log."""

    def __init__(self, root: Path, kex: str, rsa_size: bool = True, sshd_ok: bool = True) -> None:
        self.root = root
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True)
        (root / "kex").write_text(kex)
        g_out = "user root\nrequiredrsasize 1024\n" if rsa_size else "user root\n"
        (root / "ssh-G").write_text(g_out)
        _write_stub(
            bin_dir,
            "ssh",
            'case "$*" in\n'
            '  "-Q kex") cat "$BOX/kex" ;;\n'
            '  *-G*) cat "$BOX/ssh-G" ;;\n'
            "  *) exit 255 ;;\n"
            "esac\n",
        )
        _write_stub(bin_dir, "sshd", f'echo "sshd $*" >> "$CALLS"\nexit {0 if sshd_ok else 1}\n')
        _write_stub(bin_dir, "systemctl", 'echo "systemctl $*" >> "$CALLS"\nexit 0\n')
        self.conf_dir = root / "sshd_config.d"
        self.conf_dir.mkdir()
        self.conf = self.conf_dir / "mcapp-hardening.conf"
        self.calls = root / "calls.log"
        self.backups = root / "backups"
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(root),
            "LIB_DIR": str(_LIB),
            "BOX": str(root),
            "CALLS": str(self.calls),
            "LANG": "C",
            "MCAPP_CONFIG_BACKUP_DIR": str(self.backups),
            "MCAPP_SSHD_CONF_DIR": str(self.conf_dir),
        }

    def run(self) -> subprocess.CompletedProcess[str]:
        driver = self.root / "driver.sh"
        driver.write_text(_PRELUDE)
        assert _BASH is not None
        return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
            [_BASH, str(driver)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def calls_text(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    def backup_texts(self) -> list[str]:
        if not self.backups.is_dir():
            return []
        return [p.read_text() for p in sorted(self.backups.iterdir())]

    def text(self) -> str:
        return self.conf.read_text() if self.conf.exists() else ""

    def kex_line(self) -> str:
        m = re.search(r"^KexAlgorithms (.*)$", self.text(), re.MULTILINE)
        return m.group(1) if m else ""


def _upgrade_case(tmp: Path, failures: list[str]) -> None:
    box = _Box(tmp / "upgrade", _KEX_TRIXIE)
    box.conf.write_text(_EPOCH8)
    r = box.run()
    _check(r.returncode == 0, f"1: rc={r.returncode} {r.stderr}", failures)
    text = box.text()
    _check(
        box.kex_line()
        == "mlkem768x25519-sha256,sntrup761x25519-sha512,sntrup761x25519-sha512@openssh.com,"
        "curve25519-sha256,curve25519-sha256@libssh.org",
        f"1: kex is PQ-first: {box.kex_line()!r}",
        failures,
    )
    for line in ("HostKeyAlgorithms ssh-ed25519", "RequiredRSASize 3072", "PermitRootLogin no"):
        _check(f"\n{line}\n" in text, f"1: {line!r} missing", failures)
    _check(box.backup_texts() == [_EPOCH8], "1: backup holds the epoch-8 file", failures)
    _check("systemctl restart sshd" in box.calls_text(), "1: sshd restarted", failures)

    # 3. Rerun: nothing to do.
    box.calls.unlink(missing_ok=True)
    before = box.text()
    r = box.run()
    _check(r.returncode == 0, f"3: rerun rc={r.returncode} {r.stderr}", failures)
    _check(box.text() == before, "3: rerun leaves the file as is", failures)
    _check(len(box.backup_texts()) == 1, "3: rerun adds no backup", failures)
    _check("restart" not in box.calls_text(), "3: rerun restarts nothing", failures)


def _older_openssh_case(tmp: Path, failures: list[str]) -> None:
    box = _Box(tmp / "bookworm", _KEX_BOOKWORM, rsa_size=False)
    r = box.run()
    _check(r.returncode == 0, f"2: rc={r.returncode} {r.stderr}", failures)
    _check(
        box.kex_line()
        == "sntrup761x25519-sha512@openssh.com,curve25519-sha256,curve25519-sha256@libssh.org",
        f"2: kex follows ssh -Q kex: {box.kex_line()!r}",
        failures,
    )
    text = box.text()
    _check(text != "", "2: drop-in written", failures)
    _check("RequiredRSASize" not in text, "2: no RequiredRSASize pre-9.1", failures)
    _check("\n\n" not in text, "2: no blank line left behind", failures)


def _invalid_case(tmp: Path, failures: list[str]) -> None:
    box = _Box(tmp / "invalid-old", _KEX_TRIXIE, sshd_ok=False)
    box.conf.write_text(_EPOCH8)
    r = box.run()
    _check(r.returncode == 0, f"4: rc={r.returncode} {r.stderr}", failures)
    _check(box.text() == _EPOCH8, "4: previous file restored", failures)
    _check("restart" not in box.calls_text(), "4: no restart on invalid config", failures)

    box = _Box(tmp / "invalid-new", _KEX_TRIXIE, sshd_ok=False)
    r = box.run()
    _check(r.returncode == 0, f"4: fresh rc={r.returncode} {r.stderr}", failures)
    _check(not box.conf.exists(), "4: new file removed on invalid config", failures)


def _pi_harden_case(tmp: Path, failures: list[str]) -> None:
    box = _Box(tmp / "pi-harden", _KEX_TRIXIE)
    (box.conf_dir / "10-pi-harden.conf").write_text("KexAlgorithms curve25519-sha256\n")
    r = box.run()
    _check(r.returncode == 0, f"5: rc={r.returncode} {r.stderr}", failures)
    _check(not box.conf.exists(), "5: pi-harden box gets no mcapp drop-in", failures)
    _check(box.calls_text() == "", "5: pi-harden box: no sshd/systemctl calls", failures)


def _converge_row_case(failures: list[str]) -> None:
    m = re.search(
        r"^configure_ssh_hardening (\d+)$", (_LIB / "converge.sh").read_text(), re.MULTILINE
    )
    row = int(m.group(1)) if m else -1
    _check(row >= 10, f"6: converge row for configure_ssh_hardening is {row}, need >= 10", failures)


def run_bootstrap_ssh_hardening_tests() -> bool:
    if _BASH is None:
        print("  SKIP: bash not found")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-sshhard-") as d:
        tmp = Path(d)
        _upgrade_case(tmp, failures)
        _older_openssh_case(tmp, failures)
        _invalid_case(tmp, failures)
        _pi_harden_case(tmp, failures)
    _converge_row_case(failures)
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"Bootstrap SSH hardening tests: {len(failures)} failure(s)")
        return False
    print("Bootstrap SSH hardening tests: all passed")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_ssh_hardening_tests() else 1)
