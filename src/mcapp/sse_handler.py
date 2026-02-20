#!/usr/bin/env python3
"""
Server-Sent Events (SSE) transport for McApp using FastAPI.

This module provides an alternative to WebSocket for clients that prefer
HTTP-based event streaming. It runs alongside the existing WebSocket server.
"""
import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from .logging_setup import get_logger

VERSION = "v0.50.0"

logger = get_logger(__name__)

# Import FastAPI and related modules
try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    logger.warning("FastAPI not installed. SSE transport will not be available.")
    logger.warning("Install with: pip install fastapi uvicorn")

try:
    import uvicorn

    UVICORN_AVAILABLE = True
except ImportError:
    UVICORN_AVAILABLE = False
    logger.warning("Uvicorn not installed. SSE transport will not be available.")


class SendMessageRequest(BaseModel):
    """Request model for sending messages via SSE API."""

    type: str = "msg"
    src: str | None = None
    dst: str = "*"
    msg: str = ""
    MAC: str | None = None
    BLE_Pin: str | None = None
    before: int | None = None
    limit: int = 20


class SSEClient:
    """Represents a connected SSE client."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.connected = True
        self.connected_at = time.time()

    async def send(self, data: dict[str, Any]) -> None:
        """Queue a message for this client."""
        if self.connected:
            await self.queue.put(data)

    def disconnect(self) -> None:
        """Mark client as disconnected."""
        self.connected = False


class SSEManager:
    """
    Manages SSE connections and message broadcasting.

    Manages SSE connections and integrates with the MessageRouter.
    """

    def __init__(
        self, host: str, port: int,
        message_router: Any = None, weather_service: Any = None
    ):
        self.host = host
        self.port = port
        self.message_router = message_router
        self.weather_service = weather_service
        self.clients: dict[str, SSEClient] = {}
        self.clients_lock = asyncio.Lock()
        self.app: FastAPI | None = None
        self.server: Any = None
        self._server_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

        # Subscribe to messages from the router
        if message_router:
            message_router.subscribe("mesh_message", self._broadcast_handler)
            message_router.subscribe("websocket_message", self._broadcast_handler)
            message_router.subscribe("ble_notification", self._broadcast_handler)
            message_router.subscribe("ble_status", self._broadcast_handler)
            # Note: websocket_direct not supported for SSE (no individual connection reference)

        logger.info("SSEManager initialized for %s:%d", host, port)

    def _create_app(self) -> FastAPI:
        """Create and configure the FastAPI application."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            """Handle startup and shutdown."""
            logger.info("SSE server starting up")
            yield
            logger.info("SSE server shutting down")
            await self._disconnect_all_clients()

        app = FastAPI(
            title="McApp SSE API",
            version=VERSION,
            description="Server-Sent Events API for MeshCom message proxy",
            lifespan=lifespan,
        )

        # CORS middleware for cross-origin requests
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

        # SSE endpoint
        @app.get("/events")
        async def sse_endpoint(request: Request):
            """
            Server-Sent Events endpoint.

            Clients connect here to receive real-time message updates.
            """
            client_id = str(uuid.uuid4())[:8]
            client = SSEClient(client_id)

            async with self.clients_lock:
                self.clients[client_id] = client

            logger.info("SSE client connected: %s", client_id)

            async def event_generator():
                try:
                    # Send initial connection confirmation
                    yield self._format_sse_event(
                        {
                            "type": "connected",
                            "client_id": client_id,
                            "timestamp": int(time.time() * 1000),
                        }
                    )

                    # Send initial data (messages, positions, BLE status)
                    try:
                        storage = (
                            self.message_router.storage_handler
                            if self.message_router else None
                        )
                        if storage:
                            if hasattr(storage, 'get_smart_initial_with_summary'):
                                initial_data, summary = (
                                    await storage.get_smart_initial_with_summary()
                                )
                            else:
                                initial_data = await storage.get_smart_initial()
                                summary = await storage.get_summary()
                            logger.info(
                                "SSE client %s: sending smart_initial"
                                " (%d msgs, %d pos, %d acks)",
                                client_id,
                                len(initial_data["messages"]),
                                len(initial_data["positions"]),
                                len(initial_data.get("acks", [])),
                            )
                            yield self._format_sse_event({
                                "type": "response",
                                "msg": "smart_initial",
                                "data": initial_data,
                            })
                            yield self._format_sse_event({
                                "type": "response",
                                "msg": "summary",
                                "data": summary,
                            })
                            # Send persisted read counts for unread badge sync
                            if hasattr(storage, 'get_read_counts'):
                                read_counts = await storage.get_read_counts()
                                if read_counts:
                                    yield self._format_sse_event({
                                        "type": "response",
                                        "msg": "read_counts",
                                        "data": read_counts,
                                    })
                            if hasattr(storage, 'get_hidden_destinations'):
                                hidden_dsts = await storage.get_hidden_destinations()
                                if hidden_dsts:
                                    yield self._format_sse_event({
                                        "type": "response",
                                        "msg": "hidden_destinations",
                                        "data": hidden_dsts,
                                    })
                            if hasattr(storage, 'get_blocked_texts'):
                                blocked_texts = await storage.get_blocked_texts()
                                if blocked_texts:
                                    yield self._format_sse_event({
                                        "type": "response",
                                        "msg": "blocked_texts",
                                        "data": blocked_texts,
                                    })
                            if hasattr(storage, 'get_mheard_sidebar'):
                                sidebar = await storage.get_mheard_sidebar()
                                if sidebar:
                                    yield self._format_sse_event({
                                        "type": "response",
                                        "msg": "mheard_sidebar",
                                        "data": sidebar,
                                    })
                            if hasattr(storage, 'get_wx_sidebar'):
                                wx_sidebar = await storage.get_wx_sidebar()
                                if wx_sidebar:
                                    yield self._format_sse_event({
                                        "type": "response",
                                        "msg": "wx_sidebar",
                                        "data": wx_sidebar,
                                    })
                        else:
                            logger.warning(
                                "SSE client %s: no storage handler available",
                                client_id,
                            )

                        # Send BLE status using same format the frontend expects
                        ble_client = (
                            self.message_router.get_protocol("ble_client")
                            if self.message_router else None
                        )
                        if ble_client:
                            from .ble_client import ConnectionState

                            # Refresh from remote service to get real state
                            if hasattr(ble_client, 'refresh_status'):
                                status = await ble_client.refresh_status()
                            else:
                                status = ble_client.status
                            is_connected = status.state == ConnectionState.CONNECTED

                            if is_connected:
                                ble_info = {
                                    "src_type": "BLE",
                                    "TYP": "blueZ",
                                    "command": "connect BLE result",
                                    "result": "ok",
                                    "msg": "BLE connection already running",
                                    "device_address": status.device_address,
                                    "device_name": status.device_name,
                                    "mode": status.mode.value,
                                    "timestamp": int(time.time() * 1000),
                                }
                            else:
                                ble_info = {
                                    "src_type": "BLE",
                                    "TYP": "blueZ",
                                    "command": "disconnect",
                                    "result": "ok",
                                    "msg": "BLE not connected",
                                    "timestamp": int(time.time() * 1000),
                                }
                            yield self._format_sse_event(ble_info)

                            # If BLE is connected, serve cached registers instantly
                            # instead of re-querying the device.
                            if is_connected:
                                cached_regs = getattr(
                                    self.message_router,
                                    'cached_ble_registers', {},
                                )
                                if cached_regs:
                                    for reg_data in cached_regs.values():
                                        yield self._format_sse_event(reg_data)
                                    logger.info(
                                        "SSE client %s: sent %d cached BLE"
                                        " registers",
                                        client_id, len(cached_regs),
                                    )

                        logger.info("SSE client %s: initial data sent", client_id)
                    except Exception as e:
                        logger.error(
                            "SSE client %s: failed to send initial data: %s",
                            client_id, e, exc_info=True,
                        )

                    while client.connected:
                        # Check if client disconnected
                        if await request.is_disconnected():
                            break

                        try:
                            # Wait for message with timeout (for keepalive)
                            data = await asyncio.wait_for(client.queue.get(), timeout=30.0)
                            yield self._format_sse_event(data)
                        except asyncio.TimeoutError:
                            # Send keepalive ping
                            yield self._format_sse_event(
                                {
                                    "type": "ping",
                                    "timestamp": int(time.time() * 1000),
                                }
                            )

                except asyncio.CancelledError:
                    pass
                finally:
                    client.disconnect()
                    async with self.clients_lock:
                        self.clients.pop(client_id, None)
                    logger.info("SSE client disconnected: %s", client_id)

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # Disable nginx buffering
                },
            )

        # Message sending endpoint
        @app.post("/api/send")
        async def send_message(request: SendMessageRequest):
            """
            Send a message through the mesh network.

            This endpoint mirrors the WebSocket message sending functionality.
            """
            if not self.message_router:
                raise HTTPException(status_code=503, detail="Message router not available")

            message_data = {
                "type": request.type,
                "dst": request.dst,
                "msg": request.msg,
            }

            if request.src:
                message_data["src"] = request.src

            try:
                if request.type == "page_request":
                    # Paginated message fetch — response via SSE stream
                    page_data = {
                        "dst": request.dst,
                        "before": getattr(request, "before", None),
                        "limit": getattr(request, "limit", 20),
                    }
                    if request.src:
                        page_data["src"] = request.src
                    await self.message_router.route_command(
                        "get_messages_page",
                        websocket=None,
                        data=page_data,
                    )
                elif request.type == "command":
                    # Route command through message router
                    await self.message_router.route_command(
                        request.msg,
                        websocket=None,
                        MAC=request.MAC,
                        BLE_Pin=request.BLE_Pin,
                    )
                elif request.type == "BLE":
                    # Publish BLE message
                    await self.message_router.publish(
                        "sse",
                        "ble_message",
                        {"msg": request.msg, "dst": request.dst},
                    )
                else:
                    # Publish UDP message (default)
                    await self.message_router.publish("sse", "udp_message", message_data)

                return {"status": "ok", "message": "Message queued for delivery"}

            except Exception as e:
                logger.error("Failed to send message via SSE API: %s", e)
                raise HTTPException(status_code=500, detail=str(e))

        # Status endpoint
        @app.get("/api/status")
        async def get_status():
            """Get SSE server status."""
            async with self.clients_lock:
                client_count = len(self.clients)

            return {
                "status": "ok",
                "version": VERSION,
                "clients": client_count,
                "uptime_seconds": int(time.time() - getattr(self, "_start_time", time.time())),
            }

        # Read counts endpoints (unread badge persistence)
        @app.get("/api/read_counts")
        async def get_read_counts():
            """Get persisted read counts for unread badge sync."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "get_read_counts"):
                raise HTTPException(status_code=503, detail="Storage not available")
            return await storage.get_read_counts()

        @app.post("/api/read_counts")
        async def set_read_count(request: Request):
            """Persist a read count for a destination."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "set_read_count"):
                raise HTTPException(status_code=503, detail="Storage not available")
            body = await request.json()
            dst = body.get("dst")
            count = body.get("count")
            if not dst or count is None:
                raise HTTPException(status_code=400, detail="Missing dst or count")
            await storage.set_read_count(str(dst), int(count))
            return {"status": "ok"}

        # Hidden destinations endpoints (persist hidden groups)
        @app.get("/api/hidden_destinations")
        async def get_hidden_destinations():
            """Get list of hidden destination identifiers."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "get_hidden_destinations"):
                raise HTTPException(status_code=503, detail="Storage not available")
            return await storage.get_hidden_destinations()

        @app.post("/api/hidden_destinations")
        async def set_hidden_destinations(request: Request):
            """Show/hide destinations. Single: {dst, hidden}. Bulk: {destinations: [...]}."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "update_hidden_destination"):
                raise HTTPException(status_code=503, detail="Storage not available")
            body = await request.json()
            if "destinations" in body:
                await storage.set_hidden_destinations(
                    [str(d) for d in body["destinations"]]
                )
            else:
                dst = body.get("dst")
                hidden = body.get("hidden", True)
                if not dst:
                    raise HTTPException(status_code=400, detail="Missing dst")
                await storage.update_hidden_destination(str(dst), bool(hidden))
            return {"status": "ok"}

        # Blocked texts endpoints (persist blocked message patterns)
        @app.get("/api/blocked_texts")
        async def get_blocked_texts():
            """Get list of blocked text patterns."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "get_blocked_texts"):
                raise HTTPException(status_code=503, detail="Storage not available")
            return await storage.get_blocked_texts()

        @app.post("/api/blocked_texts")
        async def set_blocked_texts(request: Request):
            """Add/remove blocked texts. Single: {text, blocked}. Bulk: {texts: [...]}."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "update_blocked_text"):
                raise HTTPException(status_code=503, detail="Storage not available")
            body = await request.json()
            if "texts" in body:
                await storage.set_blocked_texts(
                    [str(t) for t in body["texts"]]
                )
            else:
                text = body.get("text")
                blocked = body.get("blocked", True)
                if not text:
                    raise HTTPException(status_code=400, detail="Missing text")
                await storage.update_blocked_text(str(text), bool(blocked))
            return {"status": "ok"}

        # mHeard sidebar endpoints (persist station order + hidden)
        @app.get("/api/mheard/sidebar")
        async def get_mheard_sidebar():
            """Get mheard sidebar state."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "get_mheard_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            result = await storage.get_mheard_sidebar()
            return result or {"order": [], "hidden": []}

        @app.post("/api/mheard/sidebar")
        async def set_mheard_sidebar(request: Request):
            """Set mheard sidebar state."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "set_mheard_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            body = await request.json()
            order = [str(s) for s in body.get("order", [])]
            hidden = [str(s) for s in body.get("hidden", [])]
            await storage.set_mheard_sidebar(order, hidden)
            return {"status": "ok"}

        # WX sidebar endpoints (persist station order + hidden)
        @app.get("/api/wx/sidebar")
        async def get_wx_sidebar():
            """Get WX sidebar state."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "get_wx_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            result = await storage.get_wx_sidebar()
            return result or {"order": [], "hidden": []}

        @app.post("/api/wx/sidebar")
        async def set_wx_sidebar(request: Request):
            """Set WX sidebar state."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "set_wx_sidebar"):
                raise HTTPException(status_code=503, detail="Storage not available")
            body = await request.json()
            order = [str(s) for s in body.get("order", [])]
            hidden = [str(s) for s in body.get("hidden", [])]
            await storage.set_wx_sidebar(order, hidden)
            return {"status": "ok"}

        # Health check endpoint
        @app.get("/health")
        async def health_check():
            """Health check endpoint for load balancers."""
            return {"status": "healthy"}

        # Weather data endpoint
        @app.get("/api/weather")
        async def get_weather():
            """Get current weather data from the meteo service."""
            if not self.weather_service:
                raise HTTPException(status_code=503, detail="Weather service not available")

            # If no GPS yet, try cached GPS or trigger BLE query
            if self.weather_service.lat is None and self.message_router:
                cached = getattr(self.message_router, 'cached_gps', None)
                if cached:
                    self.weather_service.update_location(cached['lat'], cached['lon'])
                else:
                    # Query BLE device for GPS (one-shot)
                    ble = self.message_router.get_protocol('ble_client')
                    if ble and hasattr(ble, 'is_connected') and ble.is_connected:
                        await ble.send_command("--pos")
                    return {
                        "error": "Warte auf GPS vom Gerät...",
                        "timestamp": int(time.time() * 1000),
                    }

            data = await asyncio.to_thread(self.weather_service.get_weather_data)
            return data

        # Server time endpoint (for frontend clock sync)
        @app.get("/api/time")
        async def get_time():
            """Return server time for frontend clock sync."""
            return {
                "server_time_ms": int(time.time() * 1000),
                "timezone": time.tzname[time.daylight and time.localtime().tm_isdst],
            }

        # Telemetry data endpoint (for WX charts)
        @app.get("/api/telemetry")
        async def get_telemetry(hours: int = 48):
            """Get telemetry data for weather charts."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "get_telemetry_chart_data"):
                raise HTTPException(
                    status_code=503, detail="Telemetry not available"
                )
            return await storage.get_telemetry_chart_data(hours=min(hours, 744))

        @app.get("/api/telemetry/yearly")
        async def get_telemetry_yearly():
            """Get telemetry data aggregated into 4h buckets for yearly charts."""
            storage = (
                self.message_router.storage_handler if self.message_router else None
            )
            if not storage or not hasattr(storage, "get_telemetry_chart_data_bucketed"):
                raise HTTPException(
                    status_code=503, detail="Telemetry not available"
                )
            return await storage.get_telemetry_chart_data_bucketed()

        @app.get("/api/timezone")
        async def get_timezone(lat: float, lon: float):
            """Return UTC offset for given coordinates using timezonefinder."""
            import zoneinfo
            from datetime import datetime

            from timezonefinder import TimezoneFinder

            tf = TimezoneFinder()
            tz_name = tf.timezone_at(lat=lat, lng=lon)
            if not tz_name:
                raise HTTPException(
                    status_code=400, detail="No timezone found for coordinates"
                )
            zone = zoneinfo.ZoneInfo(tz_name)
            offset_seconds = datetime.now(zone).utcoffset().total_seconds()
            offset_hours = offset_seconds / 3600
            abbreviation = datetime.now(zone).strftime("%Z")
            return {"timezone": tz_name, "abbreviation": abbreviation, "utc_offset": offset_hours}

        # ── Update / Deployment Endpoints ──────────────────────────

        @app.get("/api/update/check")
        async def check_update():
            """Check GitHub for available version updates (cached 5 min)."""
            return await self._check_update_version()

        @app.post("/api/update/start")
        async def start_update(request: Request):
            """Launch the update runner process."""
            body = await request.json() if request.headers.get("content-length") else {}
            dev = body.get("dev", False)
            return await self._launch_update_runner("update", dev=dev)

        @app.post("/api/update/rollback")
        async def start_rollback():
            """Launch the update runner in rollback mode."""
            return await self._launch_update_runner("rollback")

        @app.get("/api/update/slots")
        async def get_slots():
            """Get slot metadata (versions, active slot, rollback target)."""
            return await asyncio.to_thread(self._read_slot_info)

        return app

    # ── Update / Deployment Helpers ─────────────────────────────

    _update_check_cache: dict[str, Any] = {}
    _update_check_time: float = 0.0

    async def _check_update_version(self) -> dict[str, Any]:
        """Check GitHub releases for available version (cached 5 min)."""
        now = time.time()
        if now - self._update_check_time < 300 and self._update_check_cache:
            return self._update_check_cache

        import urllib.error
        import urllib.request

        installed = self._get_installed_version()
        available = "unknown"

        try:
            url = "https://api.github.com/repos/DK5EN/McApp/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "McApp"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                available = data.get("tag_name", "unknown")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass

        result = {
            "installed": installed,
            "available": available,
            "update_available": (
                available != "unknown"
                and installed != "not_installed"
                and available.lstrip("v") != installed.lstrip("v")
            ),
        }
        self._update_check_cache = result
        self._update_check_time = now
        return result

    def _get_installed_version(self) -> str:
        """Read installed version from version.html."""
        import pathlib
        for path in [
            pathlib.Path("/var/www/html/webapp/version.html"),
            pathlib.Path.home() / "mcapp-slots" / "current" / "webapp" / "version.html",
        ]:
            if path.exists():
                return path.read_text().strip()
        return "not_installed"

    async def _launch_update_runner(
        self, mode: str, dev: bool = False,
    ) -> dict[str, Any]:
        """Launch the standalone update runner via sudo systemd-run."""
        import pathlib
        import socket

        # Check if runner is already active (port 2985 in use)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", 2985))
            sock.close()
            if result == 0:
                raise HTTPException(
                    status_code=409,
                    detail="Update already in progress",
                )
        except OSError:
            pass

        # Find the update runner script
        runner = pathlib.Path.home() / "mcapp-slots" / "current" / "scripts" / "update-runner.py"
        if not runner.exists():
            raise HTTPException(
                status_code=500,
                detail=f"Update runner not found at {runner}",
            )

        # Build command
        cmd = [
            "sudo", "systemd-run",
            "--scope", "--unit=mcapp-update",
            sys.executable, str(runner),
            "--mode", mode,
            "--home", str(pathlib.Path.home()),
        ]
        if dev:
            cmd.append("--dev")

        try:
            import subprocess
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error("Failed to launch update runner: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

        # Determine the host from request context
        host = self.host if self.host != "0.0.0.0" else "localhost"

        return {
            "status": "launched",
            "mode": mode,
            "stream_url": f"http://{host}:2985/stream",
            "status_url": f"http://{host}:2985/status",
        }

    def _read_slot_info(self) -> dict[str, Any]:
        """Read slot metadata from filesystem."""
        import pathlib
        slots_dir = pathlib.Path.home() / "mcapp-slots"
        meta_dir = slots_dir / "meta"

        if not slots_dir.exists():
            return {"slots": [], "active_slot": None, "can_rollback": False}

        # Get active slot
        active_slot = None
        current = slots_dir / "current"
        if current.is_symlink():
            target = current.resolve().name
            if target.startswith("slot-"):
                active_slot = int(target.split("-")[1])

        slots = []
        for i in range(3):
            meta_file = meta_dir / f"slot-{i}.json"
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
            else:
                meta = {"slot": i, "version": None, "status": "empty",
                        "deployed_at": None}
            meta["slot"] = i
            if i == active_slot:
                meta["status"] = "active"
            elif meta.get("version"):
                meta["status"] = "available"
            else:
                meta["status"] = "empty"
            slots.append(meta)

        # Find rollback target
        rollback_target = None
        candidates = []
        for s in slots:
            if s["slot"] != active_slot and s.get("version"):
                candidates.append(s)
        if candidates:
            candidates.sort(key=lambda x: x.get("deployed_at", ""), reverse=True)
            rollback_target = candidates[0]["slot"]

        return {
            "slots": slots,
            "active_slot": active_slot,
            "can_rollback": rollback_target is not None,
            "rollback_target": rollback_target,
        }

    @staticmethod
    def _format_sse_event(data: dict[str, Any], event_type: str | None = None) -> str:
        """Format data as SSE event."""
        lines = []
        if event_type:
            lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")  # Empty line to separate events
        return "\n".join(lines) + "\n"

    async def _broadcast_handler(self, routed_message: dict[str, Any]) -> None:
        """Handle messages from the router and broadcast to SSE clients."""
        message_data = routed_message["data"]
        await self.broadcast_message(message_data)

        if logger.isEnabledFor(10):  # DEBUG level
            truncated = str(message_data)[:120]
            logger.debug(
                "SSE broadcast %s from %s: %s",
                routed_message["type"],
                routed_message["source"],
                truncated,
            )

    async def broadcast_message(self, message: dict[str, Any]) -> None:
        """Broadcast message to all connected SSE clients."""
        async with self.clients_lock:
            clients = list(self.clients.values())

        if not clients:
            return

        # Queue message for all clients
        for client in clients:
            try:
                await client.send(message)
            except Exception as e:
                logger.warning("Failed to queue message for SSE client %s: %s", client.client_id, e)

    async def _disconnect_all_clients(self) -> None:
        """Disconnect all SSE clients."""
        async with self.clients_lock:
            for client in self.clients.values():
                client.disconnect()
            self.clients.clear()

    async def start_server(self) -> None:
        """Start the SSE/FastAPI server."""
        if not FASTAPI_AVAILABLE or not UVICORN_AVAILABLE:
            logger.error("Cannot start SSE server: FastAPI or Uvicorn not installed")
            return

        self.app = self._create_app()
        self._start_time = time.time()

        # Create uvicorn config
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",  # Reduce uvicorn logging noise
            access_log=False,
        )
        self.server = uvicorn.Server(config)

        # Run server in background task
        self._server_task = asyncio.create_task(self._run_server())
        logger.info("SSE server started on http://%s:%d", self.host, self.port)

    async def _run_server(self) -> None:
        """Run the uvicorn server."""
        try:
            await self.server.serve()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SSE server error: %s", e)

    async def stop_server(self) -> None:
        """Stop the SSE/FastAPI server."""
        if self.server:
            self.server.should_exit = True

            # Wait for server to stop
            if self._server_task:
                try:
                    await asyncio.wait_for(self._server_task, timeout=5.0)
                except asyncio.TimeoutError:
                    self._server_task.cancel()
                    try:
                        await self._server_task
                    except asyncio.CancelledError:
                        pass

        await self._disconnect_all_clients()
        logger.info("SSE server stopped")

    def get_client_count(self) -> int:
        """Return number of connected SSE clients."""
        return len(self.clients)


# Convenience function for backward compatibility
def create_sse_manager(
    host: str = "127.0.0.1",
    port: int = 2981,
    message_router: Any = None,
    weather_service: Any = None,
) -> SSEManager | None:
    """
    Create an SSE manager if dependencies are available.

    Returns None if FastAPI/Uvicorn are not installed.
    """
    if not FASTAPI_AVAILABLE or not UVICORN_AVAILABLE:
        logger.warning("SSE transport not available - missing dependencies")
        return None

    return SSEManager(host, port, message_router, weather_service)
