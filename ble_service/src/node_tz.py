"""Node time zone (TZ-01 firmware): pure helpers for `BLEAdapter.set_time()`.

Contract: `doc/2026-10-04_1500-node-tz-ble-contract.md`, design:
`doc/2026-10-04_1600-node-tz-implementation-plan.md` section 3.

Firmware with TZ-01 stores a POSIX TZ rule (`CET-1CEST,M3.5.0,M10.5.0/3`) and
derives its own UTC offset and DST switch. `--utcoff` CLEARS such a rule, so
`set_time()` has to know what the node is before it sends one. Everything in
this module is a pure function of its arguments (plus the environment and one
file read) so it can be tested without a transport. Stdlib only: `ble_service`
ships a standalone lock.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Firmware `TZ_MAX_LEN` (src/tz_rule.h): longer rules are rejected by the node.
TZ_MAX_LEN = 39

HOST_LOCALTIME = Path("/etc/localtime")

# Grammar limits from firmware tzParse(): mm/ss <= 59, names 3+ chars, M rules
# month 1-12, week 1-5 (5 = last), weekday 0-6.
_MAX_MIN_SEC = 59
_MIN_NAME_LEN = 3
_MAX_MONTH = 12
_MAX_WEEK = 5
_MAX_WEEKDAY = 6

ENV_NODE_TZ = "MCAPP_NODE_TZ"
ENV_NODE_TZ_POLICY = "MCAPP_NODE_TZ_POLICY"

TzPolicy = Literal["host_if_unset", "never"]
DEFAULT_POLICY: TzPolicy = "host_if_unset"
_POLICIES: tuple[TzPolicy, ...] = ("host_if_unset", "never")


# ---------------------------------------------------------------- rule grammar
#
# A port of the firmware's `tzParse()` (src/tz_rule.cpp), not a looser regex:
# the point of validating host-side is to predict exactly what the node will
# accept, since a rejected `--settz` is invisible over BLE (contract section 3).


def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def _is_alpha(c: str) -> bool:
    # ASCII only, like the firmware; str.isalpha() would admit umlauts.
    return "a" <= c <= "z" or "A" <= c <= "Z"


class _Cursor:
    """Read position over the rule; reads past the end yield '' (the C NUL)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def peek(self) -> str:
        return self.text[self.i] if self.i < len(self.text) else ""

    def take(self) -> str:
        c = self.peek()
        self.i += 1
        return c

    def num(self, max_digits: int) -> int | None:
        """1..max_digits decimal digits, None when no digit is next."""
        if not _is_digit(self.peek()):
            return None
        v = 0
        n = 0
        while n < max_digits and _is_digit(self.peek()):
            v = v * 10 + int(self.take())
            n += 1
        return v

    def clock(self, max_h: int) -> bool:
        """[+-]hh[:mm[:ss]] with hh <= max_h."""
        if self.peek() in ("+", "-"):
            self.take()
        h = self.num(3)
        if h is None or h > max_h:
            return False
        if self.peek() == ":":
            self.take()
            m = self.num(2)
            if m is None or m > _MAX_MIN_SEC:
                return False
            if self.peek() == ":":
                self.take()
                s = self.num(2)
                if s is None or s > _MAX_MIN_SEC:
                    return False
        return True

    def name(self) -> bool:
        """3+ letters, or <...> with letters, digits, + and -."""
        n = 0
        if self.peek() == "<":
            self.take()
            while self.peek() and self.peek() != ">":
                c = self.peek()
                if not (_is_alpha(c) or _is_digit(c) or c in ("+", "-")):
                    return False
                n += 1
                self.take()
            if self.peek() != ">":
                return False
            self.take()
        else:
            while _is_alpha(self.peek()):
                n += 1
                self.take()
        return n >= _MIN_NAME_LEN

    def rule(self) -> bool:
        """Mm.w.d[/time]."""
        if self.take() != "M":
            return False
        m = self.num(2)
        ok = m is not None and 1 <= m <= _MAX_MONTH and self.take() == "."
        w = self.num(1) if ok else None
        ok = ok and w is not None and 1 <= w <= _MAX_WEEK and self.take() == "."
        d = self.num(1) if ok else None
        ok = ok and d is not None and d <= _MAX_WEEKDAY
        if ok and self.peek() == "/":
            self.take()
            ok = self.clock(167)
        return ok


def _has_day_number_rule(rule: str) -> bool:
    """The firmware's own diagnosis for `Jn` / `n` day rules: a segment (start
    of the string or after a comma) that is [J]digits followed by end, ',' or
    '/'. A name such as JST is not one."""
    for seg in rule.split(","):
        c = seg[1:] if seg[:1] in ("J", "j") else seg
        digits = len(c) - len(c.lstrip("0123456789"))
        if digits and (digits == len(c) or c[digits] in (",", "/")):
            return True
    return False


def validate_node_tz(rule: str) -> str | None:
    """None when the node accepts `rule`, else the reason it would reject it.

    Mirrors firmware `tzParse()` plus the wording of `tzRejectReason()`:
    at most 39 chars, names of 3+ letters or `<...>`, POSIX offset
    `[+-]h[:mm[:ss]]` (h <= 24), and for a DST name both `,Mm.w.d[/time]`
    rules (the firmware does not guess them). No spaces.
    """
    if len(rule) > TZ_MAX_LEN:
        return "too long (max 39 characters)"
    cur = _Cursor(rule)
    ok = bool(rule) and cur.name() and cur.clock(24)
    if ok and cur.peek():
        # DST part: name [offset] ,rule ,rule
        ok = cur.name()
        if ok and cur.peek() != ",":
            ok = cur.clock(24)
        ok = ok and cur.take() == "," and cur.rule()
        ok = ok and cur.take() == "," and cur.rule()
        ok = ok and cur.peek() == ""
    if ok:
        return None
    if _has_day_number_rule(rule):
        return "only M rules supported (Mm.w.d)"
    return "format (std offset dst,Mm.w.d/time,Mm.w.d/time)"


# --------------------------------------------------------------- host rule


def host_posix_tz(path: Path = HOST_LOCALTIME) -> str | None:
    """The host zone's POSIX rule: the footer of its TZif v2+ file.

    `/etc/localtime` is a symlink into zoneinfo; the file ends
    `...data\\n<rule>\\n` (contract section 3, measured on mcapp.local and the
    dev Mac: `CET-1CEST,M3.5.0,M10.5.0/3`). None for a missing or unreadable
    file, a v1 file (no footer), or an empty footer (a zone POSIX cannot
    express). The contract's one-liner strips ALL trailing newlines, which
    turns an empty footer into the last line of binary data; only the final
    newline is dropped here.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:4] != b"TZif" or data[4:5] in (b"", b"\x00") or not data.endswith(b"\n"):
        return None
    try:
        footer = data[:-1].rsplit(b"\n", 1)[-1].decode("ascii")
    except UnicodeDecodeError:
        return None
    return footer or None


def host_node_tz(path: Path = HOST_LOCALTIME) -> str | None:
    """The rule to push onto an unset node, or None when there is none the
    node would accept (the caller then falls back to `--utcoff`).

    `MCAPP_NODE_TZ` wins over the host file (decision D2); an override the node
    would reject is ignored with a warning rather than blocking the host file.
    """
    override = os.environ.get(ENV_NODE_TZ, "").strip()
    if override:
        reason = validate_node_tz(override)
        if reason is None:
            return override
        logger.warning("Ignoring %s=%r: node would reject it (%s)", ENV_NODE_TZ, override, reason)
    rule = host_posix_tz(path)
    if rule is None:
        return None
    reason = validate_node_tz(rule)
    if reason is not None:
        logger.warning("Host TZ rule %r not usable on the node (%s)", rule, reason)
        return None
    return rule


def resolve_policy() -> TzPolicy:
    """`MCAPP_NODE_TZ_POLICY`: `host_if_unset` (default) or `never`.

    Anything else falls back to the default with a warning; an operator typo
    must not silently change what gets written to the node.
    """
    raw = os.environ.get(ENV_NODE_TZ_POLICY, "").strip().lower()
    if not raw:
        return DEFAULT_POLICY
    for policy in _POLICIES:
        if raw == policy:
            return policy
    logger.warning(
        "Invalid %s=%r (expected one of %s); using %s",
        ENV_NODE_TZ_POLICY,
        raw,
        ", ".join(_POLICIES),
        DEFAULT_POLICY,
    )
    return DEFAULT_POLICY


# ------------------------------------------------------------ classification


class TzState(Enum):
    UNSUPPORTED = "unsupported"  # firmware without TZ-01: SN1 has no TZ key
    EMPTY = "empty"  # TZ-01, no rule (SN1.TZ == "")
    RULE = "rule"  # TZ-01 with a rule set; never overwrite, never --utcoff
    UNKNOWN = "unknown"  # not enough registers to tell


@dataclass(frozen=True)
class NodeTz:
    state: TzState
    rule: str | None = None


def classify(registers: Mapping[str, Mapping[str, Any]], *, probed: bool = False) -> NodeTz:
    """Classify the node from the register cache (decision D3).

    `SN1` cached with a `TZ` key: supported (`""` empty, else a rule). `SN1`
    cached without `TZ`: old firmware. `SN1` absent is only evidence of old
    firmware once `--nodeset` has been asked (`probed=True`) AND `SN` did
    arrive, because `SN` and `SN1` are pushed back to back; before that, or
    with neither register, it is UNKNOWN. A `TZ` that is not a string is
    UNKNOWN rather than guessed at.
    """
    sn1 = registers.get("SN1")
    if sn1 is not None:
        if "TZ" not in sn1:
            return NodeTz(TzState.UNSUPPORTED)
        tz = sn1["TZ"]
        if not isinstance(tz, str):
            return NodeTz(TzState.UNKNOWN)
        return NodeTz(TzState.EMPTY) if tz == "" else NodeTz(TzState.RULE, tz)
    if probed and "SN" in registers:
        return NodeTz(TzState.UNSUPPORTED)
    return NodeTz(TzState.UNKNOWN)
