"""Decode a MeshCom LoRa frame from the hex of a `[MC-TEST] RX_FRAME/TX_FRAME` line.

Produces the `DecodedFrame` of the RF-monitor wire contract
(webapp `docs/rf-monitor-plan.md`, "Parsed console lines"). Layout reference:
firmware `decodeAPRS()` (`aprs_functions.cpp`) and the log guide section 5.

Frame (types 0x21 position, 0x3A text, 0x40 HEY/weather)::

    type(1) msg_id(4, little endian) flags(1) path '>' dest <type byte> payload 0x00
    hw(1) mod(1) fcs(2, big endian) [fw(1) lh(1) fw_sub(1) 0x7E]

FCS = 16-bit sum of every byte from offset 0 through the MOD byte, mod 65536.
ACK (0x41) has no path, no FCS, no trailer: type, own id(4), flags(1),
acknowledged id(4), kind(1), 0x00.

`decode_frame` never raises: on any problem it fills what it can and sets
`error`. Text bytes are decoded as latin-1, so non-UTF-8 payloads are safe.
Stdlib only.
"""

from __future__ import annotations

from typing import TypedDict

_ACK = 0x41
_TEXT_TYPES = (0x21, 0x3A, 0x40)
_MIN_FRAME = 16  # firmware rejects shorter non-ACK frames
_MAX_FIELD = 120  # firmware scan limit for path and destination
_PRINTABLE_LO = 0x20
_PRINTABLE_HI = 0x7E
_OFF_ID = 1
_OFF_FLAGS = 5
_OFF_PATH = 6
_ACK_LEN = 12
_ACK_ACKED_END = 10  # acked id occupies bytes 6..9
_TRAILER_LEN = 4  # hw, mod, fcs(2)
_END_MARKER = 0x7E


class DecodedFrame(TypedDict):
    len: int
    type: str
    msg_id: str
    flags_byte: int
    server: bool
    track: bool
    app_offline: bool
    mesh: bool
    hops: int
    path: list[str] | None
    dest: str | None
    payload: str | None
    hw: int | None
    mod_country: int | None
    mod: int | None
    fcs: str | None
    fcs_calc: str | None
    fcs_ok: bool | None
    fw: int | None
    lh_hw: int | None
    fw_sub: str | None
    acked_id: str | None
    rssi: int | float | None
    snr: int | float | None
    error: str | None


def _empty(rssi: int | float | None, snr: int | float | None) -> DecodedFrame:
    return {
        "len": 0,
        "type": "",
        "msg_id": "",
        "flags_byte": 0,
        "server": False,
        "track": False,
        "app_offline": False,
        "mesh": False,
        "hops": 0,
        "path": None,
        "dest": None,
        "payload": None,
        "hw": None,
        "mod_country": None,
        "mod": None,
        "fcs": None,
        "fcs_calc": None,
        "fcs_ok": None,
        "fw": None,
        "lh_hw": None,
        "fw_sub": None,
        "acked_id": None,
        "rssi": rssi,
        "snr": snr,
        "error": None,
    }


def _type_name(b: int) -> str:
    return chr(b) if _PRINTABLE_LO <= b <= _PRINTABLE_HI else f"0x{b:02X}"


def _le32(data: bytes, off: int) -> str:
    return f"{int.from_bytes(data[off : off + 4], 'little'):08X}"


def _set_flags(out: DecodedFrame, flags: int) -> None:
    out["flags_byte"] = flags
    out["server"] = bool(flags & 0x80)
    out["track"] = bool(flags & 0x40)
    out["app_offline"] = bool(flags & 0x20)
    out["mesh"] = bool(flags & 0x10)
    out["hops"] = flags & 0x0F


def _fill_header(data: bytes, out: DecodedFrame) -> None:
    """msg_id and flags, as far as the bytes reach."""
    if len(data) > _OFF_ID + 3:
        out["msg_id"] = _le32(data, _OFF_ID)
    if len(data) > _OFF_FLAGS:
        _set_flags(out, data[_OFF_FLAGS])


def _scan(data: bytes, start: int, stop_at: int) -> int | None:
    """Index of `stop_at` from `start`; None on a non-printable byte, the limit or the end."""
    end = min(len(data), start + _MAX_FIELD)
    for i in range(start, end):
        b = data[i]
        if b == stop_at:
            return i
        if b < _PRINTABLE_LO or b > _PRINTABLE_HI:
            return None
    return None


def _decode_ack(data: bytes, out: DecodedFrame) -> None:
    n = len(data)
    _fill_header(data, out)
    if n >= _ACK_ACKED_END:
        out["acked_id"] = _le32(data, _OFF_PATH)
    if n < _ACK_LEN:
        out["error"] = "short ack" if n >= _ACK_ACKED_END else "truncated"


def decode_frame(
    hex_str: str,
    rssi: int | float | None = None,
    snr: int | float | None = None,
) -> DecodedFrame:
    """Decode the hex of a LoRa frame. Never raises; see module docstring."""
    out = _empty(rssi, snr)
    try:
        _decode(hex_str, out)
    except Exception as exc:  # contract: decode_frame never raises
        out["error"] = f"decode failed: {type(exc).__name__}"
    return out


def _parse_hex(hex_str: str) -> bytes | None:
    cleaned = "".join(hex_str.split())
    if len(cleaned) % 2:
        return None
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return None


def _decode(hex_str: str, out: DecodedFrame) -> None:
    data = _parse_hex(hex_str)
    if data is None:
        out["error"] = "bad hex"
        return
    out["len"] = len(data)
    if not data:
        out["error"] = "too short"
        return
    ptype = data[0]
    out["type"] = _type_name(ptype)
    if ptype == _ACK:
        _decode_ack(data, out)
        return
    _fill_header(data, out)
    if len(data) < _MIN_FRAME:
        out["error"] = "too short"
    elif ptype not in _TEXT_TYPES:
        out["error"] = "unknown type"
    else:
        _decode_body(data, ptype, out)


def _decode_body(data: bytes, ptype: int, out: DecodedFrame) -> None:
    gt = _scan(data, _OFF_PATH, 0x3E)
    if gt is None:
        out["error"] = "no terminator"
        out["path"] = data[_OFF_PATH : _OFF_PATH + _MAX_FIELD].decode("latin-1").split(",")
        return
    out["path"] = data[_OFF_PATH:gt].decode("latin-1").split(",")

    dend = _scan(data, gt + 1, ptype)
    if dend is None:
        out["error"] = "no terminator"
        return
    out["dest"] = data[gt + 1 : dend].decode("latin-1")

    pend = data.find(b"\x00", dend + 1)
    if pend < 0:
        out["payload"] = data[dend + 1 :].decode("latin-1")
        out["error"] = "no terminator"
        return
    out["payload"] = data[dend + 1 : pend].decode("latin-1")
    _decode_trailer(data, pend + 1, out)


def _decode_trailer(data: bytes, t: int, out: DecodedFrame) -> None:
    n = len(data)
    if t + _TRAILER_LEN > n:
        out["error"] = "truncated"
        return
    out["hw"] = data[t]
    out["mod_country"] = data[t + 1] >> 4
    out["mod"] = data[t + 1] & 0x0F
    fcs = (data[t + 2] << 8) | data[t + 3]
    calc = sum(data[: t + 2]) & 0xFFFF
    out["fcs"] = f"{fcs:04X}"
    out["fcs_calc"] = f"{calc:04X}"
    out["fcs_ok"] = fcs == calc

    i = t + _TRAILER_LEN
    if i < n:
        out["fw"] = data[i]
    if i + 1 < n:
        out["lh_hw"] = data[i + 1] & 0x7F
    if i + 2 < n:
        out["fw_sub"] = "#" if data[i + 2] in (0x00, _END_MARKER) else chr(data[i + 2])
