"""Stateless parser for one MeshCom firmware console line (RF Monitor `frame.parsed`).

`parse_console_line(text)` turns a single raw console line (meshlogger prefix already
stripped, no trailing newline needed) into the `parsed` object of the wire contract in the
webapp repo (`docs/rf-monitor-plan.md`, "Parsed console lines"). The firmware reference is
`how-to-read-meshcom-logs.md` (MeshCom fork 4.40a).

Contract points worth knowing before editing:

- Pure and stateless: one line in, one dict out, stdlib only, no I/O. The two `[MC-STAT]`
  continuation lines are recognised by their own shape (two leading spaces + `tx:`/`drop:`).
- NEVER raises. Any failure mid-parse degrades to `family="other"`, `kind=None`,
  `fields={}`. `None` is returned only for an empty / whitespace-only input.
- `frame` (decoded hex of `[MC-TEST] RX_FRAME/TX_FRAME`) is NOT produced here; this module
  only puts `len`/`rssi`/`snr`/`hex` into `fields`. The caller attaches `frame`.
- `fields` use the firmware's own `key=` names lower-cased. A value is a number when the
  whole value is one (trailing unit `ms`/`s`/`%` stripped), else a string. Message ids keep
  their 8 upper-case hex digits as a string without the `x`.
- Accepts the old `MH-LoRa:` tag with a colon and the `[HEAP]` `;`-or-space layouts
  (guide 6.2, 6.11).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import NotRequired, TypedDict

FieldValue = str | int | float | bool | list[int] | None


class PacketLine(TypedDict):
    tag: str
    len: int
    type: str
    msg_id: str
    hops: int
    server: bool | None
    track: bool | None
    mesh: bool | None
    path: list[str]
    dest: str | None
    payload: str | None
    hw: int | None
    mod_country: int | None
    mod: int | None
    fcs: str | None
    fw: int | None
    fw_sub: str | None
    lh_hw: int | None
    acked_id: str | None
    rssi: float | None
    snr: float | None
    dup: bool | None
    own_echo: bool | None
    t_ms: int | None


class ConsoleParsed(TypedDict):
    family: str
    kind: str | None
    node_time: str | None
    noise: bool
    severity: str
    fields: dict[str, FieldValue]
    packet: NotRequired[PacketLine]
    # Filled by the caller (frame_decode) for mc-test RX_FRAME / TX_FRAME; never here.
    frame: NotRequired[dict[str, object]]


# ── regexes ────────────────────────────────────────────────────────────────

_TIME_PREFIX_RE = re.compile(r"^(\d\d:\d\d:\d\d)(?:[ ;]+(.*))?$")
_STAT_CONT_RE = re.compile(r"^ {2}(tx|drop):(?:\s+(.*))?$")
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)([=:])(.*)$")
_NUM_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(?:ms|s|%)?$")
_BRACKET_TAG_RE = re.compile(r"^\[([A-Za-z0-9-]+)\]")

_PACKET_TAGS = (
    "MH-LoRa",
    "RX-LoRa2",
    "TX-LoRa",
    "TX-Lora",
    "RX-UDP",
    "TX-UDP",
    "NEW-HEY",
    "NEW-POS",
    "NEW-TXT",
    "NEW-PING",
)
_PACKET_TAG_RE = re.compile(r"^(" + "|".join(_PACKET_TAGS) + r"):?(?=\s)\s*(.*)$")

_HEAD_RE = re.compile(r"^(\d+)\s+(\S)\s+x([0-9A-Fa-f]{8})\s*(.*)$")
_ACK_RE = re.compile(
    r"^H([0-9A-Fa-f]{2})\s+x([0-9A-Fa-f]{8})(?:\s+([0-9A-Fa-f]{2}))?(?:\s+([0-9A-Fa-f]{2}))?\s*(.*)$"
)
_FLAGS_RE = re.compile(r"^H([0-9A-Fa-f]{2})\s+S(\d)\s+T(\d)\s+M([0-9A-Fa-f]{2})\s+(.*)$")
_TAIL_RE = re.compile(
    r"(?:\s+(?:RSSI:-?\d+(?:\.\d+)?|SNR:-?\d+(?:\.\d+)?|DUP:\S|OWN:\S|t=\d+))+\s*$"
)
_TAIL_ITEM_RE = re.compile(r"(RSSI|SNR|DUP|OWN):(\S+)|t=(\d+)")
_TRAILER_RE = re.compile(
    r"^(.*?)\s+HW:(\d+)\s+MOD:([0-9A-Fa-f])/(\d+)\s+FCS:([0-9A-Fa-f]{4})"
    r"\s+FW:(\d+):(\S)\s+LH:([0-9A-Fa-f]{2})\s*$"
)

_LOG_POS_RE = re.compile(r"^x([0-9A-Fa-f]{8})\s+(\S)\s+H([0-9A-Fa-f]{2})\s*(.*)$")
_MC_EVENT_RE = re.compile(r"^([A-Z][A-Z0-9_]*)(?:\[(\d+)\])?:?(?:\s+(.*))?$")
_SM_RE = re.compile(r"^(\S+)\s*->\s*(\S+)(?:\s+rc=(-?\d+))?")
_HEAP_RE = re.compile(
    r"^\[HEAP\][; ]+(?:(\d\d:\d\d:\d\d)[; ]+)?(\d+)[; ]+(\d+)[; ]+(\d+)[; ]*\(?(init|mon)\)?"
)
_LORA_RX_RE = re.compile(
    r"Received packet:\s*RSSI:\s*(-?\d+(?:\.\d+)?)\s*dBm\s*/\s*SNR:\s*(-?\d+(?:\.\d+)?)\s*dB"
    r"(?:\s*/\s*Frequency error:\s*(-?\d+(?:\.\d+)?)\s*Hz)?"
)
_EXT_LEN_RE = re.compile(r"Len:\s*(\d+)\s*$")
_TIMECLIENT_RE = re.compile(r"^TimeClient now \(UTC\):\s*(\d\d:\d\d:\d\d)")
_TRICKLE_CONS_RE = re.compile(r"consistent=(\d+)[<>=]+k=(\d+)")
_BOOT_RE = re.compile(r"^(?:\[BOOT\]|\.\.\.BOOT)(?=[; ]|$)[; ]*(.*)$")

# ── constants ──────────────────────────────────────────────────────────────

_STR_KEYS = frozenset({"msg_id", "retid", "hex", "from", "to", "call", "bssid", "reason_name"})
_ID_KEYS = frozenset({"msg_id", "retid"})

_NOISE_DBG = frozenset(
    {
        "RX_TIMEOUT_FIRE",
        "RX_RESTART",
        "RX_RESTARTED",
        "RX_FLAG_PROCESS",
        "TX_GATE_ENTER",
        "TX_START",
        "CAD_FREE",
        "RADIO_TX",
        "RING_PRIO",
    }
)
_NOISE_WORDS = frozenset({"receiveflag", "transmittedFlag", "OnTXDone", "OnRxDone"})
_NOISE_PREFIXES = ("[CHECK]", "checkOwnTx:", "Insert own_msg_id:")
_MESH_NOISE_RE = re.compile(r"^MESH\s+:")

_ALARM_DBG = frozenset({"RING_DROP_NEW", "RING_OVERFLOW", "RING_REJECT", "RETRANSMIT_GIVEUP_DM"})
_WARN_DBG = frozenset(
    {
        "RX_OTHER_ERROR",
        "CRC_ERROR",
        "RING_DROP_PRIO",
        "RING_DROP_STALE",
        "RETRANSMIT_GIVEUP",
    }
)
_BOOT_ALARM_NAMES = frozenset({"PANIC", "INT_WDT", "TASK_WDT", "BROWNOUT"})
_LOG_KINDS = frozenset({"RLY", "TX", "ERR", "STAT", "DM", "MBOX", "GWI", "GWU"})

_TAG_FAMILY = {
    "MC-DBG": "mc-dbg",
    "MC-SM": "mc-sm",
    "MC-TEST": "mc-test",
    "MC-STAT": "mc-stat",
    "MC-PRIO": "mc-prio",
    "MC-HWM": "mc-hwm",
    "MC-TRICKLE": "mc-trickle",
    "MC-WDT": "mc-wdt",
    "MC-WARN": "mc-warn",
}
_SERVER_TAGS = frozenset({"KEEP", "DATA", "BEAT", "GW"})
_NET_TAGS = frozenset({"WIFI", "ETH", "NTP"})

_LH_MASK = 0x7F
_DROP_CLASSES = 5
_UTIL_WARN = 70
_SEV_ORDER = {"info": 0, "warn": 1, "alarm": 2}


# ── helpers ────────────────────────────────────────────────────────────────


def _coerce(key: str, raw: str) -> FieldValue:
    if key in _ID_KEYS:
        return raw[1:].upper() if raw[:1] in ("x", "X") else raw.upper()
    if key in _STR_KEYS:
        return raw
    m = _NUM_RE.match(raw)
    if m:
        num = m.group(1)
        return float(num) if "." in num else int(num)
    return raw


def _kv(text: str | None) -> dict[str, FieldValue]:
    """`key=value` / `key:value` tokens -> fields (lower-cased keys, first delimiter wins)."""
    out: dict[str, FieldValue] = {}
    if not text:
        return out
    for tok in text.split():
        m = _KV_RE.match(tok)
        if not m or not m.group(3):
            continue
        key = m.group(1).lower()
        out[key] = _coerce(key, m.group(3))
    return out


def _semi_fields(text: str) -> dict[str, FieldValue]:
    """`event;[sub;]k;v;k;v` firmware records (raw Serial.printf layout)."""
    toks = text.split(";")
    while toks and toks[-1] == "":
        toks.pop()
    out: dict[str, FieldValue] = {}
    if not toks:
        return out
    out["event"] = toks[0]
    rest = toks[1:]
    if len(rest) % 2 == 1:
        out["sub"] = rest[0]
        rest = rest[1:]
    for i in range(0, len(rest), 2):
        key = rest[i].lower()
        if key:
            out[key] = _coerce(key, rest[i + 1])
    return out


def _tonum(raw: str) -> int | float:
    return float(raw) if "." in raw else int(raw)


def _num(v: FieldValue) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _max_sev(a: str, b: str) -> str:
    return a if _SEV_ORDER[a] >= _SEV_ORDER[b] else b


def _mk(  # noqa: PLR0913 - one flat constructor for every family; keyword-only extras
    family: str,
    kind: str | None,
    node_time: str | None,
    *,
    noise: bool = False,
    severity: str = "info",
    fields: dict[str, FieldValue] | None = None,
    packet: PacketLine | None = None,
) -> ConsoleParsed:
    out: ConsoleParsed = {
        "family": family,
        "kind": kind,
        "node_time": node_time,
        "noise": noise,
        "severity": severity,
        "fields": fields if fields is not None else {},
    }
    if packet is not None:
        out["packet"] = packet
    return out


# ── packet lines (grammar 2.1 - 2.3) ───────────────────────────────────────


def _parse_packet(tag: str, rest: str) -> PacketLine | None:
    """Parse `<len> <t> x<msgid> ...` (everything after the tag). None if it does not fit."""
    head = _HEAD_RE.match(rest)
    if not head:
        return None
    length = int(head.group(1))
    ptype = head.group(2)
    msg_id = head.group(3).upper()
    tail_txt = ""
    body = head.group(4)
    tm = _TAIL_RE.search(body)
    if tm:
        tail_txt = tm.group(0)
        body = body[: tm.start()]

    pkt: PacketLine = {
        "tag": tag,
        "len": length,
        "type": ptype,
        "msg_id": msg_id,
        "hops": 0,
        "server": None,
        "track": None,
        "mesh": None,
        "path": [],
        "dest": None,
        "payload": None,
        "hw": None,
        "mod_country": None,
        "mod": None,
        "fcs": None,
        "fw": None,
        "fw_sub": None,
        "lh_hw": None,
        "acked_id": None,
        "rssi": None,
        "snr": None,
        "dup": None,
        "own_echo": None,
        "t_ms": None,
    }

    if ptype == "A":
        ack = _ACK_RE.match(body)
        if not ack:
            return None
        pkt["hops"] = int(ack.group(1), 16)  # whole byte 5 (guide 2.3)
        pkt["acked_id"] = ack.group(2).upper()
    else:
        fl = _FLAGS_RE.match(body)
        if not fl:
            return None
        pkt["hops"] = int(fl.group(1), 16) & 0x0F
        pkt["server"] = fl.group(2) != "0"
        pkt["track"] = fl.group(3) != "0"
        pkt["mesh"] = int(fl.group(4), 16) != 0
        _apply_main(pkt, fl.group(5))
    _apply_tail(pkt, tail_txt)
    return pkt


def _apply_main(pkt: PacketLine, main: str) -> None:
    """Trailer (HW/MOD/FCS/FW/LH) and `path>dest<t>payload` of a non-ACK packet line."""
    tr = _TRAILER_RE.match(main)
    if tr:
        main = tr.group(1)
        pkt["hw"] = int(tr.group(2))
        pkt["mod_country"] = int(tr.group(3), 16)
        pkt["mod"] = int(tr.group(4))
        pkt["fcs"] = tr.group(5)
        pkt["fw"] = int(tr.group(6))
        pkt["fw_sub"] = tr.group(7)
        pkt["lh_hw"] = int(tr.group(8), 16) & _LH_MASK  # drop the 0x80 marker bit
    gt = main.find(">")
    if gt < 0:
        return
    pkt["path"] = [p for p in main[:gt].split(",") if p]
    after = main[gt + 1 :]
    ti = after.find(pkt["type"])
    if ti >= 0:
        pkt["dest"] = after[:ti]
        pkt["payload"] = after[ti + 1 :] or None
    else:
        pkt["dest"] = after


def _apply_tail(pkt: PacketLine, tail_txt: str) -> None:
    """`[LOG]` RX tail: RSSI/SNR/DUP/OWN/t=."""
    for item in _TAIL_ITEM_RE.finditer(tail_txt):
        name, val, tms = item.group(1), item.group(2), item.group(3)
        if tms is not None:
            pkt["t_ms"] = int(tms)
        elif name == "RSSI":
            pkt["rssi"] = _tonum(val)
        elif name == "SNR":
            pkt["snr"] = _tonum(val)
        elif name == "DUP":
            pkt["dup"] = val == "d"
        elif name == "OWN":
            pkt["own_echo"] = val == "e"


def _ack_fields(rest: str) -> dict[str, FieldValue]:
    head = _HEAD_RE.match(rest)
    if not head or head.group(2) != "A":
        return {}
    body = _TAIL_RE.sub("", head.group(4))
    ack = _ACK_RE.match(body)
    if not ack:
        return {}
    out: dict[str, FieldValue] = {}
    if ack.group(3):
        out["b10"] = ack.group(3).upper()
    if ack.group(4):
        out["b11"] = ack.group(4).upper()
    return out


# ── family parsers ─────────────────────────────────────────────────────────


def _stat_severity(fields: dict[str, FieldValue]) -> str:
    """`[LOG] STAT`: also adds `drop_by_prio` to `fields`."""
    sev = "info"
    drop = fields.get("drop")
    if isinstance(drop, str):
        parts = drop.split("/")
        if len(parts) == _DROP_CLASSES and all(p.isdigit() for p in parts):
            by_prio = [int(p) for p in parts]
            fields["drop_by_prio"] = by_prio
            if by_prio[0] > 0 or by_prio[1] > 0:
                sev = "alarm"
            elif any(n > 0 for n in by_prio[2:]):
                sev = "warn"
    txfail = _num(fields.get("txfail"))
    if txfail is not None and txfail > 0:
        sev = "alarm"
    return sev


def _parse_log(rest: str, nt: str | None) -> ConsoleParsed:
    rest = rest.strip()
    if rest[:1].isdigit():
        pkt = _parse_packet("[LOG]", rest)
        if pkt is None:
            return _mk("log", "RX", nt)
        if pkt["type"] == "A":
            return _mk("log", "ACK", nt, fields=_ack_fields(rest), packet=pkt)
        return _mk("log", "RX", nt, packet=pkt)

    word, _, args = rest.partition(" ")
    kind = word if word in _LOG_KINDS else None
    fields: dict[str, FieldValue] = {}
    sev = "info"

    if kind in ("RLY", "TX", "GWI", "GWU"):
        pos = _LOG_POS_RE.match(args.strip())
        if pos:
            fields["msg_id"] = pos.group(1).upper()
            fields["type"] = pos.group(2)
            fields["h"] = int(pos.group(3), 16)
            fields.update(_kv(pos.group(4)))
        else:
            fields.update(_kv(args))
        if kind == "RLY" and fields.get("q") == "full":
            sev = "warn"
    elif kind in ("ERR", "DM", "MBOX"):
        fields.update(_kv(args))
        if kind == "ERR":
            sev = "warn"
    elif kind == "STAT":
        fields.update(_kv(args))
        sev = _stat_severity(fields)
    return _mk("log", kind, nt, severity=sev, fields=fields)


def _first_word_kind(rest: str) -> tuple[str | None, str]:
    """`SEND consistent=0<k=2 ...` -> ("SEND", args); `p5_lat_avg=...` -> (None, rest)."""
    rest = rest.strip()
    first, _, args = rest.partition(" ")
    if first and re.fullmatch(r"[A-Z][A-Z0-9_]*", first):
        return first, args
    return None, rest


def _parse_mc_dbg(rest: str, nt: str | None) -> ConsoleParsed:
    m = _MC_EVENT_RE.match(rest.strip())
    if not m:
        return _mk("mc-dbg", None, nt, fields=_kv(rest))
    event, count, args = m.group(1), m.group(2), m.group(3)
    fields: dict[str, FieldValue] = {}
    if event in ("CRC_PAYLOAD", "ERR_PAYLOAD"):
        if count is not None:
            fields["n"] = int(count)
        if args:
            fields["hex"] = "".join(args.split())
    else:
        fields = _kv(args)
    sev = "info"
    if event in _ALARM_DBG:
        sev = "alarm"
    elif event in _WARN_DBG:
        sev = "warn"
    state = _num(fields.get("state"))
    if state is not None and state != 0 and (event.startswith("RX_RESTART") or event == "TX_DONE"):
        sev = "alarm"
    if event == "CHANNEL_UTIL":
        util = _num(fields.get("util"))
        if util is not None and util >= _UTIL_WARN:
            sev = _max_sev(sev, "warn")
    return _mk("mc-dbg", event, nt, noise=event in _NOISE_DBG, severity=sev, fields=fields)


def _parse_mc_sm(rest: str, nt: str | None) -> ConsoleParsed:
    m = _SM_RE.match(rest.strip())
    if not m:
        return _mk("mc-sm", "transition", nt, noise=True)
    src, dst = m.group(1), m.group(2)
    fields: dict[str, FieldValue] = {"from": src, "to": dst}
    sev = "info"
    if m.group(3) is not None:
        rc = int(m.group(3))
        fields["rc"] = rc
        if rc != 0 and not (src == "TX_PREPARE" and dst == "IDLE" and rc == -1):
            sev = "alarm"
    return _mk("mc-sm", "transition", nt, noise=True, severity=sev, fields=fields)


def _parse_mc_test(rest: str, nt: str | None) -> ConsoleParsed:
    kind, args = _first_word_kind(rest)
    fields = _kv(args)
    sev = "warn" if kind == "CAPTURE_DROPPED" else "info"
    return _mk("mc-test", kind, nt, severity=sev, fields=fields)


def _parse_mc_other(tag: str, rest: str, nt: str | None) -> ConsoleParsed:
    family = _TAG_FAMILY[tag]
    if tag == "MC-STAT":
        return _mk(family, "window", nt, fields=_kv(rest))
    kind, args = _first_word_kind(rest)
    fields = _kv(args)
    if tag == "MC-TRICKLE":
        cm = _TRICKLE_CONS_RE.search(args)
        if cm:
            fields["consistent"] = int(cm.group(1))
            fields["k"] = int(cm.group(2))
    sev = "alarm" if tag == "MC-WDT" else "info"
    return _mk(family, kind, nt, severity=sev, fields=fields)


def _parse_heap(body: str, nt: str | None) -> ConsoleParsed:
    m = _HEAP_RE.match(body)
    if not m:
        return _mk("heap", None, nt)
    fields: dict[str, FieldValue] = {
        "free": int(m.group(2)),
        "minfree": int(m.group(3)),
        "maxblock": int(m.group(4)),
    }
    return _mk("heap", m.group(5), nt or m.group(1), fields=fields)


def _parse_lora(body: str, nt: str | None) -> ConsoleParsed:
    rx = _LORA_RX_RE.search(body)
    if rx:
        fields: dict[str, FieldValue] = {"rssi": _tonum(rx.group(1)), "snr": _tonum(rx.group(2))}
        if rx.group(3) is not None:
            fields["freq_err"] = _tonum(rx.group(3))
        return _mk("lora", "received", nt, fields=fields)
    if "Receive Timeout" in body:
        return _mk("lora", "timeout", nt, noise=True)
    return _mk("lora", None, nt)


def _parse_ext(body: str, nt: str | None) -> ConsoleParsed:
    rest = body[len("[EXT]") :]
    if rest.startswith(";"):
        return _mk("ext", None, nt, fields=_semi_fields(rest[1:]))
    rest = rest.strip()
    kind: str | None = None
    if rest.startswith("Tele-Out:"):
        kind = "tele"
    elif rest.startswith("Out:"):
        kind = "out"
    fields: dict[str, FieldValue] = {}
    ln = _EXT_LEN_RE.search(rest)
    if ln:
        fields["len"] = int(ln.group(1))
    lo, hi = rest.find("{"), rest.rfind("}")
    if kind and 0 <= lo < hi:
        try:
            obj = json.loads(rest[lo : hi + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for key in ("src_type", "type", "src"):
                val = obj.get(key)
                if isinstance(val, str):
                    fields[key] = val
    return _mk("ext", kind, nt, fields=fields)


def _parse_boot(rest: str, nt: str | None) -> ConsoleParsed:
    fields: dict[str, FieldValue] = {}
    toks = rest.split()
    for tok in toks:
        m = _KV_RE.match(tok)
        if m and m.group(2) == "=":
            key = m.group(1).lower()
            fields[key] = _coerce(key, m.group(3))
    if "reset_reason" in fields:
        after = rest.split("RESET_REASON=", 1)[1].split()
        if len(after) > 1 and "=" not in after[1]:
            fields["reason_name"] = after[1]
    if rest and "=" not in rest.split(None, 1)[0] and ";" in rest:
        fields.update(_semi_fields(rest))
    sev = "info"
    name = fields.get("reason_name")
    if isinstance(name, str) and name.upper() in _BOOT_ALARM_NAMES:
        sev = "alarm"
    return _mk("boot", None, nt, severity=sev, fields=fields)


def _h_log(tag: str, body: str, nt: str | None) -> ConsoleParsed:
    return _parse_log(body[len(tag) + 2 :], nt)


def _h_mc(tag: str, body: str, nt: str | None) -> ConsoleParsed:
    rest = body[len(tag) + 2 :]
    if tag == "MC-DBG":
        return _parse_mc_dbg(rest, nt)
    if tag == "MC-SM":
        return _parse_mc_sm(rest, nt)
    if tag == "MC-TEST":
        return _parse_mc_test(rest, nt)
    return _parse_mc_other(tag, rest, nt)


def _h_heap(_tag: str, body: str, nt: str | None) -> ConsoleParsed:
    return _parse_heap(body, nt)


def _h_lora(_tag: str, body: str, nt: str | None) -> ConsoleParsed:
    return _parse_lora(body, nt)


def _h_ext(_tag: str, body: str, nt: str | None) -> ConsoleParsed:
    return _parse_ext(body, nt)


def _h_semi(tag: str, body: str, nt: str | None) -> ConsoleParsed:
    """`[GW];...` / `[WIFI];...` / `[KEEP]...` : tag verbatim as kind."""
    rest = body[len(tag) + 2 :]
    fields = _semi_fields(rest[1:]) if rest.startswith(";") else {}
    return _mk("net" if tag in _NET_TAGS else "server", tag, nt, fields=fields)


def _h_nbr(_tag: str, _body: str, nt: str | None) -> ConsoleParsed:
    return _mk("nbr", None, nt)


_Handler = Callable[[str, str, "str | None"], ConsoleParsed]
_HANDLERS: dict[str, _Handler] = {
    "LOG": _h_log,
    "HEAP": _h_heap,
    "LoRa": _h_lora,
    "EXT": _h_ext,
    "NBR": _h_nbr,
    **dict.fromkeys(_TAG_FAMILY, _h_mc),
    **dict.fromkeys(_SERVER_TAGS | _NET_TAGS, _h_semi),
}


def _dispatch(s: str) -> ConsoleParsed:
    cont = _STAT_CONT_RE.match(s)
    if cont:
        return _mk("mc-stat", cont.group(1), None, fields=_kv(cont.group(2)))

    nt: str | None = None
    body = s.strip()
    tm = _TIME_PREFIX_RE.match(body)
    if tm:
        nt = tm.group(1)
        body = (tm.group(2) or "").strip()
    return _dispatch_body(body, nt) if body else _mk("other", None, nt)


def _dispatch_body(body: str, nt: str | None) -> ConsoleParsed:
    pm = _PACKET_TAG_RE.match(body)
    if pm:
        pkt = _parse_packet(pm.group(1), pm.group(2))
        if pkt is None:
            return _mk("packet", pm.group(1), nt)
        fields = _ack_fields(pm.group(2)) if pkt["type"] == "A" else {}
        return _mk("packet", pm.group(1), nt, fields=fields, packet=pkt)

    bm = _BOOT_RE.match(body)
    if bm:
        return _parse_boot(bm.group(1), nt)

    tag_m = _BRACKET_TAG_RE.match(body)
    if tag_m:
        handler = _HANDLERS.get(tag_m.group(1))
        if handler:
            return handler(tag_m.group(1), body, nt)

    tc = _TIMECLIENT_RE.match(body)
    if tc:
        return _mk("net", "time", nt, fields={"utc": tc.group(1)})

    noise = (
        body in _NOISE_WORDS
        or body.startswith(_NOISE_PREFIXES)
        or _MESH_NOISE_RE.match(body) is not None
    )
    return _mk("other", None, nt, noise=noise)


def _fallback(s: str) -> ConsoleParsed:
    nt: str | None = None
    try:
        m = _TIME_PREFIX_RE.match(s.strip())
        if m:
            nt = m.group(1)
    except Exception:  # never-raise contract: the fallback itself must not fail
        nt = None
    return _mk("other", None, nt)


def parse_console_line(text: str) -> ConsoleParsed | None:
    """Parse one raw console line. None only for an empty / whitespace-only input."""
    try:
        s = text.rstrip("\r\n")
        if not s.strip():
            return None
    except Exception:  # never-raise contract (e.g. a non-str argument)
        return None
    try:
        return _dispatch(s)
    except Exception:  # never-raise contract: any parse bug degrades to "other"
        return _fallback(s)
