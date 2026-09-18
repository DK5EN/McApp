"""Startup regression suite for the bootstrap's network-safety rules.

Background: doc/2026-09-18_2320-wifi-outage-postmortem.md. The in-session
``apt-get upgrade`` in ``install_apt_deps()`` replaced wpasupplicant, whose
postinst restarted the link the ssh session rode on; the new build made
NetworkManager try WPA3-SAE that the Zero 2 W cannot complete, and the box
was gone until the SD card was repaired offline. Four rules came out of it,
one function each in bootstrap/lib/packages.sh and bootstrap/lib/system.sh:

1. ``hold_network_packages`` / ``unhold_network_packages`` -- wpasupplicant
   and network-manager are held for the apt phase and a pending upgrade is
   reported, never applied inline. An operator's own hold is never released.
2. ``run_apt_detached`` -- apt runs as a transient systemd unit so a dropped
   session cannot interrupt dpkg; the exit status propagates; without systemd
   it runs inline.
3. ``snapshot_link_state`` / ``verify_link_state`` -- the default route must
   be back after the apt phase or the run stops with a diagnostic block.
4. ``configure_wpasupplicant_pin`` -- trixie only, idempotent by content,
   removes the hand-written predecessor; ``_mcapp_fstab_ensure_line`` puts the
   journal bind mount after the tmpfs line exactly once.

Like scripts/bootstrap_pinning_tests.py this drives the REAL bash functions
via subprocess against stub binaries placed first on PATH -- never a Python
re-implementation of the bash logic. Every stub logs its argv to a file the
test reads back. bash 3.2 (macOS) and bash 5 (Linux) both work.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_LIB = _REPO / "bootstrap" / "lib"
_BASH = shutil.which("bash")

# Minimal log_* shims so lib functions can be sourced without mcapp.sh.
_DRIVER_PRELUDE = """#!/bin/bash
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


def _check(ok: bool, label: str, failures: list[str]) -> None:
    if not ok:
        failures.append(label)


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp: Path, bin_dir: Path, script: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    driver = tmp / "driver.sh"
    driver.write_text(_DRIVER_PRELUDE + script)
    full_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp),
        "LIB_DIR": str(_LIB),
        "CALLS": str(tmp / "calls.log"),
        "LANG": "C",
    }
    if env:
        full_env.update(env)
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, str(driver)],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(tmp),
        timeout=60,
        check=False,
    )


def _calls(tmp: Path) -> list[str]:
    path = tmp / "calls.log"
    return path.read_text().splitlines() if path.exists() else []


# ── 1. hold / unhold ─────────────────────────────────────────────────────────


def _apt_stubs(
    bin_dir: Path, *, installed: tuple[str, ...], held: tuple[str, ...], pending: str
) -> None:
    inst = " ".join(installed)
    _write_stub(
        bin_dir,
        "dpkg-query",
        f'pkg="${{@: -1}}"; for p in {inst}; do '
        f'[ "$p" = "$pkg" ] && {{ printf installed; exit 0; }}; done; exit 1\n',
    )
    held_lines = "\n".join(held)
    _write_stub(
        bin_dir,
        "apt-mark",
        f'echo "apt-mark $*" >> "$CALLS"\n'
        f'if [ "$1" = showhold ]; then printf "%s\\n" "{held_lines}"; fi\nexit 0\n',
    )
    _write_stub(
        bin_dir, "apt-get", f'echo "apt-get $*" >> "$CALLS"\nprintf "%s\\n" "{pending}"\nexit 0\n'
    )


def _test_hold_and_report(tmp: Path, failures: list[str]) -> None:
    bin_dir = tmp / "bin1"
    bin_dir.mkdir()
    _apt_stubs(
        bin_dir,
        installed=("wpasupplicant",),
        held=(),
        pending="Inst wpasupplicant [2:2.10-24] (2:2.10-24+rpt1 Raspberry Pi:stable [arm64])",
    )
    result = _run(
        tmp,
        bin_dir,
        'hold_network_packages\necho "HELD=$MCAPP_TEMP_HELD"\n'
        'unhold_network_packages\necho "AFTER=$MCAPP_TEMP_HELD"\n',
    )
    calls = _calls(tmp)
    _check(
        result.returncode == 0,
        f"case 1: driver exit {result.returncode}: {result.stderr}",
        failures,
    )
    _check(
        "HELD=wpasupplicant" in result.stdout,
        "case 1: only the installed package is held",
        failures,
    )
    _check(
        "apt-mark hold wpasupplicant" in calls,
        "case 1: apt-mark hold called for wpasupplicant",
        failures,
    )
    _check(
        "apt-mark hold network-manager" not in calls,
        "case 1: a package that is not installed is not held",
        failures,
    )
    _check(
        "apt-mark unhold wpasupplicant" in calls,
        "case 1: our hold is released afterwards",
        failures,
    )
    _check("AFTER=" in result.stdout, "case 1: MCAPP_TEMP_HELD cleared after unhold", failures)
    _check(
        "Deferred" in result.stderr and "2:2.10-24+rpt1" in result.stderr,
        "case 1: pending upgrade is reported",
        failures,
    )
    _check(
        "apt-get install --only-upgrade wpasupplicant network-manager" in result.stderr,
        "case 1: console command shown",
        failures,
    )


def _test_operator_hold_untouched(tmp: Path, failures: list[str]) -> None:
    bin_dir = tmp / "bin2"
    bin_dir.mkdir()
    (tmp / "calls.log").unlink(missing_ok=True)
    _apt_stubs(
        bin_dir, installed=("wpasupplicant", "network-manager"), held=("wpasupplicant",), pending=""
    )
    result = _run(
        tmp,
        bin_dir,
        'hold_network_packages\necho "HELD=$MCAPP_TEMP_HELD"\nunhold_network_packages\n',
    )
    calls = _calls(tmp)
    _check(
        result.returncode == 0,
        f"case 2: driver exit {result.returncode}: {result.stderr}",
        failures,
    )
    _check(
        "HELD=network-manager" in result.stdout,
        "case 2: only the package without an operator hold is held",
        failures,
    )
    _check(
        "apt-mark hold wpasupplicant" not in calls,
        "case 2: operator's hold is not re-issued",
        failures,
    )
    _check(
        "apt-mark unhold wpasupplicant" not in calls,
        "case 2: operator's hold is never released",
        failures,
    )
    _check("apt-mark unhold network-manager" in calls, "case 2: our own hold is released", failures)
    _check("Deferred" not in result.stderr, "case 2: nothing pending, nothing reported", failures)


# ── 2. run_apt_detached ──────────────────────────────────────────────────────


def _test_run_apt_detached(tmp: Path, failures: list[str]) -> None:
    bin_dir = tmp / "bin3"
    bin_dir.mkdir()
    (tmp / "calls.log").unlink(missing_ok=True)
    # Stub systemd-run: record options, then exec the command after them.
    _write_stub(
        bin_dir,
        "systemd-run",
        'echo "systemd-run $*" >> "$CALLS"\n'
        'while [ $# -gt 0 ]; do case "$1" in '
        '--*|-p) [ "$1" = -p ] && shift; shift ;; *) break ;; esac; done\n'
        'exec "$@"\n',
    )
    systemd_dir = tmp / "systemd"
    systemd_dir.mkdir()
    log = tmp / "bootstrap.log"
    script = (
        'run_apt_detached true && echo "OK_TRUE"\n'
        'run_apt_detached false; echo "RC_FALSE=$?"\n'
        'MCAPP_SYSTEMD_RUNTIME_DIR=/nonexistent run_apt_detached true && echo "OK_INLINE"\n'
    )
    result = _run(
        tmp,
        bin_dir,
        script,
        env={"MCAPP_SYSTEMD_RUNTIME_DIR": str(systemd_dir), "MCAPP_BOOTSTRAP_LOG": str(log)},
    )
    calls = _calls(tmp)
    _check("OK_TRUE" in result.stdout, "case 3: success propagates through systemd-run", failures)
    _check("RC_FALSE=1" in result.stdout, "case 3: failure exit status propagates", failures)
    _check("apt failed (exit 1)" in result.stderr, "case 3: failure is logged", failures)
    _check("OK_INLINE" in result.stdout, "case 3: inline fallback without systemd", failures)
    _check(
        sum(1 for c in calls if c.startswith("systemd-run")) == 2,
        "case 3: systemd-run used exactly twice",
        failures,
    )
    _check(
        any("--wait" in c and "--collect" in c and f"append:{log}" in c for c in calls),
        "case 3: --wait, --collect and log append are set",
        failures,
    )
    _check(
        any("DEBIAN_FRONTEND=noninteractive" in c for c in calls),
        "case 3: noninteractive frontend passed",
        failures,
    )


# ── 3. link guard ────────────────────────────────────────────────────────────


def _test_link_guard(tmp: Path, failures: list[str]) -> None:
    bin_dir = tmp / "bin4"
    bin_dir.mkdir()
    route_flag = tmp / "route-up"
    route_flag.write_text("1")
    _write_stub(
        bin_dir,
        "ip",
        f'[ -f "{route_flag}" ] && '
        'echo "default via 192.168.68.1 dev wlan0 proto dhcp src 192.168.68.74 metric 600"\n'
        "exit 0\n",
    )
    _write_stub(
        bin_dir, "wpa_cli", 'printf "ssid=ORBI63\\nkey_mgmt=WPA2-PSK\\nwpa_state=COMPLETED\\n"\n'
    )
    _write_stub(
        bin_dir,
        "journalctl",
        'echo "NetworkManager[1]: device (wlan0): '
        'supplicant interface state: associating -> disconnected"\n',
    )
    script = (
        'snapshot_link_state\necho "IFACE=$MCAPP_LINK_IFACE KEY=$MCAPP_LINK_KEYMGMT"\n'
        'verify_link_state && echo "VERIFY1_OK"\n'
        f'rm -f "{route_flag}"\n'
        'verify_link_state; echo "VERIFY2_RC=$?"\n'
        'MCAPP_LINK_IFACE="" verify_link_state && echo "VERIFY3_OK"\n'
    )
    result = _run(tmp, bin_dir, script, env={"MCAPP_LINK_WAIT_S": "0"})
    _check(
        "IFACE=wlan0 KEY=WPA2-PSK" in result.stdout,
        "case 4: snapshot records iface and key_mgmt",
        failures,
    )
    _check("VERIFY1_OK" in result.stdout, "case 4: link still up passes", failures)
    _check("VERIFY2_RC=1" in result.stdout, "case 4: lost default route fails the phase", failures)
    _check(
        "Network link lost during the apt phase: wlan0 (WPA2-PSK)" in result.stderr,
        "case 4: diagnostic names the lost link",
        failures,
    )
    _check(
        "associating -> disconnected" in result.stderr, "case 4: journal tail is printed", failures
    )
    _check("VERIFY3_OK" in result.stdout, "case 4: offline before apt is not a failure", failures)


# ── 4. wpasupplicant pin + journal bind line ─────────────────────────────────


def _test_wpasupplicant_pin(tmp: Path, failures: list[str]) -> None:
    bin_dir = tmp / "bin5"
    bin_dir.mkdir()
    prefs = tmp / "prefs"
    prefs.mkdir()
    (prefs / "wpasupplicant-pin").write_text("legacy\n")
    _write_stub(bin_dir, "lsb_release", 'echo "${CODENAME:-trixie}"\n')
    script = "configure_wpasupplicant_pin\nconfigure_wpasupplicant_pin\n"
    result = _run(tmp, bin_dir, script, env={"MCAPP_APT_PREFERENCES_DIR": str(prefs)})
    pin = prefs / "mcapp-wpasupplicant"
    _check(
        result.returncode == 0,
        f"case 5: driver exit {result.returncode}: {result.stderr}",
        failures,
    )
    _check(pin.exists(), "case 5: pin file written on trixie", failures)
    text = pin.read_text() if pin.exists() else ""
    _check(
        "Pin: version 2:2.10-24\n" in text and "Pin-Priority: 1001\n" in text,
        "case 5: pin content",
        failures,
    )
    _check("raspberrypi/linux#7634" in text, "case 5: pin carries the reason", failures)
    _check(result.stdout.count("pinned to 2:2.10-24") == 1, "case 5: written once", failures)
    _check("already in place" in result.stdout, "case 5: second run is a no-op", failures)
    _check(
        not (prefs / "wpasupplicant-pin").exists(),
        "case 5: hand-written predecessor removed",
        failures,
    )
    if pin.exists():
        _check(stat.S_IMODE(pin.stat().st_mode) == 0o644, "case 5: mode 0644", failures)

    # bookworm: no pin
    prefs2 = tmp / "prefs2"
    prefs2.mkdir()
    result = _run(
        tmp,
        bin_dir,
        "configure_wpasupplicant_pin\n",
        env={"MCAPP_APT_PREFERENCES_DIR": str(prefs2), "CODENAME": "bookworm"},
    )
    _check(not (prefs2 / "mcapp-wpasupplicant").exists(), "case 5: no pin on bookworm", failures)
    _check("no pin needed" in result.stdout, "case 5: bookworm says why", failures)


def _test_fstab_bind_line(tmp: Path, failures: list[str]) -> None:
    bin_dir = tmp / "bin6"
    bin_dir.mkdir()
    fstab = tmp / "fstab"
    fstab.write_text(
        "proc /proc proc defaults 0 0\n"
        "PARTUUID=5922ef7f-02 / ext4 defaults,noatime 0 1\n"
        "\n# McApp tmpfs - begin\n"
        "tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,noexec,size=30M 0 0\n"
        "tmpfs /tmp tmpfs defaults,noatime,nosuid,mode=1777,size=150M 0 0\n"
        "# McApp tmpfs - end\n"
    )
    script = (
        f'_mcapp_fstab_ensure_line "{fstab}" "$MCAPP_JOURNAL_BIND_LINE" '
        '"^tmpfs /var/log tmpfs" && echo "ONE"\n'
        f'_mcapp_fstab_ensure_line "{fstab}" "$MCAPP_JOURNAL_BIND_LINE" '
        '"^tmpfs /var/log tmpfs" && echo "TWO"\n'
    )
    result = _run(tmp, bin_dir, script)
    lines = fstab.read_text().splitlines()
    bind = "/var/lib/mcapp/journal /var/log/journal none bind 0 0"
    _check(
        "ONE" in result.stdout and "TWO" in result.stdout,
        "case 6: helper returns 0 both times",
        failures,
    )
    _check(
        lines.count(bind) == 1, "case 6: bind line present exactly once after two runs", failures
    )
    idx = lines.index(bind) if bind in lines else -1
    _check(
        idx > 0 and lines[idx - 1].startswith("tmpfs /var/log tmpfs"),
        "case 6: bind line directly after the /var/log tmpfs line",
        failures,
    )
    _check(lines[-1] == "# McApp tmpfs - end", "case 6: block end marker intact", failures)

    # journald config through the same helper, with all paths redirected.
    conf_dir = tmp / "journald.d"
    conf_dir.mkdir()
    (conf_dir / "mcapp-volatile.conf").write_text("[Journal]\nStorage=volatile\nRuntimeMaxUse=8M\n")
    journal_dir = tmp / "journal"
    _write_stub(bin_dir, "systemctl", "exit 0\n")
    _write_stub(bin_dir, "mountpoint", "exit 1\n")
    _write_stub(bin_dir, "mount", "exit 0\n")
    _write_stub(bin_dir, "chown", "exit 0\n")
    result = _run(
        tmp,
        bin_dir,
        "configure_journald\nconfigure_journald\n",
        env={
            "MCAPP_FSTAB": str(fstab),
            "MCAPP_JOURNALD_CONF_DIR": str(conf_dir),
            "MCAPP_JOURNAL_DIR": str(journal_dir),
        },
    )
    conf = conf_dir / "mcapp-journal.conf"
    text = conf.read_text() if conf.exists() else ""
    _check(
        result.returncode == 0,
        f"case 6: journald driver exit {result.returncode}: {result.stderr}",
        failures,
    )
    _check(
        "Storage=persistent\n" in text and "SystemMaxUse=16M\n" in text,
        "case 6: persistent journald drop-in",
        failures,
    )
    _check(
        not (conf_dir / "mcapp-volatile.conf").exists(),
        "case 6: volatile drop-in removed",
        failures,
    )
    _check(journal_dir.is_dir(), "case 6: journal dir created", failures)
    _check(
        "already configured" in result.stdout,
        "case 6: second configure_journald is a no-op",
        failures,
    )
    _check(
        fstab.read_text().splitlines().count(bind) == 1,
        "case 6: configure_journald did not duplicate the bind line",
        failures,
    )


def run_bootstrap_network_safety_tests() -> bool:
    if _BASH is None:
        print("bootstrap_network_safety: SKIP (no bash)")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-netsafe-") as td:
        tmp = Path(td)
        _test_hold_and_report(tmp, failures)
        _test_operator_hold_untouched(tmp, failures)
        _test_run_apt_detached(tmp, failures)
        _test_link_guard(tmp, failures)
        _test_wpasupplicant_pin(tmp, failures)
        _test_fstab_bind_line(tmp, failures)
    for f in failures:
        print(f"  FAIL: {f}")
    print(f"bootstrap_network_safety: {len(failures)} failure(s) across 6 cases")
    return not failures


if __name__ == "__main__":
    ok = run_bootstrap_network_safety_tests()
    raise SystemExit(0 if ok else 1)
