#!/usr/bin/env python3
"""
Server-Sent Events (SSE) transport for MCProxy using FastAPI.

This module provides an alternative to WebSocket for clients that prefer
HTTP-based event streaming. It runs alongside the existing WebSocket server.
"""
import asyncio
import json
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
    dst: str
    msg: str
    MAC: str | None = None
    BLE_Pin: str | None = None


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

    Mirrors the WebSocketManager pattern for consistent integration
    with the MessageRouter.
    """

    def __init__(self, host: str, port: int, message_router: Any = None):
        self.host = host
        self.port = port
        self.message_router = message_router
        self.clients: dict[str, SSEClient] = {}
        self.clients_lock = asyncio.Lock()
        self.app: FastAPI | None = None
        self.server: Any = None
        self._server_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

        # Subscribe to messages (same as WebSocketManager)
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
            title="MCProxy SSE API",
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
                if request.type == "command":
                    # Route command through message router
                    await self.message_router.route_command(
                        request.msg,
                        websocket=None,  # No websocket for SSE
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

        # Health check endpoint
        @app.get("/health")
        async def health_check():
            """Health check endpoint for load balancers."""
            return {"status": "healthy"}

        return app

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
    host: str = "0.0.0.0",
    port: int = 2981,
    message_router: Any = None,
) -> SSEManager | None:
    """
    Create an SSE manager if dependencies are available.

    Returns None if FastAPI/Uvicorn are not installed.
    """
    if not FASTAPI_AVAILABLE or not UVICORN_AVAILABLE:
        logger.warning("SSE transport not available - missing dependencies")
        return None

    return SSEManager(host, port, message_router)
