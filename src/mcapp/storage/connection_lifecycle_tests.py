"""Built-in regression suite for SQLite connection lifecycle.

Guards the pair of mistakes that bracket the correct pattern. Both were live in
this repo: the first shipped for the project's whole life, the second was
introduced and caught while fixing the first.

  1. **Leak** — ``with sqlite3.connect(...) as conn:`` does NOT close the
     connection. sqlite3's context manager is a *transaction* manager; ``__exit__``
     only commits or rolls back. The Connection then lives until the CYCLIC GC
     reaches it — not refcounting: a `Connection` is GC-tracked and sits in a
     C-level cycle, so with ``gc.disable()`` even a minimal one-function
     ``with`` block leaks its fd until a collection runs. Until then it holds an
     open fd, a page cache and a lookaside arena.

  2. **Silent rollback** — "just wrap it in ``closing()``" drops the transaction
     manager, so any write that relied on the implicit commit rolls back on close
     with no error anywhere.

The shape used everywhere that writes is therefore BOTH:
``with closing(sqlite3.connect(...)) as conn, conn:`` — ``closing`` closes, the
bare ``conn`` commits on success / rolls back on error. Read paths need only
``closing``. Measurements from the incident that motivated this are recorded once,
in ``doc/connection-leak-fable-verdict.md``; they are deliberately not repeated
here, because a leaked-fd count depends on when the cyclic GC happened to run and
is not reproducible without that context.

Two of the tests below are NOT equally strong, and the difference is worth
knowing before trusting a green run:

  * ``no leaked connections`` is a true regression test, verified in both
    directions on this repo: every connection left open before the fix, none
    after. Remove a ``closing()`` from a site the suite drives and it fails.

  * ``writes are committed`` is a **guard, not a reproduction**. Stripping the
    ``, conn:`` transaction manager today does not fail it, because every write
    path currently commits by another route — ``_mutate``/``_execute_many``, the
    prefs setters and the classifier writers all call ``conn.commit()``
    explicitly, and the migrator's DDL runs in sqlite3's legacy autocommit mode
    while its data steps commit inside ``_set_schema_version``. Verified by
    mutation: the suite still passes with every ``, conn:`` removed. The
    assertions earn their place by catching the *next* write that lands without a
    commit — at which point removing the transaction manager stops being harmless.

Both run against an ephemeral tempfile DB and read results back through a
FRESH connection, so a rolled-back transaction cannot be masked by the writing
connection's own view.

All timestamps are milliseconds (project-wide invariant).
"""

import ast
import asyncio
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..sqlite_storage import create_sqlite_storage
from .constants import db_read, db_write
from .migration_chain_tests import FINAL_SCHEMA_VERSION

logger = get_logger(__name__)

BASE_TS = 1_770_000_000_000  # fixed ms timestamp so the suite is deterministic

# Floor for how many connections the tracked block must open. The real count is
# stable (77 at the time of writing) but drifts whenever a `_query`/`_mutate` is
# added to or removed from `store_message`, so pinning the exact number would be
# a tripwire on unrelated changes. A floor still catches the failure that a bare
# `opened > 0` would miss: a regression that silently no-ops most of the probe
# loop, leaving "no leaks" trivially true because almost nothing ran.
MIN_TRACKED_CONNECTS = 40

# Computed once at import time (sync context) rather than inside the async test
# that uses it: `.resolve()` touches the filesystem and ASYNC240 flags pathlib
# I/O methods called from an `async def`.
_MCAPP_PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../src/mcapp


class _TrackedConnection(sqlite3.Connection):
    """Connection subclass that reports its own ``close()`` to the active tracker."""

    tracker: "_ConnectionTracker | None" = None

    def close(self) -> None:
        tracker = _TrackedConnection.tracker
        if tracker is not None:
            tracker.mark_closed(id(self))
        super().close()


class _ConnectionTracker:
    """Records every sqlite3 Connection handed out while installed, and which closed.

    Patches BOTH the ``sqlite3.connect`` and ``sqlite3.dbapi2.connect`` module
    attributes. Every call site in this project resolves ``sqlite3.connect(...)``
    or ``sqlite3.dbapi2.connect(...)`` at call time, so patching both attributes
    covers sqlite_storage, storage/* and sse_handler alike — including
    connections opened inside ``asyncio.to_thread`` worker threads. The two names
    are distinct attribute bindings of the same underlying function
    (``sqlite3/__init__.py`` does ``from sqlite3.dbapi2 import *``), so patching
    one does NOT patch the other — both must be assigned and restored.

    One blind spot remains, and it cannot be closed by interception: a name
    already bound by ``from sqlite3 import connect`` (or
    ``from sqlite3.dbapi2 import connect``) is resolved into the IMPORTING
    module's namespace at that module's import time, before this tracker ever
    installs its patch — reassigning the attribute afterwards does not reach a
    reference that already exists elsewhere. A leak introduced through that form
    would be invisible to interception and the suite would pass. Rather than a
    blind spot, this is guarded by DETECTION: ``_test_no_unpatchable_sqlite_imports``
    scans every ``.py`` file under the ``mcapp`` package for that import form and
    fails the suite if it finds one, before the leak itself could ever occur.

    Closure is detected by observing ``Connection.close()`` via a `factory=`
    subclass, NOT by probing the handle. Probing is not a usable instrument here:
    sqlite3 raises ``ProgrammingError`` both for "cannot operate on a closed
    database" AND for "created in a thread can only be used in that same thread",
    and these connections are opened inside `asyncio.to_thread` workers — so a
    probe from the main thread scores every live connection as closed and the
    suite passes on the leaky code it is supposed to catch. That instrument was
    written first and did exactly that.

    Strong references to every handle are kept so ids cannot be reused while the
    tracker is alive. (CPython's C-level Connection dealloc does not dispatch to a
    Python ``close()`` override, so a collected connection could never pre-mark an
    id either way — the strong refs are belt and braces.)

    No teardown sweep is attempted: the connections are created in
    ``asyncio.to_thread`` workers and sqlite3 defaults to ``check_same_thread=True``,
    so closing them from here raises ``ProgrammingError`` and closes nothing. On a
    failing run the leaked handles simply die with this object; POSIX unlinks open
    files, so ``TemporaryDirectory`` cleanup is unaffected on the supported hosts
    (Pi, macOS).
    """

    def __init__(self) -> None:
        self.handles: list[sqlite3.Connection] = []
        self._closed: set[int] = set()
        self._lock = threading.Lock()  # connections are opened in worker threads
        self._real = sqlite3.connect
        self._real_dbapi2 = sqlite3.dbapi2.connect

    def mark_closed(self, conn_id: int) -> None:
        with self._lock:
            self._closed.add(conn_id)

    def __enter__(self) -> "_ConnectionTracker":
        def _tracked(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            kwargs["factory"] = _TrackedConnection
            conn: sqlite3.Connection = self._real(*args, **kwargs)
            with self._lock:
                self.handles.append(conn)
            return conn

        _TrackedConnection.tracker = self
        sqlite3.connect = _tracked  # type: ignore[assignment]  # deliberate test seam
        # Distinct attribute binding of the same function (see class docstring) —
        # must be patched separately or a caller resolving through this name
        # entirely escapes tracking.
        sqlite3.dbapi2.connect = _tracked  # type: ignore[assignment]  # deliberate test seam
        return self

    def __exit__(self, *exc: object) -> None:
        # Restore `connect` BEFORE nulling the tracker: the reverse order leaves a
        # window where a freshly-tracked connection's close() goes unrecorded.
        sqlite3.connect = self._real
        sqlite3.dbapi2.connect = self._real_dbapi2
        _TrackedConnection.tracker = None

    def still_open(self) -> int:
        """Count tracked connections whose close() was never called."""
        with self._lock:
            return sum(1 for conn in self.handles if id(conn) not in self._closed)


def _find_unpatchable_sqlite_imports(package_root: Path) -> list[str]:
    """Scan ``.py`` files under ``package_root`` for the one form `_ConnectionTracker`
    cannot intercept: ``from sqlite3 import connect`` or
    ``from sqlite3.dbapi2 import connect`` (with or without ``as``).

    Both bind ``connect`` straight into the importing module's namespace at that
    module's import time — reassigning ``sqlite3.connect`` /
    ``sqlite3.dbapi2.connect`` afterwards never reaches that reference, so a
    connection opened through it would be invisible to the tracker and this
    suite would report PASS on a real leak. Returns one ``"path:lineno"`` string
    per offending import, empty if none found. AST-based rather than a text/regex
    match so it does not fire on the substring appearing in a comment or string.
    """
    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "sqlite3",
                "sqlite3.dbapi2",
            ):
                offenders.extend(
                    f"{path}:{node.lineno}" for alias in node.names if alias.name == "connect"
                )
    return offenders


async def _drive_every_connect_site(storage: Any) -> None:
    """Exercise every storage module that opens its own connection.

    Deliberately not just `store_message` + `_query`: those only reach
    `sqlite_storage`'s three shared helpers plus the migrator. The classifier,
    prefs and query mixins open their own connections, and a dropped `closing()`
    there would otherwise ship green because the leaking code never runs inside
    the tracked window.

    Coverage is complete for production code as of this writing. The one
    remaining `db_write` in the package that this cannot reach is the fixture
    insert inside `sse_handler.run_startup_tests` — it lives in that module's own
    embedded test harness, not behind any storage method, so no call from here
    can drive it. If it ever leaks, that is bounded to a single handle per test
    run, not production.
    """
    for i in range(25):
        await storage.store_message(
            {
                "type": "msg",
                "src": "DK5EN-1",
                "dst": "*",
                "msg": f"leak probe {i}",
                "msg_id": f"LEAK{i:04X}",
                "timestamp": BASE_TS + i * 1000,
            },
            "{}",
        )
        await storage._query("SELECT COUNT(*) AS n FROM messages WHERE type = ?", ("msg",))

    # storage/classifier_api.py — all three connect sites
    await storage.insert_classifier_rule(
        name="leak-probe",
        pattern="^leak probe",
        category="test",
    )
    await storage.upsert_beacon_template("abc123def456", "leak probe 0", "DK5EN-1", BASE_TS)
    await storage.clear_stale_auto_beacons(frozenset({"test"}), 3)

    # storage/prefs.py — _set_identifier_list and delete_messages_by_dst
    await storage.set_kickban_callsigns(["OE1ABC-1"])
    await storage.delete_messages_by_dst("*")

    # storage/query.py — get_smart_initial_with_summary is the one read path that
    # opens its own connection rather than going through `_query`. It is also the
    # SSE hot path (every client connect builds this payload), so a leak here
    # would scale with reconnects.
    await storage.get_smart_initial_with_summary()


async def _test_no_leaked_connections(results: list[tuple[str, bool]]) -> None:
    """Every connection the storage layer opens must be closed when it returns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "leak.db"
        with _ConnectionTracker() as tracker:
            try:
                storage = await create_sqlite_storage(str(db_path))
                await _drive_every_connect_site(storage)
                await storage.close()
            except Exception:
                # House rule (migration_chain_tests): a raising probe reports FAIL
                # rather than propagating — the runner has ~7 suites after this one.
                logger.exception("connection-lifecycle probe raised")
                results.append(("no leaked connections: probe runs end-to-end", False))
                return

            opened = len(tracker.handles)
            leaked = tracker.still_open()

        enough = opened >= MIN_TRACKED_CONNECTS
        results.append(
            (f"probe opened at least {MIN_TRACKED_CONNECTS} connections (opened={opened})", enough)
        )
        results.append((f"no leaked connections ({leaked} of {opened} left open)", leaked == 0))


async def _test_writes_are_committed(results: list[tuple[str, bool]]) -> None:
    """Adding closing() must not swallow the commit the transaction manager did.

    Covers all three write shapes plus the migrator: schema DDL (migrations
    `_init_db`, no explicit commit at all), `_mutate`, `_execute_many`, and a
    `PrefsMixin` write — each verified through a FRESH connection, so a rolled-back
    transaction cannot be masked by the writing connection's own view.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "commit.db"
        try:
            storage = await create_sqlite_storage(str(db_path))
            await storage.store_message(
                {
                    "type": "msg",
                    "src": "DK5EN-1",
                    "dst": "*",
                    "msg": "durability probe",
                    "msg_id": "CMT00001",
                    "timestamp": BASE_TS,
                },
                "{}",
            )
            await storage._execute_many(
                "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("CMT00002", "DK5EN-2", "*", "many a", "msg", BASE_TS + 1000),
                    ("CMT00003", "DK5EN-2", "*", "many b", "msg", BASE_TS + 2000),
                ],
            )
            await storage.set_kickban_callsigns(["OE1ABC-1"])
            await storage.close()
        except Exception:
            logger.exception("durability probe raised")
            results.append(("writes are committed: probe runs end-to-end", False))
            return

        # Fresh process-independent connection: only durable, committed state is visible.
        verify = sqlite3.connect(db_path)
        try:
            row = verify.execute("SELECT MAX(version) FROM schema_version").fetchone()
            schema_version = row[0] if row and row[0] is not None else 0
            msg_ids = {
                r[0]
                for r in verify.execute(
                    "SELECT msg_id FROM messages WHERE msg_id LIKE 'CMT%'"
                ).fetchall()
            }
            blocked = {r[0] for r in verify.execute("SELECT callsign FROM kickban_callsigns")}
        finally:
            verify.close()

        schema_ok = schema_version == FINAL_SCHEMA_VERSION
        results.append((f"migrator committed schema v{schema_version}", schema_ok))

        mutate_ok = "CMT00001" in msg_ids
        results.append(("_mutate write is durable", mutate_ok))

        many_ok = {"CMT00002", "CMT00003"} <= msg_ids
        results.append(("_execute_many write is durable", many_ok))

        prefs_ok = blocked == {"OE1ABC-1"}
        results.append(("prefs write is durable", prefs_ok))


async def _test_no_unpatchable_sqlite_imports(results: list[tuple[str, bool]]) -> None:
    """The one blind spot interception cannot close must stay detected, not just documented.

    Guards against a future ``from sqlite3 import connect`` (or
    ``from sqlite3.dbapi2 import connect``) anywhere under the ``mcapp`` package —
    either form would open connections invisible to `_ConnectionTracker`, and
    this suite would silently PASS on the resulting leak. See
    `_find_unpatchable_sqlite_imports` and the `_ConnectionTracker` docstring.
    """
    offenders = _find_unpatchable_sqlite_imports(_MCAPP_PACKAGE_ROOT)
    label = "no `from sqlite3[.dbapi2] import connect` under mcapp"
    if offenders:
        label += f" (found: {', '.join(offenders)}; tracker cannot see connections opened this way)"
    results.append((label, not offenders))


async def _test_tracker_detects_known_leak(results: list[tuple[str, bool]]) -> None:
    """Self-check: the tracker must still catch the exact broken pattern it exists to catch.

    Guards the measurement instrument itself. A prior version of this tracker
    (probing handle state instead of observing `close()`) silently reported
    PASS on leaky code — see the class docstring. Uses its own tracker instance
    and its own temp DB, so it does not perturb the counts
    `_test_no_leaked_connections` reads.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "self_check.db"
        with _ConnectionTracker() as tracker:
            with sqlite3.connect(db_path) as conn:  # deliberately broken: no closing()
                conn.execute("CREATE TABLE t (x INTEGER)")
            detected = tracker.still_open()
            # Close from the thread that opened it: this test runs on the main
            # thread, so a plain close() works here — unlike the storage layer's
            # asyncio.to_thread connections, which raise if closed off-thread.
            conn.close()

    results.append((f"tracker detects a known leak (still_open={detected})", detected == 1))


async def _test_write_connections_use_synchronous_normal(results: list[tuple[str, bool]]) -> None:
    """F1: `db_write` must set `PRAGMA synchronous=NORMAL`, and only `db_write`.

    In WAL mode the default `synchronous=FULL` fsyncs the WAL on every commit,
    which on the production Pi's SD card measured 15 ms typical / up to 1.7 s
    per commit and was the entire cause of the F1 handler stalls (see
    `db_write`'s docstring in `constants.py`). `db_read` never commits, so it
    is deliberately left at the SQLite default (`FULL` / `2`) rather than
    also being switched — this asserts that omission is intentional, not a
    gap.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "synchronous.db"
        try:
            # Put the file into WAL mode first, as production databases run.
            with closing(sqlite3.connect(db_path)) as setup_conn:
                setup_conn.execute("PRAGMA journal_mode=WAL")

            with db_write(db_path) as conn:
                write_sync = conn.execute("PRAGMA synchronous").fetchone()[0]

            with db_read(db_path) as conn:
                read_sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        except Exception:
            logger.exception("synchronous-pragma probe raised")
            results.append(("synchronous pragma probe runs end-to-end", False))
            return

    results.append((f"db_write sets synchronous=NORMAL (got {write_sync})", write_sync == 1))
    results.append(
        (f"db_read stays at the default synchronous=FULL (got {read_sync})", read_sync == 2)
    )


async def _test_writer_conn_keeps_wal_alive_between_writes(results: list[tuple[str, bool]]) -> None:
    """F1 second half: two `_mutate` calls must NOT checkpoint the WAL between them.

    Closing the LAST connection to a WAL database checkpoints and deletes/truncates
    the `-wal` file, which is itself a DB-file fsync (measured 23.7 ms per write vs.
    ~0.2 ms on a persistent connection, 2026-09-18). The pre-fix `_mutate` opened and
    closed its own connection on every call, so it paid that checkpoint on every
    single write. This asserts the `-wal` file is still non-empty right after two
    `_mutate` calls, before `storage.close()` runs — which fails on the old
    per-call-connection code (each call's close already checkpointed it) and passes
    once `_mutate` shares one persistent writer connection across both calls.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "wal_persist.db"
        wal_path = db_path.with_name(db_path.name + "-wal")
        wal_state: tuple[bool, int] | None = None
        try:
            storage = await create_sqlite_storage(str(db_path))
            try:
                await storage._mutate(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    ("WALP0001", "DK5EN-1", "*", "wal persist a", "msg", BASE_TS),
                )
                await storage._mutate(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    ("WALP0002", "DK5EN-1", "*", "wal persist b", "msg", BASE_TS + 1),
                )
                wal_state = (
                    wal_path.exists(),
                    wal_path.stat().st_size if wal_path.exists() else 0,
                )
            finally:
                await storage.close()
        except Exception:
            logger.exception("wal-persistence probe raised")
            results.append(
                ("writer conn keeps -wal alive between writes: probe runs end-to-end", False)
            )
            return

    exists, size = wal_state
    results.append(
        (
            (
                f"-wal file is still non-empty right after two _mutate calls (exists={exists}, "
                f"size={size}) — no checkpoint-on-close between them"
            ),
            exists and size > 0,
        )
    )


async def _test_writer_conn_closes_then_reopens(results: list[tuple[str, bool]]) -> None:
    """`storage.close()` must close the writer connection, and a later `_mutate`
    must transparently reopen it (many startup-test suites close and reuse a
    storage instance)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "writer_reopen.db"
        still_open_after_first_close: int | None = None
        still_open_after_second_close: int | None = None
        try:
            with _ConnectionTracker() as tracker:
                storage = await create_sqlite_storage(str(db_path))
                await storage._mutate(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    ("REOPEN01", "DK5EN-1", "*", "before close", "msg", BASE_TS),
                )
                await storage.close()
                still_open_after_first_close = tracker.still_open()

                # Reopen: a write after close() must still work.
                await storage._mutate(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    ("REOPEN02", "DK5EN-2", "*", "after reopen", "msg", BASE_TS + 1),
                )
                await storage.close()
                still_open_after_second_close = tracker.still_open()
        except Exception:
            logger.exception("writer-reopen probe raised")
            results.append(("writer connection close/reopen: probe runs end-to-end", False))
            return

        results.append(
            (
                (
                    f"writer connection is closed after storage.close() (still_open="
                    f"{still_open_after_first_close})"
                ),
                still_open_after_first_close == 0,
            )
        )
        results.append(
            (
                (
                    f"writer connection reopens for a _mutate after close(), and closes again"
                    f" (still_open={still_open_after_second_close})"
                ),
                still_open_after_second_close == 0,
            )
        )

        verify = sqlite3.connect(db_path)
        try:
            msg_ids = {
                r[0]
                for r in verify.execute(
                    "SELECT msg_id FROM messages WHERE msg_id LIKE 'REOPEN%'"
                ).fetchall()
            }
        finally:
            verify.close()
        results.append(
            (
                "both the pre-close and the post-reopen write are durable",
                msg_ids == {"REOPEN01", "REOPEN02"},
            )
        )


async def _test_concurrent_mutate_calls_do_not_race(results: list[tuple[str, bool]]) -> None:
    """Two `_mutate` calls from different `asyncio.to_thread` worker threads, run
    concurrently via `asyncio.gather`, must both commit against the shared writer
    connection with no `sqlite3.ProgrammingError`. Smoke only: CPython's sqlite3
    is built in serialized threading mode, so this passes even without
    `_writer_lock`; the lock is pinned by review, not by this test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "concurrent_mutate.db"
        try:
            storage = await create_sqlite_storage(str(db_path))
            try:
                await asyncio.gather(
                    storage._mutate(
                        "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        ("CONC0001", "DK5EN-1", "*", "concurrent a", "msg", BASE_TS),
                    ),
                    storage._mutate(
                        "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        ("CONC0002", "DK5EN-2", "*", "concurrent b", "msg", BASE_TS + 1),
                    ),
                )
            finally:
                await storage.close()
        except Exception:
            logger.exception("concurrent-_mutate probe raised")
            results.append(
                (
                    (
                        "two concurrent _mutate calls both commit with no ProgrammingError:"
                        " probe runs end-to-end"
                    ),
                    False,
                )
            )
            return

        verify = sqlite3.connect(db_path)
        try:
            msg_ids = {
                r[0]
                for r in verify.execute(
                    "SELECT msg_id FROM messages WHERE msg_id LIKE 'CONC%'"
                ).fetchall()
            }
        finally:
            verify.close()
        results.append(
            (
                "two concurrent _mutate calls both commit (no ProgrammingError, both durable)",
                msg_ids == {"CONC0001", "CONC0002"},
            )
        )


async def run_connection_lifecycle_tests() -> bool:
    """Run the SQLite connection-lifecycle suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    await _test_no_leaked_connections(results)
    await _test_writes_are_committed(results)
    await _test_no_unpatchable_sqlite_imports(results)
    await _test_tracker_detects_known_leak(results)
    await _test_write_connections_use_synchronous_normal(results)
    await _test_writer_conn_keeps_wal_alive_between_writes(results)
    await _test_writer_conn_closes_then_reopens(results)
    await _test_concurrent_mutate_calls_do_not_race(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    all_ok = all(ok for _, ok in results)
    print(f"    connection_lifecycle: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
