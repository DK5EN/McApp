"""Unified message normalization and shadow mode comparison."""

from __future__ import annotations

import re

from ..logging_setup import get_logger

logger = get_logger(__name__)


def compare_parse_command(
    v1_result: tuple[str, dict] | None,
    v2_result: tuple[str, dict] | None,
    msg_text: str,
) -> None:
    """Compare v1 and v2 parse_command output, log mismatches."""
    if v1_result == v2_result:
        return

    if v1_result is None or v2_result is None:
        logger.warning(
            "SHADOW parse_command MISMATCH (None): msg=%r v1=%r v2=%r",
            msg_text, v1_result, v2_result,
        )
        return

    v1_cmd, v1_kwargs = v1_result
    v2_cmd, v2_kwargs = v2_result

    if v1_cmd != v2_cmd:
        logger.warning(
            "SHADOW parse_command CMD MISMATCH: msg=%r v1_cmd=%r v2_cmd=%r",
            msg_text, v1_cmd, v2_cmd,
        )
        return

    if v1_kwargs != v2_kwargs:
        logger.warning(
            "SHADOW parse_command KWARGS MISMATCH: msg=%r cmd=%r v1=%r v2=%r",
            msg_text, v1_cmd, v1_kwargs, v2_kwargs,
        )


def compare_outbound_decision(
    v1_action: str,
    v1_reason: str,
    v2_action: str,
    v2_reason: str,
    protocol: str,
    message_data: dict,
) -> None:
    """Compare v1 outbound handler path with v2 classification."""
    if v1_action == v2_action:
        if v1_action == "suppress" and v1_reason != v2_reason:
            logger.warning(
                "SHADOW outbound REASON MISMATCH: proto=%s src=%s dst=%s "
                "v1_reason=%r v2_reason=%r",
                protocol,
                message_data.get("src", ""),
                message_data.get("dst", ""),
                v1_reason,
                v2_reason,
            )
        return

    logger.warning(
        "SHADOW outbound ACTION MISMATCH: proto=%s src=%s dst=%s msg=%.40s "
        "v1=%s v2=%s",
        protocol,
        message_data.get("src", ""),
        message_data.get("dst", ""),
        message_data.get("msg", ""),
        v1_action,
        v2_action,
    )


def normalize_unified(message_data: dict, context: str = "command") -> dict:
    """Unified normalization — standardizes src/dst/msg fields.

    Args:
        message_data: Raw message dict with src, dst, msg keys.
        context: "command" (default src=UNKNOWN) or "message" (default src="").
    """
    src_default = "UNKNOWN" if context == "command" else ""
    src_raw = message_data.get("src", src_default)
    src = (
        src_raw.split(",")[0].strip().upper()
        if "," in src_raw
        else src_raw.strip().upper()
    )
    dst = message_data.get("dst", "").strip().upper()
    msg = message_data.get("msg", "").strip()
    # Strip MeshCom message ID suffix ({NNN) before any routing decisions
    msg = re.sub(r"\{\d+$", "", msg).strip()

    result = message_data.copy()
    result.update({"src": src, "dst": dst, "msg": msg})
    return result
