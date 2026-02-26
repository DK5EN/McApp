"""Unified outbound message classification (v2).

Pure-function classifier that determines what should happen to an outbound
message (suppress / self_message / send) without executing any side effects.
Used for shadow comparison against the v1 protocol-specific handlers in main.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .main import MessageValidator


@dataclass(frozen=True)
class OutboundDecision:
    """Result of classifying an outbound message."""

    action: Literal["suppress", "self_message", "send"]
    protocol: str
    reason: str = ""
    normalized_data: dict | None = None


def classify_outbound_v2(
    message_data: dict,
    protocol_type: str,
    my_callsign: str,
    validator: MessageValidator,
) -> OutboundDecision:
    """Classify outbound message intent without side effects.

    Replicates the decision logic shared by _udp_message_handler and
    _ble_message_handler in main.py.  Does NOT send messages, publish
    events, or write to the database.
    """
    normalized = validator.normalize_message_data(message_data)

    if not normalized.get("src") and my_callsign:
        normalized["src"] = my_callsign

    # Suppress check (mirrors _should_suppress_outbound)
    if validator.should_suppress_outbound(normalized):
        reason = validator.get_suppression_reason(normalized)
        return OutboundDecision(
            action="suppress",
            protocol=protocol_type,
            reason=reason,
            normalized_data=normalized,
        )

    # Self-message check (mirrors _is_message_to_self)
    dst = normalized.get("dst", "")
    if my_callsign and dst == my_callsign:
        return OutboundDecision(
            action="self_message",
            protocol=protocol_type,
            normalized_data=normalized,
        )

    # External send
    return OutboundDecision(
        action="send",
        protocol=protocol_type,
        normalized_data=normalized,
    )
