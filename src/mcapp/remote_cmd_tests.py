"""Built-in test suite for the RM1 pure core in ``remote_cmd.py``.

Plan: ``doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md`` (section 6.1,
A1). Pure functions only: no DB, no network, no node. Follows the house
pattern (``commands/hashtag_dst_tests.py``): PASS/FAIL line per case, a
summary, and a bool return.

``remote_cmd_vectors.json`` is a byte-exact copy of the firmware repo's
``tools/tests/remote_cmd_vectors.json`` (branch ``fork-dev``; the firmware
repo is canonical, never edit the copy). It is read by THIS suite only: the
runtime module never opens it, because the production release tarball does
not ship ``*.json`` corpora.

Mutation record (each must fail exactly the named group, then be restored):
  (a) drop ``dst`` from the ``rm_tag`` input      -> "command vector" cases
  (b) swap dst/src in ``rm_reply_tag``            -> "reply vector" cases
  (c) remove ``strip_ack_suffix`` in parse_reply   -> "udp suffix" cases
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from .remote_cmd import (
    ALLOWLIST,
    CTR_MAX,
    RM_CACHE_MS,
    RM_RATE_MS,
    RM_REPLY_TIMEOUT_MS,
    TX_MAX_DEFAULT,
    ParsedReply,
    RmError,
    build_command_text,
    derive_key,
    normalize_call,
    parse_reply,
    parse_sync_hwm,
    rm_reply_tag,
    rm_tag,
    validate_command,
    validate_password,
    verify_reply,
)

_VECTORS_PATH = Path(__file__).parent / "remote_cmd_vectors.json"
_MODULE_PATH = Path(__file__).parent / "remote_cmd.py"

# sha256 of the raw bytes of the vendored vectors copy. A change means the
# firmware corpus changed: re-copy it byte-exact, re-pin this constant and the
# copy in ONE commit, and re-check the hand-written cases below still agree.
_EXPECTED_SHA256 = "65cbfb36ac39fec42bc764e84351bc46e2783af700c3787a311cfccd8bd09418"

UDP_SUFFIX = "{087"  # the firmware ack-request suffix on an Extern-UDP copy

Record = Callable[[str, bool], None]


def _raises(fn: Callable[[], Any]) -> bool:
    try:
        fn()
    except RmError:
        return True
    return False


def _load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_VECTORS_PATH.read_bytes())
    return data


# ---------------------------------------------------------------------------
# 1. corpus
# ---------------------------------------------------------------------------


def _test_corpus(record: Record) -> None:
    raw = _VECTORS_PATH.read_bytes()
    record("vectors sha256 pin", hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256)
    corpus = _load()
    commands: list[dict[str, Any]] = corpus["commands"]
    replies: list[dict[str, Any]] = corpus["replies"]
    record("vectors count 19 commands + 4 replies", len(commands) == 19 and len(replies) == 4)

    for i, v in enumerate(commands):
        key = derive_key(v["passwd"])
        line = v["cmd"] + (" " + v["args"] if v["args"] else "")
        label = f"command vector {i}: {v['dm_text']}"
        ok = key.hex() == v["key_hex"]
        ok = ok and rm_tag(key, v["dst"], v["src"], v["ctr"], line) == v["tag"]
        ok = (
            ok
            and build_command_text(key, v["dst"], v["src"], v["ctr"], v["cmd"], v["args"])
            == v["dm_text"]
        )
        record(label, ok)

    for i, v in enumerate(replies):
        key = derive_key(v["passwd"])
        label = f"reply vector {i}: {v['reply_text']}"
        parsed = parse_reply(v["reply_text"])
        ok = key.hex() == v["key_hex"]
        ok = ok and rm_reply_tag(key, v["dst"], v["src"], v["ctr"], v["result"]) == v["reply_tag"]
        ok = ok and parsed is not None and parsed.ctr == v["ctr"] and parsed.result == v["result"]
        ok = ok and parsed is not None and parsed.tag == v["reply_tag"]
        verified = verify_reply(key, v["dst"], v["src"], v["reply_text"])
        ok = ok and verified == parsed
        record(label, ok)

    # sync reply vector: "ok ctr=42 v=4.40a" -> body parses to the high-water mark
    sync = next(v for v in replies if v["ctr"] == 0)
    parsed = parse_reply(sync["reply_text"])
    record(
        "reply vector sync: parse_sync_hwm == 42, status ok",
        parsed is not None and parsed.status == "ok" and parse_sync_hwm(parsed.body) == 42,
    )
    err = next(v for v in replies if v["result"].startswith("err "))
    parsed = parse_reply(err["reply_text"])
    record(
        "reply vector err: status err, body 'range'",
        parsed is not None and parsed.status == "err" and parsed.body == "range",
    )

    # the runtime module must never read the corpus (release tarball rule)
    record(
        "runtime module does not reference the vectors file",
        "vectors.json" not in _MODULE_PATH.read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# 2. UDP copy shape: the same reply with the firmware ack suffix appended
# ---------------------------------------------------------------------------


def _test_udp_suffix(record: Record) -> None:
    for i, v in enumerate(_load()["replies"]):
        key = derive_key(v["passwd"])
        plain = parse_reply(v["reply_text"])
        udp = parse_reply(v["reply_text"] + UDP_SUFFIX)
        ok = plain is not None and udp == plain
        ok = ok and verify_reply(key, v["dst"], v["src"], v["reply_text"] + UDP_SUFFIX) == plain
        record(
            f"udp suffix reply vector {i}: '{UDP_SUFFIX}' copy parses and verifies identically", ok
        )

    v = _load()["replies"][0]
    key = derive_key(v["passwd"])
    # a braced `{087}` is chat text, not the firmware suffix: it must not be stripped
    record(
        "udp suffix: braced '{087}' is not stripped and does not verify",
        verify_reply(key, v["dst"], v["src"], v["reply_text"] + "{087}") is None,
    )
    # the suffix never rescues a forged tag
    forged = v["reply_text"][:-1] + ("0" if v["reply_text"][-1] != "0" else "1")
    record(
        "udp suffix: forged tag with suffix still rejected",
        verify_reply(key, v["dst"], v["src"], forged + UDP_SUFFIX) is None,
    )


# ---------------------------------------------------------------------------
# 3. validators
# ---------------------------------------------------------------------------


def _test_validate_command(record: Record) -> None:
    accept: list[tuple[str, str, int, str]] = [
        ("reboot", "", TX_MAX_DEFAULT, "reboot"),
        ("status", "", TX_MAX_DEFAULT, "status"),
        ("sendpos", "", TX_MAX_DEFAULT, "sendpos"),
        ("sendtrack", "", TX_MAX_DEFAULT, "sendtrack"),
        ("sync", "", TX_MAX_DEFAULT, "sync"),
        ("gps", "on", TX_MAX_DEFAULT, "gps on"),
        ("track", "off", TX_MAX_DEFAULT, "track off"),
        ("display", "off", TX_MAX_DEFAULT, "display off"),
        ("led", "on", TX_MAX_DEFAULT, "led on"),
        ("led", "off", TX_MAX_DEFAULT, "led off"),
        ("gateway", "on", TX_MAX_DEFAULT, "gateway on"),
        ("mesh", "off", TX_MAX_DEFAULT, "mesh off"),
        ("setout", "a2 on", TX_MAX_DEFAULT, "setout a2 on"),
        ("setout", "b7 off", TX_MAX_DEFAULT, "setout b7 off"),
        ("setout", "a0 off", TX_MAX_DEFAULT, "setout a0 off"),
        ("txpower", "0", TX_MAX_DEFAULT, "txpower 0"),
        ("txpower", "15", TX_MAX_DEFAULT, "txpower 15"),
        ("txpower", "22", 22, "txpower 22"),
    ]
    for cmd, args, tx_max, want in accept:
        got: str | None
        try:
            got = validate_command(cmd, args, tx_max)
        except RmError:
            got = None
        record(f"validator accepts {want!r} (tx_max {tx_max})", got == want)

    reject: list[tuple[str, str, int, str]] = [
        ("setout", "2 1", TX_MAX_DEFAULT, "old paper grammar 'setout 2 1'"),
        ("setout", "c2 on", TX_MAX_DEFAULT, "setout bank c"),
        ("setout", "a8 on", TX_MAX_DEFAULT, "setout pin 8"),
        ("setout", "a2 1", TX_MAX_DEFAULT, "setout value 1"),
        ("setout", "a2  on", TX_MAX_DEFAULT, "setout double space"),
        ("setout", "a2 ON", TX_MAX_DEFAULT, "setout upper case value"),
        ("setout", "a2", TX_MAX_DEFAULT, "setout without value"),
        ("txpower", "16", TX_MAX_DEFAULT, "txpower above tx_max 15"),
        ("txpower", "23", 22, "txpower above tx_max 22"),
        ("txpower", "015", TX_MAX_DEFAULT, "txpower leading zero"),
        ("txpower", "-1", TX_MAX_DEFAULT, "txpower negative"),
        ("txpower", "1000", TX_MAX_DEFAULT, "txpower four digits"),
        ("txpower", "", TX_MAX_DEFAULT, "txpower without value"),
        ("txpower", "1.5", TX_MAX_DEFAULT, "txpower decimal"),
        ("gps", "1", TX_MAX_DEFAULT, "toggle with 1"),
        ("gps", "ON", TX_MAX_DEFAULT, "toggle upper case"),
        ("gps", "", TX_MAX_DEFAULT, "toggle without value"),
        ("reboot", "now", TX_MAX_DEFAULT, "reboot with args"),
        ("sync", "x", TX_MAX_DEFAULT, "sync with args"),
        ("--x", "", TX_MAX_DEFAULT, "console command '--x'"),
        ("status;", "", TX_MAX_DEFAULT, "semicolon in cmd"),
        ("gps", "on;off", TX_MAX_DEFAULT, "semicolon in args"),
        ("gps", "on {", TX_MAX_DEFAULT, "brace in args"),
        ("gps", "o%n", TX_MAX_DEFAULT, "percent in args"),
        ("gps", "--", TX_MAX_DEFAULT, "double dash in args"),
        ("Reboot", "", TX_MAX_DEFAULT, "upper case cmd"),
        ("reboot ", "", TX_MAX_DEFAULT, "trailing space in cmd"),
        ("setout a2", "on", TX_MAX_DEFAULT, "space inside cmd"),
        ("", "", TX_MAX_DEFAULT, "empty cmd"),
        ("cleanflash", "", TX_MAX_DEFAULT, "hard-blocked cleanflash"),
        ("passwd", "x", TX_MAX_DEFAULT, "hard-blocked passwd"),
        ("ota-update", "", TX_MAX_DEFAULT, "hard-blocked ota-update"),
        ("a" * 16, "", TX_MAX_DEFAULT, "cmd of 16 characters"),
        ("setout", "a2 on" + " " * 19, TX_MAX_DEFAULT, "args of 24 characters"),
        ("gps", " on", TX_MAX_DEFAULT, "leading space in args"),
        ("gps", "on ", TX_MAX_DEFAULT, "trailing space in args"),
        ("gps", "ön", TX_MAX_DEFAULT, "non-ASCII args"),
    ]
    for cmd, args, tx_max, why in reject:
        record(
            f"validator rejects {why}",
            _raises(partial(validate_command, cmd, args, tx_max)),
        )

    record(
        "validator: txpower 15 inside a 22 dBm node still bounded by default",
        _raises(lambda: validate_command("txpower", "16")),
    )
    record(
        "validator: tx_max must be a non-negative int",
        _raises(lambda: validate_command("txpower", "1", -1)),
    )
    record(
        "allowlist covers exactly the firmware commands",
        set(ALLOWLIST)
        == {
            "reboot", "status", "sendpos", "sendtrack", "sync",
            "gps", "track", "display", "led", "gateway", "mesh",
            "txpower", "setout",
        },
    )  # fmt: skip
    record(
        "constants: firmware rate/cache and McApp reply timeout",
        (RM_RATE_MS, RM_CACHE_MS, RM_REPLY_TIMEOUT_MS, CTR_MAX, TX_MAX_DEFAULT)
        == (10_000, 600_000, 120_000, 4_294_967_295, 15),
    )


def _test_build_command(record: Record) -> None:
    key = derive_key("secret")
    record(
        "build: ctr 0 with a non-sync command rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DK5EN-1", 0, "status")),
    )
    record(
        "build: sync with ctr 1 rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DK5EN-1", 1, "sync")),
    )
    record(
        "build: sync with ctr 0 accepts",
        build_command_text(key, "DK5EN-90", "DK5EN-1", 0, "sync").startswith("RM1 0 sync "),
    )
    record(
        "build: ctr above CTR_MAX rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DK5EN-1", CTR_MAX + 1, "status")),
    )
    record(
        "build: ctr CTR_MAX accepts",
        build_command_text(key, "DK5EN-90", "DK5EN-1", CTR_MAX, "status").startswith(
            "RM1 4294967295 status "
        ),
    )
    record(
        "build: negative ctr rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DK5EN-1", -1, "status")),
    )
    record(
        "build: bool ctr rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DK5EN-1", True, "status")),
    )
    record(
        "build: blocked command rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DK5EN-1", 5, "--x")),
    )
    record(
        "build: over-limit txpower rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DK5EN-1", 5, "txpower", "16")),
    )
    record(
        "build: txpower within a raised tx_max accepts",
        build_command_text(key, "DK5EN-90", "DK5EN-1", 5, "txpower", "20", tx_max=20).startswith(
            "RM1 5 txpower 20 "
        ),
    )
    record(
        "build: bad dst call rejects",
        _raises(lambda: build_command_text(key, "NOPE", "DK5EN-1", 5, "status")),
    )
    record(
        "build: 10-character src rejects",
        _raises(lambda: build_command_text(key, "DK5EN-90", "DL10ABC-12", 5, "status")),
    )
    record(
        "build: short key rejects",
        _raises(lambda: build_command_text(b"short", "DK5EN-90", "DK5EN-1", 5, "status")),
    )
    lower = build_command_text(key, "dk5en-90", " dk5en-1 ", 5, "status")
    record(
        "build: lower-case padded calls normalise to the same frame",
        lower == build_command_text(key, "DK5EN-90", "DK5EN-1", 5, "status"),
    )


def _test_normalize_call(record: Record) -> None:
    cases: list[tuple[str, str | None]] = [
        ("DK5EN-14", "DK5EN-14"),
        (" dk5en-14 ", "DK5EN-14"),
        ("DK5EN", "DK5EN"),
        ("DK5EN-01", "DK5EN-1"),
        ("DK5EN-001", None),
        ("DK5EN-0", "DK5EN"),
        ("DK5EN-00", "DK5EN"),
        ("DK5EN-", "DK5EN"),
        ("DK5EN-90", "DK5EN-90"),
        ("DJ5ABC-12", "DJ5ABC-12"),  # exactly 9 characters
        ("DL10ABC-12", None),  # 10 characters
        ("DK5EN-123", None),
        ("DK5EN-A", None),
        ("DK5EN12", None),  # firmware regex allows it, no node emits it: refused
        ("HELLO", None),
        ("", None),
        ("   ", None),
        ("*", None),
        ("DK5EN 14", None),
        ("DK5EN-1\n", "DK5EN-1"),
        ("ÄK5EN", None),
    ]
    for given, want in cases:
        got: str | None
        try:
            got = normalize_call(given)
        except RmError:
            got = None
        record(f"normalize_call({given!r}) == {want!r}", got == want)


def _test_validate_password(record: Record) -> None:
    ok_cases = [
        "secret",
        "secret   ",
        "abcdefghijklmn",
        "p@ss w0rd!",
        "None",
        "a",
        "x" * 11 + "   ",
    ]
    bad_cases: list[tuple[str, str]] = [
        ("", "empty"),
        ("   ", "only spaces"),
        (" secret", "leading space"),
        ("none", "'none' clears the node password"),
        ("none  ", "'none' with trailing spaces"),
        ("abcdefghijklmno", "15 bytes"),
        ("über", "non-ASCII"),
        ("a\tb", "control character"),
        ("a\nb", "newline"),
    ]
    for pw in ok_cases:
        got = True
        try:
            validate_password(pw)
        except RmError:
            got = False
        record(f"password accepts {pw!r}", got)
    for pw, why in bad_cases:
        record(f"password rejects {why}", _raises(partial(validate_password, pw)))
    record(
        "derive_key: trailing spaces are not part of the key",
        derive_key("secret") == derive_key("secret   ") and len(derive_key("secret")) == 32,
    )
    record(
        "derive_key: refuses what validate_password refuses", _raises(lambda: derive_key("none"))
    )


def _test_parse_reply(record: Record) -> None:
    v = _load()["replies"][0]
    key = derive_key(v["passwd"])
    good = v["reply_text"]
    tag = v["reply_tag"]

    parsed = parse_reply(good)
    record(
        "parse_reply returns a frozen ParsedReply with prefix-bearing result",
        isinstance(parsed, ParsedReply)
        and parsed.result == "ok rebooting"
        and parsed.body == "rebooting"
        and parsed.status == "ok"
        and parsed.ctr == 1,
    )

    bad: list[tuple[str, str]] = [
        ("RM1 1 maybe x " + tag, "unknown status"),
        ("RM1 1 ok rebooting " + tag.upper(), "upper-case tag"),
        ("RM1 1 ok rebooting " + tag[:-1], "15-hex tag"),
        ("RM1 1 ok rebooting", "no tag"),
        ("RM1 01 ok rebooting " + tag, "ctr with leading zero"),
        ("RM1 4294967296 ok rebooting " + tag, "ctr above CTR_MAX"),
        ("RM1 1 reboot " + tag, "a command, not a reply"),
        ("RM1 1 ok " + "x" * 61 + " " + tag, "result over 63 chars"),
        ("rm1 1 ok rebooting " + tag, "lower-case proto"),
        ("RM1  1 ok rebooting " + tag, "double space after proto"),
        ("", "empty text"),
        ("hello", "plain chat"),
    ]
    for text, why in bad:
        record(f"parse_reply rejects {why}", parse_reply(text) is None)
    record("parse_reply rejects non-str input", parse_reply(None) is None)  # type: ignore[arg-type]  # deliberate wrong type

    record(
        "parse_reply accepts a 63-char result",
        parse_reply("RM1 1 ok " + "x" * 60 + " " + tag) is not None,
    )

    record(
        "verify_reply: genuine reply verifies",
        verify_reply(key, v["dst"], v["src"], good) is not None,
    )
    record(
        "verify_reply: wrong key rejects",
        verify_reply(derive_key("other"), v["dst"], v["src"], good) is None,
    )
    record(
        "verify_reply: swapped dst/src rejects (command orientation)",
        verify_reply(key, v["src"], v["dst"], good) is None,
    )
    record(
        "verify_reply: other node rejects", verify_reply(key, "DK5EN-92", v["src"], good) is None
    )
    record(
        "verify_reply: tampered result rejects",
        verify_reply(key, v["dst"], v["src"], good.replace("rebooting", "rebootinx")) is None,
    )
    record(
        "verify_reply: tampered ctr rejects",
        verify_reply(key, v["dst"], v["src"], good.replace("RM1 1 ", "RM1 2 ")) is None,
    )
    record(
        "verify_reply: lower-case call arguments normalise",
        verify_reply(key, "dk5en-90", "dk5en-1", good) is not None,
    )
    record(
        "verify_reply: unusable call arguments reject, not raise",
        verify_reply(key, "NOPE", v["src"], good) is None,
    )
    record(
        "verify_reply: garbage text rejects",
        verify_reply(key, v["dst"], v["src"], "RM1 nonsense") is None,
    )

    sync_body: list[tuple[str, int | None]] = [
        ("ctr=42 v=4.40a", 42),
        ("ctr=0 v=4.40a", 0),
        ("ctr=4294967295", CTR_MAX),
        ("ctr=4294967296", None),
        ("ctr=12345678901", None),
        ("ctr=", None),
        ("ctr=x", None),
        ("v=1 ctr=5", None),
        ("", None),
    ]
    for body, want in sync_body:
        record(f"parse_sync_hwm({body!r}) == {want!r}", parse_sync_hwm(body) == want)


# ---------------------------------------------------------------------------
# 4. bare src vs full src
# ---------------------------------------------------------------------------


def _test_src_binding(record: Record) -> None:
    """The node tags with ``msg_source_call``: its call WITH the SSID. McApp's
    configured ``CALL_SIGN`` is bare (``DK5EN``) while the attached node reports
    ``DK5EN-14``; tagging with the bare call would produce a frame the node
    silently rejects (and counts toward its lockout). The tag therefore binds
    the exact src string, and a bare src must never equal the full one."""
    key = derive_key("secret")
    bare = build_command_text(key, "DK5EN-90", "DK5EN", 9, "status")
    full = build_command_text(key, "DK5EN-90", "DK5EN-14", 9, "status")
    record(
        "bare src DK5EN and full src DK5EN-14 produce different tags",
        bare != full and bare.split(" ")[-1] != full.split(" ")[-1],
    )
    record(
        "src SSID spelling is normalised before tagging (-01 == -1)",
        build_command_text(key, "DK5EN-90", "DK5EN-01", 9, "status")
        == build_command_text(key, "DK5EN-90", "DK5EN-1", 9, "status"),
    )
    record(
        "reply tag binds src too: bare-src reply does not verify under the full src",
        verify_reply(key, "DK5EN-90", "DK5EN-14", _reply(key, "DK5EN-90", "DK5EN", 9, "ok sent"))
        is None,
    )


def _reply(key: bytes, dst: str, src: str, ctr: int, result: str) -> str:
    return f"RM1 {ctr} {result} {rm_reply_tag(key, dst, src, ctr, result)}"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run_remote_cmd_tests() -> bool:
    """Run every case, print PASS/FAIL per case and a summary; True iff all pass."""
    print("\nTesting RM1 pure core (remote_cmd):")
    print("=" * 55)
    results: list[tuple[str, bool]] = []

    def record(label: str, ok: bool) -> None:
        results.append((label, ok))
        print(f"{'PASS' if ok else 'FAIL'} | {label}")

    for group in (
        _test_corpus,
        _test_udp_suffix,
        _test_validate_command,
        _test_build_command,
        _test_normalize_call,
        _test_validate_password,
        _test_parse_reply,
        _test_src_binding,
    ):
        try:
            group(record)
        except Exception as exc:  # a crashing group must fail the suite, loudly
            record(f"{group.__name__} raised {type(exc).__name__}: {exc}", False)

    passed = sum(1 for _, ok in results if ok)
    for label, ok in results:
        if not ok:
            print(f"FAIL | {label}")
    print(f"\nremote_cmd Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if run_remote_cmd_tests() else 1)
