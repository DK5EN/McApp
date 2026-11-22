"""ORIGIN axis (human | machine) of the two-axis classifier.

Port of ``proxy/src/classifier/origin.ts`` (mcmap).  The contract (``origin`` section of
``classifier_contract.json``, schema 2) carries the data, this module the control flow.
Pure: no storage imports, ships to MCProxy via the classifier subtree.

Rules, first match wins: machine-rule, data-format-timed, strong-data-layout,
layout-on-clock, default-human.  Timing None / one-shot / jittered never fires
data-format-timed or layout-on-clock (strong-data-layout needs no timing).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .axes import AxesEngine
    from .engine import Engine

ORIGIN_HUMAN = "human"
ORIGIN_MACHINE = "machine"

# Rule names in evaluation order; the contract must list exactly these (ORIGIN_RULES in origin.ts).
_RULE_NAMES = (
    "machine-rule",
    "data-format-timed",
    "strong-data-layout",
    "layout-on-clock",
    "default-human",
)


@dataclass(frozen=True)
class OriginSpec:
    values: tuple[str, ...]
    rules: tuple[str, ...]
    data_formats: frozenset[str]
    data_timings: frozenset[str]
    strong_data_signals: frozenset[str]
    strong_data_min: int
    clock_timing: str
    value_classes: frozenset[str]
    prose_signal: str

    @classmethod
    def from_contract(cls, data: Mapping[str, Any]) -> OriginSpec:
        rules = tuple(str(r) for r in data["rules"])
        if rules != _RULE_NAMES:
            raise ValueError(f"origin contract: rules {rules!r} differ from {_RULE_NAMES!r}")
        values = tuple(str(v) for v in data["values"])
        if values != (ORIGIN_HUMAN, ORIGIN_MACHINE):
            raise ValueError(f"origin contract: unexpected values {values!r}")
        return cls(
            values=values,
            rules=rules,
            data_formats=frozenset(str(x) for x in data["data_formats"]),
            data_timings=frozenset(str(x) for x in data["data_timings"]),
            strong_data_signals=frozenset(str(x) for x in data["strong_data_signals"]),
            strong_data_min=int(data["strong_data_min"]),
            clock_timing=str(data["clock_timing"]),
            value_classes=frozenset(str(x) for x in data["value_classes"]),
            prose_signal=str(data["prose_signal"]),
        )


@dataclass(frozen=True)
class OriginResult:
    origin: str
    rule: str


def carries_values(spec: OriginSpec, tokens: Iterable[tuple[str, str]]) -> bool:
    """True when any token's class is one of spec.value_classes.

    mcmap checks the structure skeleton, which only collapses runs of a class
    (``NUM*3``), so class presence in the token stream is equivalent.
    """
    return any(cls in spec.value_classes for cls, _text in tokens)


def origin_of(  # noqa: PLR0913 - keyword-only, API pinned by the integration contract
    spec: OriginSpec,
    *,
    machine_rules: Sequence[str],
    dominant: str | None,
    signals: Sequence[str],
    timing: str | None,
    carries_values: bool,
) -> OriginResult:
    r = spec.rules
    if machine_rules:
        return OriginResult(ORIGIN_MACHINE, r[0])
    if dominant is not None and dominant in spec.data_formats and timing in spec.data_timings:
        return OriginResult(ORIGIN_MACHINE, r[1])
    if (
        "mostly-data" in signals
        and sum(1 for s in signals if s in spec.strong_data_signals) >= spec.strong_data_min
    ):
        return OriginResult(ORIGIN_MACHINE, r[2])
    if timing == spec.clock_timing and spec.prose_signal not in signals and carries_values:
        return OriginResult(ORIGIN_MACHINE, r[3])
    return OriginResult(ORIGIN_HUMAN, r[4])


def message_origin(  # noqa: PLR0913, PLR0917 - positional API pinned by the brief
    engine: Engine,
    axes: AxesEngine,
    text: str,
    src: str,
    dst: str,
    timing: str | None,
) -> OriginResult:
    """Origin of one message; ``timing`` outside the axes' timing values counts as None."""
    t = timing if timing is not None and timing in axes.timing_values else None
    spec = engine.origin_spec
    fmt = axes.message_format(text)
    return origin_of(
        spec,
        machine_rules=engine.classify(text, src, dst).machine_rules,
        dominant=fmt.dominant,
        signals=fmt.signals,
        timing=t,
        carries_values=carries_values(spec, axes.tokenize(text)),
    )
