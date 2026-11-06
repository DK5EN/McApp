"""Startup regression suite for configure_logdirs in bootstrap/lib/system.sh.

McApp puts /var/log on tmpfs; mcapp.conf only recreates the folders McApp
knows, so another service's log folder (nginx) vanishes on reboot and its unit
stays failed. ``configure_logdirs`` installs a generator script, a oneshot
service and an hourly timer; the generator writes
``/etc/tmpfiles.d/mcapp-logdirs.conf`` with one ``d`` line per folder.

Rules pinned here, against the REAL bash functions with stub binaries
(stat, systemctl, systemd-tmpfiles) on PATH and every path redirected:

1. A real folder gets its actual mode/owner/group; real values beat the
   packaged default (0755 root adm); a gone packaged folder gets the default.
   A UID/GID without a name (stat prints UNKNOWN) is written numerically.
2. ``.log`` entries and nested paths from dpkg lists, mcapp.conf's folders,
   symlinks and packaged names that exist as plain files are skipped; names
   outside ``[A-Za-z0-9._@+-]`` (tmpfiles expands ``%``, blanks and quotes
   break a line) never reach the file.
3. The conf is rewritten and systemd-tmpfiles run only on change.
4. A ramlog board installs nothing and removes a prior install.
5. No failure of systemctl / systemd-tmpfiles aborts a ``set -eo pipefail``
   plus ``set -u`` caller.
6. A rerun of configure_logdirs changes nothing (no daemon-reload, no backup).
7. First install: configure_tmpfs runs the generator BEFORE the /var/log mount
   (the stub mount really hides the folders), then runs
   ``systemd-tmpfiles --create`` on both files AFTER it; a failing mount or
   tmpfiles never aborts.

Set BOOTSTRAP_LOGDIRS_LIB_DIR to point the suite at another lib dir (used to
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
_LIB = Path(os.environ.get("BOOTSTRAP_LOGDIRS_LIB_DIR", str(_REPO / "bootstrap" / "lib")))
_BASH = shutil.which("bash")

_PRELUDE = """#!/bin/bash
set -eo pipefail
set -u
log_info() { echo "[INFO] $*"; }
log_ok() { echo "[OK] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }
source "$LIB_DIR/detect.sh"
source "$LIB_DIR/system.sh"
"""

_MCAPP_CONF = (
    "d /var/log/mcapp     0755 root     root     -\n"
    "d /var/log/lighttpd  0755 www-data www-data -\n"
    "d /var/log/journal   0755 root     root     -\n"
    "f /var/log/wtmp                0664 root utmp -\n"
)
_NGINX_REAL = "d {root}/nginx 0755 www-data adm -"


def _check(ok: bool, label: str, failures: list[str]) -> None:
    if not ok:
        failures.append(label)


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _Board:
    def __init__(self, tmp: Path, name: str, ramlog: str | None = None) -> None:
        self.root = tmp / name
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        self.log = self.root / "varlog"
        self.dpkg = self.root / "dpkg"
        self.tmpfiles = self.root / "tmpfiles.d"
        self.units = self.root / "units"
        self.sbin = self.root / "sbin"
        self.backups = self.root / "backups"
        self.calls = self.root / "calls.log"
        self.statmap = self.root / "stat.map"
        for d in (self.log, self.dpkg, self.tmpfiles, self.units, self.sbin, self.root / "default"):
            d.mkdir()
        self.statmap.write_text("")
        self.calls.write_text("")
        (self.tmpfiles / "mcapp.conf").write_text(_MCAPP_CONF)
        enabled = ramlog or "none.service"
        _write_stub(
            self.bin,
            "systemctl",
            'echo "systemctl $*" >> "$CALLS"\n'
            '[ -n "${FAIL_SYSTEMCTL:-}" ] && exit 1\n'
            'if [ "$1" = "is-enabled" ]; then\n'
            f'  if [ "$2" = "{enabled}" ]; then echo enabled; exit 0; fi\n'
            "  exit 1\nfi\nexit 0\n",
        )
        _write_stub(
            self.bin,
            "systemd-tmpfiles",
            'echo "tmpfiles $*" >> "$CALLS"\n[ -n "${FAIL_TMPFILES:-}" ] && exit 1\n'
            # Like the real tool: `d` lines (re)create their folder. Only below the sandbox root.
            'if [ "$1" = --create ] && [ -r "$2" ]; then\n'
            '  awk \'$1 == "d" { print $2 }\' "$2" | while IFS= read -r p; do\n'
            '    case "$p" in "$MCAPP_LOG_ROOT"/*) mkdir -p "$p" ;; esac\n'
            "  done\nfi\nexit 0\n",
        )
        # stat -c '%a %u %g %U %G' <dir>: answer from the fixed map by basename
        # (tab-separated, so names with blanks work). No entry: stat fails.
        _write_stub(
            self.bin,
            "stat",
            'fmt="$2"; name="${!#}"; name="${name##*/}"\n'
            'line="$(N="$name" awk -F"\\t" \'$1 == ENVIRON["N"] { print $2; found = 1; exit }'
            ' END { exit !found }\' "$STATMAP")" || exit 1\n'
            "set -- $line\n"  # mode uid gid owner group
            # The pre-rework generator asked for '%a %U %G'; answer that too.
            'if [ "$fmt" = "%a %U %G" ]; then echo "$1 $4 $5"; else echo "$line"; fi\n',
        )
        _write_stub(self.bin, "date", "echo 20260101\n")
        # mount /var/log "mounts a tmpfs": the folders under MCAPP_LOG_ROOT vanish
        # (moved aside), exactly what makes a generator run after it see nothing.
        _write_stub(
            self.bin,
            "mount",
            'echo "mount $*" >> "$CALLS"\n'
            'if [ "${!#}" = /var/log ]; then\n'
            '  [ -n "${FAIL_MOUNT:-}" ] && exit 1\n'
            '  hidden="${MCAPP_LOG_ROOT}.hidden"; mkdir -p "$hidden"\n'
            '  for f in "$MCAPP_LOG_ROOT"/*; do\n'
            '    if [ -e "$f" ] || [ -L "$f" ]; then mv "$f" "$hidden/"; fi\n'
            "  done\n"
            "fi\nexit 0\n",
        )
        for stub, code in (("findmnt", 1), ("mountpoint", 1), ("chown", 0)):
            _write_stub(self.bin, stub, f"exit {code}\n")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.root),
            "LIB_DIR": str(_LIB),
            "CALLS": str(self.calls),
            "STATMAP": str(self.statmap),
            "LANG": "C",
            "MCAPP_FSTAB": str(self.root / "fstab"),
            "MCAPP_REBOOT_MARKER": str(self.root / "state" / "reboot-required"),
            "MCAPP_LOG_ROOT": str(self.log),
            "MCAPP_DPKG_INFO": str(self.dpkg),
            "MCAPP_TMPFILES_DIR": str(self.tmpfiles),
            "MCAPP_SYSTEMD_UNIT_DIR": str(self.units),
            "MCAPP_SBIN_DIR": str(self.sbin),
            "MCAPP_CONFIG_BACKUP_DIR": str(self.backups),
            "MCAPP_DEFAULTS_DIR": str(self.root / "default"),
        }

    def real(
        self, name: str, mode: str, owner: str, group: str, ids: tuple[str, str] = ("1000", "1000")
    ) -> None:
        (self.log / name).mkdir(exist_ok=True)
        self.stat_entry(name, mode, owner, group, ids)

    def stat_entry(
        self, name: str, mode: str, owner: str, group: str, ids: tuple[str, str] = ("1000", "1000")
    ) -> None:
        with self.statmap.open("a") as fh:
            fh.write(f"{name}\t{mode} {ids[0]} {ids[1]} {owner} {group}\n")

    def pkg(self, pkg: str, *paths: str) -> None:
        (self.dpkg / f"{pkg}.list").write_text("".join(p + "\n" for p in paths))

    @property
    def conf(self) -> Path:
        return self.tmpfiles / "mcapp-logdirs.conf"

    @property
    def gen(self) -> Path:
        return self.sbin / "mcapp-logdirs"

    def calls_text(self) -> str:
        return self.calls.read_text()

    def run(self, script: str, **extra: str) -> subprocess.CompletedProcess[str]:
        driver = self.root / "driver.sh"
        driver.write_text(_PRELUDE + script)
        assert _BASH is not None
        return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
            [_BASH, str(driver)],
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def lines(self) -> list[str]:
        if not self.conf.exists():
            return []
        return [ln for ln in self.conf.read_text().splitlines() if ln.startswith("d ")]


def _test_content(tmp: Path, failures: list[str]) -> None:
    b = _Board(tmp, "content")
    b.real("nginx", "755", "www-data", "adm")
    b.real("both", "750", "svc", "svcgrp")
    b.real("mcapp", "755", "root", "root")  # in mcapp.conf: skipped
    b.real("lighttpd", "755", "www-data", "www-data")
    (b.log / "linked").symlink_to(b.root)
    # stat WOULD answer for the symlink: only the [ -L ] check keeps it out.
    b.stat_entry("linked", "755", "root", "adm")
    b.pkg(
        "nginx-common",
        "/var/log/nginx",
        "/var/log/gone",
        "/var/log/both",
        "/var/log/nginx/error.log",
        "/var/log/x/y",
        "/var/log/foo.log",
        "/var/log/journal",
        "/var/log/linked",
    )
    r = b.run("configure_logdirs\necho after\n")
    _check(
        r.returncode == 0 and "after" in r.stdout,
        f"content: rc 0 ({r.returncode}) {r.stderr}",
        failures,
    )
    lines = b.lines()
    root = str(b.log)
    _check(
        _NGINX_REAL.format(root=root) in lines,
        f"1 real nginx line exact: {lines}",
        failures,
    )
    _check(
        f"d {root}/gone 0755 root adm -" in lines,
        "2 gone packaged folder gets 0755 root adm",
        failures,
    )
    _check(
        f"d {root}/both 0750 svc svcgrp -" in lines, "3 real values beat package default", failures
    )
    _check(
        not any(x in ln for ln in lines for x in ("foo.log", "/x", "error.log")),
        f"4 .log entries and nested paths skipped: {lines}",
        failures,
    )
    _check(
        not any(
            f"{root}/{n} " in ln for ln in lines for n in ("mcapp", "lighttpd", "journal", "linked")
        ),
        f"5 mcapp.conf folders and symlinks skipped: {lines}",
        failures,
    )
    _check(lines == sorted(lines), "sorted output", failures)
    _check(
        f"tmpfiles --create {b.conf}" in b.calls_text(),
        "tmpfiles called with the generated file on first run",
        failures,
    )

    # 6: second run, nothing changed.
    if not b.conf.exists():
        failures.append("6 conf missing after first run")
        return
    before = b.conf.stat().st_mtime_ns
    n_calls = b.calls_text().count("tmpfiles --create")
    r = b.run(f'"{b.gen}"\n')
    _check(r.returncode == 0, "6 second generator run rc 0", failures)
    _check(b.conf.stat().st_mtime_ns == before, "6 unchanged: conf not rewritten", failures)
    _check(
        b.calls_text().count("tmpfiles --create") == n_calls,
        "6 unchanged: tmpfiles not called",
        failures,
    )

    # 7: new folder.
    b.real("newsvc", "755", "root", "adm")
    r = b.run(f'"{b.gen}"\n')
    _check(f"d {root}/newsvc 0755 root adm -" in b.lines(), "7 new folder added", failures)
    _check(
        b.calls_text().count("tmpfiles --create") == n_calls + 1,
        "7 new folder: tmpfiles called once more",
        failures,
    )

    # 10: unit text.
    timer = (b.units / "mcapp-logdirs.timer").read_text()
    svc = (b.units / "mcapp-logdirs.service").read_text()
    _check("OnBootSec=5min" in timer and "OnUnitActiveSec=1h" in timer, "10 timer lines", failures)
    _check("WantedBy=timers.target" in timer, "10 timer install section", failures)
    _check(
        f"ExecStart={b.gen}" in svc and "Type=oneshot" in svc, "10 service runs script", failures
    )
    _check(
        bool(b.gen.stat().st_mode & stat.S_IXUSR) and oct(b.gen.stat().st_mode & 0o777) == "0o755",
        "10 generator mode 0755",
        failures,
    )
    _check("enable --now mcapp-logdirs.timer" in b.calls_text(), "10 timer enabled", failures)
    bash_n = subprocess.run(  # noqa: S603 - fixed argv
        [str(_BASH), "-n", str(b.gen)],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    _check(bash_n.returncode == 0, "generator passes bash -n", failures)


def _test_from_tmpfs(tmp: Path, failures: list[str]) -> None:
    b = _Board(tmp, "fromtmpfs")
    (b.root / "fstab").write_text("proc /proc proc defaults 0 0\n")
    b.real("nginx", "755", "www-data", "adm")
    r = b.run("configure_tmpfs\necho after\n")
    _check(
        r.returncode == 0 and "after" in r.stdout, f"configure_tmpfs rc 0 ({r.stderr})", failures
    )
    _check(b.gen.exists(), "configure_tmpfs installs the generator", failures)
    _check(any("/nginx " in ln for ln in b.lines()), "configure_tmpfs writes the conf", failures)


def _test_first_install(tmp: Path, failures: list[str]) -> None:
    """7: the generator must see the folders BEFORE the tmpfs mount hides them."""
    b = _Board(tmp, "firstinstall")
    (b.root / "fstab").write_text("proc /proc proc defaults 0 0\n")
    b.real("nginx", "755", "www-data", "adm", ("33", "4"))
    b.real("custom", "2750", "www-data", "adm", ("33", "4"))
    snap = b.tmpfiles / "snap"
    r = b.run(
        "configure_tmpfs\n"
        f'cp "{b.conf}" "{snap}" 2>/dev/null || true\n'
        "configure_logdirs\n"  # as setup_system does right after configure_tmpfs
        "echo after\n"
    )
    _check(
        r.returncode == 0 and "after" in r.stdout,
        f"7 first install: rc 0 ({r.returncode}) {r.stderr}",
        failures,
    )
    root = str(b.log)
    _check(
        (b.root / "varlog.hidden" / "nginx").is_dir(),
        "7 instrument: the mount stub really hid the folders",
        failures,
    )
    want = [f"d {root}/custom 2750 www-data adm -", f"d {root}/nginx 0755 www-data adm -"]
    snap_lines = (
        [ln for ln in snap.read_text().splitlines() if ln.startswith("d ")] if snap.exists() else []
    )
    _check(
        snap_lines == want,
        f"7 first install: conf right after configure_tmpfs has the real folders ({snap_lines})",
        failures,
    )
    _check(b.lines() == want, f"7 first install: second call keeps them ({b.lines()})", failures)
    calls = b.calls_text().splitlines()
    mounted = [i for i, ln in enumerate(calls) if ln.startswith("mount /var/log")]
    _check(len(mounted) == 1, f"7 first install: one /var/log mount ({mounted})", failures)
    if len(mounted) != 1:
        return
    after_mount = calls[mounted[0] + 1 :]
    for conf in (b.tmpfiles / "mcapp.conf", b.conf):
        _check(
            f"tmpfiles --create {conf}" in after_mount,
            f"7 first install: systemd-tmpfiles --create {conf.name} runs after the mount",
            failures,
        )
    _check(
        calls.count("systemctl daemon-reload") == 1,
        "7 first install: the second configure_logdirs call is idempotent (one daemon-reload)",
        failures,
    )


def _test_names(tmp: Path, failures: list[str]) -> None:
    """Names tmpfiles would expand or split never reach the file; orphan ids are numeric."""
    b = _Board(tmp, "names")
    bad_real = ("my dir", "100%s", "it's", 'q"x')
    for n in bad_real:
        b.real(n, "755", "root", "adm")
    b.real("ok.d@x+y-z_1", "755", "root", "adm")
    b.real("orphan", "755", "UNKNOWN", "UNKNOWN", ("4242", "4343"))
    b.real("halfgrp", "750", "svc", "UNKNOWN", ("999", "4344"))
    (b.log / "regfile").write_text("plain file\n")
    b.stat_entry("regfile", "644", "root", "adm")
    bad_pkg = ("pct%x", "it's2", 'q"y')
    b.pkg(
        "pkg",
        *(f"/var/log/{n}" for n in (*bad_pkg, "okpkg+1", "regfile", "gonepkg")),
        "/var/log/with space",
    )
    r = b.run("configure_logdirs\necho after\n")
    _check(r.returncode == 0 and "after" in r.stdout, f"names: rc 0 ({r.stderr})", failures)
    lines = b.lines()
    root = str(b.log)
    _check(
        not any(n in ln for ln in lines for n in (*bad_real, *bad_pkg, "with space")),
        f"b names with a blank, %, ' or \" produce no line: {lines}",
        failures,
    )
    _check(
        f"d {root}/ok.d@x+y-z_1 0755 root adm -" in lines
        and f"d {root}/okpkg+1 0755 root adm -" in lines,
        f"b the allowed set . @ + - _ digits still passes: {lines}",
        failures,
    )
    _check(
        f"d {root}/orphan 0755 4242 4343 -" in lines,
        f"d orphan UID and GID are written numerically: {lines}",
        failures,
    )
    _check(
        f"d {root}/halfgrp 0750 svc 4344 -" in lines,
        f"d only the unnamed half is numeric: {lines}",
        failures,
    )
    _check(
        not any("UNKNOWN" in ln for ln in lines), f"d no UNKNOWN in the output: {lines}", failures
    )
    _check(
        not any(f"{root}/regfile " in ln for ln in lines),
        f"d a packaged name that is a regular file gets no line: {lines}",
        failures,
    )
    _check(
        f"d {root}/gonepkg 0755 root adm -" in lines,
        "d gone packaged folder still listed",
        failures,
    )


def _test_merge(tmp: Path, failures: list[str]) -> None:
    """A scan of an empty tmpfs must never lose what an earlier scan captured."""
    b = _Board(tmp, "merge")
    b.real("custom", "750", "1234", "adm", ("1234", "4"))
    b.real("keep", "2750", "www-data", "adm", ("33", "4"))
    b.pkg("pkg", "/var/log/keep", "/var/log/newpkg")
    r = b.run("configure_logdirs\n")
    root = str(b.log)
    want_custom = f"d {root}/custom 0750 1234 adm -"
    want_keep = f"d {root}/keep 2750 www-data adm -"
    before = b.lines()
    _check(
        r.returncode == 0 and want_custom in before and want_keep in before,
        f"merge: first scan captures both: {before}",
        failures,
    )
    # tmpfiles failed, or a scan landed right after the mount: the folders are gone.
    for name in ("custom", "keep"):
        (b.log / name).rmdir()
    b.statmap.write_text("")
    r = b.run(f'"{b.gen}"\n', FAIL_TMPFILES="1")
    after = b.lines()
    _check(
        want_custom in after, f"merge: unpackaged line survives an empty scan: {after}", failures
    )
    _check(want_keep in after, f"merge: owner not downgraded to the default: {after}", failures)
    _check(
        f"d {root}/newpkg 0755 root adm -" in after,
        f"merge: a new packaged folder is still added: {after}",
        failures,
    )
    # A real folder beats the old line.
    b.real("custom", "700", "root", "root", ("0", "0"))
    b.run(f'"{b.gen}"\n')
    after = b.lines()
    _check(
        f"d {root}/custom 0700 root root -" in after and want_custom not in after,
        f"merge: the real folder wins over the old line: {after}",
        failures,
    )
    # A path that has become a plain file loses its old line.
    (b.log / "custom").rmdir()
    (b.log / "custom").write_text("plain\n")
    b.run(f'"{b.gen}"\n')
    _check(
        not any("/custom " in ln for ln in b.lines()),
        f"merge: an old line for what is now a file is dropped: {b.lines()}",
        failures,
    )
    # An owner name with a blank (directory services) is written numerically, not dropped.
    b = _Board(tmp, "merge-blank")
    b.real("ad", "750", "DOM user", "dom grp", ("5000", "5001"))
    b.run("configure_logdirs\n")
    _check(
        f"d {b.log}/ad 0750 5000 5001 -" in b.lines(),
        f"owner with a blank: numeric, not dropped: {b.lines()}",
        failures,
    )


def _test_mount_failures(tmp: Path, failures: list[str]) -> None:
    """e: a failing mount, or a failing tmpfiles after it, never aborts the caller."""
    b = _Board(tmp, "mountfail")
    (b.root / "fstab").write_text("proc /proc proc defaults 0 0\n")
    b.real("nginx", "755", "www-data", "adm")
    r = b.run("configure_tmpfs\necho after\n", FAIL_MOUNT="1")
    _check(
        r.returncode == 0 and "after" in r.stdout,
        f"e mount failing: rc 0 and the next line runs ({r.returncode}) {r.stderr}",
        failures,
    )
    _check(
        "deferred to next reboot" in r.stderr, "e mount failing: reported as a warning", failures
    )
    _check(
        any("/nginx " in ln for ln in b.lines()),
        "e mount failing: the generator already ran before the mount",
        failures,
    )

    c = _Board(tmp, "tmpfilesfail")
    (c.root / "fstab").write_text("proc /proc proc defaults 0 0\n")
    c.real("nginx", "755", "www-data", "adm")
    r = c.run("configure_tmpfs\necho after\n", FAIL_TMPFILES="1")
    _check(
        r.returncode == 0 and "after" in r.stdout,
        f"e tmpfiles failing after the mount: rc 0 ({r.returncode}) {r.stderr}",
        failures,
    )
    _check(
        "after the /var/log mount failed" in r.stderr,
        "e tmpfiles failing after the mount: reported as a warning",
        failures,
    )


def _test_empty(tmp: Path, failures: list[str]) -> None:
    b = _Board(tmp, "empty")
    shutil.rmtree(b.dpkg)  # unreadable / missing dpkg dir
    r = b.run("configure_logdirs\necho after\n")
    _check(r.returncode == 0 and "after" in r.stdout, "empty: rc 0 with no sources", failures)
    _check(b.conf.exists() and b.lines() == [], "empty: header-only conf, no d lines", failures)


def _test_ramlog(tmp: Path, failures: list[str]) -> None:
    b = _Board(tmp, "ramlog-fresh", ramlog="orangepi-ramlog.service")
    r = b.run("configure_logdirs\necho after\n")
    _check(r.returncode == 0 and "after" in r.stdout, "8 ramlog: rc 0", failures)
    _check(not b.gen.exists(), "8 ramlog: no generator installed", failures)
    _check(not list(b.units.iterdir()), "8 ramlog: no units installed", failures)
    _check(not b.conf.exists(), "8 ramlog: no conf", failures)

    # Prior install on a board that later turned out to be ramlog.
    p = _Board(tmp, "ramlog-prior")
    p.real("nginx", "755", "www-data", "adm")
    p.run("configure_logdirs\n")
    _check(p.gen.exists() and p.conf.exists(), "8 prior install present first", failures)
    # Same dirs, now with the ramlog unit enabled.
    _write_stub(
        p.bin,
        "systemctl",
        'echo "systemctl $*" >> "$CALLS"\n'
        'if [ "$1" = "is-enabled" ] && [ "$2" = "armbian-ramlog.service" ]; then\n'
        "  echo enabled; exit 0\nfi\n"
        "exit 0\n",
    )
    r = p.run("configure_logdirs\necho after\n")
    _check(r.returncode == 0 and "after" in r.stdout, "8 prior: rc 0", failures)
    _check(not p.gen.exists(), "8 prior: generator removed", failures)
    _check(
        not (p.units / "mcapp-logdirs.timer").exists()
        and not (p.units / "mcapp-logdirs.service").exists(),
        "8 prior: units removed",
        failures,
    )
    _check(not p.conf.exists(), "8 prior: generated conf removed", failures)
    _check(
        "disable --now mcapp-logdirs.timer" in p.calls_text(), "8 prior: timer disabled", failures
    )


def _test_failures(tmp: Path, failures: list[str]) -> None:
    b = _Board(tmp, "failing")
    b.real("nginx", "755", "www-data", "adm")
    r = b.run(
        "configure_logdirs\necho after\n",
        FAIL_SYSTEMCTL="1",
        FAIL_TMPFILES="1",
    )
    _check(r.returncode == 0, f"9 failing tools: rc 0 ({r.returncode}) {r.stderr}", failures)
    _check("after" in r.stdout, "9 failing tools: next line runs", failures)
    _check(bool(b.lines()), "9 failing tools: conf still written", failures)

    # Unwritable tmpfiles dir: warn and continue.
    c = _Board(tmp, "unwritable")
    c.tmpfiles.chmod(0o500)
    try:
        r = c.run("configure_logdirs\necho after\n")
        _check(
            r.returncode == 0 and "after" in r.stdout,
            "9 unwritable tmpfiles dir: continues",
            failures,
        )
    finally:
        c.tmpfiles.chmod(0o700)


def _test_rerun(tmp: Path, failures: list[str]) -> None:
    b = _Board(tmp, "rerun")
    b.real("nginx", "755", "www-data", "adm")
    b.run("configure_logdirs\n")
    backups = sorted(p.name for p in b.backups.glob("*")) if b.backups.exists() else []
    b.calls.write_text("")
    r = b.run("configure_logdirs\n")
    _check(r.returncode == 0, "11 rerun rc 0", failures)
    _check("daemon-reload" not in b.calls_text(), "11 rerun: no daemon-reload", failures)
    after = sorted(p.name for p in b.backups.glob("*")) if b.backups.exists() else []
    _check(backups == after, f"11 rerun: no new backup ({backups} -> {after})", failures)
    _check("tmpfiles --create" not in b.calls_text(), "11 rerun: tmpfiles not called", failures)

    # A changed unit is backed up first and reloaded.
    (b.units / "mcapp-logdirs.timer").write_text("stale\n")
    b.calls.write_text("")
    b.run("configure_logdirs\n")
    _check("daemon-reload" in b.calls_text(), "11 changed unit: daemon-reload", failures)
    _check(
        len(sorted(b.backups.glob("*"))) > len(backups),
        "11 changed unit: old file backed up",
        failures,
    )


def run_bootstrap_logdirs_tests() -> bool:
    if _BASH is None:
        print("bootstrap_logdirs: SKIP (no bash)")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-logdirs-") as td:
        tmp = Path(td)
        _test_content(tmp, failures)
        _test_from_tmpfs(tmp, failures)
        _test_first_install(tmp, failures)
        _test_names(tmp, failures)
        _test_merge(tmp, failures)
        _test_mount_failures(tmp, failures)
        _test_empty(tmp, failures)
        _test_ramlog(tmp, failures)
        _test_failures(tmp, failures)
        _test_rerun(tmp, failures)
    for f in failures:
        print(f"  FAIL: {f}")
    print(f"bootstrap_logdirs: {len(failures)} failure(s) across 9 case groups")
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_logdirs_tests() else 1)
