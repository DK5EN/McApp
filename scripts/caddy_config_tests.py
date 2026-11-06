"""Startup regression suite for the Caddy front-door config.

Guards the fix for the "blank page under every name but mcapp.local" bug.

Up to Caddyfile.mcapp v2 the site addresses were the hard-coded literal
``mcapp.local`` on both :80 and :443. Caddy matches sites by Host header, so a
request arriving under any *other* name the box answers to — ``<host>.fritz.box``
from a Fritz!Box's DHCP-DNS, ``.home.arpa``/``.lan`` from other routers, a
resolv.conf search domain, the reverse-DNS name, or the bare LAN IP — matched no
site and got Caddy's no-site-matched reply: an **empty 200**. Not a 404, not an
error page; a white page. The installer's own success summary printed the IP URL
unconditionally, so it advertised exactly such a blank page as an access point.

Four invariants are pinned here, one per moving part of the fix:

1. **Template** — :80 is a catch-all (no host matcher) so every name works, and
   :443 carries an explicit multi-name SAN list (a bare :443 has no name to
   issue an internal-CA certificate for, so it cannot be a catch-all). The
   version marker embeds the hostname, so renaming the box counts as stale.
2. **caddy_site()** — must still find the probe target now that the :443 line is
   a comma-separated list; the pre-v3 ``-F:`` parse matched none of it and
   silently fell back to ``$(hostname).local``.
3. **pick_caddy_template()** — must refuse a template whose own version does not
   match the code rendering it. v3 first shipped without this and did not take
   effect at all: configure_caddy() runs from install_packages(), BEFORE
   deploy_app() swaps the slot symlink, so INSTALL_DIR still pointed at the
   PREVIOUS release. The v2 template was rendered and installed while the
   success line — built from the constant, not the file — reported v3.
4. **probe_access_point()** — must reject an empty 200. This is the invariant
   most likely to be "simplified" away, because ``curl -f`` looks sufficient
   and is not: it reports success on the exact response that renders blank.

Like scripts/update_runner_tests.py this exercises bootstrap/ shell code rather
than an importable module, so it drives the real functions via subprocess
instead of copying their logic — a copy would drift from what ships.
"""

from __future__ import annotations

import http.server
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Resolved to absolute paths up front, matching config_migration_tests.py: the
# suite must not depend on whatever PATH the caller happens to export, and an
# absolute argv[0] is what keeps these subprocess calls honest.
_AWK = shutil.which("awk")
_BASH = shutil.which("bash")

_REPO = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO / "bootstrap" / "templates" / "caddy" / "Caddyfile.mcapp"
_PACKAGES_SH = _REPO / "bootstrap" / "lib" / "packages.sh"
_HEALTH_SH = _REPO / "bootstrap" / "lib" / "health.sh"

# Placeholder configure_caddy() substitutes with `hostname -s` at install time.
_PLACEHOLDER = "@@HOST@@"

# Suffixes the :443 SAN list must cover. .local is mDNS/avahi (the Apple path),
# .fritz.box is Fritz!Box DHCP-DNS (the common German ham setup), .home.arpa is
# RFC 8375, and .lan / .home are what OpenWrt and friends hand out.
_REQUIRED_TLS_SUFFIXES = (".local", ".fritz.box", ".home.arpa", ".lan", ".home")

_failures: list[str] = []


def _check(ok: bool, label: str) -> bool:
    print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")
    if not ok:
        _failures.append(label)
    return ok


def _render(host: str = "mcapp") -> str:
    """Render the template the way configure_caddy() does."""
    return _TEMPLATE.read_text(encoding="utf-8").replace(_PLACEHOLDER, host)


def _site_address_lines(rendered: str) -> list[str]:
    """Lines that open a site block, i.e. non-comment lines ending in '{'.

    Excludes the global options block ('{' alone) and snippet definitions
    ('(name) {'), neither of which is a site address.
    """
    lines = []
    for raw in rendered.splitlines():
        line = raw.strip()
        if not line.endswith("{") or line.startswith(("#", "(")):
            continue
        if line == "{":
            continue
        lines.append(line)
    return lines


# ── 1. template invariants ────────────────────────────────────────────────


def _test_template() -> None:
    template_text = _TEMPLATE.read_text(encoding="utf-8")

    _check(
        _PLACEHOLDER in template_text,
        f"template is parameterised: {_PLACEHOLDER} placeholder present "
        "(hard-coding the hostname is what broke every renamed box)",
    )

    rendered = _render("mcapp")
    _check(
        _PLACEHOLDER not in rendered,
        "rendering substitutes every placeholder occurrence (no @@ left behind)",
    )

    sites = _site_address_lines(rendered)

    http_sites = [s for s in sites if ":80" in s]
    _check(
        http_sites == [":80 {"],
        f"port 80 is a bare catch-all site — every Host header is served, got {http_sites!r}",
    )

    tls_sites = [s for s in sites if ":443" in s]
    _check(
        len(tls_sites) == 1,
        f"exactly one :443 site block, got {len(tls_sites)}",
    )

    tls_line = tls_sites[0] if tls_sites else ""
    for suffix in _REQUIRED_TLS_SUFFIXES:
        _check(
            f"mcapp{suffix}:443" in tls_line,
            f":443 SAN list covers mcapp{suffix} (internal CA issues one leaf per name)",
        )

    _check(
        not re.search(r"^\s*:443\s*\{", rendered, re.MULTILINE),
        ":443 is NOT a catch-all — a nameless TLS site has no certificate to present",
    )

    # The marker is how configure_caddy() decides whether the installed file is
    # stale. Carrying the hostname is the part that makes a rename re-render.
    marker = re.search(r"mcapp-caddy-config-version:\s*(\d+)\s+host=(\S+)", template_text)
    _check(
        marker is not None,
        "version marker carries both version and host= (a rename must count as stale)",
    )

    declared = _declared_version()
    _check(
        marker is not None and declared is not None and marker.group(1) == declared,
        "template marker version == CADDY_CONFIG_VERSION in packages.sh "
        f"(template={marker.group(1) if marker else '?'}, "
        f"packages.sh={declared or '?'}) — a mismatch means "
        "upgrades either never re-render or re-render on every run",
    )


# ── 2. caddy_site() must parse the comma-separated :443 list ──────────────


def _declared_version() -> str | None:
    """CADDY_CONFIG_VERSION as declared in packages.sh."""
    match = re.search(
        r"^readonly CADDY_CONFIG_VERSION=(\d+)",
        _PACKAGES_SH.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _extract_caddy_site_awk() -> str | None:
    """Pull the awk program out of packages.sh so the test can't drift from it."""
    text = _PACKAGES_SH.read_text(encoding="utf-8")
    match = re.search(r"site=\$\(awk '(.*?)' /etc/caddy/Caddyfile", text, re.DOTALL)
    return match.group(1) if match else None


def _test_caddy_site() -> None:
    if not _check(_AWK is not None, "awk is available to run caddy_site()'s parser"):
        return
    program = _extract_caddy_site_awk()
    if not _check(
        program is not None, "caddy_site()'s awk program is extractable from packages.sh"
    ):
        return
    assert program is not None
    assert _AWK is not None

    cases = [
        (
            "mcapp.local:443, mcapp.fritz.box:443, mcapp.home.arpa:443 {",
            "mcapp.local",
            "v3 comma-separated list -> first (the .local name avahi can resolve)",
        ),
        (
            "mcapp.local:443 {",
            "mcapp.local",
            "v2 single-address form still parses (an old file may be on disk)",
        ),
        (
            "{$TLS_HOSTNAME} {",
            "",
            "public-TLS template yields nothing, so caddy_site falls back to config.json",
        ),
        (
            "\t# comment mentioning :443 {\nmcapp.local:443 {",
            "mcapp.local",
            "a comment is not mistaken for a site address",
        ),
        # Field report 2026-10-25 (Orange Pi McApp-OP2W): an operator prepended
        # the LAN IP to the v3 line. Caddy issues an IP-SAN leaf for it, but the
        # loopback probe sends no SNI for an IP host, so Caddy picks a cert by
        # the local address 127.0.0.1, finds none and fails the handshake -- a
        # permanent false WARN naming the IP. A hostname is the probe target.
        (
            "10.0.0.236:443, McApp-OP2W.local:443, McApp-OP2W.fritz.box:443 {",
            "McApp-OP2W.local",
            "a leading IP literal is skipped in favour of the first hostname",
        ),
        (
            "mcapp.local:443, 10.0.0.236:443 {",
            "mcapp.local",
            "a trailing IP literal does not displace the hostname",
        ),
        (
            "10.0.0.236:443, 192.168.1.5:443 {",
            "",
            "an IP-only line yields nothing, so caddy_site falls back",
        ),
        (
            "10.0.0.236:443 {\nmcapp.local:443 {",
            "mcapp.local",
            "an IP-only block is skipped and the next site line is read",
        ),
    ]

    for source, expected, label in cases:
        result = subprocess.run(  # noqa: S603 - fixed argv, _AWK is a resolved absolute path
            [_AWK, program],
            input=source + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        got = result.stdout.strip()
        _check(got == expected, f"caddy_site: {label} (got {got!r}, want {expected!r})")


# ── 3. pick_caddy_template() must refuse a template from another release ──


def _bash_out(script: str) -> str:
    """Run a bash snippet and return its stdout, stripped."""
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout.strip()


def _write_template(path: Path, version: str) -> Path:
    """A minimal template carrying `version` in its marker line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# mcapp-caddy-config-version: {version} host=@@HOST@@\n:80 {{\n}}\n", encoding="utf-8"
    )
    return path


def _test_template_pick() -> None:
    if not _check(_BASH is not None, "bash is available to source packages.sh"):
        return
    current = _declared_version()
    if not _check(current is not None, "CADDY_CONFIG_VERSION is readable from packages.sh"):
        return
    stale = str(int(current or "3") - 1)

    def pick(script_dir: str, install_dir: str) -> str:
        return _bash_out(
            f'SCRIPT_DIR="{script_dir}"; INSTALL_DIR="{install_dir}"; '
            f'source "{_PACKAGES_SH}"; pick_caddy_template'
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        script_dir = root / "checkout"
        install_dir = root / "slot"
        script_tmpl = script_dir / "templates" / "caddy" / "Caddyfile.mcapp"
        install_tmpl = install_dir / "bootstrap" / "templates" / "caddy" / "Caddyfile.mcapp"

        # THE REGRESSION: during an upgrade configure_caddy() runs before the
        # slot symlink is swapped, so INSTALL_DIR still holds the PREVIOUS
        # release. Picking it renders old content under a new marker claim.
        _write_template(install_tmpl, stale)
        _check(
            pick("", str(install_dir)) == "",
            f"a stale INSTALL_DIR template (v{stale}) is REFUSED, so the caller downloads instead "
            "— preferring it is what made v3 ship without taking effect",
        )

        # An INSTALL_DIR template at the right version is fine to use.
        _write_template(install_tmpl, current or "3")
        _check(
            pick("", str(install_dir)) == str(install_tmpl),
            "an INSTALL_DIR template at the current version IS used",
        )

        # A local checkout (non-piped mode) wins over the deployed slot.
        _write_template(script_tmpl, current or "3")
        _write_template(install_tmpl, stale)
        _check(
            pick(str(script_dir), str(install_dir)) == str(script_tmpl),
            "SCRIPT_DIR (the checkout being run) is preferred over the deployed slot",
        )

        # Nothing configured at all: piped mode.
        _check(
            pick("", "") == "",
            "piped mode (no SCRIPT_DIR, no INSTALL_DIR) yields no local template",
        )

        # Version parsing itself.
        marked = _write_template(root / "marked.caddy", "7")
        _check(
            _bash_out(f'source "{_PACKAGES_SH}"; caddy_template_version "{marked}"') == "7",
            "caddy_template_version reads the version out of a marker line",
        )
        unmarked = root / "unmarked.caddy"
        unmarked.write_text(":80 {\n}\n", encoding="utf-8")
        _check(
            _bash_out(f'source "{_PACKAGES_SH}"; caddy_template_version "{unmarked}"') == "",
            "caddy_template_version yields nothing for an unmarked file (ssl-tunnel-setup's)",
        )


# ── 4. probe_access_point() must reject the empty 200 ─────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the two responses that matter, keyed by path."""

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's required name
        if self.path == "/health":
            body = self.server.health_body  # type: ignore[attr-defined]  # set in _serving()
            self.send_response(200)
            # Mirror Caddy's no-site-matched reply: 200, and for the empty case
            # no Content-Type at all — the shape that fools `curl -f`.
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@contextmanager
def _serving(body: bytes) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    server.health_body = body  # type: ignore[attr-defined]  # read back in do_GET
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _bash(script: str) -> int:
    """Run a bash snippet against health.sh and return its exit status."""
    assert _BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
        [_BASH, "-c", script],
        capture_output=True,
        timeout=30,
        check=False,
    ).returncode


def _probe(host: str) -> bool:
    """Run the real probe_access_point() out of health.sh."""
    return _bash(f'source "{_HEALTH_SH}"; probe_access_point "{host}" http') == 0


def _test_probe() -> None:
    if not _check(_BASH is not None, "bash is available to source health.sh"):
        return

    # Guard next: a MISSING function also makes bash exit non-zero, which would
    # let "rejects an empty 200" pass for entirely the wrong reason.
    defined = _bash(f'source "{_HEALTH_SH}"; declare -F probe_access_point')
    if not _check(defined == 0, "health.sh defines probe_access_point as a top-level function"):
        return

    with _serving(b'{"status":"healthy"}') as host:
        _check(_probe(host), "probe accepts a real /health JSON response")

    # The regression itself: Caddy's no-site-matched reply.
    with _serving(b"") as host:
        _check(
            not _probe(host),
            "probe REJECTS an empty 200 — the blank page `curl -f` alone calls success",
        )

    # Nothing listening at all.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = f"127.0.0.1:{probe.getsockname()[1]}"
    _check(not _probe(dead), "probe rejects a closed port")


def _block(text: str, header: str) -> str | None:
    """Body of the first `<header> {` block (brace-matched), or None."""
    m = re.search(re.escape(header) + r"\s*\{", text)
    if m is None:
        return None
    depth = 1
    i = m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _test_webapp_routes() -> None:
    """Caddy serves the SPA itself: pre-compressed, immutable assets, no-cache shell."""
    templates = sorted(_TEMPLATE.parent.glob("Caddyfile.*"))
    _check(len(templates) == 4, f"four Caddyfile templates exist (found {len(templates)})")
    for tmpl in templates:
        text = tmpl.read_text(encoding="utf-8")
        name = tmpl.name
        _check("precompressed br gzip" in text, f"{name}: file_server uses `precompressed br gzip`")
        _check(
            "reverse_proxy 127.0.0.1:8082" not in text,
            f"{name}: no reverse_proxy to lighttpd on 127.0.0.1:8082 remains",
        )
        # Runtime gzip belongs on the two dynamic routes and nowhere else. Static files use
        # sidecars, zstd holds more RAM per long-lived SSE client, and /update-stream is an
        # install log nobody needs compressed.
        for header in ("handle /events*", "handle /api/*"):
            route = _block(text, header)
            _check(
                route is not None and re.search(r"encode \{\s+gzip 1\s+\}", route) is not None,
                f"{name}: {header} uses `encode` with gzip level 1",
            )
        _check(
            len(re.findall(r"^\s*encode\b", text, re.MULTILINE)) == 2,
            f"{name}: `encode` appears on exactly those two routes (static files use sidecars)",
        )
        _check(
            not re.search(r"^\s*zstd\b", text, re.MULTILINE),
            f"{name}: no zstd (holds more RAM per SSE client)",
        )
        _check(
            "flush_interval -1" in (_block(text, "handle /events*") or ""),
            f"{name}: /events still flushes per event",
        )
        assets = _block(text, "handle /webapp/assets/*")
        if _check(assets is not None, f"{name}: has a /webapp/assets/* handler") and assets:
            _check(
                "public, max-age=31536000, immutable" in assets,
                f"{name}: assets are Cache-Control immutable",
            )
            _check(
                "try_files" not in assets,
                f"{name}: assets have NO SPA fallback (a missing asset is a real 404)",
            )
            # Seen on real Caddy: a bare `header` also stamps the 404, so a browser could
            # keep a missing asset's failure for a year.
            _check(
                re.search(r"@asset_exists file\s", assets) is not None
                and 'header @asset_exists Cache-Control "public' in assets,
                f"{name}: immutable applies only to an asset that exists",
            )
        shell = _block(text, "handle @spa")
        if _check(shell is not None, f"{name}: has a shell (@spa) handler") and shell:
            _check('Cache-Control "no-cache"' in shell, f"{name}: shell is Cache-Control no-cache")
            # Seen on real Caddy: Go's MIME table has no .webmanifest on this box, so
            # file_server sent no Content-Type where lighttpd sent the right one.
            _check(
                "@manifest path *.webmanifest" in shell
                and 'header @manifest Content-Type "application/manifest+json"' in shell,
                f"{name}: the web manifest has an explicit Content-Type",
            )
            _check(
                re.search(r"try_files\s+\{path\}\s+/webapp/index\.html", shell) is not None,
                f"{name}: shell falls back to /webapp/index.html via try_files",
            )
        _check(
            re.search(r"@spa path /webapp /webapp/\*", text) is not None,
            f"{name}: @spa matches /webapp and every deep route under it",
        )
        _check(
            re.search(r"handle\s*\{\s*respond [^\n]*404", text) is not None,
            f"{name}: anything else is a plain 404",
        )
        for route in ("/api/*", "/events*", "/health*", "/update-stream"):
            _check(f"handle {route}" in text, f"{name}: still has the {route} handler")
    mcapp = _TEMPLATE.read_text(encoding="utf-8")
    _check("handle /root.crt" in mcapp, "Caddyfile.mcapp keeps the /root.crt handler")
    _check("redir * /webapp/" in mcapp, "Caddyfile.mcapp redirects / to /webapp/")
    _check(
        _declared_version() == "5"
        and re.search(r"mcapp-caddy-config-version:\s*5\b", mcapp) is not None,
        "Caddyfile.mcapp marker and CADDY_CONFIG_VERSION are both 5",
    )


# ── 4. configure_caddy() must roll back a restart that fails after validate ──

_ROLLBACK_DRIVER = """set -eo pipefail
set -u
T="$1"; OLD="$2"; TLS="$3"; MODE="$4"
source "{packages_sh}"
fn=$(cat "$T/configure_caddy.sh")
eval "$fn"
CONFIG_FILE="$T/config.json"
caddy() {{
  if [[ -f "$T/installed_invalid" && "$*" == *"etc/caddy/Caddyfile"* ]]; then return 1; fi
  return 0
}}
journalctl() {{ return 0; }}
log_info() {{ :; }}; log_ok() {{ :; }}; log_warn() {{ :; }}
log_error() {{ echo "ERROR: $*" >&2; }}
caddy_render_host() {{ echo mcapp; }}
pick_caddy_template() {{ echo "$T/tmpl"; }}
publish_caddy_root_crt() {{ :; }}
systemctl() {{
  case "$1" in
    restart)
      echo r >> "$T/restarts"
      if [[ ! -f "$T/allow_new" ]] && grep -q NEWCONF "$T/etc/caddy/Caddyfile"; then
        rm -f "$T/active"; return 1
      fi
      touch "$T/active"; return 0 ;;
    is-active) [[ -f "$T/active" ]] ;;
    *) return 0 ;;
  esac
}}
mkdir -p "$T/etc/caddy"
if [[ "$OLD" != "-" ]]; then printf '%s\\n' "$OLD" > "$T/etc/caddy/Caddyfile"; fi
if [[ "$TLS" == "tls" ]]; then echo '{{"TLS_ENABLED": true}}' > "$T/config.json"; fi
case "$MODE" in
  # The real caller at mcapp.sh --skip runs it bare, with errexit live.
  bare) trap 'echo "rc=$?"' EXIT; configure_caddy; exit 0 ;;
  second-run) configure_caddy || true; touch "$T/allow_new" ;;
  installed-invalid) touch "$T/installed_invalid" ;;
esac
rc=0
configure_caddy || rc=$?
echo "rc=$rc"
"""


def _run_configure_caddy(
    old: str | None, *, restart_fails: bool, tls: bool = False, mode: str = "guarded"
) -> tuple[int, str | None, int, bool] | None:
    """Drive the real configure_caddy() against a sandbox instead of /etc/caddy.

    Returns (rc, Caddyfile content afterwards, restart count, leftover backup).
    The new template carries the NEWCONF marker the restart stub refuses.
    """
    assert _BASH is not None
    current = _declared_version()
    source = _PACKAGES_SH.read_text(encoding="utf-8")
    match = re.search(r"^configure_caddy\(\) \{\n.*?^\}$", source, re.MULTILINE | re.DOTALL)
    if match is None or current is None:
        return None
    with tempfile.TemporaryDirectory() as raw:
        t = Path(raw)
        (t / "configure_caddy.sh").write_text(
            match.group(0).replace("/etc/caddy", f"{t}/etc/caddy"), encoding="utf-8"
        )
        body = "NEWCONF" if restart_fails else "FRESHCONF"
        (t / "tmpl").write_text(
            f"# mcapp-caddy-config-version: {current} host=@@HOST@@\n{body}\n", encoding="utf-8"
        )
        driver = t / "driver.sh"
        driver.write_text(_ROLLBACK_DRIVER.format(packages_sh=_PACKAGES_SH), encoding="utf-8")
        res = subprocess.run(  # noqa: S603 - fixed argv, _BASH is a resolved absolute path
            [
                _BASH,
                str(driver),
                str(t),
                old if old is not None else "-",
                "tls" if tls else "lan",
                mode,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        m = re.search(r"rc=(\d+)", res.stdout)
        if m is None:
            print(f"    driver output: {res.stdout!r} {res.stderr[-300:]!r}")
            return None
        conf = t / "etc" / "caddy" / "Caddyfile"
        restarts = (t / "restarts").read_text().count("r") if (t / "restarts").exists() else 0
        leftovers = any(f.name != "Caddyfile" for f in conf.parent.iterdir())
        return (
            int(m.group(1)),
            conf.read_text(encoding="utf-8") if conf.exists() else None,
            restarts,
            leftovers,
        )


def _test_rollback() -> None:
    if not _check(_BASH is not None, "bash is available to drive configure_caddy()"):
        return
    current = _declared_version()
    if current is None:
        _check(False, "CADDY_CONFIG_VERSION is readable from packages.sh")
        return
    old = f"# mcapp-caddy-config-version: {int(current) - 1} host=mcapp\nOLDCONF"

    bare = _run_configure_caddy(old, restart_fails=True, mode="bare")
    _check(
        bare is not None
        and bare[0] == 1
        and bare[1] is not None
        and "OLDCONF" in bare[1]
        and bare[2] == 2
        and not bare[3],
        "a failed restart rolls back with errexit live (the bare mcapp.sh caller)",
    )
    second = _run_configure_caddy(old, restart_fails=True, mode="second-run")
    _check(
        second is not None
        and second[0] == 0
        and second[1] is not None
        and "NEWCONF" in second[1]
        and second[2] == 3
        and not second[3],
        "after a rollback the next run retries the upgrade",
    )
    damaged = _run_configure_caddy(old, restart_fails=False, mode="installed-invalid")
    _check(
        damaged is not None
        and damaged[0] == 1
        and damaged[1] is not None
        and "OLDCONF" in damaged[1]
        and damaged[2] == 0
        and not damaged[3],
        "a damaged install is rolled back and fails, so the epoch does not advance",
    )

    failed = _run_configure_caddy(old, restart_fails=True)
    _check(
        failed is not None
        and failed[0] == 1
        and failed[1] is not None
        and "OLDCONF" in failed[1]
        and "NEWCONF" not in failed[1]
        and failed[2] == 2
        and not failed[3],
        "failed restart: old Caddyfile restored, Caddy restarted again, still rc=1, "
        f"backup removed ({failed})",
    )

    ok = _run_configure_caddy(old, restart_fails=False)
    _check(
        ok is not None
        and ok[0] == 0
        and ok[1] is not None
        and "FRESHCONF" in ok[1]
        and ok[2] == 1
        and not ok[3],
        f"successful restart: new Caddyfile kept, backup removed ({ok})",
    )

    first = _run_configure_caddy(None, restart_fails=False)
    _check(
        first is not None and first[0] == 0 and first[1] is not None and "FRESHCONF" in first[1],
        f"first install with no old Caddyfile still works ({first})",
    )

    first_bad = _run_configure_caddy(None, restart_fails=True)
    _check(
        first_bad is not None and first_bad[0] == 1 and first_bad[2] == 1 and not first_bad[3],
        f"first install with a failing restart: rc=1, no restore attempt, no backup ({first_bad})",
    )

    if shutil.which("jq") is None:
        print("    (jq not on PATH - TLS_ENABLED check skipped: NOT VERIFIED here)")
        return
    tls_conf = "TLSPROVIDERCONF"
    tls = _run_configure_caddy(tls_conf, restart_fails=True, tls=True)
    _check(
        tls is not None
        and tls[0] == 0
        and tls[1] is not None
        and tls[1].strip() == tls_conf
        and tls[2] == 0
        and not tls[3],
        f"TLS_ENABLED box: Caddyfile untouched, no restart, no backup ({tls})",
    )


def run_caddy_config_tests() -> bool:
    """Run the suite. Returns True when every check passed."""
    print("\n🧪 Testing Caddy front-door config (template, caddy_site, probe):")
    _failures.clear()
    _test_template()
    _test_webapp_routes()
    _test_caddy_site()
    _test_template_pick()
    _test_probe()
    _test_rollback()
    if _failures:
        print(f"\n  {len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"    - {failure}")
    return not _failures


if __name__ == "__main__":
    raise SystemExit(0 if run_caddy_config_tests() else 1)
