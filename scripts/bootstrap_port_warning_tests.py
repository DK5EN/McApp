"""Startup regression suite for check_coexistence in bootstrap/lib/health.sh.

An operator who installs another service (nginx on :8080) gets an unreachable
page because McApp's firewall drops everything not on its list. health_check
now prints advisory warnings for blocked listening ports, a faulty custom
nftables config and a faulty Caddyfile. Rules pinned, against the REAL bash
function with stub ss/nft/iptables/caddy on PATH:

1. A listening non-loopback TCP port the live ruleset does not allow warns once
   (port, process, instruction), however many sockets it is bound on.
2. Every allowed form is silent: single, set, range, ``ip saddr`` restricted,
   trailing comment, conntrack NEW. So is a loopback bind, McApp's own internal
   ports (2981/8081/8082) and ``policy accept``.
3. Only an ``accept`` verdict grants; the first rule covering a port wins, so a
   drop before an accept blocks. Interface-bound, negated, non-NEW conntrack,
   sport and UDP rules never grant. ``policy accept`` plus a bare final drop is
   not open. iptables follows the same rules.
4. No readable ruleset prints one info line. Faulty nft / Caddy config warn
   (Caddy is validated with caddy.env loaded), and nothing can flip
   health_check's result.

Set BOOTSTRAP_PORT_WARNING_LIB_DIR to point the suite at another lib dir (used
to prove the tests fail against the pre-change health.sh).
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
_LIB = Path(os.environ.get("BOOTSTRAP_PORT_WARNING_LIB_DIR", str(_REPO / "bootstrap" / "lib")))
_BASH = shutil.which("bash")

_PRELUDE = """#!/bin/bash
set -u
log_info() { echo "[INFO] $*"; }
log_ok() { echo "[OK] $*"; }
log_warn() { echo "[WARN] $*"; }
source "$LIB_DIR/health.sh"
"""

_SS = (
    "LISTEN 0 511 0.0.0.0:8080 0.0.0.0:* "
    'users:(("nginx",pid=812,fd=5))\n'
    "LISTEN 0 511 [::]:8080 [::]:* "
    'users:(("nginx",pid=812,fd=6))\n'
    "LISTEN 0 128 127.0.0.1:2981 0.0.0.0:* "
    'users:(("mcapp",pid=1,fd=3))\n'
    "LISTEN 0 128 [::1]:2982 [::]:*\n"
)
_HEAD = (
    "table inet filter {\n chain input {\n  type filter hook input priority filter; policy drop;\n"
)
_BASE = "  ct state established,related accept\n  tcp dport 22 accept\n"


def _chain(*rules: str, policy: str = "drop") -> str:
    head = _HEAD.replace("policy drop", f"policy {policy}")
    return head + _BASE + "".join(f"  {r}\n" for r in rules) + " }\n}\n"


def _check(ok: bool, label: str, failures: list[str]) -> None:
    if not ok:
        failures.append(label)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _board(  # noqa: PLR0913 - flat scenario knobs, all keyword-only and optional
    tmp: Path,
    name: str,
    *,
    nft_chain: str | None = None,
    ipt: str | None = None,
    nft_bad: str | None = None,
    caddy_bad: str | None = None,
    have_nft: bool = True,
    have_caddy: bool = False,
    ss: str = _SS,
) -> tuple[Path, dict[str, str]]:
    root = tmp / name
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (root / "ss.txt").write_text(ss)
    _stub(bin_dir, "ss", 'cat "$ROOT/ss.txt"\n')
    if have_nft:
        chain = root / "chain.txt"
        if nft_chain is not None:
            chain.write_text(nft_chain)
        (root / "nft_err.txt").write_text(nft_bad or "")
        _stub(
            bin_dir,
            "nft",
            'if [ "$1" = "-c" ]; then\n'
            '  if [ -s "$ROOT/nft_err.txt" ]; then cat "$ROOT/nft_err.txt" >&2; exit 1; fi\n'
            "  exit 0\nfi\n"
            'if [ "$2" = "ruleset" ]; then\n'
            '  if [ -f "$ROOT/ruleset.txt" ]; then cat "$ROOT/ruleset.txt"; fi\n'
            "  exit 0\nfi\n"
            'if [ -f "$ROOT/chain.txt" ]; then cat "$ROOT/chain.txt"; exit 0; fi\nexit 1\n',
        )
    _stub(
        bin_dir,
        "iptables",
        'if [ -f "$ROOT/ipt.txt" ]; then cat "$ROOT/ipt.txt"; exit 0; fi\nexit 1\n',
    )
    if ipt is not None:
        (root / "ipt.txt").write_text(ipt)
    if have_caddy:
        (root / "caddy_err.txt").write_text(caddy_bad or "")
        _stub(
            bin_dir,
            "caddy",
            'if [ -s "$ROOT/caddy_err.txt" ]; then cat "$ROOT/caddy_err.txt" >&2; exit 1; fi\n'
            "exit 0\n",
        )
    (root / "nftables.conf").write_text("# McApp firewall rules\n")
    (root / "Caddyfile").write_text(":80 {\n}\n")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(root),
        "ROOT": str(root),
        "LIB_DIR": str(_LIB),
        "LANG": "C",
        "MCAPP_NFT_CONF": str(root / "nftables.conf"),
        "MCAPP_CADDY_CONF": str(root / "Caddyfile"),
        "MCAPP_CADDY_ENV": str(root / "caddy.env"),
    }
    return root, env


def _run(root: Path, env: dict[str, str], script: str) -> subprocess.CompletedProcess[str]:
    driver = root / "driver.sh"
    driver.write_text(_PRELUDE + script)
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, str(driver)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def _warns(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [ln for ln in proc.stdout.splitlines() if ln.startswith("[WARN]")]


def _test_blocked_nft(tmp: Path, failures: list[str]) -> None:
    root, env = _board(tmp, "blocked", nft_chain=_chain())
    proc = _run(root, env, "check_coexistence\necho rc=$?\n")
    warns = _warns(proc)
    _check("rc=0" in proc.stdout, "blocked: rc 0", failures)
    _check(len(warns) == 1, f"blocked: one warning (v4+v6 deduped), got {len(warns)}", failures)
    w = warns[0] if warns else ""
    _check("8080" in w and "nginx" in w, "blocked: names port and process", failures)
    _check(
        'tcp dport 8080 accept" to /etc/mcapp/nftables.d/custom.nft' in w
        and "sudo nft -c -f /etc/nftables.conf" in w
        and "sudo systemctl reload nftables" in w,
        "blocked: custom.nft instruction",
        failures,
    )
    _check(
        "2981" not in proc.stdout and "2982" not in proc.stdout, "blocked: loopback quiet", failures
    )
    _check(
        "22" not in re.findall(r"Port (\d+)", proc.stdout), "blocked: allowed 22 quiet", failures
    )


def _test_allowed_forms(tmp: Path, failures: list[str]) -> None:
    forms = {
        "single": "tcp dport 8080 accept",
        "set": "tcp dport { 80, 8080 } accept",
        "range": "tcp dport 8000-8100 accept",
        "setrange": "tcp dport { 80, 8000-8100 } accept",
        "saddr": "ip saddr { 192.168.0.0/16 } tcp dport 8080 accept",
        "comment": 'tcp dport 8080 accept comment "x"',
    }
    for label, rule in forms.items():
        root, env = _board(tmp, f"allowed-{label}", nft_chain=_chain(rule))
        proc = _run(root, env, "check_coexistence\necho rc=$?\n")
        _check(
            not _warns(proc) and "rc=0" in proc.stdout,
            f"allowed {label}: no warning ({_warns(proc)})",
            failures,
        )
    root, env = _board(
        tmp, "near-miss", nft_chain=_chain("tcp dport 80808 accept", "tcp dport 8081 accept")
    )
    _check(
        len(_warns(_run(root, env, "check_coexistence\n"))) == 1,
        "near-miss ports still warn",
        failures,
    )
    root, env = _board(tmp, "policy-accept", nft_chain=_chain(policy="accept"))
    _check(
        not _warns(_run(root, env, "check_coexistence\n")), "policy accept: no warning", failures
    )


_SS_INTERNAL = (
    'LISTEN 0 1024 0.0.0.0:8082 0.0.0.0:* users:(("lighttpd",pid=1,fd=4))\n'
    'LISTEN 0 128 0.0.0.0:2981 0.0.0.0:* users:(("mcapp",pid=2,fd=3))\n'
    'LISTEN 0 128 [::]:8081 [::]:* users:(("python",pid=3,fd=3))\n'
)

# (label, extra rules after the base, chain policy, expect a port-8080 warning)
_NFT_CASES: tuple[tuple[str, tuple[str, ...], str, bool], ...] = (
    ("comment-mentions-accept", ('tcp dport 8080 drop comment "never accept"',), "drop", True),
    ("accept-comment-mentions-drop", ('tcp dport 8080 accept comment "drop all"',), "drop", False),
    ("iif", ('iif "lo" tcp dport 8080 accept',), "drop", True),
    ("iifname", ('iifname "wg0" tcp dport 8080 accept',), "drop", True),
    ("negated-dport", ("tcp dport != 80 accept",), "drop", True),
    ("negated-saddr", ("ip saddr != 10.0.0.0/8 tcp dport 8080 accept",), "drop", True),
    ("ct-established", ("tcp dport 8080 ct state established accept",), "drop", True),
    ("drop-then-accept", ("tcp dport 8080 drop", "tcp dport 8080 accept"), "drop", True),
    ("reject-then-accept", ("tcp dport 8080 reject", "tcp dport 8080 accept"), "drop", True),
    (
        "drop-set-then-accept",
        ("tcp dport { 80, 8080 } drop", "tcp dport 8080 accept"),
        "drop",
        True,
    ),
    ("accept-then-drop", ("tcp dport 8080 accept", "tcp dport 8080 drop"), "drop", False),
    ("sport", ("tcp sport 8080 accept",), "drop", True),
    ("udp", ("udp dport 8080 accept",), "drop", True),
    ("policy-accept-counter-drop", ("counter packets 3 bytes 9 drop",), "accept", True),
    ("policy-accept-bare-drop", ("tcp dport 80 accept", "drop"), "accept", True),
    ("policy-accept-explicit-drop", ("tcp dport 8080 drop",), "accept", True),
    ("policy-accept-clean", ("tcp dport 80 accept",), "accept", False),
    (
        "saddr-set",
        ("ip saddr { 10.0.0.0/8, 192.168.0.0/16 } tcp dport 8080 accept",),
        "drop",
        False,
    ),
    (
        "ct-new-limit",
        ("tcp dport 8080 ct state new limit rate 6/minute burst 5 packets accept",),
        "drop",
        False,
    ),
    (
        "limited-drop-then-accept",
        ("tcp dport 8080 limit rate over 5/second drop", "tcp dport 8080 accept"),
        "drop",
        False,
    ),
    ("udp-drop-then-accept", ("udp dport 8080 drop", "tcp dport 8080 accept"), "drop", False),
)

_IPT_BASE = "-P INPUT DROP\n-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n"
_IPT_CASES: tuple[tuple[str, str, bool], ...] = (
    (
        "drop-then-accept",
        "-A INPUT -p tcp --dport 8080 -j DROP\n-A INPUT -p tcp --dport 8080 -j ACCEPT\n",
        True,
    ),
    (
        "reject-then-accept",
        "-A INPUT -p tcp --dport 8080 -j REJECT\n-A INPUT -p tcp --dport 8080 -j ACCEPT\n",
        True,
    ),
    ("log-only", "-A INPUT -p tcp --dport 8080 -j LOG\n", True),
    ("interface", "-A INPUT -i wg0 -p tcp --dport 8080 -j ACCEPT\n", True),
    ("negated-source", "-A INPUT ! -s 10.0.0.0/8 -p tcp --dport 8080 -j ACCEPT\n", True),
    ("negated-dport", "-A INPUT -p tcp ! --dport 80 -j ACCEPT\n", True),
    (
        "state-established",
        "-A INPUT -p tcp -m state --state ESTABLISHED --dport 8080 -j ACCEPT\n",
        True,
    ),
    ("udp", "-A INPUT -p udp -m udp --dport 8080 -j ACCEPT\n", True),
    ("sport", "-A INPUT -p tcp -m tcp --sport 8080 -j ACCEPT\n", True),
    (
        "accept-then-drop",
        "-A INPUT -p tcp --dport 8080 -j ACCEPT\n-A INPUT -p tcp --dport 8080 -j DROP\n",
        False,
    ),
    ("state-new", "-A INPUT -p tcp -m state --state NEW --dport 8080 -j ACCEPT\n", False),
    (
        "recent-drop-then-accept",
        (
            "-A INPUT -p tcp --dport 8080 -m state --state NEW -m recent "
            "--update --seconds 60 --hitcount 4 -j DROP\n"
            "-A INPUT -p tcp --dport 8080 -j ACCEPT\n"
        ),
        False,
    ),
    (
        "udp-drop-then-accept",
        "-A INPUT -p udp --dport 8080 -j DROP\n-A INPUT -p tcp --dport 8080 -j ACCEPT\n",
        False,
    ),
)


def _port_8080_warned(proc: subprocess.CompletedProcess[str]) -> bool:
    return "8080" in re.findall(r"Port (\d+)/tcp", proc.stdout)


def _test_rule_shapes(tmp: Path, failures: list[str]) -> None:
    for label, rules, policy, expect in _NFT_CASES:
        root, env = _board(tmp, f"nft-{label}", nft_chain=_chain(*rules, policy=policy))
        proc = _run(root, env, "check_coexistence\n")
        _check(
            _port_8080_warned(proc) == expect,
            f"nft {label}: expected {'warning' if expect else 'quiet'}, got {_warns(proc)}",
            failures,
        )
    for label, ipt_rules, expect in _IPT_CASES:
        root, env = _board(tmp, f"ipt-{label}", ipt=_IPT_BASE + ipt_rules, have_nft=False)
        proc = _run(root, env, "check_coexistence\n")
        _check(
            _port_8080_warned(proc) == expect,
            f"iptables {label}: expected {'warning' if expect else 'quiet'}, got {_warns(proc)}",
            failures,
        )
    root, env = _board(
        tmp, "ipt-policy-accept-drop", ipt="-P INPUT ACCEPT\n-A INPUT -j DROP\n", have_nft=False
    )
    _check(
        _port_8080_warned(_run(root, env, "check_coexistence\n")),
        "iptables policy accept + bare final DROP: blocked",
        failures,
    )


def _test_internal_ports(tmp: Path, failures: list[str]) -> None:
    # lighttpd really binds 0.0.0.0:8082 despite server.bind = 127.0.0.1.
    for label in ("nft", "iptables"):
        root, env = _board(
            tmp,
            f"internal-{label}",
            ss=_SS_INTERNAL,
            nft_chain=_chain() if label == "nft" else None,
            ipt=_IPT_BASE if label == "iptables" else None,
            have_nft=label == "nft",
        )
        proc = _run(root, env, "check_coexistence\n")
        _check(
            not _warns(proc), f"{label}: internal 2981/8081/8082 quiet ({_warns(proc)})", failures
        )


def _test_iptables(tmp: Path, failures: list[str]) -> None:
    base = "-P INPUT DROP\n-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n"
    root, env = _board(tmp, "ipt-blocked", ipt=base, have_nft=False)
    w = _warns(_run(root, env, "check_coexistence\n"))
    _check(len(w) == 1 and "8080" in w[0] and "nginx" in w[0], "iptables blocked: warns", failures)
    _check(
        bool(w)
        and "sudo iptables -A INPUT -p tcp --dport 8080 -j ACCEPT" in w[0]
        and "iptables-save > /etc/iptables/rules.v4" in w[0],
        "iptables blocked: instruction",
        failures,
    )
    for label, rule in (
        ("single", "-A INPUT -p tcp -m tcp --dport 8080 -j ACCEPT"),
        ("multiport", "-A INPUT -p tcp -m multiport --dports 80,8080 -j ACCEPT"),
        ("range", "-A INPUT -p tcp -m tcp --dport 8000:8100 -j ACCEPT"),
    ):
        root, env = _board(tmp, f"ipt-{label}", ipt=base + rule + "\n", have_nft=False)
        _check(
            not _warns(_run(root, env, "check_coexistence\n")), f"iptables {label}: quiet", failures
        )
    root, env = _board(tmp, "ipt-accept", ipt="-P INPUT ACCEPT\n", have_nft=False)
    _check(
        not _warns(_run(root, env, "check_coexistence\n")),
        "iptables policy accept: quiet",
        failures,
    )


def _test_no_firewall(tmp: Path, failures: list[str]) -> None:
    root, env = _board(tmp, "none")
    proc = _run(root, env, "check_coexistence\necho rc=$?\n")
    _check("rc=0" in proc.stdout, "no firewall: rc 0", failures)
    _check(not _warns(proc), "no firewall: no port warning", failures)
    _check("irewall not active" in proc.stdout, "no firewall: info line", failures)


def _test_faulty_configs(tmp: Path, failures: list[str]) -> None:
    root, env = _board(
        tmp,
        "faulty",
        nft_chain=_chain("tcp dport 8080 accept"),
        nft_bad="/etc/nftables.conf:9:3-12: Error: syntax error, unexpected foo\n",
        caddy_bad="Error: adapting config using caddyfile: Caddyfile:3: unexpected token\n",
        have_caddy=True,
    )
    warns = _warns(_run(root, env, "check_coexistence\n"))
    nft_w = [w for w in warns if "syntax error" in w]
    caddy_w = [w for w in warns if "Caddy" in w]
    _check(
        len(nft_w) == 1 and "refuse" in nft_w[0] and "falls back" in nft_w[0],
        "faulty nft: warns with first error, reload and boot fallback",
        failures,
    )
    _check(
        len(caddy_w) == 1 and "Caddyfile:3" in caddy_w[0] and "web UI down" in caddy_w[0],
        "faulty caddy: warns with error and consequence",
        failures,
    )
    # Everything failing under strict shell options must neither abort nor flip rc.
    root, env = _board(
        tmp, "allfail", nft_bad="Error: x\n", caddy_bad="Error: y\n", have_caddy=True
    )
    for n in ("ss", "nft", "iptables", "caddy"):
        _stub(root / "bin", n, "exit 1\n")
    proc = _run(
        root,
        env,
        "set -eo pipefail\nset -u\ncheck_coexistence\nrc=$?\necho after rc=$rc\n"
        "check_coexistence || true\necho still-running\n",
    )
    _check(
        "after rc=0" in proc.stdout and "still-running" in proc.stdout,
        "all stubs failing: rc 0, next line runs",
        failures,
    )
    # Same with a real warning set and ss failing mid-pipeline.
    root, env = _board(tmp, "strict-warn", nft_chain=_chain(), nft_bad="Error: x\n")
    proc = _run(root, env, "set -eo pipefail\nset -u\ncheck_coexistence\necho after rc=$?\n")
    _check(
        "after rc=0" in proc.stdout and _warns(proc) != [],
        "warnings under strict opts: rc 0",
        failures,
    )


def _test_caddy_env(tmp: Path, failures: list[str]) -> None:
    needs_env = (
        'if [ -z "${TEST_TOKEN:-}" ]; then echo "Error: env TEST_TOKEN unset" >&2; exit 1; fi\n'
        "exit 0\n"
    )
    strict = "set -eo pipefail\nset -u\ncheck_coexistence\nrc=$?\necho after rc=$rc\n"
    for label, env_text, expect_warn in (
        ("loaded", "TEST_TOKEN=abc\n", False),
        ("missing", None, True),
        ("broken", "if ((\n", True),
        ("unbound-var", "echo $NOPE_NOT_SET\nTEST_TOKEN=abc\n", False),
    ):
        root, env = _board(tmp, f"caddyenv-{label}", have_caddy=True)
        _stub(root / "bin", "caddy", needs_env)
        if env_text is not None:
            (root / "caddy.env").write_text(env_text)
        proc = _run(root, env, strict)
        caddy_w = [w for w in _warns(proc) if "Caddy" in w]
        _check(
            "after rc=0" in proc.stdout and (len(caddy_w) == 1) == expect_warn,
            f"caddy env {label}: validate sees env, never aborts ({caddy_w}, {proc.stderr[-120:]})",
            failures,
        )


def _test_operator_firewall(tmp: Path, failures: list[str]) -> None:
    # A firewall in another table cannot be parsed: never call that "not active" or "OK".
    foreign = (
        "table inet myfw {\n chain input {\n"
        "  type filter hook input priority 0; policy drop;\n }\n}\n"
    )
    for name, ipt in (("no-iptables", None), ("iptables-open", "-P INPUT ACCEPT\n")):
        root, env = _board(tmp, f"foreign-{name}", ipt=ipt)
        (root / "ruleset.txt").write_text(foreign)
        p = _run(root, env, "check_coexistence\n")
        out = p.stdout + p.stderr
        _check(p.returncode == 0, f"foreign {name}: rc {p.returncode}", failures)
        _check("another table" in out, f"foreign {name}: says so: {out}", failures)
        _check("2985" in out, f"foreign {name}: names what McApp needs", failures)
        _check("not active" not in out, f"foreign {name}: not called inactive", failures)
        _check("No port or config" not in out, f"foreign {name}: no OK line", failures)
    # A kept operator file does not include custom.nft: that advice would open nothing,
    # and there is no boot fallback to promise.
    root, env = _board(tmp, "unmanaged", nft_chain=_chain())
    (root / "nftables.conf").write_text("# my firewall\n")
    p = _run(root, env, "check_coexistence\n")
    out = "\n".join(_warns(p))
    _check("8080" in out and "custom.nft" not in out, f"unmanaged: no custom.nft: {out}", failures)
    _check("nftables.conf" in out, f"unmanaged: points at the operator's file: {out}", failures)
    root, env = _board(
        tmp, "unmanaged-bad", nft_chain=_chain("tcp dport 8080 accept"), nft_bad="Error: x\n"
    )
    (root / "nftables.conf").write_text("# my firewall\n")
    out = "\n".join(_warns(_run(root, env, "check_coexistence\n")))
    _check("falls back" not in out and "faulty" in out, f"unmanaged bad: {out}", failures)
    # A McApp-managed box keeps both promises.
    root, env = _board(
        tmp, "managed-bad", nft_chain=_chain("tcp dport 8080 accept"), nft_bad="Error: x\n"
    )
    out = "\n".join(_warns(_run(root, env, "check_coexistence\n")))
    _check("falls back" in out, f"managed bad: {out}", failures)


def _test_source(failures: list[str]) -> None:
    text = (_LIB / "health.sh").read_text()
    m = re.search(r"^health_check\(\) \{\n(.*?)^\}", text, re.DOTALL | re.MULTILINE)
    body = m.group(1) if m else ""
    calls = [
        ln.strip()
        for ln in body.splitlines()
        if "check_coexistence" in ln and not ln.strip().startswith("#")
    ]
    _check(
        calls == ["check_coexistence || true"],
        f"health_check calls it as '|| true' ({calls})",
        failures,
    )
    _check(
        body.find("check_versions") < body.find("check_coexistence") if m else False,
        "health_check: coexistence after check_versions",
        failures,
    )


def run_bootstrap_port_warning_tests() -> bool:
    if _BASH is None:
        print("bootstrap_port_warning: SKIP (no bash)")
        return True
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mcapp-portwarn-") as td:
        tmp = Path(td)
        _test_blocked_nft(tmp, failures)
        _test_allowed_forms(tmp, failures)
        _test_rule_shapes(tmp, failures)
        _test_internal_ports(tmp, failures)
        _test_iptables(tmp, failures)
        _test_no_firewall(tmp, failures)
        _test_faulty_configs(tmp, failures)
        _test_caddy_env(tmp, failures)
        _test_operator_firewall(tmp, failures)
        _test_source(failures)
    for f in failures:
        print(f"  FAIL: {f}")
    print(f"bootstrap_port_warning: {len(failures)} failure(s) across 10 case groups")
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if run_bootstrap_port_warning_tests() else 1)
