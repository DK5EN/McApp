"""Weather / telemetry / timezone REST endpoints (SSE-01)."""

from __future__ import annotations

import asyncio
import contextlib
import math
import sys
import zoneinfo
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging_setup import get_logger
from ..meteo import is_valid_position
from ..util import now_ms
from ..weblite import APIRouter, HTTPException

if TYPE_CHECKING:
    from ..sse_handler import SSEManager

logger = get_logger(__name__)

TELEMETRY_MAX_HOURS = 744

# Time-zone lookup runs in a short-lived child (numpy + TimezoneFinder are ~20 MB
# resident; the child peaks at ~33 MB for 1.2-2 s and is gone afterwards).
TZ_WORKER_PATH = Path(__file__).resolve().parent.parent / "tz_lookup_worker.py"
TZ_WORKER_TIMEOUT_S = 8.0  # the browser aborts at 10 s
TZ_MAX_WAITING = 4  # this many already queued behind the running lookup => refuse the next
_LAT_MAX = 90
_LON_MAX = 180
TZ_CACHE_MAX = 64
_EXIT_NOT_INSTALLED = 3

_tz_cache: dict[tuple[float, float], str] = {}
_tz_sem: asyncio.Semaphore | None = None
_tz_waiting = 0


class TzLookupError(Exception):
    """Worker failed (timeout, crash, bad output). Maps to HTTP 503."""


class TzNotInstalledError(Exception):
    """timezonefinder is not importable in the worker. Maps to HTTP 501."""


def _tz_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 2), round(lon, 2))


async def lookup_timezone(lat: float, lon: float, worker: Path | None = None) -> str | None:
    """Zone name for the coordinates, or None when none covers them.

    At most one child at a time; successes are cached (0.01 degree grid), failures never.
    One deadline (TZ_WORKER_TIMEOUT_S) covers the wait for the slot AND the run, and at most
    TZ_MAX_WAITING requests queue: Starlette does not cancel on client disconnect, so an
    unbounded queue would spawn children for requests nobody is waiting on any more.
    """
    global _tz_sem, _tz_waiting
    key = _tz_key(lat, lon)
    if key in _tz_cache:
        return _tz_cache[key] or None
    if _tz_sem is None:
        _tz_sem = asyncio.Semaphore(1)
    sem = _tz_sem
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TZ_WORKER_TIMEOUT_S
    if _tz_waiting >= TZ_MAX_WAITING:
        raise TzLookupError("busy")
    _tz_waiting += 1
    try:
        async with asyncio.timeout(TZ_WORKER_TIMEOUT_S):
            await sem.acquire()
    except TimeoutError:
        raise TzLookupError("queue timeout") from None
    finally:
        _tz_waiting -= 1
    try:
        if key in _tz_cache:
            return _tz_cache[key] or None
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TzLookupError("queue timeout")
        name = await _run_tz_worker(lat, lon, worker or TZ_WORKER_PATH, remaining)
        while len(_tz_cache) >= TZ_CACHE_MAX:
            del _tz_cache[next(iter(_tz_cache))]
        _tz_cache[key] = name
        return name or None
    finally:
        sem.release()


async def _run_tz_worker(
    lat: float, lon: float, worker: Path, timeout_s: float | None = None
) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(worker),
            repr(lat),
            repr(lon),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("tz worker spawn failed: %s", exc)
        raise TzLookupError("spawn failed") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s or TZ_WORKER_TIMEOUT_S)
    except BaseException as exc:
        # Timeout or cancellation: kill AND reap so no zombie or 33 MB child lingers.
        with contextlib.suppress(ProcessLookupError):  # already exited: nothing to kill
            proc.kill()
        await asyncio.shield(proc.wait())
        if isinstance(exc, TimeoutError):
            logger.warning("tz worker timed out after %.0f s", TZ_WORKER_TIMEOUT_S)
            raise TzLookupError("timeout") from exc
        raise
    if proc.returncode == _EXIT_NOT_INSTALLED:
        raise TzNotInstalledError
    if proc.returncode != 0:
        logger.warning(
            "tz worker exit %s: %s",
            proc.returncode,
            err.decode("utf-8", "replace")[:200].strip(),
        )
        raise TzLookupError(f"exit {proc.returncode}")
    return out.decode("utf-8", "replace").strip()


def build_weather_router(manager: SSEManager) -> APIRouter:
    """Build the /api/weather, /api/telemetry, /api/timezone router."""
    router = APIRouter()

    # Weather data endpoint
    @router.get("/api/weather")
    async def get_weather() -> Any:
        """Get current weather data from the meteo service."""
        if not manager.weather_service:
            raise HTTPException(status_code=503, detail="Weather service not available")

        # If no GPS yet, try cached GPS or trigger BLE query
        if (
            not is_valid_position(manager.weather_service.lat, manager.weather_service.lon)
            and manager.message_router
        ):
            cached = getattr(manager.message_router, "cached_gps", None)
            if cached and is_valid_position(cached.get("lat"), cached.get("lon")):
                manager.weather_service.update_location(cached["lat"], cached["lon"])
            else:
                # Query BLE device for GPS (one-shot)
                ble = manager.message_router.get_protocol("ble_client")
                if ble and hasattr(ble, "is_connected") and ble.is_connected:
                    await ble.send_command("--pos")
                return {
                    "error": "Warte auf GPS vom Gerät...",
                    "timestamp": now_ms(),
                }

        return await asyncio.to_thread(manager.weather_service.get_weather_data)

    @router.get("/api/weather/preview")
    async def get_weather_preview(text: str = "") -> dict[str, str]:
        """Return the formatted WX message as it would appear on the mesh."""
        if not manager.weather_service:
            raise HTTPException(status_code=503, detail="Weather service not available")

        if not is_valid_position(manager.weather_service.lat, manager.weather_service.lon):
            return {"preview": "WX: Warte auf GPS..."}

        data = await asyncio.to_thread(manager.weather_service.get_weather_data)
        if "error" in data:
            return {"preview": f"WX ERR: {data['error'][:25]}"}

        formatted = manager.weather_service.format_for_lora(data, prefix_text=text)
        return {"preview": formatted}

    # Telemetry data endpoint (for WX charts)
    @router.get("/api/telemetry")
    async def get_telemetry(hours: int = 48) -> Any:
        """Get telemetry data for weather charts."""
        storage = manager.require_storage()
        return await storage.get_telemetry_chart_data(hours=min(hours, TELEMETRY_MAX_HOURS))

    @router.get("/api/telemetry/yearly")
    async def get_telemetry_yearly() -> Any:
        """Get telemetry data aggregated into 4h buckets for yearly charts."""
        storage = manager.require_storage()
        return await storage.get_telemetry_chart_data_bucketed()

    @router.get("/api/timezone")
    async def get_timezone(lat: float, lon: float) -> dict[str, str | float]:
        """Return UTC offset for given coordinates in a short-lived subprocess."""
        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and abs(lat) <= _LAT_MAX
            and abs(lon) <= _LON_MAX
        ):
            raise HTTPException(
                status_code=400,
                detail="lat must be within +/-90 and lon within +/-180 (finite numbers)",
            )
        try:
            tz_name = await lookup_timezone(lat, lon)
        except TzNotInstalledError:
            raise HTTPException(status_code=501, detail="timezonefinder is not installed") from None
        except TzLookupError:
            raise HTTPException(status_code=503, detail="Timezone lookup failed") from None
        if not tz_name:
            raise HTTPException(status_code=400, detail="No timezone found for coordinates")
        zone = zoneinfo.ZoneInfo(tz_name)
        offset = datetime.now(zone).utcoffset()
        if offset is None:
            raise HTTPException(status_code=500, detail="Unable to calculate UTC offset")
        offset_seconds = offset.total_seconds()
        offset_hours = offset_seconds / 3600
        abbreviation = datetime.now(zone).strftime("%Z")
        return {"timezone": tz_name, "abbreviation": abbreviation, "utc_offset": offset_hours}

    return router
