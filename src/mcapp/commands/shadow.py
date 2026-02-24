"""Shadow mode: parallel comparison of old vs new logic for safe refactoring."""

import re

from ..logging_setup import get_logger

logger = get_logger(__name__)

_stats: dict[str, int] = {
    "normalize_total": 0,
    "normalize_mismatches": 0,
    "routing_total": 0,
    "routing_mismatches": 0,
}


def get_shadow_stats() -> dict[str, int]:
    """Return a copy of shadow comparison statistics."""
    return dict(_stats)


def normalize_unified(message_data: dict, context: str = "command") -> dict:
    """Unified normalization — shadow candidate to replace both normalizers.

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


def compare_normalize(
    old_result: dict, new_result: dict, context: str, input_data: dict
) -> None:
    """Compare old normalizer output with unified normalizer, log mismatches."""
    _stats["normalize_total"] += 1
    diffs: dict[str, dict[str, str | None]] = {}
    for key in ("src", "dst", "msg"):
        if old_result.get(key) != new_result.get(key):
            diffs[key] = {"old": old_result.get(key), "new": new_result.get(key)}
    if diffs:
        _stats["normalize_mismatches"] += 1
        logger.warning(
            "SHADOW normalize(%s) MISMATCH #%d: %s | input: %s",
            context,
            _stats["normalize_mismatches"],
            diffs,
            {k: str(v)[:80] for k, v in input_data.items() if k in ("src", "dst", "msg")},
        )


def compare_routing(
    old_result: tuple, new_result: tuple, src: str, dst: str, msg: str
) -> None:
    """Compare old routing decision with v2, log mismatches."""
    _stats["routing_total"] += 1
    if old_result != new_result:
        _stats["routing_mismatches"] += 1
        logger.warning(
            "SHADOW routing MISMATCH #%d: old=%s new=%s | src=%s dst=%s msg=%.40s",
            _stats["routing_mismatches"],
            old_result,
            new_result,
            src,
            dst,
            msg,
        )
