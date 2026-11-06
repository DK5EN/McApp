"""Tests for /api/timezone's subprocess lookup (weather.py + tz_lookup_worker.py).

Offline. Fake workers stand in for timezonefinder except the one real-worker case.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import httpx

from mcapp.weblite import FastAPI

from . import weather

_FAKE_OK = 'import sys; print("Europe/Berlin")\n'
_FAKE_EMPTY = 'print("")\n'
_FAKE_EXIT3 = "import sys; sys.exit(3)\n"
_FAKE_CRASH = 'import sys; sys.stderr.write("boom" * 200); sys.exit(1)\n'
_FAKE_HANG = textwrap.dedent(
    """
    import os, sys, time
    open(sys.argv[3] if len(sys.argv) > 3 else os.devnull, "w").close()
    open(os.environ["TZ_PID_FILE"], "w").write(str(os.getpid()))
    time.sleep(60)
    """
)
_FAKE_OVERLAP = textwrap.dedent(
    """
    import os, sys, time
    d = os.environ["TZ_OVERLAP_DIR"]
    me = os.path.join(d, "run-%d" % os.getpid())
    open(me, "w").close()
    n = len([f for f in os.listdir(d) if f.startswith("run-")])
    open(os.path.join(d, "seen-%d-%d" % (os.getpid(), n)), "w").close()
    time.sleep(0.2)
    os.remove(me)
    print("Europe/Berlin")
    """
)
_FAKE_COUNT = textwrap.dedent(
    """
    import os
    with open(os.environ["TZ_COUNT_FILE"], "a") as f:
        f.write("x")
    print("Europe/Berlin")
    """
)


def _reset() -> None:
    weather._tz_cache.clear()
    weather._tz_sem = None


def _write(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body)
    return p


def _client(worker: Path | None) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(weather.build_weather_router(None))  # type: ignore[arg-type]  # manager unused by /api/timezone
    if worker is not None:
        weather.TZ_WORKER_PATH = worker
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


async def _get(c: httpx.AsyncClient, lat: float = 48.1, lon: float = 11.5) -> httpx.Response:
    return await c.get("/api/timezone", params={"lat": lat, "lon": lon})


async def _get_capped(
    c: httpx.AsyncClient, lat: float, lon: float, cap: float
) -> httpx.Response | None:
    """_get, or None when it does not answer within `cap` (keeps a broken run finite)."""
    try:
        return await asyncio.wait_for(_get(c, lat, lon), cap)
    except TimeoutError:
        return None


async def _cases(results: list[tuple[str, bool]]) -> None:
    orig_path = weather.TZ_WORKER_PATH
    orig_timeout = weather.TZ_WORKER_TIMEOUT_S
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)

            # success shape
            _reset()
            async with _client(_write(d, "ok.py", _FAKE_OK)) as c:
                r = await _get(c)
                body: dict[str, Any] = r.json()
            results.append(
                (
                    "success shape",
                    r.status_code == 200
                    and set(body) == {"timezone", "abbreviation", "utc_offset"}
                    and body["timezone"] == "Europe/Berlin"
                    and body["utc_offset"] in (1.0, 2.0),
                )
            )

            # empty => 400
            _reset()
            async with _client(_write(d, "empty.py", _FAKE_EMPTY)) as c:
                r = await _get(c)
            results.append(("empty => 400", r.status_code == 400))

            # exit 3 => 501
            _reset()
            async with _client(_write(d, "e3.py", _FAKE_EXIT3)) as c:
                r = await _get(c)
            results.append(("exit 3 => 501", r.status_code == 501))

            # crash => 503
            _reset()
            async with _client(_write(d, "crash.py", _FAKE_CRASH)) as c:
                r = await _get(c)
            results.append(("crash => 503", r.status_code == 503))

            # hang => 503 and child reaped
            _reset()
            pidf = d / "hang.pid"
            os.environ["TZ_PID_FILE"] = str(pidf)
            weather.TZ_WORKER_TIMEOUT_S = 1.5
            async with _client(_write(d, "hang.py", _FAKE_HANG)) as c:
                r = await _get(c)
            weather.TZ_WORKER_TIMEOUT_S = orig_timeout
            pid = int(pidf.read_text()) if pidf.exists() else -1
            results.append(
                ("hang => 503, child gone", r.status_code == 503 and pid > 0 and _pid_gone(pid))
            )

            # failures not cached: fail, then succeed at same coords
            _reset()
            async with _client(_write(d, "f.py", _FAKE_EXIT3)) as c:
                r1 = await _get(c)
                weather.TZ_WORKER_PATH = _write(d, "f2.py", _FAKE_OK)
                r2 = await _get(c)
            results.append(("failures not cached", r1.status_code == 501 and r2.status_code == 200))

            await _cases_b(d, results)
            await _cases_c(d, results)
            await _cases_d(d, results)
            await _cases_deadline(d, results)
            await _cases_waiter_count(d, results)
    finally:
        weather.TZ_WORKER_PATH = orig_path
        weather.TZ_WORKER_TIMEOUT_S = orig_timeout
        _reset()


async def _cases_b(d: Path, results: list[tuple[str, bool]]) -> None:
    # concurrency: never more than one child at a time
    _reset()
    od = d / "overlap"
    od.mkdir()
    os.environ["TZ_OVERLAP_DIR"] = str(od)
    async with _client(_write(d, "ov.py", _FAKE_OVERLAP)) as c:
        rs = await asyncio.gather(*[_get(c, 10.0 + i, 20.0) for i in range(5)])
    seen = [int(f.name.rsplit("-", 1)[1]) for f in od.iterdir() if f.name.startswith("seen-")]
    results.append(
        (
            "concurrency: max 1 child, 5 spawned",
            all(x.status_code == 200 for x in rs) and len(seen) == 5 and max(seen) == 1,
        )
    )

    # cache hit spawns no child; 0.01 rounding shares a key
    _reset()
    cf = d / "count.txt"
    os.environ["TZ_COUNT_FILE"] = str(cf)
    async with _client(_write(d, "cnt.py", _FAKE_COUNT)) as c:
        await _get(c, 48.101, 11.502)
        await _get(c, 48.104, 11.498)
        await asyncio.gather(*[_get(c, 48.1, 11.5) for _ in range(3)])
    results.append(("cache hit spawns no child", cf.read_text() == "x"))

    # cache bounded
    _reset()
    for i in range(weather.TZ_CACHE_MAX + 5):
        weather._tz_cache[(float(i), 0.0)] = "X"
    await weather.lookup_timezone(1.5, 1.5, _write(d, "ok2.py", _FAKE_OK))
    results.append(("cache bounded", len(weather._tz_cache) <= weather.TZ_CACHE_MAX))


async def _outcome(coro: Any) -> str:
    try:
        await coro
    except weather.TzLookupError as exc:
        return f"TzLookupError:{exc}"
    except BaseException as exc:
        return type(exc).__name__
    return "returned"


async def _cases_c(d: Path, results: list[tuple[str, bool]]) -> None:
    """R1: a child that exited before kill() must not replace the real outcome."""
    ok = _write(d, "ok3.py", _FAKE_OK)
    real_wait_for = asyncio.wait_for

    def patched(exc: type[BaseException]) -> Any:
        async def fake(aw: Any, _t: float | None = None) -> Any:
            await aw  # the child has run to completion and been reaped
            raise exc

        return fake

    try:
        asyncio.wait_for = patched(TimeoutError)
        got = await _outcome(weather._run_tz_worker(48.1, 11.5, ok))
        results.append(
            ("timeout on exited child => TzLookupError(timeout)", got == "TzLookupError:timeout")
        )
        asyncio.wait_for = patched(asyncio.CancelledError)
        got = await _outcome(weather._run_tz_worker(48.1, 11.5, ok))
        results.append(("cancel on exited child => CancelledError", got == "CancelledError"))
    finally:
        asyncio.wait_for = real_wait_for

    # real cancel mid-lookup: CancelledError propagates, child is gone
    _reset()
    pidf = d / "cancel.pid"
    os.environ["TZ_PID_FILE"] = str(pidf)
    task = asyncio.ensure_future(
        weather.lookup_timezone(48.1, 11.5, _write(d, "hang2.py", _FAKE_HANG))
    )
    for _ in range(400):
        if pidf.exists() and pidf.read_text():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    got = await _outcome(task)
    pid = int(pidf.read_text()) if pidf.exists() and pidf.read_text() else -1
    results.append(
        (
            "cancel mid-lookup: CancelledError, child gone",
            got == "CancelledError" and pid > 0 and _pid_gone(pid),
        )
    )
    _reset()


async def _cases_d(d: Path, results: list[tuple[str, bool]]) -> None:
    """R2 (coordinate validation) and R3 (bounded queue, one deadline)."""
    cf = d / "spawns.txt"
    os.environ["TZ_COUNT_FILE"] = str(cf)
    slow = _write(
        d, "slow.py", _FAKE_COUNT.replace("print(", "import time; time.sleep(0.3); print(")
    )

    # R2: non-finite / out-of-range coordinates => 400, zero spawns
    _reset()
    cf.unlink(missing_ok=True)
    bad = [(91.0, 0.0), (-90.5, 0.0), (0.0, 180.5), (0.0, -181.0)]
    bad += [(float("nan"), 0.0), (0.0, float("nan")), (float("inf"), 0.0), (0.0, float("-inf"))]
    async with _client(slow) as c:
        codes = [(await _get(c, la, lo)) for la, lo in bad]
    results.append(
        (
            "bad coordinates => 400, zero spawns",
            all(r.status_code == 400 for r in codes) and not cf.exists(),
        )
    )

    # R3a: more than 3 waiting => immediate 503, no spawn for the rejected one
    _reset()
    cf.unlink(missing_ok=True)
    weather.TZ_WORKER_TIMEOUT_S = 5.0
    weather._tz_sem = asyncio.Semaphore(1)
    await weather._tz_sem.acquire()  # a "running" lookup
    async with _client(slow) as c:
        waiters = [asyncio.ensure_future(_get(c, 10.0 + i, 20.0)) for i in range(4)]
        await asyncio.sleep(0.2)
        t0 = asyncio.get_running_loop().time()
        over = await _get_capped(c, 50.0, 20.0, 3.0)
        elapsed = asyncio.get_running_loop().time() - t0
        no_spawn_yet = not cf.exists()
        weather._tz_sem.release()
        done = await asyncio.gather(*waiters)
    results.append(
        (
            "over-limit caller => immediate 503, no spawn; waiters still served",
            over is not None
            and over.status_code == 503
            and elapsed < 1.0
            and no_spawn_yet
            and all(r.status_code == 200 for r in done)
            and cf.read_text() == "xxxx",
        )
    )


async def _cases_deadline(d: Path, results: list[tuple[str, bool]]) -> None:
    cf = d / "spawns.txt"
    os.environ["TZ_COUNT_FILE"] = str(cf)
    slow = _write(d, "slow2.py", _FAKE_COUNT)

    # R3b: a waiter past the deadline => 503, no spawn
    _reset()
    cf.unlink(missing_ok=True)
    weather.TZ_WORKER_TIMEOUT_S = 0.5
    weather._tz_sem = asyncio.Semaphore(1)
    await weather._tz_sem.acquire()
    async with _client(slow) as c:
        r = await _get_capped(c, 60.0, 20.0, 3.0)
    weather._tz_sem.release()
    await asyncio.sleep(0.2)
    results.append(
        (
            "waiter past deadline => 503, no spawn",
            r is not None and r.status_code == 503 and not cf.exists(),
        )
    )

    # R3c: ONE deadline covers wait AND run. 0.6 s of a 1.0 s deadline goes on the wait, so a
    # run that got a fresh full deadline would take about 1.6 s.
    _reset()
    pidf = d / "deadline.pid"
    os.environ["TZ_PID_FILE"] = str(pidf)
    weather.TZ_WORKER_TIMEOUT_S = 1.0
    loop = asyncio.get_running_loop()
    weather._tz_sem = asyncio.Semaphore(1)
    await weather._tz_sem.acquire()
    held = weather._tz_sem
    loop.call_later(0.6, held.release)
    t0 = loop.time()
    async with _client(_write(d, "hang3.py", _FAKE_HANG)) as c:
        r = await _get(c, 61.0, 21.0)
    took = loop.time() - t0
    pid = int(pidf.read_text()) if pidf.exists() and pidf.read_text() else -1
    results.append(
        (
            "one deadline over wait and run: 503 at about 1 s and child gone",
            r.status_code == 503 and pid > 0 and _pid_gone(pid) and took < 1.4,
        )
    )

    _reset()


async def _cases_waiter_count(d: Path, results: list[tuple[str, bool]]) -> None:
    # A leaked waiter count would answer every later request 503 until a restart. Cancelled
    # and timed-out waiters must both give their place back.
    _reset()
    weather.TZ_WORKER_TIMEOUT_S = 5.0
    weather._tz_sem = asyncio.Semaphore(1)
    await weather._tz_sem.acquire()
    good = _write(d, "good4.py", 'print("Europe/Berlin")')
    cancelled = [
        asyncio.ensure_future(weather.lookup_timezone(70.0 + i, 20.0, good))
        for i in range(weather.TZ_MAX_WAITING)
    ]
    await asyncio.sleep(0.05)
    queued = weather._tz_waiting
    for task in cancelled:
        task.cancel()
    await asyncio.gather(*cancelled, return_exceptions=True)
    after_cancel = weather._tz_waiting
    weather.TZ_WORKER_TIMEOUT_S = 0.1
    expired = await asyncio.gather(
        *(weather.lookup_timezone(75.0 + i, 20.0, good) for i in range(weather.TZ_MAX_WAITING)),
        return_exceptions=True,
    )
    after_timeout = weather._tz_waiting
    weather._tz_sem.release()
    weather.TZ_WORKER_TIMEOUT_S = 5.0
    async with _client(good) as c:
        r = await _get(c, 62.0, 22.0)
    results.append(
        (
            "cancelled and timed-out waiters leave no count behind, next request 200",
            queued == weather.TZ_MAX_WAITING
            and after_cancel == 0
            and after_timeout == 0
            and all(isinstance(e, weather.TzLookupError) for e in expired)
            and r.status_code == 200,
        )
    )
    _reset()


async def _real_worker(results: list[tuple[str, bool]]) -> None:
    _reset()
    try:
        name = await weather.lookup_timezone(48.1, 11.5)
        ok = name == "Europe/Berlin"
    except weather.TzNotInstalledError:
        ok = False
    results.append(("real worker: Munich => Europe/Berlin", ok))
    _reset()


def _guard_no_heavy_imports(results: list[tuple[str, bool]]) -> None:
    code = (
        "import sys, mcapp.main\n"
        "bad = [m for m in ('numpy', 'timezonefinder') if m in sys.modules]\n"
        "print('LOADED:' + ','.join(bad) if bad else 'CLEAN')\n"
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    out = proc.stdout.strip()
    results.append(
        (
            f"mcapp.main import leaves numpy/timezonefinder out ({out or proc.stderr[-200:]})",
            out == "CLEAN",
        )
    )


async def run_timezone_tests() -> bool:
    """Run the /api/timezone subprocess suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []
    await _cases(results)
    await _real_worker(results)
    await asyncio.to_thread(_guard_no_heavy_imports, results)
    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    timezone: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_timezone_tests()) else 1)
