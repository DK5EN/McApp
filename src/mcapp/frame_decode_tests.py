"""Regression suite for `frame_decode.py` (RF monitor `DecodedFrame`).

Fixtures are the guide's worked examples and real `[MC-TEST]` frames from the
dk5en-98 console log of 2026-10-05 (cross-checked against the matching
`MH-LoRa` / `[LOG]` packet lines: HW, MOD, FCS, FW, LH, path), plus one
synthetic DM, corrupted/truncated/malformed variants. Pure functions, no I/O.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .commands.constants import has_console
from .frame_decode import decode_frame

_Record = Callable[[str, bool], None]

# Guide 5.2: 115-byte position beacon, relayed over two hops (log: x3BAC51B8 HW:39 FCS:1BCC)
_POSITION = (
    "21B851AC3B904444374D482D35352C4442304D4D522D31322C444C324A412D323E2A21343830392E39304E2F3031"
    "3233372E30304579506F7765724E6F6465204A4E363848442348616E732F413D3030313533352F4E31302F4F3D32"
    "302E342F523D393B32363338363B0027881BCC28AB617E"
)
# Own HEY (guide 5.3 / TX-LoRa x1AE1E034, FCS:06DB)
_TX_HEY = "4034E0E11A14444B35454E2D39383E48474052363B002B8806DB28AB617E"
# Relayed HEY (log: xA183638F H02 S0 M01, FCS:0E35, HW:43, LH:AB)
_RX_HEY = (
    "408F6383A11244463852442D312C44423049534D2D312C44423045442D39393E48474052333B382C39392C343B39"
    "2C3132322C2D31343B002B880E3528AB617E"
)
# Group text with UTF-8 payload (dest 262, from the real log)
_GROUP_TEXT = (
    "3A5B600F6993444F3251472D312C444B35454E2D39382C444B35454E2D39323E3236323A4963682077C3BC6E7363"
    "686520616C6C656E2065696E652067757465204E6163687420E29880EFB88F002B881B17288C617E"
)
_ACK = "419164F700814303B5030100"


def _build(head: tuple[int, int, int], path: str, dest: str, payload: bytes) -> str:
    """Synthetic frame with a valid FCS, HW 43, MOD 8/8, FW 40 'a'."""
    ptype, msg_id, flags = head
    body = bytearray([ptype])
    body += msg_id.to_bytes(4, "little")
    body.append(flags)
    body += path.encode() + b">" + dest.encode() + bytes([ptype]) + payload + b"\x00"
    body += bytes([0x2B, 0x88])
    body += (sum(body) & 0xFFFF).to_bytes(2, "big")
    body += bytes([0x28, 0xAB, 0x61, 0x7E])
    return body.hex()


def _flip(hex_str: str, byte_index: int) -> str:
    raw = bytearray.fromhex(hex_str)
    raw[byte_index] ^= 0x01
    return raw.hex()


def _test_position(record: _Record) -> None:
    d = decode_frame(_POSITION, -114, -2)
    record(
        "position: header and flags",
        d["len"] == 115
        and d["type"] == "!"
        and d["msg_id"] == "3BAC51B8"
        and d["flags_byte"] == 0x90
        and d["server"] is True
        and d["track"] is False
        and d["app_offline"] is False
        and d["mesh"] is True
        and d["hops"] == 0
        and d["rssi"] == -114
        and d["snr"] == -2,
    )
    payload = d["payload"] or ""
    record(
        "position: path, dest, payload",
        d["path"] == ["DD7MH-55", "DB0MMR-12", "DL2JA-2"]
        and d["dest"] == "*"
        and payload.startswith("4809.90N/")
        and payload.endswith("R=9;26386;"),
    )
    record(
        "position: trailer and FCS",
        d["hw"] == 39
        and d["mod_country"] == 8
        and d["mod"] == 8
        and d["fcs"] == "1BCC"
        and d["fcs_calc"] == "1BCC"
        and d["fcs_ok"] is True
        and d["fw"] == 40
        and d["lh_hw"] == 43
        and d["fw_sub"] == "a"
        and d["error"] is None,
    )


def _test_hey(record: _Record) -> None:
    d = decode_frame(_TX_HEY)
    record(
        "own HEY (guide TX_FRAME): FCS 06DB ok, H04 mesh",
        d["type"] == "@"
        and d["msg_id"] == "1AE1E034"
        and d["hops"] == 4
        and d["mesh"] is True
        and d["server"] is False
        and d["path"] == ["DK5EN-98"]
        and d["dest"] == "HG"
        and d["payload"] == "R6;"
        and d["fcs"] == "06DB"
        and d["fcs_ok"] is True
        and d["rssi"] is None
        and d["error"] is None,
    )
    r = decode_frame(_RX_HEY, -113, -2)
    record(
        "relayed HEY: three-hop path, H02 S0, matches [LOG] line",
        r["msg_id"] == "A183638F"
        and r["hops"] == 2
        and r["server"] is False
        and r["path"] == ["DF8RD-1", "DB0ISM-1", "DB0ED-99"]
        and r["dest"] == "HG"
        and r["payload"] == "R3;8,99,4;9,122,-14;"
        and r["hw"] == 43
        and r["fcs"] == "0E35"
        and r["fcs_ok"] is True
        and r["fw"] == 40
        and r["lh_hw"] == 43,
    )


def _test_text(record: _Record) -> None:
    dm = decode_frame(_build((0x3A, 0x1AE1E057, 0x14), "DK5EN-98", "DL2JA-2", b"Hallo Welt{087"))
    record(
        "text DM: dest callsign, payload keeps {NNN, fcs ok",
        dm["type"] == ":"
        and dm["msg_id"] == "1AE1E057"
        and dm["dest"] == "DL2JA-2"
        and dm["payload"] == "Hallo Welt{087"
        and dm["fcs_ok"] is True
        and dm["error"] is None,
    )
    g = decode_frame(_GROUP_TEXT)
    record(
        "group text: real frame, dest 262, FCS ok, no exception on UTF-8 bytes",
        g["dest"] == "262"
        and g["fcs_ok"] is True
        and g["path"] == ["DO2QG-1", "DK5EN-98", "DK5EN-92"]
        and (g["payload"] or "").startswith("Ich w")
        and g["error"] is None,
    )
    nonutf = decode_frame(_build((0x3A, 1, 0x10), "DK5EN-98", "*", b"\xfc\xff\x80x"))
    record(
        "non-UTF-8 payload bytes decode (latin-1) and FCS still ok",
        nonutf["payload"] == "\xfc\xff\x80x"
        and nonutf["fcs_ok"] is True
        and nonutf["error"] is None,
    )


def _test_ack(record: _Record) -> None:
    d = decode_frame(_ACK, -116, -4)
    record(
        "ACK (guide 5.4): own id, acked id, server flag, no FCS",
        d["type"] == "A"
        and d["len"] == 12
        and d["msg_id"] == "00F76491"
        and d["acked_id"] == "03B50343"
        and d["flags_byte"] == 0x81
        and d["server"] is True
        and d["hops"] == 1
        and d["path"] is None
        and d["fcs"] is None
        and d["fcs_ok"] is None
        and d["error"] is None,
    )
    short = decode_frame("41" + "9164F700" + "81" + "01")
    record(
        "ACK short form: decodes what is there, sets error",
        short["msg_id"] == "00F76491"
        and short["flags_byte"] == 0x81
        and short["error"] is not None,
    )


def _test_corruption(record: _Record) -> None:
    for label, hx in (("TX_FRAME", _TX_HEY), ("position", _POSITION)):
        bad = decode_frame(_flip(hx, 8))
        good = decode_frame(hx)
        record(
            f"corrupted {label}: fcs_ok False, calc differs, still parsed",
            bad["fcs_ok"] is False
            and bad["fcs"] == good["fcs"]
            and bad["fcs_calc"] != bad["fcs"]
            and bad["error"] is None,
        )


def _test_malformed(record: _Record) -> None:
    odd = decode_frame(_TX_HEY[:-1])
    record("odd-length hex -> bad hex", odd["error"] == "bad hex" and odd["len"] == 0)
    inv = decode_frame("40ZZ")
    record("invalid hex characters -> bad hex", inv["error"] == "bad hex")
    empty = decode_frame("")
    record("empty hex -> too short", empty["error"] == "too short" and empty["len"] == 0)
    short = decode_frame("3A34E0E11A14444B35")
    record(
        "non-ACK < 16 bytes -> too short, msg_id still filled",
        short["error"] == "too short" and short["msg_id"] == "1AE1E034" and short["len"] == 9,
    )
    # Cut inside the payload: no 0x00 terminator
    cut_payload = decode_frame(_TX_HEY[: 2 * 20])
    record(
        "cut inside payload -> no terminator, path/dest kept",
        cut_payload["error"] == "no terminator"
        and cut_payload["path"] == ["DK5EN-98"]
        and cut_payload["dest"] == "HG"
        and cut_payload["fcs_ok"] is None,
    )
    # Cut inside the path: no '>' within data
    cut_path = decode_frame(_POSITION[: 2 * 20])
    record("cut inside path -> no terminator", cut_path["error"] == "no terminator")
    # Cut inside the trailer (after payload 0x00; HW at byte 22, FCS ends at 26): truncated
    cut_trailer = decode_frame(_TX_HEY[: 2 * 24])
    record(
        "cut inside trailer -> truncated",
        cut_trailer["error"] == "truncated" and cut_trailer["fcs_ok"] is None,
    )
    # Trailer without fw/lh/sub (just through FCS) is valid, fields None
    bare = decode_frame(_TX_HEY[: 2 * 26])
    record(
        "frame ending right after FCS: ok, fw/lh/sub None",
        bare["fcs_ok"] is True
        and bare["fw"] is None
        and bare["lh_hw"] is None
        and bare["fw_sub"] is None,
    )
    unk = decode_frame("7F" + "00" * 20)
    record(
        "unknown type byte -> error, no exception",
        unk["error"] == "unknown type" and unk["type"] == "0x7F",
    )
    ws = decode_frame("  " + _ACK[:12] + " \n" + _ACK[12:])
    record("whitespace inside hex is tolerated", ws["error"] is None and ws["msg_id"] == "00F76491")


def _test_never_raises(record: _Record) -> None:
    ok = True
    # Every prefix of every fixture, plus single-byte garbage, must return a dict.
    for hx in (_POSITION, _TX_HEY, _RX_HEY, _GROUP_TEXT, _ACK):
        for cut in range(0, len(hx) + 1, 2):
            try:
                d = decode_frame(hx[:cut])
            except Exception:  # the test is that nothing escapes
                ok = False
                continue
            ok = ok and isinstance(d["len"], int)
    for hx in ("00", "3A", "21FFFFFFFFFF", "40" + "FF" * 30, "41"):
        try:
            decode_frame(hx)
        except Exception:  # the test is that nothing escapes
            ok = False
    record("never raises on any prefix or garbage", ok)


async def run_frame_decode_tests() -> bool:
    """Return True iff every frame decoder regression case passes."""
    if has_console:
        print("\nTesting frame_decode (LoRa frame -> DecodedFrame):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'PASS' if ok else 'FAIL'} | {label}")

    _test_position(_record)
    _test_hey(_record)
    _test_text(_record)
    _test_ack(_record)
    _test_corruption(_record)
    _test_malformed(_record)
    _test_never_raises(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\nframe_decode Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run_frame_decode_tests()) else 1)
