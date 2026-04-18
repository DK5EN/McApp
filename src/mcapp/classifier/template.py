# Layer 2: template fingerprint, per-template stats, auto-beacon threshold.
# Template hash is computed pre-INSERT; auto_beacon_status adds 1 to the historical
# count so the effective count reflects the message that is about to be stored.
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..logging_setup import get_logger

logger = get_logger(__name__)

# Tunable thresholds — module constants so we can lift them to config later.
AUTO_BEACON_THRESHOLD = 5
AUTO_BEACON_WINDOW_SEC = 24 * 60 * 60
SRCS_CAP = 20

_URL_RE = re.compile(r"https?://\S+")
# Emoji ranges: supplementary multilingual plane + symbols + ZWJ joiner cleanup.
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]"
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_WS_RE = re.compile(r"\s+")


def fingerprint(text: str | None) -> str:
    """Normalize a message into a short stable hash.

    Steps (order matters):
      1. strip
      2. URLs → 'URL'
      3. emoji/symbol chars → 'E'
      4. numbers (int/decimal with , or .) → '#'
      5. collapse whitespace
      6. lowercase
      7. sha1 → first 12 hex chars
    """
    t = (text or "").strip()
    t = _URL_RE.sub("URL", t)
    t = _EMOJI_RE.sub("E", t)
    t = _NUMBER_RE.sub("#", t)
    t = _WS_RE.sub(" ", t)
    t = t.lower()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:12]


async def update_stats(
    storage,
    template_hash: str,
    msg: dict[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    """UPSERT into beacon_templates. Returns the resulting row as a dict.

    - count += 1
    - last_seen = now
    - first_seen set on insert only
    - example_msg / example_src refreshed to the most recent
    - srcs list deduplicated, capped at SRCS_CAP (oldest dropped)
    """
    now_iso = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat()
    src = str(msg.get("src") or "")
    text = str(msg.get("msg") or "")

    # Atomic upsert — two concurrent classifies on the same fresh
    # template_hash must not race between SELECT and INSERT.
    await storage._execute(
        "INSERT INTO beacon_templates "
        "(template_hash, example_msg, example_src, srcs, count, "
        " first_seen, last_seen, auto_beacon, user_action) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, 0, NULL) "
        "ON CONFLICT(template_hash) DO UPDATE SET "
        "  example_msg = excluded.example_msg, "
        "  example_src = excluded.example_src, "
        "  count = beacon_templates.count + 1, "
        "  last_seen = excluded.last_seen",
        (template_hash, text, src, json.dumps([src]), now_iso, now_iso),
        fetch=False,
    )
    # Read back and update srcs list separately — srcs needs in-Python
    # deduplication + cap, which is awkward to express in pure SQL.
    rows = await storage._execute(
        "SELECT template_hash, example_msg, example_src, srcs, count, "
        "first_seen, last_seen, auto_beacon, user_action "
        "FROM beacon_templates WHERE template_hash = ?",
        (template_hash,),
    )
    row = rows[0]
    try:
        srcs_list = json.loads(row["srcs"]) if row["srcs"] else []
        if not isinstance(srcs_list, list):
            srcs_list = []
    except json.JSONDecodeError:
        srcs_list = []
    # Dedupe-preserving order: remove then append so newest src is last.
    if src in srcs_list:
        srcs_list.remove(src)
    srcs_list.append(src)
    if len(srcs_list) > SRCS_CAP:
        srcs_list = srcs_list[-SRCS_CAP:]
    await storage._execute(
        "UPDATE beacon_templates SET srcs = ? WHERE template_hash = ?",
        (json.dumps(srcs_list), template_hash),
        fetch=False,
    )
    return {
        "template_hash": template_hash,
        "example_msg": row["example_msg"],
        "example_src": row["example_src"],
        "srcs": srcs_list,
        "count": int(row["count"]),
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "auto_beacon": bool(row["auto_beacon"]),
        "user_action": row["user_action"],
    }


async def auto_beacon_status(
    storage,
    template_hash: str,
    src: str,
    now_ms: int,
    user_action: str | None,
) -> tuple[bool, bool]:
    """Return (is_auto_beacon, just_crossed_threshold).

    Semantics:
      - user_action == 'promote' → always True, never 'just_crossed'
        (caller won't re-emit).
      - user_action == 'demote' → always False.
      - otherwise: count messages in the last AUTO_BEACON_WINDOW_SEC
        with same (src, template_hash). If >= AUTO_BEACON_THRESHOLD,
        auto_beacon True. We also flip beacon_templates.auto_beacon
        from 0 -> 1 on the crossing and signal just_crossed so the
        caller can emit the SSE event.
    """
    if user_action == "promote":
        return True, False
    if user_action == "demote":
        return False, False

    window_start_ms = now_ms - AUTO_BEACON_WINDOW_SEC * 1000
    rows = await storage._execute(
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE template_hash = ? AND src = ? AND timestamp >= ?",
        (template_hash, src, window_start_ms),
    )
    count = int(rows[0]["n"]) if rows else 0
    # The current message has not been inserted yet (classification runs
    # pre-INSERT), so the count reflects prior history. Include the current
    # message by adding 1.
    effective = count + 1
    if effective < AUTO_BEACON_THRESHOLD:
        return False, False

    # Already auto_beacon? If yes, no transition.
    tpl_rows = await storage._execute(
        "SELECT auto_beacon FROM beacon_templates WHERE template_hash = ?",
        (template_hash,),
    )
    was_auto = bool(tpl_rows[0]["auto_beacon"]) if tpl_rows else False
    if was_auto:
        return True, False
    await storage._execute(
        "UPDATE beacon_templates SET auto_beacon = 1 WHERE template_hash = ?",
        (template_hash,),
        fetch=False,
    )
    return True, True
