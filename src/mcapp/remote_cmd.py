"""Pure core of the RM1 HMAC remote-admin protocol (Node Admin).

No I/O, no DB, no clock. Ground truth is the firmware, branch ``fork-dev``:
``src/remote_cmd.cpp`` (``rmParse``, ``allowed``, ``isForbiddenText``,
``rmDeriveKey``, ``rmCanonical``, ``rmReply``, ``rmVerifyReply``),
``src/regex_functions.cpp`` (``checkRegexCall``, ``normalizeOwnCall``) and
``docs/adr-remote-hmac.md``. Plan: ``doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md``.

Wire format::

    command  RM1 <ctr> <cmd>[ <args>] <tag16>
    reply    RM1 <ctr> ok|err <text> <tag16>

    K          = SHA-256(password with trailing spaces stripped)
    command    tag = HMAC-SHA256(K, "RM1|<dst>|<src>|<ctr>|<cmd line>")[:8]
    reply      tag = HMAC-SHA256(K, "RM1R|<dst>|<src>|<ctr>|<result>")[:8]

Both tags use the COMMAND's orientation: ``dst`` is the managed node and
``src`` the commanding node, as ``msg_source_call`` stores it on the node
(upper case, SSID included). The reply frame's own src/dst are never used.
``result`` includes the ``ok `` / ``err `` prefix.

This module never reads the firmware test corpus (the production release
tarball does not ship ``*.json`` test corpora); only ``remote_cmd_tests`` does.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Final, Literal

from .util import strip_ack_suffix

PROTO: Final = "RM1"

RM_RATE_MS: Final = 10_000  # firmware RM_RATE_MS: min spacing of accepted frames
RM_CACHE_MS: Final = 600_000  # firmware RM_CACHE_MS: lost-reply recovery window
RM_REPLY_TIMEOUT_MS: Final = 120_000  # McApp: a row with no reply is "no reply" after this
CTR_MAX: Final = 4_294_967_295
TX_MAX_DEFAULT: Final = 15  # lowest board maximum (Heltec V2); see plan D5

PASSWD_MAX: Final = 14  # node_passwd is char[15]
CMD_MAX: Final = 15  # RmCmd.cmd is char[16]
ARGS_MAX: Final = 23  # RmCmd.args is char[24]
RESULT_MAX: Final = 63  # RM_MAX_RESULT
REPLY_MAX: Final = 160  # rmVerifyReply refuses longer texts
CALL_MAX: Final = 9  # meshcom_settings.node_call is char[10]
CALL_BASE_MIN: Final = 3  # normalizeOwnCall touches only an ordinary call:
CALL_BASE_MAX: Final = 6  # base of 3..6 characters
KEY_LEN: Final = 32
TAG_HEX_LEN: Final = 16  # 8 bytes

# Command name -> argument spec, mirroring firmware allowed():
#   "none"     no arguments
#   "onoff"    exactly "on" or "off"
#   "txpower"  1..3 decimal digits, no leading zero, 0..tx_max
#   "setout"   "<a0..a7|b0..b7> <on|off>"
ALLOWLIST: Final[dict[str, str]] = {
    "reboot": "none",
    "status": "none",
    "sendpos": "none",
    "sendtrack": "none",
    "sync": "none",
    "gps": "onoff",
    "track": "onoff",
    "display": "onoff",
    "gateway": "onoff",
    "mesh": "onoff",
    "txpower": "txpower",
    "setout": "setout",
}

_ONOFF: Final = frozenset({"on", "off"})
_TXPOWER_RE: Final = re.compile(r"(0|[1-9][0-9]{0,2})")
_SETOUT_RE: Final = re.compile(r"[ab][0-7] (on|off)")
_CMD_RE: Final = re.compile(r"[a-z]+")
_TAG_RE: Final = re.compile(r"[0-9a-f]{16}")

# Firmware checkRegexCall(): ^[0-9A-Z]?[A-Z]?[0-9]+[A-Z][A-Z]?[A-Z]?[%-]?[0-9]?[0-9]?$.
# Deliberately stricter in one place: the firmware pattern also accepts the SSID
# digits WITHOUT the dash ("DK5EN12"), which no node ever emits; here the dash is
# required before any SSID digit. A bare trailing "-" is accepted like the
# firmware does (normalizeOwnCall turns it into the bare call).
_CALL_RE: Final = re.compile(r"([0-9A-Z]?[A-Z]?[0-9]+[A-Z][A-Z]?[A-Z]?)(?:-([0-9]{0,2}))?")

_SYNC_HWM_RE: Final = re.compile(r"ctr=([0-9]{1,10})(?![0-9])")

# Reply text after the ack suffix is gone: "RM1 <ctr> <ok|err> <text> <tag16>".
_REPLY_RE: Final = re.compile(
    r"RM1 (?P<ctr>0|[1-9][0-9]{0,9}) (?P<result>(?P<status>ok|err) (?P<body>.*)) "
    r"(?P<tag>[0-9a-f]{16})"
)


class RmError(ValueError):
    """A call, password, command or counter was refused."""


@dataclass(frozen=True)
class ParsedReply:
    """A syntactically valid RM1 reply. NOT authenticated until verify_reply."""

    ctr: int
    status: Literal["ok", "err"]
    result: str  # everything after "RM1 <ctr> ": includes the ok/err prefix (the tag input)
    body: str  # result without the "ok " / "err " prefix
    tag: str


# ---------------------------------------------------------------------------
# call signs and passwords
# ---------------------------------------------------------------------------


def normalize_call(s: str) -> str:
    """Trim, upper-case and validate a callsign the way the node holds it.

    Grammar is firmware ``checkRegexCall`` (see ``_CALL_RE``); SSID handling is
    firmware ``normalizeOwnCall`` (``--setcall``): ``-0`` / ``-00`` / a bare
    ``-`` drop the SSID, ``-01`` becomes ``-1``. Only an ordinary call (base 3..6
    chars) is touched, like the firmware. The result, SSID included, must fit
    the node's 9-character call field.
    """
    if not isinstance(s, str):
        raise RmError("callsign must be a string")
    t = s.strip()
    if not t.isascii():
        raise RmError("callsign must be ASCII")
    t = t.upper()
    m = _CALL_RE.fullmatch(t)
    if m is None:
        raise RmError(f"bad callsign {t!r}")
    base, ssid = m.group(1), m.group(2)
    if ssid is None:
        out = base
    elif CALL_BASE_MIN <= len(base) <= CALL_BASE_MAX:
        n = int(ssid) if ssid else 0
        out = base if n == 0 else f"{base}-{n}"
    else:
        out = t  # not an ordinary call: the firmware leaves it untouched
    if len(out) > CALL_MAX:
        raise RmError(f"callsign {out!r} longer than {CALL_MAX} characters")
    return out


def validate_password(pw: str) -> None:
    """Raise RmError unless ``pw`` is a password the node's ``--passwd`` accepts.

    1..14 bytes, printable ASCII, no leading space, not ``none`` (which clears
    the node password). Trailing spaces are allowed but carry no entropy: the
    key derivation strips them, and a password of only spaces is empty.
    """
    if not isinstance(pw, str):
        raise RmError("password must be a string")
    if not pw.isascii() or not pw.isprintable():
        raise RmError("password must be printable ASCII")
    if len(pw) > PASSWD_MAX:
        raise RmError(f"password longer than {PASSWD_MAX} characters")
    if pw.startswith(" "):
        raise RmError("password must not start with a space")
    stripped = pw.rstrip(" ")
    if not stripped:
        raise RmError("password is empty")
    if stripped == "none":
        raise RmError("password 'none' clears the node password")


def derive_key(password: str) -> bytes:
    """K = SHA-256(password with trailing spaces stripped), 32 bytes."""
    validate_password(password)
    return hashlib.sha256(password.rstrip(" ").encode("ascii")).digest()


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


def _check_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != KEY_LEN:
        raise RmError(f"key must be {KEY_LEN} bytes")


def _check_ctr(ctr: int) -> None:
    if isinstance(ctr, bool) or not isinstance(ctr, int):
        raise RmError("ctr must be an integer")
    if ctr < 0 or ctr > CTR_MAX:
        raise RmError(f"ctr out of range 0..{CTR_MAX}")


def _tag(key: bytes, canonical: str) -> str:
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()[:TAG_HEX_LEN]


def rm_tag(key: bytes, dst: str, src: str, ctr: int, cmd_line: str) -> str:
    """16-hex command tag. ``dst`` = managed node, ``src`` = commanding node."""
    _check_key(key)
    _check_ctr(ctr)
    return _tag(key, f"{PROTO}|{dst}|{src}|{ctr}|{cmd_line}")


def rm_reply_tag(key: bytes, dst: str, src: str, ctr: int, result: str) -> str:
    """16-hex reply tag, in the COMMAND's orientation (``dst`` = managed node).

    ``result`` is the text after ``RM1 <ctr> ``, including the ``ok `` / ``err `` prefix.
    """
    _check_key(key)
    _check_ctr(ctr)
    return _tag(key, f"{PROTO}R|{dst}|{src}|{ctr}|{result}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _forbidden_text(s: str) -> bool:
    """Firmware isForbiddenText(): ``;`` ``{`` ``%`` and ``--``."""
    return ";" in s or "{" in s or "%" in s or "--" in s


def _check_text(cmd: str, args: str, tx_max: int) -> None:
    """Syntax gate before the allowlist: types, lengths, charset, spacing."""
    if not isinstance(cmd, str) or not isinstance(args, str):
        raise RmError("cmd and args must be strings")
    if isinstance(tx_max, bool) or not isinstance(tx_max, int) or tx_max < 0:
        raise RmError("tx_max must be a non-negative integer")
    if not cmd or len(cmd) > CMD_MAX:
        raise RmError(f"cmd must be 1..{CMD_MAX} characters")
    if len(args) > ARGS_MAX:
        raise RmError(f"args longer than {ARGS_MAX} characters")
    if _forbidden_text(cmd) or _forbidden_text(args):
        raise RmError("forbidden character sequence")
    if _CMD_RE.fullmatch(cmd) is None:
        raise RmError("cmd must be lower-case letters only")
    if args and (not args.isascii() or not args.isprintable() or args != args.lower()):
        raise RmError("args must be lower-case printable ASCII")
    if args.startswith(" ") or args.endswith(" ") or "  " in args:
        raise RmError("args need single spaces, none leading or trailing")


def _args_match(spec: str, args: str, tx_max: int) -> bool:
    """Firmware allowed(): does ``args`` fit the command's argument shape?"""
    if spec == "none":
        return args == ""
    if spec == "onoff":
        return args in _ONOFF
    if spec == "txpower":
        return _TXPOWER_RE.fullmatch(args) is not None and int(args) <= tx_max
    return _SETOUT_RE.fullmatch(args) is not None  # "setout"


def validate_command(cmd: str, args: str, tx_max: int = TX_MAX_DEFAULT) -> str:
    """Return the canonical command line, or raise RmError.

    Mirrors firmware ``rmCommandAllowed`` / ``allowed`` exactly: lower-case
    ASCII, single spaces, ``cmd`` <= 15 and ``args`` <= 23 chars, no ``;`` ``{``
    ``%`` ``--``, then the allowlist with its argument shape. ``txpower`` is
    bounded by ``tx_max`` because an over-limit value is a silent, lockout-counted
    reject on the node, never an ``err`` reply.
    """
    _check_text(cmd, args, tx_max)
    spec = ALLOWLIST.get(cmd)
    if spec is None:
        raise RmError(f"command {cmd!r} is not on the allowlist")
    if not _args_match(spec, args, tx_max):
        raise RmError(f"bad arguments for {cmd!r}: {args!r}")
    return f"{cmd} {args}" if args else cmd


def build_command_text(  # noqa: PLR0913, PLR0917  # signature pinned by the Node Admin plan (W2 codes against it)
    key: bytes,
    dst: str,
    src: str,
    ctr: int,
    cmd: str,
    args: str = "",
    tx_max: int = TX_MAX_DEFAULT,
) -> str:
    """Return the DM text ``RM1 <ctr> <cmd>[ <args>] <tag16>``.

    ``dst`` / ``src`` are normalised with ``normalize_call``. ``ctr`` is 0 for
    ``sync`` and only for ``sync``; every other command takes 1..CTR_MAX.
    """
    line = validate_command(cmd, args, tx_max)
    _check_ctr(ctr)
    if cmd == "sync":
        if ctr != 0:
            raise RmError("sync uses ctr 0")
    elif ctr < 1:
        raise RmError("ctr 0 is reserved for sync")
    d = normalize_call(dst)
    s = normalize_call(src)
    return f"{PROTO} {ctr} {line} {rm_tag(key, d, s, ctr, line)}"


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------


def parse_reply(text: str) -> ParsedReply | None:
    """Parse an RM1 reply, or None. Does NOT authenticate: see ``verify_reply``.

    The firmware ``{NNN`` ack-request suffix is stripped first: the Extern-UDP
    copy of a reply carries it, the BLE copy does not. Mirrors ``rmIsReply`` /
    ``rmVerifyReply``: ctr without leading zeros, ``ok `` / ``err `` prefix,
    16 lower-case hex tag after a single space, result <= 63 chars, text <= 160.
    """
    if not isinstance(text, str):
        return None
    t = strip_ack_suffix(text)
    if len(t) > REPLY_MAX or not t.isascii() or not t.isprintable():
        return None
    m = _REPLY_RE.fullmatch(t)
    if m is None:
        return None
    ctr = int(m.group("ctr"))
    result = m.group("result")
    if ctr > CTR_MAX or len(result) > RESULT_MAX:
        return None
    status: Literal["ok", "err"] = "ok" if m.group("status") == "ok" else "err"
    return ParsedReply(
        ctr=ctr, status=status, result=result, body=m.group("body"), tag=m.group("tag")
    )


def verify_reply(key: bytes, dst: str, src: str, text: str) -> ParsedReply | None:
    """Parse ``text`` and check its tag; None unless it authenticates.

    ``dst`` = managed node, ``src`` = commanding node (the COMMAND's
    orientation, as stored on the log row), never the reply frame's own pair.
    """
    parsed = parse_reply(text)
    if parsed is None:
        return None
    try:
        d = normalize_call(dst)
        s = normalize_call(src)
        want = rm_reply_tag(key, d, s, parsed.ctr, parsed.result)
    except RmError:
        return None
    if not hmac.compare_digest(want, parsed.tag):
        return None
    return parsed


def parse_sync_hwm(body: str) -> int | None:
    """High-water mark from a sync reply body ``ctr=<hwm> v=...``, else None.

    Call only on the body of a reply that passed ``verify_reply``.
    """
    m = _SYNC_HWM_RE.match(body)
    if m is None:
        return None
    hwm = int(m.group(1))
    return hwm if hwm <= CTR_MAX else None
