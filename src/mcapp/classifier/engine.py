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

SUPPORTED_SCHEMAS = frozenset({1, 2})
_SCHEMA_WITH_ORIGIN = 2
_NOT_TOPICS = frozenset({"qso", "auto_beacon"})
_SCOPES = frozenset({"msg", "dst", "src", "combined"})
_FLAGS = {"": 0, "i": regex.IGNORECASE, "s": regex.DOTALL, "is": regex.IGNORECASE | regex.DOTALL}
_DIRECTED = "directed"
# ECMAScript WhiteSpace + LineTerminator, i.e. what JS String.prototype.trim() strips.
# Python's str.strip() differs (keeps U+FEFF, strips U+001C-U+001F and U+0085).
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


@dataclass(frozen=True)
class L1Result:
    tags: frozenset[str]
    rules: tuple[str, ...]
    directed: bool
    # The tags the matched rules carried before the v7 taxonomy (bot, node, software, time).
    # The category layer reads these, so categories and the timestamp-beacon drop keep their
    # meaning while the stored tags use the new vocabulary (auto_beacon).
    legacy_tags: frozenset[str] = frozenset()
    # v8 two-axis view, from the same regex pass: sorted topic ids (tags minus qso and
    # auto_beacon) and the matched rules flagged machine (origin rule 1), in rule order.
    topics: tuple[str, ...] = ()
    machine_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Rule:
    name: str
    scope: str
    tag: str | None
    pattern: regex.Pattern[str]
    legacy_tag: str | None = None
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
        self._tag_ids = frozenset(str(t["id"]) for t in data["tags"])
        self._automated = frozenset(str(t) for t in data["automated_tags"])
        qso = data["qso"]
        self._qso_tag = str(qso["tag"])
        self._weather_tag = str(qso["weather_tag"])
        self._exempt = frozenset(str(e) for e in qso["exempt_evidence"])
        self._addressee = _compile(
            str(data["addressee_pattern"]),
            str(data.get("addressee_flags", "")),
            "addressee_pattern",
        )
        rules: list[_Rule] = []
        for raw in data["rules"]:
            name = str(raw["name"])
            scope = str(raw["scope"])
            if scope not in _SCOPES:
                raise ValueError(f"rule {name!r}: unknown scope {scope!r}")
            tag = raw["tag"]
            # contracts before v7 have no legacy_tag: the tag is its own legacy tag
            legacy = raw.get("legacy_tag", tag)
            rules.append(
                _Rule(
                    name,
                    scope,
                    None if tag is None else str(tag),
                    _compile(str(raw["pattern"]), str(raw["flags"]), f"rule {name!r}"),
                    None if legacy is None else str(legacy),
                    # schema 2 must carry the flag: a missing one would silently disable
                    # origin rule 1; only schema 1 (no origin axis) defaults to False
                    bool(raw["machine"]) if schema == _SCHEMA_WITH_ORIGIN else False,
                )
            )
        self._rules = tuple(rules)  # contract order is evaluation order
        # schema 2 only: topics + origin; a schema 1 contract has neither
        self._topic_ids = (
            tuple(str(t["id"]) for t in data["topics"]) if schema == _SCHEMA_WITH_ORIGIN else ()
        )
        self._origin_spec = (
            OriginSpec.from_contract(data["origin"]) if schema == _SCHEMA_WITH_ORIGIN else None
        )

    @property
    def taxonomy_version(self) -> int:
        return self._taxonomy_version

    @property
    def tag_ids(self) -> frozenset[str]:
        return self._tag_ids

    @property
    def topic_ids(self) -> tuple[str, ...]:
        return self._topic_ids

    @property
    def origin_spec(self) -> OriginSpec:
        if self._origin_spec is None:
            raise ValueError("contract has no origin section (schema 1)")
        return self._origin_spec

    @property
    def automated_tags(self) -> frozenset[str]:
        return self._automated

    @property
    def source_commit(self) -> str:
        return self._source_commit

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def classify(self, text: str, src: str, dst: str) -> L1Result:
        directed = False
        names: list[str] = []
        tags: set[str] = set()
        legacy: set[str] = set()
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
                tags.add(rule.tag)
            if rule.legacy_tag is not None:
                legacy.add(rule.legacy_tag)
        evidence = (
            ("directed" in self._exempt and directed)
            or ("question" in self._exempt and "question" in tags)
            or ("addressee" in self._exempt and self._addressee.search(text) is not None)
        )
        automated = any(
            t in self._automated and not (t == self._weather_tag and evidence) for t in tags
        )
        # topics are taken before qso is derived: qso/auto_beacon are never topics
        topics = tuple(sorted(tags - _NOT_TOPICS))
        if text.strip(_JS_WHITESPACE) != "" and not automated:
            tags.add(self._qso_tag)
        return L1Result(
            frozenset(tags), tuple(names), directed, frozenset(legacy), topics, tuple(machine)
        )


def load_contract(data: Mapping[str, Any]) -> Engine:
    return Engine(data)


def load_contract_file(path: Path) -> Engine:
    return Engine(json.loads(path.read_text(encoding="utf-8")))


def conformance_failures(engine: Engine, data: Mapping[str, Any]) -> list[str]:
    """Run the contract's vectors; return human-readable mismatches (empty = conformant)."""
    failures: list[str] = []
    strict = data.get("schema") == _SCHEMA_WITH_ORIGIN
    for i, v in enumerate(data.get("vectors", [])):
        if strict and ("topics" not in v or "machine_rules" not in v):
            failures.append(f"vector {i} {v['text']!r}: schema 2 vector lacks topics/machine_rules")
            continue
        got = engine.classify(v["text"], v["from"], v["to"])
        want_tags = sorted(v["tags"])
        want_rules = list(v["rules"])
        if (
            sorted(got.tags) != want_tags
            or list(got.rules) != want_rules
            or got.directed != bool(v["directed"])
            or ("topics" in v and list(got.topics) != list(v["topics"]))
            or ("machine_rules" in v and list(got.machine_rules) != list(v["machine_rules"]))
        ):
            failures.append(
                f"vector {i} {v['text']!r}: got tags={sorted(got.tags)} rules={list(got.rules)} "
                f"directed={got.directed}; want tags={want_tags} rules={want_rules} "
                f"directed={v['directed']} topics={v.get('topics')} "
                f"machine_rules={v.get('machine_rules')}; got topics={list(got.topics)} "
                f"machine_rules={list(got.machine_rules)}"
            )
    return failures
