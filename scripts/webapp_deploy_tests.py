"""Startup regression suite for the webapp deploy path.

Guards two defects found by diffing mcapp.local's serve directory against the
v2.0.1 release artifact.

**1. The deploy overlaid instead of replacing.** Both callers ran
``cp -a src/. "$WEBAPP_DIR/"``, which writes the new build on top of the old and
removes nothing. Vite emits content-hashed filenames, so nothing is ever
overwritten and every release left its whole predecessor behind: **868 files
served where the release contains 70** — 26 MB, 721 files in ``assets/`` alone.
The leftovers are inert, because nothing references them, which is exactly why
this went unnoticed through every release to date.

**2. macOS AppleDouble sidecars.** 133 ``._*`` files had reached the serve
directory. They are created by *macOS tar*, which emits a ``._<name>`` member for
any file carrying extended attributes — so they were manufactured during the
release build, not copied out of ``dist/``. ``release.sh`` now sets
``COPYFILE_DISABLE=1`` (plus an ``--exclude``), and the deploy strips any that
survive by another route.

The replace is staged and swapped by two renames in one filesystem, so the two
failure modes that matter are pinned too: a failed copy must leave the live tree
untouched, and a failed install must restore it.

**3. An unchecked chown/chmod fell through to the swap.** Commit ``35c79f6``
found that ``chown -R www-data:www-data "$staging"`` and ``chmod -R 755
"$staging"`` were unchecked, and both call sites invoke
``install_webapp_tree`` as ``... || return 1`` / ``if ! ...`` — which
disables errexit for the WHOLE function body, not just the guarded command —
so a failing chown or chmod used to reach the two ``mv``s and activate a
staged tree nobody had actually chowned/chmodded. Every step is now checked
by hand; this suite pins both failures directly by making them actually fail.

Like ``caddy_config_tests.py`` this sources the real shell file via subprocess
rather than reimplementing its logic — ``deploy.sh`` sources cleanly standalone
(no ``readonly`` declarations, no top-level statements), so the whole file is
loaded instead of extracting one function by regex; a rename or reflow that a
regex would silently stop matching cannot go unnoticed this way.
``chown``/``chmod`` are stubbed on PATH because the function targets
``www-data`` and the suite does not run as root — stubbing keeps the
production code strict instead of loosening it for the test.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Protocol

_BASH = shutil.which("bash")

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO / "bootstrap" / "lib" / "deploy.sh"
_RELEASE_SH = _REPO / "scripts" / "release.sh"
_PRECOMPRESS = _REPO / "scripts" / "precompress_webapp.mjs"
_PACKAGES_SH = _REPO / "bootstrap" / "lib" / "packages.sh"
_NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level `name() { ... }` block out of a shell file.

    Only used for release.sh's build_tarball(): release.sh declares `readonly
    PROJECT_DIR`/`WEBAPP_DIR` from BASH_SOURCE, so it cannot be sourced whole
    the way deploy.sh is below.
    """
    pattern = rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$"
    match = re.search(pattern, source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(
            f"{name}() is no longer defined as a top-level shell function — "
            "this suite extracts it by name and cannot test what it cannot find."
        )
    return match.group(0)


# Matches production exactly: `set -eo pipefail` (bootstrap/mcapp.sh:25) plus a
# separate `set -u` (:60). A prior version of this driver ran `set -uo
# pipefail` — no errexit — which let a case pass even if a stubbed failure
# were silently swallowed by the very `|| return 1` guards this suite exists
# to pin.
#
# The call itself is deliberately `if install_webapp_tree ...; then/else`,
# NOT a bare statement. Both real call sites invoke the function as
# `... || return 1` / `if ! ...`, and bash disables errexit for the WHOLE
# function body when it is called as the condition of a conditional — the
# exact trap the deploy.sh comments describe. A bare statement here would
# instead let THIS driver's own `set -e` abort the script on the very first
# unguarded failing command inside the function, which would report a
# reverted (unchecked) chown/chmod as a failure for the wrong reason — our
# errexit, not the function's own since-added checks — and mask the real
# regression: a reverted guard falling through to the swap unnoticed.
_DRIVER = """set -eo pipefail
set -u
WEBAPP_DIR="$1"
SRC="$2"
source "{deploy_sh}"
log_error() {{ echo "ERROR: $*" >&2; }}
log_warn() {{ :; }}
log_info() {{ :; }}
log_ok() {{ :; }}
if install_webapp_tree "$SRC"; then
  rc=0
else
  rc=$?
fi
echo "rc=$rc"
"""


def _stub_bin(tmp: Path, extra: dict[str, str] | None = None) -> Path:
    """chown/chmod stubs — the real ones need root and www-data to exist.

    `extra` overrides one or more stub bodies (e.g. `exit 1`) for a single
    case without disturbing the rest.
    """
    extra = extra or {}
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name in ("chown", "chmod"):
        stub = bin_dir / name
        stub.write_text(extra.get(name, "#!/bin/sh\nexit 0\n"), encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _run_install(
    tmp: Path, webapp_dir: Path, src: Path, stub_overrides: dict[str, str] | None = None
) -> tuple[int, str]:
    assert _BASH is not None
    driver = tmp / "driver.sh"
    driver.write_text(_DRIVER.format(deploy_sh=_DEPLOY_SH), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{_stub_bin(tmp, stub_overrides)}:{env['PATH']}"

    result = subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
        [_BASH, str(driver), str(webapp_dir), str(src)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    match = re.search(r"rc=(\d+)", result.stdout)
    return (int(match.group(1)) if match else -1), result.stderr


def _tree(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


class Recorder(Protocol):
    def __call__(self, label: str, ok: bool, detail: str = "") -> None: ...


def _safe_extract(record: Recorder, source: str, name: str) -> str | None:
    """Wrap `_extract_function` so a renamed/reshaped shell function fails
    this ONE case instead of raising an uncaught `AssertionError` past
    `run_webapp_deploy_tests` — which would abort the whole
    `run_startup_tests.py` run and silently skip every suite registered
    after this one.
    """
    try:
        return _extract_function(source, name)
    except AssertionError as exc:
        record(f"{name}() is extractable from release.sh's source", False, f"({exc})")
        return None


def _case_replaces_the_tree(record: Recorder) -> None:
    """A previous release's content-hashed assets must not survive a deploy."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        (webapp / "assets").mkdir(parents=True)
        (webapp / "index.html").write_text("OLD", encoding="utf-8")
        (webapp / "version.html").write_text("v1.0.0", encoding="utf-8")
        (webapp / "assets" / "index-OLDHASH.js").write_text("stale", encoding="utf-8")
        (webapp / "assets" / "HelpView-OLDHASH.js").write_text("stale", encoding="utf-8")

        src = tmp / "new"
        (src / "assets").mkdir(parents=True)
        (src / "index.html").write_text("NEW", encoding="utf-8")
        (src / "version.html").write_text("v2.0.0", encoding="utf-8")
        (src / "assets" / "index-NEWHASH.js").write_text("fresh", encoding="utf-8")

        rc, stderr = _run_install(tmp, webapp, src)
        record(
            "install succeeds over an existing tree", rc == 0, f"(rc={rc} {stderr.strip()[:120]})"
        )

        installed = _tree(webapp)
        record(
            "stale content-hashed assets from the previous release are GONE",
            "assets/index-OLDHASH.js" not in installed
            and "assets/HelpView-OLDHASH.js" not in installed,
            f"(tree: {sorted(installed)})",
        )
        record(
            "the new build is fully present",
            installed == {"index.html", "version.html", "assets/index-NEWHASH.js"},
            f"(tree: {sorted(installed)})",
        )
        record(
            "overwritten files carry the NEW content",
            (webapp / "index.html").read_text(encoding="utf-8") == "NEW",
            "",
        )
        record(
            "no .new/.old staging directories are left behind",
            not (tmp / "webapp.new").exists() and not (tmp / "webapp.old").exists(),
            "",
        )


def _case_strips_appledouble(record: Recorder) -> None:
    """macOS `._*` sidecars must never reach the serve directory."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        src = tmp / "new"
        (src / "assets").mkdir(parents=True)
        (src / "index.html").write_text("NEW", encoding="utf-8")
        (src / "._index.html").write_text("applesauce", encoding="utf-8")
        (src / "assets" / "._app.js").write_text("applesauce", encoding="utf-8")
        (src / "assets" / "app.js").write_text("real", encoding="utf-8")

        rc, _ = _run_install(tmp, webapp, src)
        installed = _tree(webapp)
        record("install succeeds onto a fresh box (no existing tree)", rc == 0, f"(rc={rc})")
        record(
            "AppleDouble ._* sidecars are not installed, at any depth",
            not any(Path(p).name.startswith("._") for p in installed),
            f"(tree: {sorted(installed)})",
        )
        record(
            "the real files beside them survive",
            installed == {"index.html", "assets/app.js"},
            f"(tree: {sorted(installed)})",
        )


def _case_failure_is_safe(record: Recorder) -> None:
    """A failed copy must leave the live tree exactly as it was."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        webapp.mkdir()
        (webapp / "index.html").write_text("LIVE", encoding="utf-8")

        rc, _ = _run_install(tmp, webapp, tmp / "does-not-exist")
        record("a missing source is reported as a failure", rc != 0, f"(rc={rc})")
        record(
            "the live tree is untouched after a failed copy",
            _tree(webapp) == {"index.html"}
            and (webapp / "index.html").read_text(encoding="utf-8") == "LIVE",
            f"(tree: {sorted(_tree(webapp))})",
        )
        record(
            "no staging directory is left behind after a failure",
            not (tmp / "webapp.new").exists(),
            "",
        )


def _run_chown_chmod_failure_case(record: Recorder, tool: str) -> None:
    """Shared body for the chown/chmod failure cases below.

    Regression for commit 35c79f6: `chown -R www-data:www-data "$staging"`
    and `chmod -R 755 "$staging"` were unchecked, and both call sites invoke
    `install_webapp_tree` as `... || return 1` / `if ! ...` — which disables
    errexit for the WHOLE function body, not just the tested command — so a
    failing chown/chmod fell straight through to the two `mv`s and activated
    a staged tree nobody had actually chowned/chmodded.
    """
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        webapp = tmp / "webapp"
        webapp.mkdir()
        (webapp / "index.html").write_text("OLD", encoding="utf-8")

        src = tmp / "new"
        src.mkdir()
        (src / "index.html").write_text("NEW", encoding="utf-8")

        rc, _ = _run_install(tmp, webapp, src, stub_overrides={tool: "#!/bin/sh\nexit 1\n"})
        record(f"a failing {tool} is reported as a failure", rc != 0, f"(rc={rc})")
        record(
            f"the live tree still has the OLD content after a failing {tool}",
            (webapp / "index.html").read_text(encoding="utf-8") == "OLD",
            "",
        )
        record(
            f"no .new/.old staging directories survive a failing {tool}",
            not (tmp / "webapp.new").exists() and not (tmp / "webapp.old").exists(),
            "",
        )


def _case_chown_failure_is_safe(record: Recorder) -> None:
    """A failing chown must not reach the swap — see _run_chown_chmod_failure_case."""
    _run_chown_chmod_failure_case(record, "chown")


def _case_chmod_failure_is_safe(record: Recorder) -> None:
    """A failing chmod must not reach the swap — same regression as chown above."""
    _run_chown_chmod_failure_case(record, "chmod")


# release.sh declares `readonly PROJECT_DIR`/`WEBAPP_DIR` computed from
# BASH_SOURCE, so build_tarball() cannot be sourced into a driver the way
# deploy.sh is above — PROJECT_DIR/WEBAPP_DIR here are plain (non-readonly)
# driver variables the extracted function body reads instead.
_TARBALL_DRIVER = """set -eo pipefail
set -u
PROJECT_DIR="{project_dir}"
WEBAPP_DIR="{webapp_dir}"
log_info() {{ :; }}
log_warn() {{ echo "WARN: $*" >&2; }}
_CLEANUP_TMPDIR=""
_CLEANUP_TARBALL=""
{function}
build_tarball "{version}" {include_tests}
"""


def _write_minimal_release_fixture(tmp: Path) -> tuple[Path, Path]:
    """A minimal PROJECT_DIR/WEBAPP_DIR pair satisfying every cp/find in
    build_tarball(), with a literal `._stray` file planted in dist/ next to
    a normal one — standing in for a dist/ built on macOS without
    COPYFILE_DISABLE, which is exactly the case the source-level --exclude
    exists to cover.
    """
    project = tmp / "project"
    webapp = tmp / "webapp"

    (project / "src" / "mcapp").mkdir(parents=True)
    (project / "src" / "mcapp" / "__init__.py").write_text("", encoding="utf-8")
    # Package DATA, not code: the corpora and wire contracts that ship inside
    # the package and that its *_tests.py modules read by path. build_tarball()
    # used to copy `*.py` only, so it shipped every test module and none of
    # their data -- all eleven .json files were silently absent from every
    # deployed slot (found on mcapp.local at v2.0.11).
    (project / "src" / "mcapp" / "contract").mkdir(parents=True)
    (project / "src" / "mcapp" / "contract" / "push_contract.json").write_text(
        '{"version": 11}', encoding="utf-8"
    )
    (project / "src" / "mcapp" / "contract" / "classifier_contract.json").write_text(
        '{"taxonomy_version": 6}', encoding="utf-8"
    )
    (project / "src" / "mcapp" / "contract" / "axes_contract.json").write_text(
        '{"schema": 1}', encoding="utf-8"
    )
    (project / "src" / "mcapp" / "group_labels_icssw.json").write_text(
        '{"entries": []}', encoding="utf-8"
    )
    (project / "src" / "mcapp" / "storage").mkdir(parents=True)
    (project / "src" / "mcapp" / "contract" / "suppression_vectors.json").write_text(
        '{"version": 2}', encoding="utf-8"
    )
    # A decoy: __pycache__ stays excluded, for data as well as for code.
    (project / "src" / "mcapp" / "__pycache__").mkdir(parents=True)
    (project / "src" / "mcapp" / "__pycache__" / "cached.json").write_text("{}", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

    (project / "ble_service" / "src").mkdir(parents=True)
    (project / "ble_service" / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project / "ble_service" / "pyproject.toml").write_text(
        '[project]\nname = "x"\n', encoding="utf-8"
    )
    (project / "ble_service" / "README.md").write_text("ble\n", encoding="utf-8")

    (project / "bootstrap").mkdir()
    (project / "bootstrap" / "mcapp.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    # A test module beside the runtime code, and the gate runner beside the
    # update runner: production must carry neither, a dev build both.
    (project / "src" / "mcapp" / "storage" / "suppression_tests.py").write_text(
        "", encoding="utf-8"
    )
    (project / "src" / "mcapp" / "commands").mkdir(parents=True)
    (project / "src" / "mcapp" / "commands" / "tests.py").write_text("", encoding="utf-8")
    # The /api/timezone worker is RUNTIME code (spawned by sse_routes/weather.py
    # as `python -I <file>`) and its suite sits beside the route: production
    # must ship the first and not the second.
    (project / "src" / "mcapp" / "tz_lookup_worker.py").write_text("", encoding="utf-8")
    (project / "src" / "mcapp" / "sse_routes").mkdir(parents=True)
    (project / "src" / "mcapp" / "sse_routes" / "timezone_tests.py").write_text(
        "", encoding="utf-8"
    )

    (project / "scripts").mkdir()
    (project / "scripts" / "update-runner.py").write_text("", encoding="utf-8")
    (project / "scripts" / "run_startup_tests.py").write_text("", encoding="utf-8")
    # build_tarball() runs the pre-compressor from PROJECT_DIR/scripts.
    shutil.copy(_PRECOMPRESS, project / "scripts" / "precompress_webapp.mjs")

    (webapp / "dist").mkdir(parents=True)
    (webapp / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (webapp / "dist" / "._stray").write_text("applesauce", encoding="utf-8")
    (webapp / "dist" / "assets").mkdir()
    (webapp / "dist" / "assets" / "app.js").write_text("var a = 1;\n" * 400, encoding="utf-8")

    return project, webapp


def _case_build_tarball_excludes_appledouble(record: Recorder) -> None:
    """Real replacement for a grep on `--exclude='._*'` in release.sh's source
    text: the grep passed or failed on the string's presence alone, so
    reformatting the real invocation (no behaviour change) could turn it red,
    and deleting the invocation while leaving the string in a comment would
    have kept it green. This drives the real build_tarball() against a
    fixture dist/ containing a literal AppleDouble-shaped file and inspects
    the tarball it actually produces.
    """
    if _BASH is None:
        record("bash is available to drive build_tarball()", False, "")
        return

    release_src = _RELEASE_SH.read_text(encoding="utf-8")
    function = _safe_extract(record, release_src, "build_tarball")
    if function is None:
        return

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, webapp = _write_minimal_release_fixture(tmp)
        version = "v0.0.0-test"
        driver = tmp / "driver.sh"
        driver.write_text(
            _TARBALL_DRIVER.format(
                project_dir=project,
                webapp_dir=webapp,
                function=function,
                version=version,
                include_tests="true",
            ),
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
            [_BASH, str(driver)],
            capture_output=True,
            text=True,
            check=False,
        )
        tarball_path = project / f"mcapp-{version}.tar.gz"
        record(
            "build_tarball succeeds against the fixture tree",
            result.returncode == 0 and tarball_path.exists(),
            f"(rc={result.returncode} {result.stderr.strip()[:200]})",
        )
        if not tarball_path.exists():
            return

        with tarfile.open(tarball_path) as tar:
            names = tar.getnames()
        record(
            "the produced tarball does NOT contain the ._stray AppleDouble sidecar",
            not any(Path(n).name == "._stray" for n in names),
            f"(names: {names})",
        )
        record(
            "the produced tarball ships .br and .gz sidecars for a compressible asset",
            any(n.endswith("webapp/assets/app.js.br") for n in names)
            and any(n.endswith("webapp/assets/app.js.gz") for n in names),
            f"(names: {names})",
        )
        record(
            "no sidecar is made for the ._stray AppleDouble file",
            not any("._stray." in n for n in names),
            f"(names: {names})",
        )
        record(
            "the produced tarball DOES contain the real webapp file beside it",
            any(Path(n).name == "index.html" and "webapp" in n for n in names),
            f"(names: {names})",
        )
        record(
            "the produced tarball ships package DATA, not just *.py (contract/push_contract.json)",
            any(n.endswith("src/mcapp/contract/push_contract.json") for n in names),
            f"(names: {names})",
        )
        record(
            "package data is shipped from nested packages too (contract/suppression_vectors.json)",
            any(n.endswith("src/mcapp/contract/suppression_vectors.json") for n in names),
            f"(names: {names})",
        )
        record(
            "__pycache__ data is still excluded, like __pycache__ code",
            not any("__pycache__" in n for n in names),
            f"(names: {names})",
        )
        record(
            "a dev tarball ships the test harness: *_tests.py, tests.py and the gate runner",
            any(Path(n).name.endswith("_tests.py") for n in names)
            and any(Path(n).name == "tests.py" for n in names)
            and any(Path(n).name == "run_startup_tests.py" for n in names),
            f"(names: {names})",
        )


def _case_release_sh_guards(record: Recorder) -> None:
    """COPYFILE_DISABLE is macOS/bsdtar-only and genuinely not reproducible
    under Linux CI's GNU tar (it creates no AppleDouble sidecars to exclude
    in the first place), so this ONE clause stays a narrowly-scoped source
    grep by necessity — everything else the sidecars require is exercised
    behaviourally in _case_build_tarball_excludes_appledouble above.
    """
    release_src = _RELEASE_SH.read_text(encoding="utf-8")
    found = release_src.count("COPYFILE_DISABLE=1 tar")
    record(
        "release.sh sets COPYFILE_DISABLE on both tar invocations "
        "(macOS/bsdtar-only guard; not reproducible on Linux CI's GNU tar, hence a grep)",
        found == 2,
        f"(found {found})",
    )


def _case_build_tarball_production_is_runtime_only(record: Recorder) -> None:
    """A production tarball carries no test harness at all — no `*_tests.py`,
    no `tests.py`, none of the .json corpora, and not the gate runner. The dev
    shape (pinned in the case above) carries all four.

    This is the pair that matters: the two shapes are produced by ONE function
    whose only difference is its second argument, so a predicate edit that
    collapses them shows up here rather than on a box three releases later.
    """
    if _BASH is None:
        record("bash is available to drive build_tarball()", False, "")
        return

    release_src = _RELEASE_SH.read_text(encoding="utf-8")
    function = _safe_extract(record, release_src, "build_tarball")
    if function is None:
        return

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, webapp = _write_minimal_release_fixture(tmp)
        version = "v0.0.0-prod"
        driver = tmp / "driver.sh"
        driver.write_text(
            _TARBALL_DRIVER.format(
                project_dir=project,
                webapp_dir=webapp,
                function=function,
                version=version,
                include_tests="false",
            ),
            encoding="utf-8",
        )
        result = subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
            [_BASH, str(driver)],
            capture_output=True,
            text=True,
            check=False,
        )
        tarball_path = project / f"mcapp-{version}.tar.gz"
        record(
            "build_tarball succeeds in production mode",
            result.returncode == 0 and tarball_path.exists(),
            f"(rc={result.returncode} {result.stderr.strip()[:200]})",
        )
        if not tarball_path.exists():
            return

        with tarfile.open(tarball_path) as tar:
            names = tar.getnames()

        record(
            "production tarball ships NO *_tests.py",
            not any(Path(n).name.endswith("_tests.py") for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball ships NO tests.py (the commands suite)",
            not any(Path(n).name == "tests.py" for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball ships NO .json package data except the runtime data files",
            not any(
                n.endswith(".json")
                and "/src/" in n
                and not n.endswith("src/mcapp/contract/classifier_contract.json")
                and not n.endswith("src/mcapp/contract/axes_contract.json")
                and not n.endswith("src/mcapp/group_labels_icssw.json")
                for n in names
            ),
            f"(names: {names})",
        )
        record(
            "production tarball ships group_labels_icssw.json (group_labels reads it at runtime)",
            any(n.endswith("src/mcapp/group_labels_icssw.json") for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball ships contract/classifier_contract.json (runtime reads it at boot)",
            any(n.endswith("src/mcapp/contract/classifier_contract.json") for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball ships contract/axes_contract.json (message axes load it at boot)",
            any(n.endswith("src/mcapp/contract/axes_contract.json") for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball ships tz_lookup_worker.py (/api/timezone spawns it at runtime)",
            any(n.endswith("src/mcapp/tz_lookup_worker.py") for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball ships NO timezone_tests.py",
            not any(Path(n).name == "timezone_tests.py" for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball ships NO gate runner",
            not any(Path(n).name == "run_startup_tests.py" for n in names),
            f"(names: {names})",
        )
        record(
            "production tarball STILL ships the runtime code and update-runner",
            any(n.endswith("src/mcapp/__init__.py") for n in names)
            and any(Path(n).name == "update-runner.py" for n in names),
            f"(names: {names})",
        )


_WARM_DRIVER = """set -eo pipefail
set -u
source "{deploy_sh}"
log_error() {{ echo "ERROR: $*" >&2; }}
log_warn() {{ :; }}
log_info() {{ :; }}
log_ok() {{ :; }}
warm_tz_worker_bytecode "$1" "$2"
echo "rc=$?"
"""


def _case_tz_worker_bytecode_warmup(record: Recorder) -> None:
    """`uv sync` compiles no bytecode, and the removed warm-up used to pay it:
    the first /api/timezone child costs 6.56 s cold against 1.64 s warm (8 s
    deadline). setup_python_env must run the worker once from the new slot,
    best effort -- a failing worker, a missing venv or a missing worker file
    must never fail the deploy.
    """
    assert _BASH is not None
    deploy_src = _DEPLOY_SH.read_text(encoding="utf-8")
    setup = _safe_extract(record, deploy_src, "setup_python_env")
    if setup is not None:
        record(
            "setup_python_env warms the tz worker after a successful uv sync",
            "warm_tz_worker_bytecode" in setup
            and setup.index("sync_ok=false") < setup.index("warm_tz_worker_bytecode"),
        )

    def run(tmp: Path, target: Path, user: str, with_sudo: bool) -> tuple[str, str]:
        driver = tmp / "warm_driver.sh"
        driver.write_text(_WARM_DRIVER.format(deploy_sh=_DEPLOY_SH), encoding="utf-8")
        env = dict(os.environ)
        if with_sudo:
            bin_dir = tmp / "sbin"
            bin_dir.mkdir()
            sudo = bin_dir / "sudo"
            sudo.write_text(
                f'#!/bin/sh\necho "sudo $*" >> "{tmp}/calls"\nshift 2\nexec "$@"\n',
                encoding="utf-8",
            )
            sudo.chmod(0o755)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
        res = subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
            [_BASH, str(driver), str(target), user],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return res.stdout, res.stderr

    def make_slot(tmp: Path, *, venv: bool, worker: bool) -> Path:
        target = tmp / "slot"
        (target / "src" / "mcapp").mkdir(parents=True)
        if worker:
            (target / "src" / "mcapp" / "tz_lookup_worker.py").write_text("", encoding="utf-8")
        if venv:
            py = target / ".venv" / "bin" / "python"
            py.parent.mkdir(parents=True)
            # A worker that FAILS (exit 3 = timezonefinder not installed): the
            # deploy must carry on regardless.
            py.write_text(f'#!/bin/sh\necho "$PWD|$*" >> "{tmp}/calls"\nexit 3\n', encoding="utf-8")
            py.chmod(0o755)
        return target

    for user, with_sudo, label in (("root", False, "root"), ("martin", True, "slot user via sudo")):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = make_slot(tmp, venv=True, worker=True)
            out, err = run(tmp, target, user, with_sudo)
            calls = (tmp / "calls").read_text(encoding="utf-8") if (tmp / "calls").exists() else ""
            record(
                f"warm-up ({label}) runs the worker isolated from the slot dir; "
                "a failing worker does not fail the deploy",
                "rc=0" in out
                and f"{target}|-I src/mcapp/tz_lookup_worker.py 48 11" in calls
                and (not with_sudo or "sudo -u martin" in calls),
                f"(out={out!r} err={err[:200]!r} calls={calls!r})",
            )

    for venv, worker, label in ((False, True, "no venv"), (True, False, "no worker file")):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = make_slot(tmp, venv=venv, worker=worker)
            out, err = run(tmp, target, "root", False)
            record(
                f"warm-up with {label} is a harmless no-op",
                "rc=0" in out and not (tmp / "calls").exists(),
                f"(out={out!r} err={err[:200]!r})",
            )


_LIGHTTPD_LEGACY_ASSET = "/webapp/assets/x.js"


def _case_lighttpd_shell_no_cache_regex(record: Recorder) -> None:
    """Exercise the real no-cache URL regex out of the lighttpd heredoc.

    The old rule ended in `(index|sw|...)$` and so never matched the bare
    directory URL /webapp/, which is what the SPA is actually opened at: no
    Cache-Control at all on the shell.
    """
    text = _PACKAGES_SH.read_text(encoding="utf-8")
    heredoc = re.search(r"cat > \"\$conf_file\" << 'EOF'\n(.*?)\nEOF\n", text, re.DOTALL)
    if heredoc is None:
        record("lighttpd 99-mcapp.conf heredoc is findable in packages.sh", False)
        return
    rule = re.search(
        r'\$HTTP\["url"\] =~ "([^"]*)" \{\s*'
        r'setenv\.add-response-header \+= \( "Cache-Control" => "no-cache"',
        heredoc.group(1),
    )
    if rule is None:
        record("lighttpd has a no-cache URL rule", False)
        return
    pattern = rule.group(1)
    for url, want in (
        ("/webapp/", True),
        ("/webapp/index.html", True),
        ("/webapp/sw.js", True),
        (_LIGHTTPD_LEGACY_ASSET, False),
    ):
        got = re.search(pattern, url) is not None
        record(f"lighttpd no-cache regex {'matches' if want else 'skips'} {url}", got == want)
    record(
        "lighttpd conf version bumped to 4",
        re.search(r"^readonly LIGHTTPD_MCAPP_CONF_VERSION=4$", text, re.MULTILINE) is not None,
    )


def _case_release_runs_precompress(record: Recorder) -> None:
    release_src = _RELEASE_SH.read_text(encoding="utf-8")
    function = _extract_function(release_src, "build_tarball")
    run = function.find("precompress_webapp.mjs")
    tar = function.find("tar -czf")
    record(
        "build_tarball runs precompress_webapp.mjs before building the tarball",
        run != -1 and tar != -1 and run < tar,
    )
    record(
        "a failing pre-compression aborts the build (guarded, returns 1)",
        re.search(
            r"if ! node [^\n]*precompress_webapp\.mjs[^\n]*; then\n[^\n]*\n\s*return 1", function
        )
        is not None,
    )
    record("release.sh requires node", '"npm" "node"' in release_src)


def _case_precompress_behaviour(record: Recorder) -> None:
    if _NODE is None:
        record("node is available to run precompress_webapp.mjs", False)
        return
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "assets").mkdir()
        text = ("function f() { return 42; }\n" * 300).encode()
        (root / "assets" / "app.js").write_bytes(text)
        (root / "index.html").write_bytes(b"<p>hello</p>\n" * 200)
        (root / "tiny.js").write_bytes(b"x=1;")
        (root / "big.wasm").write_bytes(b"\0" * 5000)
        (root / "._app.js").write_bytes(text)
        (root / "random.json").write_bytes(os.urandom(4000))
        (root / "photo.png").write_bytes(b"\0" * 5000)

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(  # noqa: S603 - fixed argv, absolute binary
                [_NODE, str(_PRECOMPRESS), str(root)], capture_output=True, text=True, check=False
            )

        first = run()
        record(
            "precompress exits 0 and prints a one-line summary",
            first.returncode == 0
            and first.stdout.strip().count("\n") == 0
            and "precompress:" in first.stdout,
            f"(rc={first.returncode} {first.stdout!r} {first.stderr!r})",
        )
        js = root / "assets" / "app.js"
        record(
            "a .gz and a .br sidecar exist for a compressible file",
            (root / "assets" / "app.js.gz").exists() and (root / "assets" / "app.js.br").exists(),
        )
        record(
            ".gz decompresses back byte-identical",
            (root / "assets" / "app.js.gz").exists()
            and gzip.decompress((root / "assets" / "app.js.gz").read_bytes()) == text,
        )
        check = subprocess.run(  # noqa: S603 - fixed argv, absolute binary
            [
                _NODE,
                "-e",
                (
                    "const z=require('node:zlib'),f=require('node:fs');"
                    "const a=z.brotliDecompressSync(f.readFileSync(process.argv[1]));"
                    "process.exit(a.equals(f.readFileSync(process.argv[2]))?0:1)"
                ),
                str(root / "assets" / "app.js.br"),
                str(js),
            ],
            capture_output=True,
            check=False,
        )
        record(".br decompresses back byte-identical", check.returncode == 0)
        record(
            "sidecar mtime equals its source mtime to the millisecond (never newer)",
            all(
                -0.0000005
                <= js.stat().st_mtime - (root / "assets" / f"app.js.{ext}").stat().st_mtime
                < 0.002
                for ext in ("br", "gz")
            ),
        )
        record("html is compressed too", (root / "index.html.br").exists())
        record(
            "a .wasm file is skipped",
            not (root / "big.wasm.br").exists() and not (root / "big.wasm.gz").exists(),
        )
        record("a file under 1024 bytes is skipped", not (root / "tiny.js.br").exists())
        record(
            "an incompressible file gets no sidecar (< 5 percent saving)",
            not (root / "random.json.br").exists() and not (root / "random.json.gz").exists(),
        )
        record("an AppleDouble ._ file gets no sidecar", not (root / "._app.js.br").exists())
        record("a non-listed extension (.png) is skipped", not (root / "photo.png.br").exists())

        before = sorted(p.name for p in root.rglob("*"))
        second = run()
        after = sorted(p.name for p in root.rglob("*"))
        record(
            "a second run is idempotent (same files, no .br.br / .gz.gz)",
            second.returncode == 0
            and before == after
            and not any(n.endswith((".br.br", ".gz.gz", ".br.gz", ".gz.br")) for n in after),
            f"(rc={second.returncode})",
        )
        missing = subprocess.run(  # noqa: S603 - fixed argv, absolute binary
            [_NODE, str(_PRECOMPRESS), str(root / "nope")],
            capture_output=True,
            text=True,
            check=False,
        )
        record("a missing directory exits non-zero", missing.returncode != 0)


def run_webapp_deploy_tests() -> bool:
    """Return True if every invariant holds."""
    if _BASH is None:
        print("webapp_deploy: SKIPPED - bash not on PATH")
        return True

    if not _RELEASE_SH.exists():
        # release.sh is developer-machine tooling and is deliberately NOT in
        # any tarball -- a dev build ships scripts/*.py so the gate can run on
        # the box, but the release script itself has no business on a Pi. These
        # cases drive the real release.sh, so without it there is nothing to
        # verify. "NOT VERIFIED" wording, like config_migration's bash-4 skip,
        # so this can never be misread as coverage. On a source checkout the
        # file is always present, so a skip here is itself the signal.
        print(
            "webapp_deploy: SKIPPED - NOT VERIFIED "
            "(no scripts/release.sh in this tree; expected when run from a deployed slot)"
        )
        return True

    tally = {"passed": 0, "failed": 0}

    def record(label: str, ok: bool, detail: str = "") -> None:
        if ok:
            tally["passed"] += 1
            print(f"PASS | {label}")
        else:
            tally["failed"] += 1
            print(f"FAIL | {label} {detail}")

    for case in (
        _case_replaces_the_tree,
        _case_strips_appledouble,
        _case_failure_is_safe,
        _case_chown_failure_is_safe,
        _case_chmod_failure_is_safe,
        _case_build_tarball_excludes_appledouble,
        _case_build_tarball_production_is_runtime_only,
        _case_release_sh_guards,
        _case_tz_worker_bytecode_warmup,
        _case_lighttpd_shell_no_cache_regex,
        _case_release_runs_precompress,
        _case_precompress_behaviour,
    ):
        case(record)

    print(f"webapp_deploy: {tally['passed']} passed, {tally['failed']} failed")
    return tally["failed"] == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_webapp_deploy_tests() else 1)
