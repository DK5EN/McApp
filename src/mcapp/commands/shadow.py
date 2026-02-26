"""Unified message normalization for commands and messages."""

import re


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
    result.update({"src": src, "dst": dst, "msg": msg, "original": message_data})
    return result
