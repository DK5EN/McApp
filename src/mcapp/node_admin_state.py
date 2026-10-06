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
`ok v=<ver> up=<min> bat=<%> heap=<kB> s=<letters> p=<cur>/<max>[ led=<0|1>]` and the older
`... gw=<0|1> mesh=<0|1>[ led=<0|1>]` form (only those switches known). The `D` letter means
display ON; the BLE page inverts `SN.DISP`, which is a different source and must not be copied.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypedDict

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
_P_RE: Final = re.compile(r"(-?[0-9]{1,4})/(-?[0-9]{1,4})")

RowState = Callable[[dict[str, Any], int], str]


class FieldEntry(TypedDict):
    value: str | int
    at: int | None
    log_id: int
    stale: bool


class SwitchEntry(TypedDict):
    """`state`: on | off | uncertain | pending | unknown.

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
        tx_max=int(pm.group(2)) if pm else None,
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

    def _set_field(self, key: str, value: str | int | None, row: dict[str, Any]) -> None:
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
        self.fields = {}
        self._set_field("firmware", info.version, row)
        self._set_field("uptime_min", info.up_min, row)
        self._set_field("battery_pct", info.bat_pct, row)
        self._set_field("heap_kb", info.heap_kb, row)
        self._set_field("tx_power", info.tx_cur, row)
        self._set_field("tx_power_max", info.tx_max, row)
        vals = (info.gps, info.track, info.display, info.mesh, info.gateway, info.led)
        for name, val in zip(SWITCHES, vals, strict=True):
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
            if value not in ("on", "off"):
                return
            self._set_switch(name, value, row)
            if name == "gps" and value == "off":
                self._set_switch("track", "off", row)  # firmware: gps off also switches Track off
        elif verdict == "failed":
            # the node tried and the setting did not change: it kept the opposite of what we asked
            self._set_switch(name, _opposite(str(row.get("args") or "")), row)
        elif verdict == "unsupported" and name == "led":
            self.has_led = False
            self._set_switch("led", None, row)

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
