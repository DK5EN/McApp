"""Startup test suite for the standalone update-runner's env construction.

`update-runner.py` is a hyphenated, dependency-free script (not an importable
`mcapp.*` module), so it is loaded here via importlib. The regression guard
that matters is `build_bootstrap_env`: HOME must be the EXECUTING user's home
(root, when the runner drives an update), never the slot user's — otherwise a
root-run `caddy validate` drops a root-owned 0700 `~/.local/share` into the
slot user's home and breaks the later `sudo -u <user> uv sync` with EACCES
(the 2026-07-15 incident). Convention matches the other `*_tests.py` suites:
a `run_*_tests()` returning a bool, wired into `run_startup_tests.py`.
"""

import importlib.util
import json
import os
import pwd
import tempfile
from pathlib import Path
from typing import Any

_RUNNER_PATH = Path(__file__).resolve().parent / "update-runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("update_runner", _RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_update_runner_tests() -> bool:
    runner = _load_runner()
    build = runner.build_bootstrap_env
    root_home = pwd.getpwuid(os.geteuid()).pw_dir
    slot_home = Path("/home/someotheruser")
    failures: list[str] = []

    env = build(slot_home, {"PATH": "/usr/bin"})

    # HOME is the EXECUTING user's home, NOT the slot user's. The old code did
    # `env["HOME"] = str(home)`; both checks below fail on that code.
    if env["HOME"] != root_home:
        failures.append(f"HOME={env['HOME']!r}, expected executing-user home {root_home!r}")
    if env["HOME"] == str(slot_home):
        failures.append("HOME must not be the slot user's home")

    # SUDO_USER is derived from the slot home when absent.
    if env.get("SUDO_USER") != "someotheruser":
        failures.append(f"SUDO_USER={env.get('SUDO_USER')!r}, expected 'someotheruser'")

    # The slot user's ~/.local/bin is prepended so uv is found.
    if not env["PATH"].startswith("/home/someotheruser/.local/bin:"):
        failures.append(f"PATH must start with the slot .local/bin: {env['PATH']!r}")

    # An existing SUDO_USER is preserved, never overwritten.
    env2 = build(slot_home, {"SUDO_USER": "preset", "PATH": "/usr/bin"})
    if env2.get("SUDO_USER") != "preset":
        failures.append("existing SUDO_USER must be preserved")

    # The caller's base env is not mutated in place.
    base = {"PATH": "/usr/bin"}
    build(slot_home, base)
    if "HOME" in base:
        failures.append("base_env must not be mutated in place")

    failures.extend(_test_ble_health_check(runner))
    failures.extend(_test_run_converge_no_local_bootstrap(runner))
    failures.extend(_test_run_converge_failure_path(runner))
    failures.extend(_test_run_converge_success_path(runner))
    failures.extend(_test_run_update_converge_integration(runner))
    failures.extend(_test_activate_swaps_and_installs_webapp(runner))
    failures.extend(_test_activate_bidirectional(runner))
    failures.extend(_test_activate_invalid_slot(runner))
    failures.extend(_test_activate_without_webapp_dir(runner))
    failures.extend(_test_activate_health_failure(runner))
    failures.extend(_test_run_update_health_failure_calls_activate(runner))
    failures.extend(_test_rollback_no_candidate(runner))
    failures.extend(_test_restore_helpers_removed(runner))

    for line in failures:
        print(f"  update_runner: {line}")
    return not failures


def _test_ble_health_check(runner: Any) -> list[str]:
    """The post-deploy gate must notice a dead or unreachable BLE service.

    `run_health_checks` covered mcapp, lighttpd, the webapp and the SSE health
    endpoint but nothing BLE, so a `mcapp-ble` that failed to come back after a
    deploy passed the gate and never triggered the automatic rollback. The
    sharpest case is an API-key rotation (`migrate_config` replaces a weak key)
    where the unit is not restarted: the process keeps the OLD key from its
    environment, stays `active`, and 401s every call from mcapp.
    """
    failures: list[str] = []
    original_config = runner.MCAPP_CONFIG_PATH
    original_unit = runner.BLE_UNIT_PATH

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        config_path = tmp / "config.json"
        unit_path = tmp / "mcapp-ble.service"
        runner.MCAPP_CONFIG_PATH = str(config_path)
        runner.BLE_UNIT_PATH = str(unit_path)
        try:
            # No unit installed -> a UDP-only box must not be failed by this check.
            config_path.write_text(json.dumps({"BLE_MODE": "remote"}), encoding="utf-8")
            if runner._ble_is_expected():
                failures.append("BLE must not be expected when mcapp-ble.service is absent")

            unit_path.write_text("[Unit]\n", encoding="utf-8")
            if not runner._ble_is_expected():
                failures.append("BLE must be expected when the unit exists and mode is remote")

            # An operator who disabled BLE must not have deploys fail on it.
            config_path.write_text(json.dumps({"BLE_MODE": "disabled"}), encoding="utf-8")
            if runner._ble_is_expected():
                failures.append('BLE must not be expected when BLE_MODE is "disabled"')

            # A missing/corrupt config must not raise out of a health check.
            config_path.unlink()
            try:
                runner._ble_is_expected()
            except Exception as exc:
                failures.append(f"_ble_is_expected raised on a missing config: {exc!r}")
            config_path.write_text("{ not json", encoding="utf-8")
            if runner._read_config() != {}:
                failures.append("a corrupt config must read as {} rather than raising")

            # The check is registered in the gate only when BLE is expected.
            config_path.write_text(json.dumps({"BLE_MODE": "remote"}), encoding="utf-8")
            names = _health_check_names(runner)
            if "ble_service" not in names:
                failures.append(f"ble_service must be gated when BLE is expected: {names}")
            config_path.write_text(json.dumps({"BLE_MODE": "disabled"}), encoding="utf-8")
            if "ble_service" in _health_check_names(runner):
                failures.append("ble_service must not be gated when BLE is disabled")
        finally:
            runner.MCAPP_CONFIG_PATH = original_config
            runner.BLE_UNIT_PATH = original_unit

    return failures


def _health_check_names(runner: Any) -> list[str]:
    """Names `run_health_checks` would gate on, without running any check.

    Drives the real function with the probes stubbed out and a recording bus, so
    the registration logic under test is the shipped one rather than a copy.
    """
    recorded: list[str] = []

    class _Bus:
        def publish(self, _topic: str, payload: dict[str, Any]) -> None:
            recorded.append(str(payload["check"]))

    originals = (runner._check_systemd, runner._check_http, runner._check_ble)
    runner._check_systemd = lambda _service: True
    runner._check_http = lambda _url: True
    runner._check_ble = lambda: True
    try:
        runner.run_health_checks(_Bus())
    finally:
        runner._check_systemd, runner._check_http, runner._check_ble = originals
    return recorded


def _write_dummy_bootstrap(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n", encoding="utf-8")


class _FakeCompletedProcess:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = b""
        self.stderr = b""


class _FakeSubprocess:
    """Records subprocess.run() calls instead of touching the real OS.

    Assigned onto the LOADED module's own `subprocess` name (rebinding that
    one global in `runner`'s namespace), never onto the real stdlib
    `subprocess` module object -- every call site below restores the
    original in a `finally` so no other test suite sharing the process is
    affected.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], **_kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(list(cmd))
        return _FakeCompletedProcess()


def _write_slot(
    slots_dir: Path,
    slot_id: int,
    version: str,
    *,
    webapp: bool = True,
    pyproject: bool = True,
) -> None:
    """Lay down a slot directory shaped well enough to pass activation
    validation (and, if `webapp` is set, to prove which slot's bundle got
    installed via a distinct version.html).
    """
    slot_dir = slots_dir / f"slot-{slot_id}"
    slot_dir.mkdir(parents=True, exist_ok=True)
    if pyproject:
        (slot_dir / "pyproject.toml").write_text('[project]\nname = "mcapp"\n', encoding="utf-8")
    if webapp:
        webapp_dir = slot_dir / "webapp"
        webapp_dir.mkdir(parents=True, exist_ok=True)
        (webapp_dir / "version.html").write_text(version, encoding="utf-8")


def _write_meta(
    meta_dir: Path,
    slot_id: int,
    version: str,
    deployed_at: str = "2026-01-01T00:00:00+00:00",
) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    data = {"slot": slot_id, "version": version, "status": "available", "deployed_at": deployed_at}
    (meta_dir / f"slot-{slot_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _set_activation_globals(runner: Any, tmp: Path) -> tuple[Path, Path, Path]:
    """Point the runner's module globals at a fresh temp layout.

    Returns (slots_dir, webapp_dir, db_path).
    """
    slots_dir = tmp / "mcapp-slots"
    meta_dir = slots_dir / "meta"
    webapp_dir = tmp / "var-www-webapp"
    db_path = tmp / "messages.db"
    meta_dir.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"sqlite-fake-db-bytes")

    runner.SLOTS_DIR = slots_dir
    runner.META_DIR = meta_dir
    runner.home = Path("/home/testuser")
    runner.WEBAPP_DIR = webapp_dir
    runner.DB_PATH = db_path
    return slots_dir, webapp_dir, db_path


def _test_run_converge_no_local_bootstrap(runner: Any) -> list[str]:
    """No slot layout at all (no `current` symlink) -> a clean, named failure
    rather than a crash or a fall-back download. Converge must ONLY ever use
    the local slot's own (version-pinned) bootstrap.
    """
    failures: list[str] = []
    originals = (runner.SLOTS_DIR, runner.META_DIR, runner.home)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        runner.SLOTS_DIR = tmp
        runner.META_DIR = tmp / "meta"
        runner.home = Path("/home/testuser")
        try:
            result = runner.run_converge(runner.EventBus())
            if result.get("status") != "failed" or result.get("reason") != "no_local_bootstrap":
                failures.append(f"run_converge with no slot layout: unexpected result {result!r}")
        finally:
            runner.SLOTS_DIR, runner.META_DIR, runner.home = originals
    return failures


def _test_run_converge_failure_path(runner: Any) -> list[str]:
    """A converge failure is reported, never rolled back -- a degraded but
    working box is intentional here; mcapp's watchdog retries later.
    """
    failures: list[str] = []
    originals = (
        runner.SLOTS_DIR,
        runner.META_DIR,
        runner.home,
        runner._run_bootstrap_streaming,
        runner._activate_slot,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        slot0_bootstrap = slots_dir / "slot-0" / "bootstrap" / "mcapp.sh"
        _write_dummy_bootstrap(slot0_bootstrap)
        meta_dir.mkdir(parents=True, exist_ok=True)
        (slots_dir / "current").symlink_to("slot-0")

        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")

        recorded_cmds: list[list[str]] = []
        activate_calls: list[int] = []

        def fake_streaming(cmd: list[str], env: dict, bus: Any) -> bool:
            recorded_cmds.append(cmd)
            return False

        runner._run_bootstrap_streaming = fake_streaming
        runner._activate_slot = lambda target_slot, bus: activate_calls.append(target_slot)

        try:
            result = runner.run_converge(runner.EventBus())
            if result.get("status") != "failed" or result.get("reason") != "converge_error":
                failures.append(f"run_converge failure path: unexpected result {result!r}")
            if not recorded_cmds or recorded_cmds[-1][-1] != "--converge":
                failures.append(
                    f"run_converge failure path: cmd did not end with --converge: {recorded_cmds!r}"
                )
            if not recorded_cmds or str(slot0_bootstrap) not in recorded_cmds[-1]:
                failures.append(
                    "run_converge failure path: cmd missing the active slot's bootstrap "
                    f"path: {recorded_cmds!r}"
                )
            if activate_calls:
                failures.append(
                    f"run_converge must never call _activate_slot, got calls: {activate_calls!r}"
                )
        finally:
            (
                runner.SLOTS_DIR,
                runner.META_DIR,
                runner.home,
                runner._run_bootstrap_streaming,
                runner._activate_slot,
            ) = originals
    return failures


def _test_run_converge_success_path(runner: Any) -> list[str]:
    """A successful bootstrap yields "success" or "warning" depending on the
    post-converge health check, and never rolls back either way.
    """
    failures: list[str] = []
    originals = (
        runner.SLOTS_DIR,
        runner.META_DIR,
        runner.home,
        runner._run_bootstrap_streaming,
        runner._activate_slot,
        runner.run_health_checks,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        slot0_bootstrap = slots_dir / "slot-0" / "bootstrap" / "mcapp.sh"
        _write_dummy_bootstrap(slot0_bootstrap)
        meta_dir.mkdir(parents=True, exist_ok=True)
        (slots_dir / "current").symlink_to("slot-0")

        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")

        activate_calls: list[int] = []
        runner._run_bootstrap_streaming = lambda cmd, env, bus: True
        runner._activate_slot = lambda target_slot, bus: activate_calls.append(target_slot)

        try:
            runner.run_health_checks = lambda bus: True
            result = runner.run_converge(runner.EventBus())
            if result.get("status") != "success":
                failures.append(
                    "run_converge success path (health ok): expected status success, "
                    f"got {result!r}"
                )

            runner.run_health_checks = lambda bus: False
            result_warn = runner.run_converge(runner.EventBus())
            if result_warn.get("status") != "warning":
                failures.append(
                    "run_converge success path (health failed): expected status warning, "
                    f"got {result_warn!r}"
                )

            if activate_calls:
                failures.append(
                    f"run_converge must never call _activate_slot, got calls: {activate_calls!r}"
                )
        finally:
            (
                runner.SLOTS_DIR,
                runner.META_DIR,
                runner.home,
                runner._run_bootstrap_streaming,
                runner._activate_slot,
                runner.run_health_checks,
            ) = originals
    return failures


def _test_run_update_converge_integration(runner: Any) -> list[str]:
    """A converge failure inside run_update must be reported (`"converge":
    "failed"`) but must NEVER downgrade the overall update status -- the
    deploy has already been accepted by the time converge runs.
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "get_active_slot": runner.get_active_slot,
        "get_oldest_slot": runner.get_oldest_slot,
        "snapshot_etc": runner.snapshot_etc,
        "_read_version": runner._read_version,
        "swap_symlink": runner.swap_symlink,
        "run_health_checks": runner.run_health_checks,
        "_check_http": runner._check_http,
        "_run_bootstrap_streaming": runner._run_bootstrap_streaming,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        active_bootstrap = slots_dir / "slot-0" / "bootstrap" / "mcapp.sh"
        target_bootstrap = slots_dir / "slot-1" / "bootstrap" / "mcapp.sh"
        _write_dummy_bootstrap(active_bootstrap)
        # Simulates what a real (mocked-out) deploy would have placed in the
        # target slot -- the converge phase must find and use THIS bootstrap.
        _write_dummy_bootstrap(target_bootstrap)
        meta_dir.mkdir(parents=True, exist_ok=True)

        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")
        runner.get_active_slot = lambda: 0
        runner.get_oldest_slot = lambda: 1
        runner.snapshot_etc = lambda slot_id: None
        runner._read_version = lambda slot_id: "vtest"
        runner.swap_symlink = lambda slot_id, symlink_dir, name="current": None
        runner.run_health_checks = lambda bus: True
        runner._check_http = lambda url: True

        recorded_cmds: list[list[str]] = []

        def fake_streaming(cmd: list[str], env: dict, bus: Any) -> bool:
            recorded_cmds.append(cmd)
            # First call is the deploy (--skip) -- succeeds. Second call is
            # the post-deploy converge (--converge) -- fails.
            return len(recorded_cmds) == 1

        runner._run_bootstrap_streaming = fake_streaming

        try:
            result = runner.run_update(runner.EventBus(), dev_mode=False)

            if result.get("status") != "success":
                failures.append(
                    f"run_update converge integration: expected status success, got {result!r}"
                )
            if result.get("converge") != "failed":
                failures.append(
                    "run_update converge integration: a converge failure must not downgrade "
                    f"the update status, got converge={result.get('converge')!r} in {result!r}"
                )
            if len(recorded_cmds) != 2:
                failures.append(
                    "run_update converge integration: expected exactly 2 bootstrap "
                    f"invocations (deploy, converge), got {recorded_cmds!r}"
                )
            else:
                converge_cmd = recorded_cmds[1]
                expected = ["bash", str(target_bootstrap), "--converge"]
                if converge_cmd != expected:
                    failures.append(
                        f"run_update converge integration: converge cmd {converge_cmd!r} "
                        f"!= expected {expected!r}"
                    )
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


def _test_activate_swaps_and_installs_webapp(runner: Any) -> list[str]:
    """A valid activation swaps `current`, replaces the served webapp bundle
    with the target slot's, cleans up any stale .new/.old leftovers, restarts
    lighttpd/mcapp/mcapp-ble in order, refreshes the target's `deployed_at`,
    and never touches the database file (contents or mtime).
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "WEBAPP_DIR": runner.WEBAPP_DIR,
        "DB_PATH": runner.DB_PATH,
        "subprocess": runner.subprocess,
        "run_health_checks": runner.run_health_checks,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir, webapp_dir, db_path = _set_activation_globals(runner, tmp)

        _write_slot(slots_dir, 0, "v1")
        _write_slot(slots_dir, 1, "v2")
        (slots_dir / "current").symlink_to("slot-0")
        original_deployed_at = "2026-01-01T00:00:00+00:00"
        _write_meta(runner.META_DIR, 0, "v1", original_deployed_at)
        _write_meta(runner.META_DIR, 1, "v2", original_deployed_at)

        webapp_dir.mkdir(parents=True, exist_ok=True)
        (webapp_dir / "version.html").write_text("v0-served", encoding="utf-8")
        # A leftover from a previous crashed run must not confuse activation.
        stale_new = webapp_dir.with_name(webapp_dir.name + ".new")
        stale_new.mkdir(parents=True, exist_ok=True)
        (stale_new / "junk.txt").write_text("stale", encoding="utf-8")

        db_before = db_path.read_bytes()
        mtime_before = db_path.stat().st_mtime_ns

        fake_subprocess = _FakeSubprocess()
        runner.subprocess = fake_subprocess
        runner.run_health_checks = lambda bus: True

        try:
            result = runner.run_activate(runner.EventBus(), 1)

            if result.get("status") != "success":
                failures.append(f"activate success path: unexpected result {result!r}")
            if runner.get_active_slot() != 1:
                failures.append("activate must swap 'current' to the target slot")
            served_version = (webapp_dir / "version.html").read_text(encoding="utf-8")
            if served_version != "v2":
                failures.append(
                    "activate must install the target slot's webapp bundle, served "
                    f"version.html reads {served_version!r}"
                )
            if webapp_dir.with_name(webapp_dir.name + ".new").exists():
                failures.append("activate must remove webapp.new after promoting it")
            if webapp_dir.with_name(webapp_dir.name + ".old").exists():
                failures.append("activate must remove webapp.old after promoting the new bundle")

            restarted = [c[2] for c in fake_subprocess.calls if c[:2] == ["systemctl", "restart"]]
            if restarted != ["lighttpd", "mcapp", "mcapp-ble"]:
                failures.append(
                    f"activate must restart lighttpd, mcapp, mcapp-ble in order, got {restarted!r}"
                )

            target_meta = runner.get_slot_meta(1)
            if target_meta.get("deployed_at") == original_deployed_at:
                failures.append("activate must refresh the target slot's deployed_at")
            if target_meta.get("version") != "v2":
                failures.append(
                    f"activate must preserve the target slot's version, got {target_meta!r}"
                )

            if db_path.read_bytes() != db_before:
                failures.append("activate must never modify the database file's contents")
            if db_path.stat().st_mtime_ns != mtime_before:
                failures.append("activate must never touch (mtime) the database file")
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


def _test_activate_bidirectional(runner: Any) -> list[str]:
    """Every slot must be activatable, not only the newest -- 0 -> 1 -> 2 -> 0
    are all reachable in sequence.
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "WEBAPP_DIR": runner.WEBAPP_DIR,
        "DB_PATH": runner.DB_PATH,
        "subprocess": runner.subprocess,
        "run_health_checks": runner.run_health_checks,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir, webapp_dir, _db_path = _set_activation_globals(runner, tmp)

        for slot_id in (0, 1, 2):
            _write_slot(slots_dir, slot_id, f"v{slot_id}")
            _write_meta(runner.META_DIR, slot_id, f"v{slot_id}")
        (slots_dir / "current").symlink_to("slot-0")
        webapp_dir.mkdir(parents=True, exist_ok=True)
        (webapp_dir / "version.html").write_text("boot", encoding="utf-8")

        runner.subprocess = _FakeSubprocess()
        runner.run_health_checks = lambda bus: True

        try:
            for target in (1, 2, 0):
                result = runner.run_activate(runner.EventBus(), target)
                if result.get("status") != "success":
                    failures.append(f"activate to slot-{target}: unexpected result {result!r}")
                if runner.get_active_slot() != target:
                    failures.append(
                        f"activate to slot-{target}: active slot is {runner.get_active_slot()!r}"
                    )
                served_version = (webapp_dir / "version.html").read_text(encoding="utf-8")
                if served_version != f"v{target}":
                    failures.append(
                        f"activate to slot-{target}: served version.html reads {served_version!r}"
                    )
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


def _test_activate_invalid_slot(runner: Any) -> list[str]:
    """Every rejection reason -- already active, empty, out of range, missing
    pyproject.toml -- must fail cleanly with status/reason invalid_slot,
    leave the symlink and served webapp untouched, and never shell out.
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "WEBAPP_DIR": runner.WEBAPP_DIR,
        "DB_PATH": runner.DB_PATH,
        "subprocess": runner.subprocess,
        "run_health_checks": runner.run_health_checks,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir, webapp_dir, _db_path = _set_activation_globals(runner, tmp)

        _write_slot(slots_dir, 0, "v0")
        _write_meta(runner.META_DIR, 0, "v0")
        (slots_dir / "current").symlink_to("slot-0")
        # slot-1 exists but has no pyproject.toml.
        _write_slot(slots_dir, 1, "v1", pyproject=False)
        _write_meta(runner.META_DIR, 1, "v1")
        # slot-2 is left completely empty (no meta, no version).

        webapp_dir.mkdir(parents=True, exist_ok=True)
        (webapp_dir / "version.html").write_text("boot", encoding="utf-8")

        fake_subprocess = _FakeSubprocess()
        runner.subprocess = fake_subprocess
        runner.run_health_checks = lambda bus: True

        cases = [
            ("already active", 0),
            ("empty slot", 2),
            ("out of range", 5),
            ("missing pyproject.toml", 1),
        ]
        try:
            for label, target in cases:
                result = runner.run_activate(runner.EventBus(), target)
                if result.get("status") != "failed" or result.get("reason") != "invalid_slot":
                    failures.append(f"activate {label} (slot {target}): unexpected {result!r}")
                if runner.get_active_slot() != 0:
                    failures.append(f"activate {label}: active slot moved, must stay slot-0")

            served_version = (webapp_dir / "version.html").read_text(encoding="utf-8")
            if served_version != "boot":
                failures.append(
                    f"invalid activation attempts must leave the webapp untouched, "
                    f"got {served_version!r}"
                )
            if fake_subprocess.calls:
                failures.append(
                    "invalid activation attempts must never shell out, got calls "
                    f"{fake_subprocess.calls!r}"
                )
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


def _test_activate_without_webapp_dir(runner: Any) -> list[str]:
    """A slot with no `webapp` directory (pre-per-slot-bundle deploy) still
    activates successfully; the served webapp is left exactly as it was.
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "WEBAPP_DIR": runner.WEBAPP_DIR,
        "DB_PATH": runner.DB_PATH,
        "subprocess": runner.subprocess,
        "run_health_checks": runner.run_health_checks,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir, webapp_dir, _db_path = _set_activation_globals(runner, tmp)

        _write_slot(slots_dir, 0, "v0")
        _write_meta(runner.META_DIR, 0, "v0")
        (slots_dir / "current").symlink_to("slot-0")
        _write_slot(slots_dir, 1, "v1", webapp=False)
        _write_meta(runner.META_DIR, 1, "v1")

        webapp_dir.mkdir(parents=True, exist_ok=True)
        (webapp_dir / "version.html").write_text("existing-content", encoding="utf-8")

        runner.subprocess = _FakeSubprocess()
        runner.run_health_checks = lambda bus: True

        try:
            result = runner.run_activate(runner.EventBus(), 1)
            if result.get("status") != "success":
                failures.append(f"activate without webapp dir: unexpected result {result!r}")
            if runner.get_active_slot() != 1:
                failures.append("activate without webapp dir: symlink must still swap")
            served_version = (webapp_dir / "version.html").read_text(encoding="utf-8")
            if served_version != "existing-content":
                failures.append(
                    "activate without webapp dir must leave the served webapp untouched, "
                    f"got {served_version!r}"
                )
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


def _test_activate_health_failure(runner: Any) -> list[str]:
    """A health-check failure after activation reports status warning /
    health_ok False, and the symlink is only ever swapped once (no retry).
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "WEBAPP_DIR": runner.WEBAPP_DIR,
        "DB_PATH": runner.DB_PATH,
        "subprocess": runner.subprocess,
        "run_health_checks": runner.run_health_checks,
        "swap_symlink": runner.swap_symlink,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir, webapp_dir, _db_path = _set_activation_globals(runner, tmp)

        _write_slot(slots_dir, 0, "v0")
        _write_meta(runner.META_DIR, 0, "v0")
        (slots_dir / "current").symlink_to("slot-0")
        _write_slot(slots_dir, 1, "v1")
        _write_meta(runner.META_DIR, 1, "v1")
        webapp_dir.mkdir(parents=True, exist_ok=True)
        (webapp_dir / "version.html").write_text("boot", encoding="utf-8")

        runner.subprocess = _FakeSubprocess()
        runner.run_health_checks = lambda bus: False

        swap_calls: list[int] = []
        original_swap = runner.swap_symlink

        def counting_swap(slot_id: int, symlink_dir: Path, name: str = "current") -> None:
            swap_calls.append(slot_id)
            original_swap(slot_id, symlink_dir, name)

        runner.swap_symlink = counting_swap

        try:
            result = runner.run_activate(runner.EventBus(), 1)
            if result.get("status") != "warning":
                failures.append(f"activate health failure: expected status warning, got {result!r}")
            if result.get("health_ok") is not False:
                failures.append(
                    f"activate health failure: expected health_ok False, got {result!r}"
                )
            if swap_calls != [1]:
                failures.append(
                    f"activate health failure: symlink must swap exactly once, got {swap_calls!r}"
                )
            if runner.get_active_slot() != 1:
                failures.append(
                    "activate health failure: the (failed-health) activation still took "
                    "effect, symlink must reflect it"
                )
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


def _test_run_update_health_failure_calls_activate(runner: Any) -> list[str]:
    """A post-deploy health-check failure inside `run_update` must fall back
    to re-activating the PREVIOUS (known-good) slot via `_activate_slot` --
    never the deleted `_do_rollback` name, and never the just-deployed slot.
    """
    failures: list[str] = []
    originals = {
        "SLOTS_DIR": runner.SLOTS_DIR,
        "META_DIR": runner.META_DIR,
        "home": runner.home,
        "get_active_slot": runner.get_active_slot,
        "get_oldest_slot": runner.get_oldest_slot,
        "snapshot_etc": runner.snapshot_etc,
        "_read_version": runner._read_version,
        "swap_symlink": runner.swap_symlink,
        "run_health_checks": runner.run_health_checks,
        "_run_bootstrap_streaming": runner._run_bootstrap_streaming,
        "_activate_slot": runner._activate_slot,
        "get_slot_meta": runner.get_slot_meta,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        active_bootstrap = slots_dir / "slot-0" / "bootstrap" / "mcapp.sh"
        _write_dummy_bootstrap(active_bootstrap)
        meta_dir.mkdir(parents=True, exist_ok=True)

        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")
        runner.get_active_slot = lambda: 0
        runner.get_oldest_slot = lambda: 1
        runner.snapshot_etc = lambda slot_id: None
        runner._read_version = lambda slot_id: "vtest"
        runner.swap_symlink = lambda slot_id, symlink_dir, name="current": None
        runner.run_health_checks = lambda bus: False
        runner._run_bootstrap_streaming = lambda cmd, env, bus: True
        runner.get_slot_meta = lambda slot_id: {"version": "v-active"}

        activate_calls: list[int] = []
        runner._activate_slot = lambda target_slot, bus: activate_calls.append(target_slot)

        try:
            result = runner.run_update(runner.EventBus(), dev_mode=False)

            if result.get("status") != "rolled_back":
                failures.append(
                    f"run_update health-failure path: expected status rolled_back, got {result!r}"
                )
            if result.get("reason") != "health_check_failed":
                failures.append(
                    f"run_update health-failure path: unexpected reason {result.get('reason')!r}"
                )
            if activate_calls != [0]:
                failures.append(
                    "run_update health-failure path: must call _activate_slot with the "
                    f"PREVIOUS active slot (0), got calls {activate_calls!r}"
                )
        finally:
            for name, value in originals.items():
                setattr(runner, name, value)
    return failures


def _test_rollback_no_candidate(runner: Any) -> list[str]:
    """`run_rollback` with nothing to roll back to must fail via the same
    invalid_slot shape as an invalid `--mode activate` call.
    """
    failures: list[str] = []
    originals = (runner.SLOTS_DIR, runner.META_DIR, runner.home)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        slots_dir = tmp / "mcapp-slots"
        meta_dir = slots_dir / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        # No 'current' symlink and no slot metadata at all -> no candidate.
        runner.SLOTS_DIR = slots_dir
        runner.META_DIR = meta_dir
        runner.home = Path("/home/testuser")
        try:
            result = runner.run_rollback(runner.EventBus())
            if result.get("status") != "failed" or result.get("reason") != "invalid_slot":
                failures.append(f"run_rollback with no candidate: unexpected result {result!r}")
        finally:
            runner.SLOTS_DIR, runner.META_DIR, runner.home = originals
    return failures


def _test_restore_helpers_removed(runner: Any) -> list[str]:
    """The destructive restore path must be gone from the module, not merely
    unused -- a future call site cannot resurrect what does not exist.
    """
    removed_names = ("restore_database", "restore_etc", "snapshot_database", "_do_rollback")
    return [
        f"{name} must not exist on the runner module anymore"
        for name in removed_names
        if hasattr(runner, name)
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_update_runner_tests() else 1)
