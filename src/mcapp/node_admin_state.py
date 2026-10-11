"""Last known state of a remote node, folded from verified RM1 reply rows.

Pure: no I/O, no clock reads (`now_ms` is a parameter). Plan:
doc/2026-10-06_1100-node-admin-remote-view-concept.md sections 3.3 and 4.3, contract in
Appendix A; the service adds the connection / pacing keys around `fold()`.

Evidence rules (all from concept 4.3):

* Only `verified == 1` rows are evidence. A `bad_tag` row keeps its text but never feeds state.
* Order is SEND order (log `id`), never `reply_at`: the node runs a command only above its
  high-water mark, so a verified reply to row N describes the state right after N however late
  it arrives. A late status must not override a newer ack. `reply_at` is reported as `at` only.
* The newest parsed status is an atomic snapshot of every status-owned field and switch (a key
  absent from it becomes unknown); acks with a higher id apply on top. Implemented as one pass
  in id order in which a parsed status resets the status-owned state.

The tokenizer and status parser mirror the firmware (`rm_sender_policy.h` `rmStatusParse`,
fork-dev): the compact form
`ok v=<ver> up=<min> bat=<%> heap=<kB> s=<letters> p=<cur>/<min>/<max>[ led=<0|1>]` (newer
firmware; the older `p=<cur>/<max>` is still accepted, without a minimum) and the older
`... gw=<0|1> mesh=<0|1>[ led=<0|1>]` form (only those switches known). The `D` letter means
display ON; the BLE page inverts `SN.DISP`, which is a different source and must not be copied.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypedDict, cast

from .remote_cmd import parse_sync_hwm

# Firmware order of the `s=` letters and of the switch tiles.
SWITCHES: Final = ("gps", "track", "display", "mesh", "gateway", "led")
_LETTERS: Final = "GTDMWL"

# Reply types whose body may carry the `rm=<n>` capability token. ONE place to change: the
# firmware decision (which reply carries it) is pending. A reply of any other command never
# changes capability.
CAPABILITY_CARRIERS: Final = frozenset({"sync"})

# Write/extended commands (concept 2.2, Phase B). A no_reply of one resets the capability to 1:
# the node may be an older firmware that silently rejects the unknown verb.
PHASE_B_COMMANDS: Final = frozenset(
    {"radio", "name", "atxt", "pos", "sens", "mh", "txq", "mbox", "maxhop"}
)

_PENDING_STATES: Final = frozenset({"queued", "waiting"})
_SILENT_STATES: Final = frozenset({"no_reply", "send_failed", "bad_tag", "abandoned"})

_INT_RE: Final = re.compile(r"-?[0-9]{1,9}")
# `p=<cur>/<max>` (older firmware) or `p=<cur>/<min>/<max>`; group 2 is the minimum.
_P_RE: Final = re.compile(r"(-?[0-9]{1,4})(?:/(-?[0-9]{1,4}))?/(-?[0-9]{1,4})")

RowState = Callable[[dict[str, Any], int], str]

# A field value: scalars for the status-owned fields, JSON-shaped maps / lists for the read
# commands (radio, sens, pos, txq, mbox, maxhop, mh, mh_call).
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

# Fields owned by a status snapshot: a status resets exactly these, never the read fields.
STATUS_FIELDS: Final = (
    "firmware",
    "uptime_min",
    "battery_pct",
    "heap_kb",
    "tx_power",
    "tx_power_min",
    "tx_power_max",
)

_FLOAT_RE: Final = re.compile(r"-?[0-9]{1,6}(?:\.[0-9]{1,6})?")
_COUNT_RE: Final = re.compile(r"[0-9]{1,5}[kM]?")
_RATIO_RE: Final = re.compile(r"([0-9]{1,6})/([0-9]{1,6})")
# Firmware rm_format.h: a call is A-Z 0-9 `-`, every other byte is `?`, and a row / rc call longer
# than 9 chars is emitted as exactly `?` (a churned-out mh row keeps its position as `?` too).
_CALL_RE: Final = re.compile(r"[A-Z0-9?-]{1,9}")
_POS_PARTS: Final = 4
_MH_HEAD: Final = 2  # `<total> <next>` before the call / minutes pairs
_BP: Final = frozenset({"quiet", "qrs", "qrt", "?"})
_MBOX_MODES: Final = frozenset({"off", "own", "list", "heard"})
_POS_SRC: Final = frozenset({"gps", "nofix", "set"})
_READ_COMMANDS: Final = frozenset(
    {"radio", "sens", "name", "atxt", "pos", "txq", "mbox", "maxhop", "chgled", "mh"}
)
_UNSUPPORTED: Final[dict[str, JsonValue]] = {"unsupported": True}
_HIDDEN: Final[dict[str, JsonValue]] = {"hidden": True}
# (cmd, whole result) of the `err` replies that ARE evidence about the node (firmware
# rm_exec_read.cpp / rm_runtime.cpp: no sensor / mailbox / PMU charge LED, or a node that does
# not beacon its position)
_ERR_MARKERS: Final[dict[tuple[str, str], dict[str, JsonValue]]] = {
    ("sens", "err unsupported"): _UNSUPPORTED,
    ("mbox", "err unsupported"): _UNSUPPORTED,
    ("chgled", "err unsupported"): _UNSUPPORTED,
    ("pos", "err hidden"): _HIDDEN,
}


class FieldEntry(TypedDict):
    value: JsonValue
    at: int | None
    log_id: int
    stale: bool


class SwitchEntry(TypedDict):
    """`state`: on | off | flash | on_or_flash | uncertain | pending | unknown.

    `flash` comes only from a verified `led flash` ack (or a status `L` that confirms it);
    `on_or_flash` is a status `L` with no led ack to tell the two apart (firmware `bRemoteLed`
    is true for both).

    `stale` is an addition to Appendix A: a switch entry carries it like a field does (a reboot
    boundary lies after its evidence).
    """

    state: str
    at: int | None
    log_id: int | None
    stale: bool


class RegisterEntry(TypedDict):
    state: str  # never | ok | unparsed | pending
    at: int | None
    log_id: int | None
    raw: str | None


class NodeStateDict(TypedDict):
    as_of_id: int
    capability: int
    has_led: bool | None  # None = not known yet
    fields: dict[str, FieldEntry]
    switches: dict[str, SwitchEntry]
    registers: dict[str, RegisterEntry]


# ---------------------------------------------------------------------------
# tokenizer and status parser
# ---------------------------------------------------------------------------


def tokenize(body: object) -> dict[str, str]:
    """Split a reply body into `key=value` tokens (draft-2 grammar). Never raises.

    Tokens are separated by single spaces (runs are tolerated). A token without `=` (free text,
    which the grammar puts last) or with an empty key is IGNORED, not kept positionally. A value
    of `-` means "absent" and the key is left out. A repeated key keeps its FIRST value, like the
    firmware parser.
    """
    out: dict[str, str] = {}
    if not isinstance(body, str):
        return out
    for tok in body.split(" "):
        key, sep, value = tok.partition("=")
        if not sep or not key or value == "-" or key in out:
            continue
        out[key] = value
    return out


@dataclass(frozen=True)
class StatusInfo:
    """A parsed `status` result. `None` = the token was absent or unreadable (unknown)."""

    version: str | None
    up_min: int | None
    bat_pct: int | None
    heap_kb: int | None
    tx_cur: int | None
    tx_min: int | None  # None for the older two-part `p=<cur>/<max>`
    tx_max: int | None
    gps: bool | None
    track: bool | None
    display: bool | None
    mesh: bool | None
    gateway: bool | None
    led: bool | None
    has_led: (
        bool | None
    )  # True: L letter or led=; False: 5-letter compact form; None: old form, no led=


def _int(value: str | None) -> int | None:
    if value is None or _INT_RE.fullmatch(value) is None:
        return None
    return int(value)


def _letters(token: str) -> list[bool | None] | None:
    """`s=` letters -> six switch values, or None when the token is invalid."""
    cnt = len(token)
    if cnt not in (len(_LETTERS), len(_LETTERS) - 1):
        return None
    sw: list[bool | None] = [None] * len(_LETTERS)
    for i, ch in enumerate(token):
        if ch == _LETTERS[i]:
            sw[i] = True
        elif ch == _LETTERS[i].lower():
            sw[i] = False
        else:
            return None
    return sw


def _bit(value: str | None) -> bool | None:
    return None if value not in ("0", "1") else value == "1"


def parse_status(result: object) -> StatusInfo | None:
    """Parse a `status` result (with its `ok ` prefix) or return None.

    Acceptance mirrors `rmStatusParse`: the text must start `ok v=`; a malformed `s=` (wrong
    length, wrong letter or order) rejects the whole status; a malformed `p=`, `up=` etc. is
    ignored (unknown).
    """
    if not isinstance(result, str) or not result.startswith("ok v="):
        return None
    toks = tokenize(result[3:])
    sw: list[bool | None] = [None] * len(_LETTERS)
    has_led: bool | None = None
    if "s" in toks:
        parsed = _letters(toks["s"])
        if parsed is None:
            return None
        sw = parsed
        has_led = len(toks["s"]) == len(_LETTERS)
    if _bit(toks.get("led")) is not None:
        has_led = True
        if sw[5] is None:
            sw[5] = _bit(toks.get("led"))
    if sw[4] is None:
        sw[4] = _bit(toks.get("gw"))
    if sw[3] is None:
        sw[3] = _bit(toks.get("mesh"))
    pm = _P_RE.fullmatch(toks.get("p", ""))
    return StatusInfo(
        version=toks.get("v") or None,
        up_min=_int(toks.get("up")),
        bat_pct=_int(toks.get("bat")),
        heap_kb=_int(toks.get("heap")),
        tx_cur=int(pm.group(1)) if pm else None,
        tx_min=int(pm.group(2)) if pm and pm.group(2) is not None else None,
        tx_max=int(pm.group(3)) if pm else None,
        gps=sw[0],
        track=sw[1],
        display=sw[2],
        mesh=sw[3],
        gateway=sw[4],
        led=sw[5],
        has_led=has_led,
    )


# ---------------------------------------------------------------------------
# fold
# ---------------------------------------------------------------------------


def _default_row_state(row: dict[str, Any], now_ms: int) -> str:
    # The service imports this module for `fold`; a top-level import of it here would be circular.
    from .node_admin_service import compute_state  # noqa: PLC0415  # circular at module level

    return compute_state(row, now_ms)


def _body(result: str) -> str:
    for prefix in ("ok ", "err "):
        if result.startswith(prefix):
            return result[len(prefix) :]
    return result


def _kv(body: str) -> dict[str, str]:
    """Like `tokenize` but keeps the `-` ("absent") values, for replies that map them to None."""
    out: dict[str, str] = {}
    for tok in body.split(" "):
        key, sep, value = tok.partition("=")
        if sep and key and key not in out:
            out[key] = value
    return out


def _flt(value: str | None) -> float | None:
    if value is None or _FLOAT_RE.fullmatch(value) is None:
        return None
    return float(value)


def _num(value: str) -> int | float | None:
    if _INT_RE.fullmatch(value):
        return int(value)
    return _flt(value)


def _opt_num(value: str | None) -> tuple[bool, int | float | None]:
    """(ok, number): `-` is a valid None, a missing or unreadable value is not ok."""
    if value is None:
        return False, None
    if value == "-":
        return True, None
    num = _num(value)
    return num is not None, num


def _ratio(value: str | None) -> tuple[int, int] | None:
    m = _RATIO_RE.fullmatch(value or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _parse_radio(body: str) -> tuple[dict[str, JsonValue], re.Match[str] | None] | None:
    kv = _kv(body)
    f, bw = _flt(kv.get("f")), _flt(kv.get("bw"))
    sf, cr = _int(kv.get("sf")), _int(kv.get("cr"))
    if f is None or bw is None or sf is None or cr is None:
        return None
    return {"f": f, "sf": sf, "cr": cr, "bw": bw}, _P_RE.fullmatch(kv.get("p", ""))


def _parse_sens(body: str) -> dict[str, JsonValue] | None:
    kv = _kv(body)
    out: dict[str, JsonValue] = {}
    for key in ("t", "h", "p", "t2"):
        raw = kv.get(key)
        if raw is None:
            return None
        if raw == "-":
            out[key] = None
            continue
        num = _flt(raw)
        if num is None:
            return None
        out[key] = num
    return out


def _parse_pos(body: str) -> dict[str, JsonValue] | None:
    parts = body.split(" ")
    if len(parts) != _POS_PARTS or parts[3] not in _POS_SRC:
        return None
    lat, lon, alt = _flt(parts[0]), _flt(parts[1]), _num(parts[2])
    if lat is None or lon is None or alt is None:
        return None
    return {"lat": lat, "lon": lon, "alt": alt, "src": parts[3]}


def _parse_txq(body: str) -> dict[str, JsonValue] | None:
    kv = _kv(body)
    q, u = _ratio(kv.get("q")), _int(kv.get("u"))
    counters = [kv.get(k, "") for k in ("tx", "rt", "dr")]
    if q is None or u is None or kv.get("bp") not in _BP:
        return None
    if not all(_COUNT_RE.fullmatch(c) for c in counters):
        return None
    return {
        "q_used": q[0],
        "q_cap": q[1],
        "bp": kv["bp"],
        "tx": counters[0],
        "rt": counters[1],
        "dr": counters[2],
        "u": u,
    }


def _parse_mbox(body: str) -> dict[str, JsonValue] | None:
    kv = _kv(body)
    u, a, b = _ratio(kv.get("u")), _ratio(kv.get("a")), _int(kv.get("b"))
    counters = {k: kv.get(k, "") for k in ("st", "dl", "ak", "dr", "bl", "nt")}
    if u is None or a is None or b is None or kv.get("m") not in _MBOX_MODES:
        return None
    if not all(_COUNT_RE.fullmatch(c) for c in counters.values()):
        return None
    out: dict[str, JsonValue] = {
        "m": kv["m"],
        "u_used": u[0],
        "u_slots": u[1],
        "b": b,
        "a_used": a[0],
        "a_ceiling": a[1],
    }
    out.update(counters)
    return out


def _parse_maxhop(body: str) -> dict[str, JsonValue] | None:
    kv = _kv(body)
    t, p = _int(kv.get("t")), _int(kv.get("p"))
    return None if t is None or p is None else {"t": t, "p": p}


def _parse_mh_page(body: str) -> tuple[int, int | None, list[dict[str, JsonValue]]] | None:
    parts = body.split(" ")
    if len(parts) < _MH_HEAD or (len(parts) - _MH_HEAD) % 2:
        return None
    total = _int(parts[0])
    nxt = None if parts[1] == "-" else _int(parts[1])
    if total is None or total < 0 or (nxt is None and parts[1] != "-"):
        return None
    rows: list[dict[str, JsonValue]] = []
    for call, mins in zip(parts[2::2], parts[3::2], strict=True):
        minutes = _int(mins)
        if _CALL_RE.fullmatch(call) is None or minutes is None:
            return None
        rows.append({"call": call, "min": minutes})
    return total, nxt, rows


def _numbers(kv: dict[str, str], keys: tuple[str, ...]) -> dict[str, JsonValue] | None:
    out: dict[str, JsonValue] = {}
    for key in keys:
        good, num = _opt_num(kv.get(key))
        if not good:
            return None
        out[key] = num
    return out


def _parse_routed(kv: dict[str, str]) -> dict[str, JsonValue] | None:
    out = _numbers(kv, ("h", "k", "g", "m", "t"))
    rc, via = kv.get("rc"), kv.get("v")
    if out is None or rc is None or via is None:
        return None
    snr: float | None = None
    rc_call: str | None = None
    if rc != "-":
        snr_txt, at, rc_call = rc.partition("@")
        snr = _flt(snr_txt)
        if not at or _CALL_RE.fullmatch(rc_call) is None or snr is None:
            return None
    out["kind"] = "routed"
    out["rc_snr"] = snr
    out["rc_call"] = rc_call
    hops = [] if via == "-" else via.split(",")
    if not all(_CALL_RE.fullmatch(h) for h in hops):
        return None
    out["v"] = cast("list[JsonValue]", hops)
    return out


def _parse_mh_call(body: str) -> dict[str, JsonValue] | None:
    kind, _, rest = body.partition(" ")
    kv = _kv(rest)
    if kind == "d":
        direct = _numbers(kv, ("g", "m", "r", "s", "la", "lo", "di", "a", "n", "x", "h", "t"))
        return None if direct is None else {"kind": "direct", **direct}
    return _parse_routed(kv) if kind == "r" else None


_SIMPLE_PARSERS: Final[dict[str, Callable[[str], dict[str, JsonValue] | None]]] = {
    "sens": _parse_sens,
    "pos": _parse_pos,
    "txq": _parse_txq,
    "mbox": _parse_mbox,
    "maxhop": _parse_maxhop,
}


def _opposite(args: str) -> str | None:
    return {"on": "off", "off": "on"}.get(args.strip())


class _Folder:
    """One pass over the verified rows in id order; see the module docstring."""

    def __init__(self) -> None:
        self.fields: dict[str, FieldEntry] = {}
        self.sw: dict[str, SwitchEntry] = {name: self._blank() for name in SWITCHES}
        self.has_led: bool | None = None
        self.capability = 1
        self.version: str | None = None

    @staticmethod
    def _blank() -> SwitchEntry:
        return {"state": "unknown", "at": None, "log_id": None, "stale": False}

    def _set_field(self, key: str, value: JsonValue, row: dict[str, Any]) -> None:
        if value is None:
            self.fields.pop(key, None)
            return
        self.fields[key] = {
            "value": value,
            "at": row.get("reply_at"),
            "log_id": int(row["id"]),
            "stale": False,
        }

    def _set_switch(self, name: str, value: str | None, row: dict[str, Any]) -> None:
        if value is None:
            self.sw[name] = self._blank()
            return
        self.sw[name] = {
            "state": value,
            "at": row.get("reply_at"),
            "log_id": int(row["id"]),
            "stale": False,
        }

    # -- replies --------------------------------------------------------

    def apply(self, row: dict[str, Any]) -> None:
        result = row.get("result")
        cmd = str(row.get("cmd") or "")
        if not isinstance(result, str):
            return
        ok = result.startswith("ok ")
        if cmd in ("sync", "status") and ok:
            self._versioned(row, cmd, _body(result), result)
        elif cmd == "reboot" and result == "ok rebooting":
            self._reboot(row)
        elif cmd in SWITCHES:
            self._switch_reply(row, cmd, ok, _body(result))
        elif cmd == "txpower":
            self._txpower_reply(row, ok, _body(result))
        elif cmd in _READ_COMMANDS:
            self._read_reply(row, cmd, result)

    def _versioned(self, row: dict[str, Any], cmd: str, body: str, result: str) -> None:
        info = parse_status(result) if cmd == "status" else None
        if cmd == "status" and info is None:
            return  # unparsed status: not evidence
        toks = tokenize(body)
        ver = info.version if info is not None else (toks.get("v") or None)
        if ver is not None:
            if self.version is not None and ver != self.version:
                self.capability = 1
            self.version = ver
        if info is not None:
            self._status(row, info)
        elif ver is not None and parse_sync_hwm(body) is not None:
            self._set_field("firmware", ver, row)
        if cmd in CAPABILITY_CARRIERS:
            rm = _int(toks.get("rm"))
            self.capability = rm if rm is not None and rm >= 0 else 1

    def _status(self, row: dict[str, Any], info: StatusInfo) -> None:
        for key in STATUS_FIELDS:
            self.fields.pop(key, None)
        self._set_field("firmware", info.version, row)
        self._set_field("uptime_min", info.up_min, row)
        self._set_field("battery_pct", info.bat_pct, row)
        self._set_field("heap_kb", info.heap_kb, row)
        self._set_field("tx_power", info.tx_cur, row)
        self._set_field("tx_power_min", info.tx_min, row)
        self._set_field("tx_power_max", info.tx_max, row)
        vals = (info.gps, info.track, info.display, info.mesh, info.gateway, info.led)
        for name, val in zip(SWITCHES, vals, strict=True):
            if name == "led" and val:
                # `L` is bRemoteLed: true for on AND flash. A led ack since the last reboot that
                # said on/flash is confirmed by it; without one only "on or flash" is known.
                # "Since the last reboot" is judged by the status itself (`up=` minutes): a
                # power cycle, watchdog or another sender's reboot is a reboot McApp never saw,
                # and the LED is RAM only. No `up=` -> the ack cannot be dated against the boot.
                entry = self.sw["led"]
                ack_at, reply_at = entry["at"], row.get("reply_at")
                since_boot = (
                    isinstance(ack_at, int)
                    and isinstance(reply_at, int)
                    and info.up_min is not None
                    and ack_at > reply_at - info.up_min * 60_000
                )
                prior = entry["state"]
                keep = since_boot and prior in ("on", "flash")
                self._set_switch(name, prior if keep else "on_or_flash", row)
                continue
            self._set_switch(name, None if val is None else ("on" if val else "off"), row)
        self.has_led = info.has_led

    def _reboot(self, row: dict[str, Any]) -> None:
        self.capability = 1
        self.fields.pop("uptime_min", None)
        for fld in self.fields.values():
            fld["stale"] = True
        for sw in self.sw.values():
            sw["stale"] = sw["log_id"] is not None
        if self.has_led is not False:  # the LED is RAM only: off after every boot
            self._set_switch("led", "off", row)

    def _switch_reply(self, row: dict[str, Any], name: str, ok: bool, body: str) -> None:
        verdict = body.strip()
        if ok:
            value = tokenize(body).get(name)
            if value not in ("on", "off") and not (name == "led" and value == "flash"):
                return
            self._set_switch(name, value, row)
            if name == "gps" and value == "off":
                self._set_switch("track", "off", row)  # firmware: gps off also switches Track off
        elif verdict == "failed":
            # the node tried and the setting did not change: it kept the opposite of what we asked.
            # `led flash` has no opposite (the node may have been on or off): the earlier led
            # evidence is left untouched rather than blanked or invented.
            if name == "led" and str(row.get("args") or "").strip() == "flash":
                return
            self._set_switch(name, _opposite(str(row.get("args") or "")), row)
        elif verdict == "unsupported" and name == "led":
            self.has_led = False
            self._set_switch("led", None, row)

    def _read_reply(self, row: dict[str, Any], cmd: str, result: str) -> None:
        """Read / write acks of the extended commands. Malformed or `err` replies change nothing
        (the exceptions: `err unsupported` of sens / mbox / chgled, `err hidden` of pos,
        `err unknown` of `mh <call>`)."""
        marker = _ERR_MARKERS.get((cmd, result))
        if marker is not None:
            self._set_field(cmd, dict(marker), row)
            return
        if result == "err unknown" and cmd == "mh":
            call = str(row.get("args") or "").strip().upper()
            if call and not call.isdigit():
                self._mh_call(row, call, {"kind": "unknown"})
            return
        if not result.startswith("ok "):
            return
        body = result[3:]
        if cmd == "radio":
            self._radio(row, body)
        elif cmd in ("name", "atxt"):
            prefix = "n=" if cmd == "name" else "a="
            if body.startswith(prefix):
                text = body[len(prefix) :]
                self._set_field(cmd, "" if text == "-" else text, row)
        elif cmd == "chgled":
            mode = tokenize(body).get("chgled")
            if mode in ("auto", "on", "off"):
                self._set_field("chgled", mode, row)
        elif cmd == "mh":
            self._mh_reply(row, body)
        else:
            value = _SIMPLE_PARSERS[cmd](body)
            if value is not None:
                self._set_field(cmd, value, row)

    def _radio(self, row: dict[str, Any], body: str) -> None:
        parsed = _parse_radio(body)
        if parsed is None:
            return
        self._set_field("radio", parsed[0], row)
        pm = parsed[1]
        if pm is not None:  # the same `p=` as status: 3-part has a minimum, 2-part has none
            self._set_field("tx_power", int(pm.group(1)), row)
            self._set_field("tx_power_min", int(pm.group(2)) if pm.group(2) else None, row)
            self._set_field("tx_power_max", int(pm.group(3)), row)

    def _mh_reply(self, row: dict[str, Any], body: str) -> None:
        arg = str(row.get("args") or "").strip()
        if arg.isdigit():
            page = _parse_mh_page(body)
            if page is None:
                return
            total, nxt, rows = page
            start = int(arg)
            old = self.fields.get("mh")
            old_val = old["value"] if old is not None and not old["stale"] else None
            if isinstance(old_val, dict) and old_val.get("next") == start and old_val.get("rows"):
                prior = cast("list[JsonValue]", old_val["rows"])
                start = cast(int, old_val["start"])
                rows = [*cast("list[dict[str, JsonValue]]", prior), *rows]
            self._set_field(
                "mh",
                {
                    "total": total,
                    "next": nxt,
                    "start": start,
                    "rows": cast("list[JsonValue]", rows),
                },
                row,
            )
        elif arg:
            detail = _parse_mh_call(body)
            if detail is not None:
                self._mh_call(row, arg.upper(), detail)

    def _mh_call(self, row: dict[str, Any], call: str, detail: dict[str, JsonValue]) -> None:
        old = self.fields.get("mh_call")
        merged: dict[str, JsonValue] = {}
        if old is not None and not old["stale"] and isinstance(old["value"], dict):
            merged = dict(old["value"])
        merged[call] = detail
        self._set_field("mh_call", merged, row)

    def _txpower_reply(self, row: dict[str, Any], ok: bool, body: str) -> None:
        if ok:
            self._set_field("tx_power", _int(tokenize(body).get("txpower")), row)
        elif body.strip() == "failed":
            self._set_field("tx_power", None, row)


def _register(rows: list[dict[str, Any]], states: list[str], cmd: str) -> RegisterEntry:
    """State of the `sync` / `status` register from the newest verified row of that command."""
    newest: dict[str, Any] | None = None
    for row, st in zip(rows, states, strict=True):
        if row.get("cmd") == cmd and st == "verified":
            newest = row
    entry: RegisterEntry = {"state": "never", "at": None, "log_id": None, "raw": None}
    if newest is not None:
        result = str(newest.get("result") or "")
        if cmd == "status":
            good = parse_status(result) is not None
        else:
            good = result.startswith("ok ") and parse_sync_hwm(_body(result)) is not None
        entry = {
            "state": "ok" if good else "unparsed",
            "at": newest.get("reply_at"),
            "log_id": int(newest["id"]),
            "raw": result,
        }
    newest_id = int(newest["id"]) if newest is not None else -1
    for row, st in zip(rows, states, strict=True):
        if row.get("cmd") == cmd and st in _PENDING_STATES and int(row["id"]) > newest_id:
            entry["state"] = "pending"
    return entry


def _touches(row: dict[str, Any], name: str) -> bool:
    """Whether `row` is a command that can change switch `name` (`gps off` also clears track)."""
    cmd = row.get("cmd")
    return cmd == name or (name == "track" and cmd == "gps" and row.get("args") == "off")


def _switch_states(
    folder: _Folder, rows: list[dict[str, Any]], states: list[str]
) -> dict[str, SwitchEntry]:
    out: dict[str, SwitchEntry] = {}
    for name in SWITCHES:
        entry = folder.sw[name]
        evid = entry["log_id"]
        newer = [
            st
            for row, st in zip(rows, states, strict=True)
            if _touches(row, name) and (evid is None or int(row["id"]) > evid)
        ]
        state = entry["state"]
        if any(st in _PENDING_STATES for st in newer):
            state = "pending"
        elif evid is not None and any(st in _SILENT_STATES for st in newer):
            state = "uncertain"
        out[name] = {**entry, "state": state}
    return out


def fold(
    rows: list[dict[str, Any]], now_ms: int, row_state: RowState | None = None
) -> NodeStateDict:
    """Fold a target's log rows into its last known state (Appendix A: capability, has_led, fields,
    switches, registers) plus `as_of_id`, the highest row id considered.

    `row_state` is `node_admin_service.compute_state` unless a caller injects another (tests of the
    fold alone do not need to).
    """
    state_of = row_state or _default_row_state
    ordered = sorted(rows, key=lambda r: int(r["id"]))
    states = [state_of(r, now_ms) for r in ordered]
    folder = _Folder()
    for row, st in zip(ordered, states, strict=True):
        if st == "verified":
            folder.apply(row)
        elif st == "no_reply" and row.get("cmd") in PHASE_B_COMMANDS:
            folder.capability = 1
    return {
        "as_of_id": int(ordered[-1]["id"]) if ordered else 0,
        "capability": folder.capability,
        "has_led": folder.has_led,
        "fields": folder.fields,
        "switches": _switch_states(folder, ordered, states),
        "registers": {
            "sync": _register(ordered, states, "sync"),
            "status": _register(ordered, states, "status"),
        },
    }
