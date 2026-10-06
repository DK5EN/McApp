"""Interface pins for Node Admin (RM1 remote admin), wave W0.

The W2 writers code against these names so their file sets stay disjoint: the
service (`node_admin_service.py`) implements `NodeAdminService`, the router
(`sse_routes/node_admin.py`) calls it through `SSEManager.node_admin_service`,
and `storage/ingest.py` calls the `ReplyHook`. No logic lives here.
Plan: doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md §5-6.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

# `transmit(transport, dst, msg)`: hand one DM to the transport helper that
# `_handle_outbound` uses (`transport` is "ble" or "udp"). Returns None on
# success, else a short failure reason (e.g. "BLE not connected"). Built in
# main.py; the service never imports main.
TransmitFn = Callable[[str, str, str], Awaitable[str | None]]

# Called by `store_message` BEFORE `_should_filter_message` with the raw inbound
# message dict. Observes only; the seam wraps it in try/except so a raising hook
# can never lose the stored row. Assigned by main.py to `service.on_reply`.
ReplyHook = Callable[[dict[str, Any]], Awaitable[None]]


class NodeAdminError(Exception):
    """Base class for Node Admin refusals mapped to HTTP statuses by the router."""


class NodeAdminBusyError(NodeAdminError):
    """A command is in flight for the target, or the 10 s spacing has not elapsed (HTTP 409)."""


class NodeAdminUnavailableError(NodeAdminError):
    """No route to the attached node, node call unknown, or key unreadable (HTTP 409/503)."""


class NodeAdminService(Protocol):
    """What the router and the reply hook need from the service.

    `ValueError` from any method means invalid input (HTTP 422);
    `NodeAdminBusyError` -> 409; `NodeAdminUnavailableError` -> 503. Targets are
    canonicalised by the service (`remote_cmd.normalize_call`).
    """

    async def list_targets(self) -> list[dict[str, Any]]:
        """`[{target, has_key, key_unreadable, ctr, last_hwm, last_sync_at, tx_max}]`."""
        ...

    async def set_key(self, target: str, password: str, tx_max: int | None) -> None:
        """Validate, encrypt (AAD `node_admin.password:<TARGET>`) and store. Never returned."""
        ...

    async def delete_key(self, target: str) -> None:
        """Remove the key only; counter state and history are kept."""
        ...

    async def send(self, target: str, cmd: str, args: str, transport: str) -> dict[str, Any]:
        """Allocate, log and return `{log_id, ctr, text}`; transmit runs as a background task."""
        ...

    async def reask(self, log_id: int) -> dict[str, Any]:
        """Re-send the byte-identical stored frame of the newest row; `{log_id}`."""
        ...

    async def sync(self, target: str) -> dict[str, Any]:
        """Send `RM1 0 sync <tag>`; `{log_id}`."""
        ...

    async def history(self, target: str | None, limit: int) -> list[dict[str, Any]]:
        """Rows newest first, each with a server-computed `state` field."""
        ...

    async def state(self, target: str) -> dict[str, Any]:
        """Last known state plus connection and pacing of `target` (concept Appendix A)."""
        ...

    async def on_reply(self, message: dict[str, Any]) -> None:
        """The `ReplyHook`: correlate, verify, update the row, emit `node_admin:reply`."""
        ...
