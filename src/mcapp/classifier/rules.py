"""Load and match classifier rules from the classifier_rules table."""

import json
import re
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CompiledRule:
    id: int
    name: str
    pattern: str
    scope: str
    category: str
    extra_tags: tuple[str, ...]
    priority: int
    enabled: bool
    builtin: bool
    regex: re.Pattern[str]


async def load_rules(storage) -> list[CompiledRule]:
    """Fetch enabled rules from DB, compile, sort by (priority ASC, id ASC).

    Disabled rules are filtered out. Rules whose regex fails to compile are
    logged as warnings and skipped — a bad pattern must never break the
    classifier for other rules.
    """
    rows = await storage._execute(
        "SELECT id, name, pattern, scope, category, extra_tags, priority, "
        "enabled, builtin FROM classifier_rules ORDER BY priority ASC, id ASC"
    )
    compiled: list[CompiledRule] = []
    for row in rows:
        if not row["enabled"]:
            continue
        try:
            rx = re.compile(row["pattern"])
        except re.error as exc:
            logger.warning(
                "classifier_rules[id=%s name=%s]: regex compile failed: %s",
                row["id"],
                row["name"],
                exc,
            )
            continue
        raw_tags = row["extra_tags"]
        try:
            tag_list = json.loads(raw_tags) if raw_tags else []
            if not isinstance(tag_list, list):
                tag_list = []
        except json.JSONDecodeError:
            tag_list = []
        compiled.append(
            CompiledRule(
                id=row["id"],
                name=row["name"],
                pattern=row["pattern"],
                scope=row["scope"] or "msg",
                category=row["category"],
                extra_tags=tuple(str(t) for t in tag_list),
                priority=row["priority"],
                enabled=bool(row["enabled"]),
                builtin=bool(row["builtin"]),
                regex=rx,
            )
        )
    return compiled


def _target(msg: dict[str, Any], scope: str) -> str:
    if scope == "src":
        return str(msg.get("src") or "")
    if scope == "dst":
        return str(msg.get("dst") or "")
    if scope == "combined":
        return f"{msg.get('src') or ''}|{msg.get('dst') or ''}|{msg.get('msg') or ''}"
    return str(msg.get("msg") or "")


def match(msg: dict[str, Any], rules: list[CompiledRule]) -> tuple[str, set[str]]:
    """Return (category, extra_tag_set).

    First matching rule sets the category; all matching rules contribute
    extra_tags. If nothing matches, category defaults to 'other' and tags
    is empty. rules MUST already be sorted (priority ASC, id ASC).
    """
    category: str | None = None
    tags: set[str] = set()
    for rule in rules:
        target = _target(msg, rule.scope)
        if not target:
            continue
        if rule.regex.search(target):
            if category is None:
                category = rule.category
            tags.update(rule.extra_tags)
    return category or "other", tags
