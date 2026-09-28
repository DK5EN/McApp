"""Startup regression suite for ``release.sh``'s release-notes handling.

Three real problems, all in the same corner of ``release.sh``:

1. ``upload_production`` used to pass the ENTIRE ``doc/release-history.md`` as
   ``--notes-file``, so every GitHub release page grew to contain the whole
   project history instead of just its own notes.
2. Nothing checked that the section sitting under "press Enter" at the notes
   prompt was actually the release being cut -- the v1.6.8-published-
   as-v1.6.4 incident: the operator answered the prompt against a file whose
   top section was still the previous release's, and it published anyway.
3. ``commit_release_notes`` only ever looked at ``doc/release-history.md``,
   so once notes started being condensed into a companion
   ``doc/archive/release-history-full.md`` on every release, that archive
   file could be left modified but uncommitted.

This suite pins the fix for all three:

- ``extract_release_notes_section`` prints only the lines under one release's
  own ``## v<version>`` heading (up to the next ``## ``), trimmed, with a
  footer pointing at the full history -- never the rest of the file.
- ``validate_release_notes_top`` aborts (exit 1) before any commit, merge or
  tag if the file's FIRST ``## `` heading is not that release's own, or if
  its section is empty.
- ``commit_release_notes`` stages and commits BOTH
  ``doc/release-history.md`` and ``doc/archive/release-history-full.md``,
  independently, only for whichever of the two actually changed.

Like ``release_prep_tests.py`` this drives the real functions from
``release.sh`` via subprocess rather than re-implementing their logic, and
reuses that module's extraction helper and git test-repo plumbing so the two
suites cannot drift apart on how they poke at the same file.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from release_prep_tests import _extract_function, _run

_BASH = shutil.which("bash")
_GIT = shutil.which("git")

_REPO = Path(__file__).resolve().parent.parent
_RELEASE_SH = _REPO / "scripts" / "release.sh"

_NOTES_FUNCTIONS = ("extract_release_notes_section", "validate_release_notes_top")
_COMMIT_FUNCTIONS = ("commit_release_notes",)

_GITHUB_REPO_RE = re.compile(r"^readonly GITHUB_REPO=.*$", re.MULTILINE)

_Record = Callable[..., None]


def _extract_github_repo_line(source: str) -> str:
    """Pull the `readonly GITHUB_REPO="..."` declaration out of release.sh.

    extract_release_notes_section's footer is built from this constant; pulling
    it by regex (rather than hardcoding the value here) means a rename is
    picked up automatically instead of silently testing a stale repo name.
    """
    match = _GITHUB_REPO_RE.search(source)
    if match is None:
        raise AssertionError(
            "release.sh no longer declares `readonly GITHUB_REPO=...` — "
            "this suite extracts it by pattern and cannot test the footer without it."
        )
    return match.group(0)


def _notes_functions_block(source: str) -> str:
    github_repo_line = _extract_github_repo_line(source)
    functions = "\n".join(_extract_function(source, name) for name in _NOTES_FUNCTIONS)
    return f"{github_repo_line}\n{functions}"


_EXTRACT_DRIVER = """set -euo pipefail
log_info() {{ :; }}
log_warn() {{ :; }}
log_error() {{ echo "ERROR: $*" >&2; }}
{functions}
extract_release_notes_section "$1" "$2"
"""

_VALIDATE_DRIVER = """set -euo pipefail
PROJECT_DIR="$1"
log_info() {{ :; }}
log_warn() {{ :; }}
log_error() {{ echo "ERROR: $*" >&2; }}
{functions}
validate_release_notes_top "$2"
"""

_COMMIT_DRIVER = """set -euo pipefail
PROJECT_DIR="$1"
log_info() {{ :; }}
log_warn() {{ :; }}
log_error() {{ echo "ERROR: $*" >&2; }}
{functions}
commit_release_notes "$2"
"""


def _run_extract(tmp: Path, version: str, file: Path) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    source = _RELEASE_SH.read_text(encoding="utf-8")
    driver = tmp / "extract_driver.sh"
    driver.write_text(
        _EXTRACT_DRIVER.format(functions=_notes_functions_block(source)), encoding="utf-8"
    )
    return subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
        [_BASH, str(driver), version, str(file)],
        capture_output=True,
        text=True,
        check=False,  # exercising failure exit codes is the point of these cases
    )


def _run_validate(tmp: Path, project_dir: Path, version: str) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    source = _RELEASE_SH.read_text(encoding="utf-8")
    driver = tmp / "validate_driver.sh"
    driver.write_text(
        _VALIDATE_DRIVER.format(functions=_notes_functions_block(source)), encoding="utf-8"
    )
    return subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
        [_BASH, str(driver), str(project_dir), version],
        capture_output=True,
        text=True,
        check=False,  # exercising failure exit codes is the point of these cases
    )


def _run_commit(tmp: Path, project_dir: Path, version: str) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    source = _RELEASE_SH.read_text(encoding="utf-8")
    functions = "\n".join(_extract_function(source, name) for name in _COMMIT_FUNCTIONS)
    driver = tmp / "commit_driver.sh"
    driver.write_text(_COMMIT_DRIVER.format(functions=functions), encoding="utf-8")
    return subprocess.run(  # noqa: S603 - fixed argv, absolute binaries
        [_BASH, str(driver), str(project_dir), version],
        capture_output=True,
        text=True,
        check=False,  # commit_release_notes can exit non-zero; cases inspect the result
    )


_FIXTURE_HISTORY = """# Release History

## v2.0.2 (2026-01-02)

Lead paragraph for 2.0.2.

### Highlights

- item A only in 2.0.2

## v2.0.1 (2026-01-01)

Lead paragraph for 2.0.1.

### Highlights

- item B only in 2.0.1

## Earlier releases, in brief

Intro text for the brief section.

### v2.0.0 (2025-12-31)

- old bullet only in 2.0.0
"""


def _case_extraction(record: _Record) -> None:
    """Case (a): extraction returns only the target release's own section."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        history = tmp / "release-history.md"
        history.write_text(_FIXTURE_HISTORY, encoding="utf-8")

        result = _run_extract(tmp, "2.0.1", history)

        record(
            "extract_release_notes_section exits 0 for an existing section",
            result.returncode == 0,
            f"(exit {result.returncode}: {result.stderr.strip()[:200]})",
        )
        out = result.stdout

        record("extraction contains the target release's own text", "item B only in 2.0.1" in out)
        record(
            "extraction excludes the PREVIOUS (newer) release's section",
            "item A only in 2.0.2" not in out and "## v2.0.2" not in out,
        )
        record(
            "extraction excludes the NEXT '## ' section",
            "## Earlier releases, in brief" not in out,
        )
        record(
            "extraction excludes a '### ' subsection belonging to another release",
            "item old bullet" not in out and "old bullet only in 2.0.0" not in out,
        )
        record(
            "extraction keeps a '### ' subsection that belongs to the TARGET release",
            "### Highlights" in out,
        )
        record(
            "extraction appends the full-history footer",
            "Full release history: https://github.com/DK5EN/McApp/blob/main/doc/release-history.md"
            in out,
        )
        record(
            "extraction has no leading/trailing blank-line padding around the body",
            not out.startswith("\n") and out.split("\n\n")[0] == "Lead paragraph for 2.0.1.",
        )


def _case_guard_pass_and_fail(record: _Record) -> None:
    """Case (b): guard passes on the current version, fails on a stale one
    (the v1.6.8-published-as-v1.6.4 incident)."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project = tmp / "proj"
        (project / "doc").mkdir(parents=True)
        (project / "doc" / "release-history.md").write_text(_FIXTURE_HISTORY, encoding="utf-8")

        ok_result = _run_validate(tmp, project, "2.0.2")
        record(
            "validate_release_notes_top passes when the top heading IS the version being released",
            ok_result.returncode == 0,
            f"(exit {ok_result.returncode}: {ok_result.stderr.strip()[:200]})",
        )

        stale_result = _run_validate(tmp, project, "2.0.1")
        record(
            "validate_release_notes_top fails (exit 1) when the top heading is an OLDER version",
            stale_result.returncode == 1,
            f"(exit {stale_result.returncode})",
        )
        record(
            "the failure names the heading actually found",
            "## v2.0.2 (2026-01-02)" in stale_result.stderr,
            f"(stderr: {stale_result.stderr.strip()[:300]})",
        )


def _case_guard_empty_section(record: _Record) -> None:
    """Case (c): guard fails when the top heading matches but its section is empty."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project = tmp / "proj"
        (project / "doc").mkdir(parents=True)
        empty_history = (
            "# Release History\n\n## v2.0.5 (2026-02-01)\n\n## v2.0.4 (2026-01-15)\n\nbody\n"
        )
        (project / "doc" / "release-history.md").write_text(empty_history, encoding="utf-8")

        result = _run_validate(tmp, project, "2.0.5")
        record(
            "validate_release_notes_top fails (exit 1) when the matching top section is empty",
            result.returncode == 1,
            f"(exit {result.returncode}, stderr: {result.stderr.strip()[:300]})",
        )


def _init_local_repo(path: Path, files: dict[str, str]) -> None:
    assert _GIT is not None
    path.mkdir(parents=True)
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    _run([_GIT, "init", "-b", "development"], path)
    _run([_GIT, "config", "user.email", "test@example.com"], path)
    _run([_GIT, "config", "user.name", "Test"], path)
    _run([_GIT, "add", "-A"], path)
    _run([_GIT, "commit", "-m", "initial"], path)


def _last_commit_files(project: Path) -> list[str]:
    """Files changed by the newest commit, or [] when commit_release_notes
    made no commit at all (only the initial commit exists). `git diff HEAD~1`
    would raise there and abort the suite instead of recording a FAIL."""
    assert _GIT is not None
    if _run([_GIT, "rev-list", "--count", "HEAD"], project) == "1":
        return []
    return _run([_GIT, "diff", "--name-only", "HEAD~1", "HEAD"], project).splitlines()


def _case_commit_archive_only(record: _Record) -> None:
    """Case (d): commit_release_notes commits doc/archive/release-history-full.md
    when only it changed -- doc/release-history.md must be left alone, not
    re-added/re-committed pointlessly."""
    assert _GIT is not None
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project = tmp / "proj"
        _init_local_repo(
            project,
            {
                "doc/release-history.md": _FIXTURE_HISTORY,
                "doc/archive/release-history-full.md": "# Archive\n\n(nothing archived yet)\n",
            },
        )

        # Only the archive changes -- release-history.md is untouched.
        archive = project / "doc" / "archive" / "release-history-full.md"
        archive.write_text(
            "# Archive\n\n## v2.0.2 (2026-01-02)\n\nLead paragraph for 2.0.2.\n", encoding="utf-8"
        )

        result = _run_commit(tmp, project, "2.0.3")
        record(
            "commit_release_notes exits 0 with only the archive file changed",
            result.returncode == 0,
            f"(exit {result.returncode}: {result.stderr.strip()[:300]})",
        )

        commit_message = _run([_GIT, "log", "-1", "--format=%s"], project)
        record(
            "the new commit carries the expected message",
            commit_message == "[docs] Add release notes for v2.0.3",
            f"(got {commit_message!r})",
        )

        changed_files = _last_commit_files(project)
        record(
            "the commit touches the archive file",
            "doc/archive/release-history-full.md" in changed_files,
            f"(changed: {changed_files})",
        )
        record(
            "the commit does NOT touch release-history.md, which never changed",
            "doc/release-history.md" not in changed_files,
            f"(changed: {changed_files})",
        )

        status = _run([_GIT, "status", "--porcelain"], project)
        record(
            "the working tree is clean afterwards (nothing left uncommitted)",
            status == "",
            f"(status: {status!r})",
        )


_FIXTURE_PREFIX = """# Release History

## v2.0.17 (2026-09-28)

- item only in 2.0.17

## v2.0.1 (2026-08-22)

- item only in 2.0.1
"""


def _case_prefix_versions(record: _Record) -> None:
    """Case (e): `## v2.0.17` must not satisfy a lookup for 2.0.1. Pins the
    `( |$)` anchor in both the awk (extraction) and the grep (guard); the
    2.0.2-over-2.0.1 fixture above cannot, since neither is a prefix of the
    other."""
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project = tmp / "proj"
        (project / "doc").mkdir(parents=True)
        history = project / "doc" / "release-history.md"
        history.write_text(_FIXTURE_PREFIX, encoding="utf-8")

        guard = _run_validate(tmp, project, "2.0.1")
        record(
            "validate_release_notes_top rejects 2.0.1 when `## v2.0.17` is on top",
            guard.returncode == 1,
            f"(exit {guard.returncode})",
        )

        extract = _run_extract(tmp, "2.0.1", history)
        record(
            "extract_release_notes_section 2.0.1 returns 2.0.1's body, not 2.0.17's",
            extract.returncode == 0
            and "item only in 2.0.1" in extract.stdout
            and "item only in 2.0.17" not in extract.stdout,
            f"(exit {extract.returncode}, stdout: {extract.stdout.strip()[:200]!r})",
        )


def _case_commit_both(record: _Record) -> None:
    """Case (f): both notes files changed -> ONE commit carrying both, tree clean.
    The archive-only case cannot tell "stage every changed file" from "stage the
    first one"."""
    assert _GIT is not None
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project = tmp / "proj"
        _init_local_repo(
            project,
            {
                "doc/release-history.md": _FIXTURE_HISTORY,
                "doc/archive/release-history-full.md": "# Archive\n",
            },
        )
        (project / "doc" / "release-history.md").write_text(
            _FIXTURE_HISTORY.replace("## v2.0.2", "## v2.0.3 (2026-01-03)\n\n- new\n\n## v2.0.2"),
            encoding="utf-8",
        )
        (project / "doc" / "archive" / "release-history-full.md").write_text(
            "# Archive\n\n## v2.0.2 (2026-01-02)\n\nLead paragraph for 2.0.2.\n",
            encoding="utf-8",
        )

        result = _run_commit(tmp, project, "2.0.3")
        changed_files = sorted(_last_commit_files(project))
        record(
            "commit_release_notes commits BOTH notes files in one commit",
            result.returncode == 0
            and changed_files == ["doc/archive/release-history-full.md", "doc/release-history.md"],
            f"(exit {result.returncode}, changed: {changed_files})",
        )
        status = _run([_GIT, "status", "--porcelain"], project)
        record(
            "the working tree is clean after committing both files",
            status == "",
            f"(status: {status!r})",
        )


def run_release_notes_tests() -> bool:
    """Return True if every invariant holds."""
    if _BASH is None or _GIT is None:
        print("release_notes: SKIPPED - bash or git not on PATH")
        return True

    if not _RELEASE_SH.exists():
        # release.sh is developer-machine tooling, deliberately absent from
        # any tarball -- see release_prep_tests.py's identical guard for why.
        print(
            "release_notes: SKIPPED - NOT VERIFIED "
            "(no scripts/release.sh in this tree; expected when run from a deployed slot)"
        )
        return True

    passed = 0
    failed = 0

    def record(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"PASS | {label}")
        else:
            failed += 1
            print(f"FAIL | {label} {detail}")

    try:
        _case_extraction(record)
        _case_guard_pass_and_fail(record)
        _case_guard_empty_section(record)
        _case_commit_archive_only(record)
        _case_prefix_versions(record)
        _case_commit_both(record)
    except AssertionError as exc:
        # _extract_function / _extract_github_repo_line raise when release.sh
        # no longer defines what this suite extracts by name/pattern (e.g. a
        # rename). Left uncaught this would abort every suite run after this
        # one in the same process -- see release_prep_tests.py's identical note.
        record("release.sh function extraction", False, str(exc))
    except subprocess.CalledProcessError as exc:
        # A fixture git command failed; record it rather than let it abort
        # every suite after this one in run_startup_tests.py.
        record("fixture git command", False, f"{exc.cmd}: {exc.stderr}")

    print(f"release_notes: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_release_notes_tests() else 1)
