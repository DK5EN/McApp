"""Layer-1 rule engine executing the mcmap-exported classifier contract.

Mirrors ``proxy/src/classifier/staticClassifier.ts`` (classifyStatic): the
contract carries the data (rules, patterns, tags), this module carries the
control flow.  Shared subtree: import only stdlib and ``regex`` here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import regex

from .origin import OriginSpec

SUPPORTED_SCHEMAS = frozenset({3})
_SCOPES = frozenset({"msg", "dst", "src", "combined"})
_FLAGS = {"": 0, "i": regex.IGNORECASE, "s": regex.DOTALL, "is": regex.IGNORECASE | regex.DOTALL}
_DIRECTED = "directed"


@dataclass(frozen=True)
class L1Result:
    # Sorted topic ids of the matched rules (the `directed` marker rule is not a topic).
    topics: tuple[str, ...]
    # Names of the matched rules, in rule order.
    rules: tuple[str, ...]
    directed: bool
    # The matched rules flagged machine (origin rule 1), in rule order.
    machine_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Rule:
    name: str
    scope: str
    tag: str | None
    pattern: regex.Pattern[str]
    machine: bool = False


def _compile(pattern: str, flags: str, label: str) -> regex.Pattern[str]:
    if flags not in _FLAGS:
        raise ValueError(f"{label}: unknown flags {flags!r}")
    try:
        return regex.compile(pattern, regex.V0 | _FLAGS[flags])
    except regex.error as exc:
        raise ValueError(f"{label}: pattern does not compile: {exc}") from exc


class Engine:
    def __init__(self, data: Mapping[str, Any]) -> None:
        schema = data.get("schema")
        if schema not in SUPPORTED_SCHEMAS:
            raise ValueError(f"unsupported classifier contract schema {schema!r}")
        self._taxonomy_version = int(data["taxonomy_version"])
        self._source_commit = str(data.get("source", {}).get("commit", ""))
        rules: list[_Rule] = []
        for raw in data["rules"]:
            name = str(raw["name"])
            scope = str(raw["scope"])
            if scope not in _SCOPES:
                raise ValueError(f"rule {name!r}: unknown scope {scope!r}")
            tag = raw["tag"]  # null for a machine-source rule: it carries no topic
            rules.append(
                _Rule(
                    name,
                    scope,
                    None if tag is None else str(tag),
                    _compile(str(raw["pattern"]), str(raw["flags"]), f"rule {name!r}"),
                    # a missing flag would silently disable origin rule 1
                    bool(raw["machine"]),
                )
            )
        self._rules = tuple(rules)  # contract order is evaluation order
        self._topic_ids = tuple(str(t["id"]) for t in data["topics"])
        self._origin_spec = OriginSpec.from_contract(data["origin"])

    @property
    def taxonomy_version(self) -> int:
        return self._taxonomy_version

    @property
    def topic_ids(self) -> tuple[str, ...]:
        return self._topic_ids

    @property
    def origin_spec(self) -> OriginSpec:
        return self._origin_spec

    @property
    def source_commit(self) -> str:
        return self._source_commit

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def classify(self, text: str, src: str, dst: str) -> L1Result:
        directed = False
        names: list[str] = []
        topics: set[str] = set()
        machine: list[str] = []
        for rule in self._rules:
            if rule.scope == "msg":
                target = text
            elif rule.scope == "src":
                target = src
            elif rule.scope == "dst":
                target = dst
            else:
                target = f"{src}|{dst}|{text}"
            if not target:
                continue
            if rule.pattern.search(target) is None:
                continue
            if rule.tag == _DIRECTED:
                directed = True
                continue
            names.append(rule.name)
            if rule.machine:
                machine.append(rule.name)
            if rule.tag is not None:
                topics.add(rule.tag)
        return L1Result(tuple(sorted(topics)), tuple(names), directed, tuple(machine))


def load_contract(data: Mapping[str, Any]) -> Engine:
    return Engine(data)


def load_contract_file(path: Path) -> Engine:
    return Engine(json.loads(path.read_text(encoding="utf-8")))


def conformance_failures(engine: Engine, data: Mapping[str, Any]) -> list[str]:
    """Run the contract's vectors; return human-readable mismatches (empty = conformant)."""
    failures: list[str] = []
    for i, v in enumerate(data.get("vectors", [])):
        if "topics" not in v or "machine_rules" not in v:
            failures.append(f"vector {i} {v['text']!r}: vector lacks topics/machine_rules")
            continue
        got = engine.classify(v["text"], v["from"], v["to"])
        if (
            list(got.topics) != list(v["topics"])
            or list(got.rules) != list(v["rules"])
            or got.directed != bool(v["directed"])
            or list(got.machine_rules) != list(v["machine_rules"])
        ):
            failures.append(
                f"vector {i} {v['text']!r}: got topics={list(got.topics)} rules={list(got.rules)} "
                f"directed={got.directed} machine_rules={list(got.machine_rules)}; "
                f"want topics={v['topics']} rules={v['rules']} directed={v['directed']} "
                f"machine_rules={v['machine_rules']}"
            )
    return failures
