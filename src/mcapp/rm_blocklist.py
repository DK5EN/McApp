"""Pure helpers for the RM1 `blockcall` command (firmware call blocklist, BLK-01).

`normalize`, `parse_list` and `matches` mirror the firmware's `blocked_calls.cpp`
(`blockCallNormalize`, `listBuild`, `matchNorm`); `parse_reply_body` mirrors
`rmFmtBlock` in `rm_format.h`. `build_args` and `write_refusal` are McApp's own
sender rules (operator decisions 2026-10-24). The cross-repo contract is
`rm_blocklist_vectors.json` (plan: doc/2026-10-24_1500-rm1-blockcall-plan.md).

This module imports nothing from `remote_cmd` (which imports it) and never reads
the JSON corpus: runtime code, and the prod tarball ships no corpora.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

ARGS_MAX: Final = 39
LIST_MAX: Final = 16

# Firmware `nextToken` buffer is 24 bytes: a token is clipped at 23 chars.
_TOKEN_CLIP: Final = 23
_BASE_MIN: Final = 3
_BASE_MAX: Final = 6
_SSID_MAX_DIGITS: Final = 2

_LEAD_WS: Final = " \t"
_TRAIL_WS: Final = " \t\r\n"
_SEPARATORS: Final = re.compile(r"[,;]")
_REPLY_PREFIX: Final = "b="
_REPLY_CALL_RE: Final = re.compile(r"[A-Z0-9]+(?:-[0-9]+)?")
_REPLY_MORE_RE: Final = re.compile(r"\+([1-9][0-9]*)")

_ASCII_DIGITS: Final = "0123456789"
_ASCII_LETTERS: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _upper(c: str) -> str:
    """ASCII-only upper-casing (the firmware's `toupper` in the C locale)."""
    return c.upper() if "a" <= c <= "z" else c


def _normalize_base(base: str) -> str | None:
    """Upper-cased base if it is 3..6 of `[A-Z0-9]` with a letter and a digit."""
    if not _BASE_MIN <= len(base) <= _BASE_MAX:
        return None
    base = "".join(_upper(c) for c in base)
    if not all(c in _ASCII_LETTERS or c in _ASCII_DIGITS for c in base):
        return None
    has_letter = any(c in _ASCII_LETTERS for c in base)
    has_digit = any(c in _ASCII_DIGITS for c in base)
    return base if has_letter and has_digit else None


def normalize(s: str) -> str | None:
    """Canonical form of a callsign (`BASE[-SSID]`), or None if it is not one.

    Base 3..6 of `[A-Z0-9]` with at least one letter and one digit; SSID 1..2
    digits, leading zeros dropped, SSID 0 dropped. Surrounding blanks are ignored.
    """
    text = s.lstrip(_LEAD_WS).rstrip(_TRAIL_WS)
    base_text, dash, ssid_text = text.partition("-")
    base = _normalize_base(base_text)
    if base is None or not dash:
        return base
    if not 1 <= len(ssid_text) <= _SSID_MAX_DIGITS:
        return None
    if not all(c in _ASCII_DIGITS for c in ssid_text):
        return None
    ssid = int(ssid_text)
    return f"{base}-{ssid}" if ssid > 0 else base


def _tokens(text: str) -> list[str]:
    """Split on `,`/`;`, trim, clip at 23 chars (a clipped token stays invalid)."""
    out: list[str] = []
    for raw in _SEPARATORS.split(text):
        tok = raw.lstrip(_LEAD_WS).rstrip(_TRAIL_WS)
        if len(tok) > _TOKEN_CLIP:
            tok = tok[: _TOKEN_CLIP - 1] + "?"
        if tok:
            out.append(tok)
    return out


def _is_none(text: str) -> bool:
    """True for `none` (any case) with surrounding blanks, the firmware's empty list."""
    return text.lstrip(_LEAD_WS).rstrip(_TRAIL_WS).lower() == "none"


def parse_list(text: str) -> tuple[str | None, str | None]:
    """Strictly parse an operator list into canonical CSV.

    Returns `(csv, None)` on success (empty string for an empty list or `none`),
    or `(None, bad)` with the first offending raw token, or `"too many"` when a
    17th distinct entry appears. Duplicates (after normalisation) keep the first.
    """
    if _is_none(text):
        return "", None
    kept: list[str] = []
    for tok in _tokens(text):
        norm = normalize(tok)
        if norm is not None and norm in kept:
            continue
        if norm is None:
            return None, tok
        if len(kept) >= LIST_MAX:
            return None, "too many"
        kept.append(norm)
    return ",".join(kept), None


def matches(csv: str, call: str) -> bool:
    """Firmware match rule: an entry with SSID matches exactly, a bare entry any SSID.

    A `call` that does not normalize (`*`, `H`, `WLNK-1`, ...) never matches.
    """
    if not csv:
        return False
    norm = normalize(call)
    if norm is None:
        return False
    base = norm.partition("-")[0]
    for entry in csv.split(","):
        if "-" in entry:
            if entry == norm:
                return True
        elif entry == base:
            return True
    return False


@dataclass(frozen=True)
class BlockReply:
    """A parsed `ok b=` reply: the calls shown and how many more were omitted."""

    calls: tuple[str, ...]
    omitted: int


def parse_reply_body(body: str) -> BlockReply | None:
    """Parse the text after `ok ` (`b=-`, `b=A,B,+3`, `b=+12`); None if malformed.

    Tokens are `[A-Z0-9]+(?:-[0-9]+)?` (no length check, the firmware's own
    format test uses 10-char tokens); only a final `+N` (N >= 1, no leading zero)
    may follow them; a lone `-` means an empty list.
    """
    if not body.startswith(_REPLY_PREFIX):
        return None
    rest = body[len(_REPLY_PREFIX) :]
    if rest == "-":
        return BlockReply((), 0)
    if not rest:
        return None
    parts = rest.split(",")
    omitted = 0
    more = _REPLY_MORE_RE.fullmatch(parts[-1])
    if more:
        omitted = int(more.group(1))
        parts = parts[:-1]
    if any(_REPLY_CALL_RE.fullmatch(p) is None for p in parts):
        return None
    return BlockReply(tuple(parts), omitted)


class BlocklistError(ValueError):
    """A refused `build_args`: `code` is invalid | self | target | too_long."""

    def __init__(self, code: str, entry: str | None = None, length: int | None = None) -> None:
        super().__init__(code if entry is None else f"{code}: {entry}")
        self.code = code
        self.entry = entry
        self.length = length


def build_args(calls: Sequence[str], own: Iterable[str], target: str) -> str:
    """Build the `blockcall` argument string, or raise `BlocklistError`.

    Each entry is normalized (firmware rule) and duplicates dropped keeping the
    first. Checked in order: invalid (entry = raw input), self (matches one of our
    own calls; unnormalizable own calls are ignored; entry = normalized), target
    (matches the managed node), too_long (joined length > ARGS_MAX or more than
    LIST_MAX entries). An empty list builds `none`.
    """
    entries: list[str] = []
    for raw in calls:
        norm = normalize(raw)
        if norm is None:
            raise BlocklistError("invalid", raw)
        if norm not in entries:
            entries.append(norm)
    own_norms = [n for n in (normalize(o) for o in own) if n is not None]
    target_norm = normalize(target)
    for norm in entries:
        # The entry acts as the node's csv, the own call as the heard call.
        if any(matches(norm, o) for o in own_norms):
            raise BlocklistError("self", norm)
    for norm in entries:
        if target_norm is not None and matches(norm, target_norm):
            raise BlocklistError("target", norm)
    joined = ",".join(entries)
    if len(joined) > ARGS_MAX or len(entries) > LIST_MAX:
        raise BlocklistError("too_long", length=len(joined))
    return joined or "none"


def write_refusal(known: BlockReply | None, args: str) -> str | None:
    """Why a write of `args` must not be sent, or None if it is allowed.

    `none` is always allowed. Anything else needs a known list (the last verified
    reply) that is complete (`omitted == 0`) and fits ARGS_MAX when joined: the
    node replaces the whole list, so a partial view would silently drop entries.
    Returns `unknown` | `truncated` | `too_long`.
    """
    if args.lower() == "none":
        return None
    if known is None:
        return "unknown"
    if known.omitted:
        return "truncated"
    if len(",".join(known.calls)) > ARGS_MAX:
        return "too_long"
    return None
