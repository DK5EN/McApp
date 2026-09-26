"""RF Monitor (`/monitor`) wire-level capture — MCProxy half of the RF
Monitor wire contract v1 (webapp `docs/rf-monitor-plan.md`, "Wire contract
v1" / "Two axes"; mc-chat implements the same contract independently).

`WireMonitor` is an in-memory, per-process ring of every frame this backend
has seen or sent — including frames the normal views filter out, tagged with
why. Not persisted, capped at `RING_MAXLEN`. Wired once in `build_app`
(`main.py`): `router` is the live `MessageRouter` (verdict computation reads
its blocklist) and `sse_manager` is the live `SSEManager` (envelopes are
broadcast live as `wire:frame`); both attributes may be left `None` in a
test harness, where `capture()`/`page()` still work standalone.

Two capture paths:

* **RX** — `on_mesh_message`/`on_ble_notification`/`on_ble_status` subscribe
  to the same `MessageRouter` topics the SSE broadcaster does (`sse_handler.
  SSEManager.__init__`), wired alongside it in `build_app`. `on_mesh_message`
  and `on_ble_notification` both run `broadcast_verdict()` — the SAME pure
  function `SSEManager._broadcast_handler` calls to decide what actually
  reaches SSE clients — so the monitor and the live stream can never
  disagree about a verdict.
* **TX** — a single point in `MessageRouter._handle_outbound` (main.py)
  calls `capture()` directly, once per outbound send attempt, with
  `link="app"`.

`capture()` must never raise into the publish path: a monitor bug can
never take down message delivery, only lose its own visibility into one
frame.
"""

from __future__ import annotations

import copy
import secrets
from collections import deque
from typing import Any, cast

from .logging_setup import get_logger
from .sse_handler import broadcast_verdict
from .util import now_ms

logger = get_logger(__name__)

# Ring capacity (contract: "Ring capacity: 2000 envelopes per backend
# process, in memory, not persisted").
RING_MAXLEN = 2000

DEFAULT_PAGE_LIMIT = 500
MAX_PAGE_LIMIT = 2000

# The dispatcher-recognized mesh frame `type`s (ble_protocol.py's
# transform_msg/transform_pos/transform_tele/transform_ack, plus transform_mh
# — an MHeard beacon transformed into a synthetic "pos" frame). Every BLE
# register/config notification (`ble_protocol.transform_ble`, TYP in
# ROUTINE_JSON_TYPS: I/G/SN/SA/W/IO/TM/AN/SE/SW/S1/S2/IS1/SN1) carries no `type` key
# at all — it spreads the firmware's own `TYP` field instead and stamps
# `src_type: "BLE"` (upper-case) — so gating on `type` membership already
# excludes every register/config notification without re-deriving the TYP
# allowlist here; a firmware TYP this repo doesn't recognize still falls
# outside this set and stays excluded.
_MESH_FRAME_TYPES = frozenset({"msg", "pos", "tele", "ack"})


class WireMonitor:
    """In-memory ring + live broadcast of every wire-level frame. See module
    docstring for the two capture paths and the wiring contract.
    """

    def __init__(self) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=RING_MAXLEN)
        # Node debug console lines (link == "console") live in their OWN ring,
        # so a console session's ~200 B/s of output (node debug console
        # bridge, `node_console.py`) can never evict RF frames from `_ring`
        # — the two are independent capacities, not a shared budget. `seq`
        # is still ONE shared counter across both rings (assigned in
        # `capture()` before either ring is chosen), so `page()` can merge
        # them back into one ordered stream.
        self._console_ring: deque[dict[str, Any]] = deque(maxlen=RING_MAXLEN)
        # Random per-process token (contract: "a change means seq
        # restarted") — lets a reconnecting client detect a backend restart
        # instead of misreading a reset seq counter as a gap.
        self.boot: str = secrets.token_hex(4)
        self._seq: int = 0
        # Set by build_app (main.py); None is a valid, fully-functional
        # state for a test harness (ring + page() only, no live broadcast /
        # no blocklist lookups — blocklist_decision() already tolerates a
        # None router the same way).
        self.router: Any = None
        self.sse_manager: Any = None

    async def capture(
        self,
        link: str,
        direction: str,
        verdict: str,
        reason: str | None,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one envelope, append it to the ring, and broadcast it live
        as `wire:frame` (bare payload — contract "Wire contract v1").

        Returns the envelope (`{}` if capture itself failed). `frame` is
        deep-copied immediately, before any `await`, so a caller mutating
        its own dict afterwards can never change what was captured. Never
        raises: any failure (a broadcast error, an unpicklable frame, a
        wiring bug) is logged and swallowed, never propagated into the
        publish path that triggered it.
        """
        try:
            self._seq += 1
            envelope: dict[str, Any] = {
                "v": 1,
                "boot": self.boot,
                "seq": self._seq,
                "ts": now_ms(),
                "link": link,
                "dir": direction,
                "verdict": verdict,
                "reason": reason,
                "frame": copy.deepcopy(frame),
            }
            ring = self._console_ring if link == "console" else self._ring
            ring.append(envelope)
        except Exception:
            logger.exception(
                "WireMonitor.capture failed to build/store envelope (link=%s dir=%s verdict=%s)",
                link,
                direction,
                verdict,
            )
            return {}
        try:
            if self.sse_manager is not None:
                await self.sse_manager.broadcast_event("wire:frame", envelope)
        except Exception:
            logger.exception("WireMonitor.capture: broadcast of seq=%d failed", envelope["seq"])
        return envelope

    def page(
        self,
        *,
        before: int | None = None,
        after: int | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> dict[str, Any]:
        """`GET /api/monitor/frames` payload: `{boot, frames, has_more}`,
        `frames` ordered oldest -> newest (contract "REST"). `before` (older
        than) and `after` (newer than) are mutually exclusive — the REST
        layer (`sse_routes/monitor.py`) rejects both given before this is
        ever called. Neither given returns the newest `limit` frames.

        Merges the RF ring and the console ring (node debug console bridge,
        `node_console.py`) back into ONE stream ordered by the shared `seq`
        counter — the two rings exist only so console volume cannot evict RF
        frames (see `__init__`), never as a visible split to callers of
        `page()`.
        """
        # Both deques append in oldest -> newest order; merge on `seq` since
        # each ring evicts independently, so neither is a suffix of the
        # other's timeline.
        frames = sorted((*self._ring, *self._console_ring), key=lambda f: cast("int", f["seq"]))
        if before is not None:
            older = [f for f in frames if f["seq"] < before]
            page_frames = older[-limit:] if limit > 0 else []
            has_more = len(older) > len(page_frames)
        elif after is not None:
            newer = [f for f in frames if f["seq"] > after]
            page_frames = newer[:limit]
            has_more = len(newer) > len(page_frames)
        else:
            page_frames = frames[-limit:] if limit > 0 else []
            has_more = len(frames) > len(page_frames)
        return {"boot": self.boot, "frames": page_frames, "has_more": has_more}

    # ── RX subscribers — wired onto MessageRouter topics in build_app ──────

    async def on_mesh_message(self, routed_message: dict[str, Any]) -> None:
        """Subscriber for the "mesh_message" topic.

        Today `udp_handler.py` is the only publisher of this topic, always
        with `source="udp"` — verified by inspecting every
        `message_router.publish(..., "mesh_message", ...)` call site.
        An unrecognized source is logged and skipped rather than guessed at,
        so a future new source is visible (as a gap) instead of silently
        mislabeled.
        """
        source = routed_message.get("source")
        if source != "udp":
            logger.debug("WireMonitor: unmapped mesh_message source %r, not captured", source)
            return
        await self._capture_rx("udp", routed_message["data"])

    async def on_ble_notification(self, routed_message: dict[str, Any]) -> None:
        """Subscriber for the "ble_notification" topic.

        `source == "self"` is the synthetic self-addressed-message shape
        `MessageRouter._route_to_command_handler` manufactures when an
        outbound message is addressed to our own callsign — that is an
        OUTBOUND event, already captured exactly once by the TX single
        point in `_handle_outbound` (verdict `suppressed`/`self_message`),
        so it is skipped here to avoid a duplicate envelope for the same
        attempt.

        Every other source is `"ble"` (`ble_client_remote.py`, the only
        real BLE client MCProxy runs today — "local" BLE mode was retired
        in favour of ble_service). Register/config notifications
        (`ble_protocol.transform_ble`'s output — TYP I/G/SN/SA/... — and raw
        MHeard-register passthrough) are filtered by `_is_mesh_frame`, not
        by source.
        """
        source = routed_message.get("source")
        if source == "self":
            return
        if source != "ble":
            logger.debug("WireMonitor: unmapped ble_notification source %r, not captured", source)
            return
        frame = routed_message["data"]
        if not _is_mesh_frame(frame):
            return
        await self._capture_rx("ble", frame)

    async def on_ble_status(self, routed_message: dict[str, Any]) -> None:
        """Subscriber for the "ble_status" topic: BLE connection-state
        changes (and the "CONFFIN" config-burst-finished notice, which rides
        the same topic), captured as a SYS frame (contract "SYS frames"
        note). Always `verdict: shown` — these are connectivity notices,
        never subject to the blocklist.
        """
        data = routed_message.get("data")
        if not isinstance(data, dict):
            return
        # `type` goes LAST: a status payload carrying its own `type` key must
        # not turn this SYS frame into something the webapp renders as mesh
        # traffic.
        frame: dict[str, Any] = {**data, "msg": data.get("msg"), "type": "sys"}
        await self.capture("ble", "rx", "shown", None, frame)

    async def _capture_rx(self, link: str, frame: dict[str, Any]) -> None:
        """Shared RX path for on_mesh_message/on_ble_notification: run the
        one shared verdict function (`sse_handler.broadcast_verdict`) and
        capture ITS verdict against the ORIGINAL frame — never the
        (possibly dst-rewritten) broadcast copy, so a blocklist redirect
        never leaks the rewritten dst into the envelope (contract: "before
        any dst rewrite. Copied.").
        """
        verdict, reason, _data_to_broadcast = broadcast_verdict(self.router, frame)
        direction = "tx" if frame.get("src_type") == "node" else "rx"
        await self.capture(link, direction, verdict, reason, frame)


def _is_mesh_frame(frame: dict[str, Any]) -> bool:
    """True iff `frame` is a decoded mesh frame (msg/pos/tele/ack) rather
    than a BLE register/config notification. See `_MESH_FRAME_TYPES`.
    """
    return frame.get("type") in _MESH_FRAME_TYPES
