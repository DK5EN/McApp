"""Seam between the internet feed service and its HTTP routes.

The routes (`sse_routes/internet_feed.py`) only see `InternetFeedApi`; the
service (`internet_feed.py`) implements it. Design:
doc/2026-10-11_1005-internet-feed-concept.md §4.1-§4.2.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

FeedState = Literal["off", "connecting", "connected", "error", "revoked"]

MAX_GROUPS = 10
RADIUS_KM_MIN = 10
RADIUS_KM_MAX = 2000
POS_MODES = ("off", "radius", "world")

# Topic the service publishes mapped feed messages on. Subscribers (wired in
# main.py / sse_handler.py / sse_routes/push.py): storage, SSE broadcast, push
# — in that order. Never the CommandHandler or the wire monitor.
WWW_MESSAGE_TOPIC = "www_message"

# SSE event the service emits on every state transition, replayed on connect.
STATUS_EVENT = "internet:status"

# SSE event carrying meshmap node positions from the RAM cache (concept §4.5):
# {reset?, upserts[], removed[]}; a full `reset` snapshot is replayed on connect.
POSITIONS_EVENT = "internet:positions"


class InternetFeedSettingsError(ValueError):
    """A settings PUT failed validation; the message is safe to show."""


class InternetFeedApi(Protocol):
    async def get_settings(self) -> dict[str, Any]:
        """Settings plus status for `GET /api/internet-feed`.

        Keys: enabled, groups, broadcast, pos_mode, radius_km, configured,
        status ({state, since_ms, last_batch_ms, last_connected_ms, error}),
        registered (bool). Never the token. `suggested` is added by the
        route layer (it reads the BLE register cache), not by the service.
        """
        ...

    async def apply_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Persist ALREADY-VALIDATED settings, mark `configured`, reconnect.

        Validation is the route layer's job (`sse_routes/internet_feed.py`);
        the dict carries exactly: enabled, groups, broadcast, pos_mode,
        radius_km. Returns `get_settings()`.
        """
        ...

    async def forget(self) -> dict[str, Any]:
        """Drop box_id and token; the next connect registers fresh."""
        ...

    def status(self) -> dict[str, Any]:
        """Current `internet:status` payload: {state, since_ms, last_batch_ms, error}."""
        ...
