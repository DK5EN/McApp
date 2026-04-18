"""Seed builtin classifier rules into the classifier_rules table."""

import json
from datetime import datetime, timezone

from ..logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_RULES: list[dict] = [
    {
        "name": "CET timestamp",
        "pattern": r"^\{CET\}\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
        "scope": "msg",
        "category": "timestamp_beacon",
        "extra_tags": ["beacon"],
        "priority": 10,
    },
    {
        "name": "WX emoji block",
        "pattern": r"🌡.*(📊|💧)",
        "scope": "msg",
        "category": "wx_beacon",
        "extra_tags": ["beacon", "emoji_heavy"],
        "priority": 20,
    },
    {
        "name": "WX text",
        "pattern": r"(?i)(WX\s|Temp[:= ]).*(QNH|hPa)",
        "scope": "msg",
        "category": "wx_beacon",
        "extra_tags": ["beacon"],
        "priority": 21,
    },
    {
        "name": "WX short emoji",
        "pattern": r"🌡️\d+.*\[",
        "scope": "msg",
        "category": "wx_beacon",
        "extra_tags": ["beacon", "emoji_heavy"],
        "priority": 22,
    },
    {
        "name": "MeshCom WebDesk advert",
        "pattern": r"MeshComWebDesk V\d",
        "scope": "msg",
        "category": "sw_advert",
        "extra_tags": ["beacon"],
        "priority": 30,
    },
    {
        "name": "MeshCom WebDesk banner",
        "pattern": r"\*\*\*MeshCom WebDesk",
        "scope": "msg",
        "category": "sw_advert",
        "extra_tags": ["beacon"],
        "priority": 31,
    },
    {
        "name": "URL advert",
        "pattern": r"https?://\S+",
        "scope": "msg",
        "category": "node_advert",
        "extra_tags": ["has_url"],
        "priority": 40,
    },
    {
        "name": "Greeting DE",
        "pattern": r"(?i)^(73|hallo|servus|moin|nabend|ahoi|guten (morgen|abend|tag))",
        "scope": "msg",
        "category": "greeting",
        "extra_tags": [],
        "priority": 50,
    },
    {
        "name": "Earthquake DE/EN",
        "pattern": r"(?i)(erdbeben|earthquake|magnitude\s+\d)",
        "scope": "msg",
        "category": "alert",
        "extra_tags": [],
        "priority": 60,
    },
    {
        "name": "Direct callsign",
        "pattern": r"^[A-Z0-9]+-\d+$",
        "scope": "dst",
        "category": "directed",
        "extra_tags": [],
        "priority": 90,
    },
]


async def seed_builtin_rules(storage) -> int:
    """Insert any missing builtin rules. Returns number of rows inserted.

    Matches by name+builtin=1 so that user-edited builtins are preserved.
    """
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for rule in DEFAULT_RULES:
        existing = await storage._execute(
            "SELECT 1 FROM classifier_rules WHERE name = ? AND builtin = 1 LIMIT 1",
            (rule["name"],),
        )
        if existing:
            continue
        await storage._execute(
            "INSERT INTO classifier_rules "
            "(name, pattern, scope, category, extra_tags, priority, enabled, "
            " builtin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?)",
            (
                rule["name"],
                rule["pattern"],
                rule["scope"],
                rule["category"],
                json.dumps(rule["extra_tags"]),
                rule["priority"],
                now,
                now,
            ),
            fetch=False,
        )
        inserted += 1
    return inserted
