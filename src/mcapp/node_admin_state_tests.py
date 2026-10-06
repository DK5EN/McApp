"""Built-in test suite for ``node_admin_state.py`` (Node Admin remote view, wave W1a).

Plan: ``doc/2026-10-06_1100-node-admin-remote-view-concept.md`` sections 4.3 and 9, Appendix A.
Pure functions only: no DB, no network, no node. House pattern (``remote_cmd_tests.py``): one
PASS/FAIL line per case, a summary, a bool return.

Status literals are FIRMWARE-PRODUCED: they are copied from the firmware's native test
``test/test_rm_sender_policy/test_main.cpp`` (fork-dev, ``rmFormatStatus`` / ``rmStatusParse``
cases; the source line is cited next to each) or are the live DK5EN-1 sample of 2026-10-06.

Mutation record (each single-line mutation must turn the named group red, then be restored):
  (a) sort rows by ``reply_at`` instead of ``id`` in ``fold``   -> "late status" / "revived" cases
  (b) drop the ``gps`` off -> track off rule in ``_switch_reply`` -> "gps=off clears track" case
  (c) let a non-carrier reply set/clear the capability          -> "capability: non-carrier" cases
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from . import node_admin_state as nas
from .node_admin_state import NodeStateDict, StatusInfo, fold, parse_status, tokenize

Record = Callable[[str, bool], None]

NOW = 10_000_000_000

# --- firmware-produced literals -------------------------------------------------------------
# test_main.cpp:553 (rmFormatStatus, all LED states, GPS on, display on, LED on)
FW_FULL = "ok v=4.40a up=125 bat=87 heap=212 s=GtDmwL p=17/22 led=1"
# test_main.cpp:557 (board without LED: no led= field, no L letter)
FW_NO_LED = "ok v=4.40a up=125 bat=87 heap=212 s=GtDmw p=17/22"
# test_main.cpp:588 (the exact worst string, 61 characters)
FW_WORST = "ok v=4.40a up=99999 bat=100 heap=9999 s=gtdmwl p=-99/99 led=0"
# the same with a 6-character version: the 62-character end of the "61-62 of 63" range
WORST_62 = "ok v=4.40ab up=99999 bat=100 heap=9999 s=gtdmwl p=-99/99 led=0"
# live sample, DK5EN-1, 2026-10-06 (concept 2.1)
LIVE = "ok v=4.40a up=417 bat=0 heap=115 s=gtdMwl p=2/22 led=0"
# test_main.cpp:613 and remote_cmd_vectors.json:273 (older gw=/mesh= form, no led=)
FW_OLD = "ok v=4.40a up=125 bat=87 heap=212 gw=0 mesh=1"
# test_main.cpp:621 (older form with led=)
FW_OLD_LED = "ok v=4.40a up=125 bat=87 heap=212 gw=1 mesh=1 led=0"

SYNC_OK = "ok ctr=1791268029 v=4.40a"


# --- row builders ---------------------------------------------------------------------------


def vrow(id_: int, cmd: str, args: str, result: str, reply_at: int | None = None) -> dict[str, Any]:
    """A verified row (state `verified`)."""
    at = reply_at if reply_at is not None else NOW - 1_000_000 + id_ * 1000
    return {
        "id": id_,
        "cmd": cmd,
        "args": args,
        "text": f"RM1 {id_} {cmd} {args}".strip(),
        "sent_at": at - 5000,
        "handed_off_at": at - 4000,
        "send_error": None,
        "reply_text": f"RM1 {id_} {result} 0123456789abcdef",
        "reply_at": at,
        "verified": 1,
        "result": result,
    }


def srow(id_: int, cmd: str, args: str, kind: str) -> dict[str, Any]:
    """A non-evidence row: waiting|queued|no_reply|send_failed|bad_tag|abandoned."""
    row: dict[str, Any] = {
        "id": id_,
        "cmd": cmd,
        "args": args,
        "text": f"RM1 {id_} {cmd} {args}".strip(),
        "sent_at": NOW - 1000,
        "handed_off_at": NOW - 900,
        "send_error": None,
        "reply_text": None,
        "reply_at": None,
        "verified": None,
        "result": None,
    }
    if kind == "queued":
        row["handed_off_at"] = None
    elif kind in ("no_reply", "abandoned", "bad_tag"):
        row["sent_at"] = NOW - 400_000
        row["handed_off_at"] = NOW - 399_000
    if kind == "abandoned":
        row["result"] = "abandoned"
    elif kind == "bad_tag":
        row["reply_text"] = f"RM1 {id_} ok gps=on ffffffffffffffff"
        row["reply_at"] = NOW - 300_000
        row["verified"] = 0
        row["result"] = "ok gps=on"
    elif kind == "send_failed":
        row["send_error"] = "BLE not connected"
        row["handed_off_at"] = NOW - 900
    elif kind not in ("waiting", "queued", "no_reply"):
        raise ValueError(kind)
    return row


def status_row(id_: int, result: str = LIVE, reply_at: int | None = None) -> dict[str, Any]:
    return vrow(id_, "status", "", result, reply_at)


def sw(state: NodeStateDict, name: str) -> str:
    return str(state["switches"][name]["state"])


def fld(state: NodeStateDict, name: str) -> Any:
    entry = state["fields"].get(name)
    return None if entry is None else entry["value"]


# --- tokenizer ------------------------------------------------------------------------------


def _test_tokenize(record: Record) -> None:
    record(
        "tokenize: key=value tokens",
        tokenize("ctr=17 v=4.40a rm=2") == {"ctr": "17", "v": "4.40a", "rm": "2"},
    )
    record("tokenize: '-' means absent", tokenize("up=- bat=87") == {"bat": "87"})
    record("tokenize: tokens without '=' are ignored", tokenize("free text a=1") == {"a": "1"})
    record("tokenize: first duplicate wins", tokenize("a=1 a=2") == {"a": "1"})
    record(
        "tokenize: runs of spaces and empty keys", tokenize("  a=1   =x b=") == {"a": "1", "b": ""}
    )
    record("tokenize: value keeps later '='", tokenize("k=a=b") == {"k": "a=b"})
    ok = True
    junk_inputs: list[object] = ["", None, 17, b"a=1", [], "=", "==="]
    for junk in junk_inputs:
        try:
            tokenize(junk)
        except Exception:
            ok = False
    record("tokenize: never raises", ok)


# --- status parser --------------------------------------------------------------------------


def _switches(info: StatusInfo) -> tuple[bool | None, ...]:
    return (info.gps, info.track, info.display, info.mesh, info.gateway, info.led)


def _test_parse_status(record: Record) -> None:
    full = parse_status(FW_FULL)
    record(
        "status literal (test_main.cpp:553) parses to the expected fields",
        full is not None
        and full.version == "4.40a"
        and (full.up_min, full.bat_pct, full.heap_kb) == (125, 87, 212)
        and (full.tx_cur, full.tx_max) == (17, 22)
        and _switches(full) == (True, False, True, False, False, True)
        and full.has_led is True,
    )
    no_led = parse_status(FW_NO_LED)
    record(
        "5-letter s= (test_main.cpp:557): led unknown, board has no LED",
        no_led is not None
        and _switches(no_led) == (True, False, True, False, False, None)
        and no_led.has_led is False
        and (no_led.tx_cur, no_led.tx_max) == (17, 22),
    )
    worst = parse_status(FW_WORST)
    record(
        "worst case literal (test_main.cpp:588, 61 chars): negative tx, all off, led=0",
        len(FW_WORST) == 61
        and worst is not None
        and worst.up_min == 99999
        and worst.bat_pct == 100
        and worst.heap_kb == 9999
        and (worst.tx_cur, worst.tx_max) == (-99, 99)
        and _switches(worst) == (False,) * 6,
    )
    w62 = parse_status(WORST_62)
    record(
        "62-character worst case (6-character version) still parses",
        len(WORST_62) == 62 and w62 is not None and w62.version == "4.40ab" and w62.tx_cur == -99,
    )
    live = parse_status(LIVE)
    record(
        "live DK5EN-1 sample: gps off track off display off mesh ON gateway off light off",
        live is not None
        and _switches(live) == (False, False, False, True, False, False)
        and (live.up_min, live.bat_pct, live.heap_kb) == (417, 0, 115)
        and (live.tx_cur, live.tx_max) == (2, 22),
    )
    old = parse_status(FW_OLD)
    record(
        "old gw=/mesh= form (test_main.cpp:613): only gateway and mesh known",
        old is not None
        and _switches(old) == (None, None, None, True, False, None)
        and old.has_led is None
        and old.tx_cur is None
        and old.tx_max is None
        and (old.up_min, old.bat_pct, old.heap_kb) == (125, 87, 212),
    )
    old_led = parse_status(FW_OLD_LED)
    record(
        "old form with led= (test_main.cpp:621): led known, has_led True",
        old_led is not None
        and _switches(old_led) == (None, None, None, True, True, False)
        and old_led.has_led is True,
    )
    # test_main.cpp:628-634 reject / tolerance cases
    record(
        "not a status result: ok sent / err failed -> None",
        parse_status("ok sent") is None and parse_status("err failed") is None,
    )
    record("non-string input -> None", parse_status(None) is None and parse_status(5) is None)
    record(
        "wrong letter count (test_main.cpp:628) -> None", parse_status("ok v=4.40a s=GTDM") is None
    )
    record(
        "wrong letter order (test_main.cpp:629) -> None", parse_status("ok v=4.40a s=TGDMW") is None
    )
    record("extra letter (test_main.cpp:630) -> None", parse_status("ok v=4.40a s=GTDMWLX") is None)
    bad_p = parse_status("ok v=4.40a s=GTDMW p=x/y")  # test_main.cpp:631
    record(
        "bad p= ignored, not fatal (test_main.cpp:631)",
        bad_p is not None and bad_p.tx_cur is None and bad_p.tx_max is None,
    )
    neg_p = parse_status("ok v=4.40a s=GTDMW p=-3/22")  # test_main.cpp:633
    record(
        "p=-3/22 parses (test_main.cpp:633)",
        neg_p is not None and (neg_p.tx_cur, neg_p.tx_max) == (-3, 22) and neg_p.has_led is False,
    )
    record("garbage -> None", parse_status("hello world") is None and parse_status("") is None)
    record("'ok v=' alone parses with everything unknown", parse_status("ok v=4.40a") is not None)


def _format(bits: int, led: bool) -> str:
    """Python twin of the firmware's rmFormatStatus (test_main.cpp:553-557 shapes)."""
    letters = "GTDMWL"
    cnt = 6 if led else 5
    tok = "".join(letters[i] if (bits >> i) & 1 else letters[i].lower() for i in range(cnt))
    res = f"ok v=4.40a up=125 bat=87 heap=212 s={tok} p=17/22"
    return res + f" led={(bits >> 5) & 1}" if led else res


def _test_round_trip(record: Record) -> None:
    ok = True
    for led in (False, True):
        for bits in range(64):
            if not led and bits & 32:
                continue  # the led bit only exists on boards with an LED
            info = parse_status(_format(bits, led))
            if info is None:
                ok = False
                continue
            want: list[bool | None] = [bool((bits >> i) & 1) for i in range(5)]
            want.append(bool((bits >> 5) & 1) if led else None)
            ok = ok and list(_switches(info)) == want and info.has_led is led
    record("all 96 firmware-shaped status strings round-trip (test_main.cpp round trip)", ok)


# --- fold: snapshot, order, acks -----------------------------------------------------------


def _test_snapshot(record: Record) -> None:
    st = fold([vrow(1, "sync", "", SYNC_OK), status_row(2)], NOW)
    record("as_of_id is the highest row id", st["as_of_id"] == 2)
    record("empty rows: as_of_id 0, all unknown, capability 1", _empty_ok())
    record(
        "fields keys are exactly the six known ones",
        sorted(st["fields"])
        == sorted(["firmware", "uptime_min", "battery_pct", "heap_kb", "tx_power", "tx_power_max"]),
    )
    f = st["fields"]
    record(
        "field entries carry value/at/log_id/stale from the status row",
        f["uptime_min"]["value"] == 417
        and f["battery_pct"]["value"] == 0
        and f["heap_kb"]["value"] == 115
        and f["tx_power"]["value"] == 2
        and f["tx_power_max"]["value"] == 22
        and f["firmware"]["value"] == "4.40a"
        and f["uptime_min"]["log_id"] == 2
        and f["uptime_min"]["at"] == NOW - 1_000_000 + 2000
        and f["uptime_min"]["stale"] is False,
    )
    record(
        "switch states from the snapshot (no display inversion)",
        [sw(st, n) for n in nas.SWITCHES] == ["off", "off", "off", "on", "off", "off"]
        and st["has_led"] is True,
    )
    record(
        "switch entries carry at/log_id/stale",
        st["switches"]["mesh"]
        == {"state": "on", "at": NOW - 1_000_000 + 2000, "log_id": 2, "stale": False},
    )
    record(
        "display letter D is display ON",
        sw(
            fold([status_row(1, "ok v=4.40a up=1 bat=1 heap=1 s=gtDmwl p=2/22 led=0")], NOW),
            "display",
        )
        == "on",
    )
    record("firmware also comes from a verified sync v=", _sync_firmware())


def _empty_ok() -> bool:
    st = fold([], NOW)
    return (
        st["as_of_id"] == 0
        and st["capability"] == 1
        and st["fields"] == {}
        and st["has_led"] is None
        and all(sw(st, n) == "unknown" for n in nas.SWITCHES)
        and st["registers"]["sync"]["state"] == "never"
        and st["registers"]["status"]["state"] == "never"
    )


def _sync_firmware() -> bool:
    st = fold([vrow(1, "sync", "", SYNC_OK)], NOW)
    return fld(st, "firmware") == "4.40a" and st["fields"]["firmware"]["log_id"] == 1


def _test_order(record: Record) -> None:
    # Late verified status: LOWER id, NEWER reply_at than a gps-on ack. Id order wins.
    late = status_row(4, LIVE, reply_at=NOW - 1000)  # status says gps off, answered last
    ack = vrow(5, "gps", "on", "ok gps=on", reply_at=NOW - 500_000)
    st = fold([late, ack], NOW)
    record(
        "late status (lower id, newer reply_at) does NOT override a newer gps ack",
        sw(st, "gps") == "on",
    )
    record("the ack's log_id and at are reported", st["switches"]["gps"]["log_id"] == 5)
    record(
        "rows given in any order fold identically",
        fold([ack, late], NOW) == st,
    )
    # Revived abandoned row: id 2 (gps on) is answered after id 3 (gps off).
    revived = vrow(2, "gps", "on", "ok gps=on", reply_at=NOW - 1000)
    newer = vrow(3, "gps", "off", "ok gps=off", reply_at=NOW - 600_000)
    st2 = fold([revived, newer], NOW)
    record(
        "revived abandoned row (older id, later reply) does not override the newer ack",
        sw(st2, "gps") == "off",
    )
    # The same row while still abandoned: not evidence, and the switch is uncertain.
    abandoned = srow(2, "gps", "on", "abandoned")
    st3 = fold([status_row(1), abandoned], NOW)
    record(
        "abandoned row is not evidence: switch keeps the status value but is uncertain",
        st3["switches"]["gps"]["log_id"] == 1 and sw(st3, "gps") == "uncertain",
    )
    # Newer ack applies on top; an older ack is superseded by a newer status.
    st4 = fold([vrow(1, "gps", "on", "ok gps=on"), status_row(2)], NOW)
    record("an older ack is superseded by a newer status snapshot", sw(st4, "gps") == "off")
    st5 = fold([status_row(1), vrow(2, "mesh", "off", "ok mesh=off")], NOW)
    record(
        "a newer ack applies on top of the snapshot",
        sw(st5, "mesh") == "off"
        and sw(st5, "gateway") == "off"
        and st5["switches"]["mesh"]["log_id"] == 2,
    )


def _test_ack_effects(record: Record) -> None:
    on_track = "ok v=4.40a up=1 bat=1 heap=1 s=GTdMwl p=2/22 led=0"
    st = fold([status_row(1, on_track), vrow(2, "gps", "off", "ok gps=off")], NOW)
    record(
        "gps=off clears track (the reply says only gps=off)",
        sw(st, "gps") == "off"
        and sw(st, "track") == "off"
        and st["switches"]["track"]["log_id"] == 2,
    )
    st = fold([status_row(1, on_track), vrow(2, "gps", "on", "ok gps=on")], NOW)
    record("gps=on leaves track alone", sw(st, "gps") == "on" and sw(st, "track") == "on")
    st = fold([status_row(1, on_track), vrow(2, "track", "off", "ok track=off")], NOW)
    record("track=off does not touch gps", sw(st, "gps") == "on" and sw(st, "track") == "off")

    # err failed on a switch: it kept the opposite of the requested value
    st = fold([status_row(1, on_track), vrow(2, "mesh", "off", "err failed")], NOW)
    record("err failed on mesh off: mesh kept its old value, on", sw(st, "mesh") == "on")
    st = fold([status_row(1), vrow(2, "gateway", "on", "err failed")], NOW)
    record("err failed on gateway on: gateway kept off", sw(st, "gateway") == "off")

    # txpower
    st = fold([status_row(1), vrow(2, "txpower", "10", "ok txpower=10")], NOW)
    record(
        "ok txpower=10 sets tx_power, max unchanged",
        fld(st, "tx_power") == 10
        and fld(st, "tx_power_max") == 22
        and st["fields"]["tx_power"]["log_id"] == 2,
    )
    st = fold([status_row(1), vrow(2, "txpower", "10", "err failed")], NOW)
    record(
        "err failed on txpower: tx_power unknown",
        "tx_power" not in st["fields"] and fld(st, "tx_power_max") == 22,
    )

    # led unsupported
    st = fold([status_row(1), vrow(2, "led", "on", "err unsupported")], NOW)
    record(
        "err unsupported on led: has_led false, led unknown",
        st["has_led"] is False and sw(st, "led") == "unknown",
    )
    st = fold([vrow(1, "led", "on", "err unsupported"), status_row(2)], NOW)
    record(
        "a newer status with led= restores has_led",
        st["has_led"] is True and sw(st, "led") == "off",
    )

    # no effect
    base = fold([status_row(1)], NOW)
    for label, row in (
        ("ok sent", vrow(2, "sendpos", "", "ok sent")),
        ("err storage", vrow(2, "gps", "on", "err storage")),
        ("err not output", vrow(2, "setout", "a0 on", "err not output")),
    ):
        st = fold([status_row(1), row], NOW)
        same = st["fields"] == base["fields"] and [sw(st, n) for n in nas.SWITCHES] == [
            sw(base, n) for n in nas.SWITCHES
        ]
        record(f"{label}: no effect on fields and switches", same)

    # newer status without L: led unknown
    five = "ok v=4.40a up=9 bat=1 heap=1 s=GtdMw p=2/22"
    st = fold([status_row(1, LIVE), status_row(2, five)], NOW)
    record(
        "newer status without L makes led unknown (atomic snapshot)",
        sw(st, "led") == "unknown"
        and st["has_led"] is False
        and st["switches"]["led"]["log_id"] is None,
    )
    old = fold([status_row(1, LIVE), status_row(2, FW_OLD)], NOW)
    record(
        "old-form status as newest snapshot: unlisted switches become unknown, fields drop p=",
        sw(old, "gps") == "unknown"
        and sw(old, "mesh") == "on"
        and "tx_power" not in old["fields"]
        and old["has_led"] is None,
    )


def _test_reboot(record: Record) -> None:
    led_on = "ok v=4.40a up=417 bat=50 heap=115 s=gtdMwL p=2/22 led=1"
    rows = [status_row(1, led_on), vrow(2, "reboot", "", "ok rebooting")]
    st = fold(rows, NOW)
    record(
        "reboot boundary: led off, uptime unknown",
        sw(st, "led") == "off"
        and st["switches"]["led"]["log_id"] == 2
        and "uptime_min" not in st["fields"],
    )
    record(
        "reboot boundary: every other field and switch is stale",
        all(e["stale"] for e in st["fields"].values())
        and st["switches"]["mesh"]["stale"] is True
        and st["switches"]["led"]["stale"] is False
        and fld(st, "heap_kb") == 115
        and sw(st, "mesh") == "on",
    )
    st = fold([*rows, status_row(3)], NOW)
    record(
        "a status after the boundary clears stale",
        all(not e["stale"] for e in st["fields"].values()) and not st["switches"]["mesh"]["stale"],
    )
    st = fold([status_row(1, FW_NO_LED), vrow(2, "reboot", "", "ok rebooting")], NOW)
    record("reboot boundary on a board without LED leaves led unknown", sw(st, "led") == "unknown")
    st = fold([status_row(1), vrow(2, "reboot", "", "err failed")], NOW)
    record("a refused reboot is not a boundary", st["fields"]["uptime_min"]["value"] == 417)


# --- fold: switch states ---------------------------------------------------------------------


def _test_switch_states(record: Record) -> None:
    base = [status_row(1)]
    st = fold([*base, srow(2, "mesh", "off", "no_reply")], NOW)
    record(
        "uncertain after no_reply of a toggle",
        sw(st, "mesh") == "uncertain" and st["switches"]["mesh"]["log_id"] == 1,
    )
    record(
        "uncertain keeps the evidence's at/log_id",
        st["switches"]["mesh"]["at"] == NOW - 1_000_000 + 1000,
    )
    for kind in ("send_failed", "bad_tag", "abandoned"):
        st = fold([*base, srow(2, "gps", "on", kind)], NOW)
        record(f"uncertain after a {kind} row of that switch", sw(st, "gps") == "uncertain")
    st = fold([*base, srow(2, "mesh", "off", "no_reply")], NOW)
    record("uncertain is per switch (gps untouched)", sw(st, "gps") == "off")
    on_track = "ok v=4.40a up=1 bat=1 heap=1 s=GTdMwl p=2/22 led=0"
    st = fold([status_row(1, on_track), srow(2, "gps", "off", "no_reply")], NOW)
    record(
        "a silent gps off makes track uncertain too (gps off also clears track)",
        sw(st, "gps") == "uncertain" and sw(st, "track") == "uncertain",
    )
    st = fold([status_row(1, on_track), srow(2, "gps", "on", "no_reply")], NOW)
    record("a silent gps on leaves track alone", sw(st, "track") == "on")
    st = fold([srow(1, "mesh", "off", "no_reply")], NOW)
    record("no evidence at all stays unknown, not uncertain", sw(st, "mesh") == "unknown")
    st = fold([srow(1, "gps", "on", "no_reply"), status_row(2)], NOW)
    record(
        "a silent row OLDER than the evidence does not make it uncertain", sw(st, "gps") == "off"
    )
    st = fold(
        [*base, srow(2, "mesh", "off", "no_reply"), vrow(3, "mesh", "off", "ok mesh=off")], NOW
    )
    record("a later verified ack of that switch ends the uncertainty", sw(st, "mesh") == "off")

    for kind in ("waiting", "queued"):
        st = fold([*base, srow(2, "gps", "on", kind)], NOW)
        record(
            f"pending while a row is {kind}",
            sw(st, "gps") == "pending" and st["switches"]["gps"]["log_id"] == 1,
        )
    st = fold([srow(1, "gps", "on", "waiting")], NOW)
    record("pending without prior evidence", sw(st, "gps") == "pending")

    bad = srow(2, "gps", "on", "bad_tag")  # its reply_text says "ok gps=on" with a wrong tag
    st = fold([*base, bad], NOW)
    record(
        "bad_tag row never feeds state (value stays off)",
        sw(st, "gps") == "uncertain" and fld(st, "tx_power") == 2,
    )
    st = fold([bad], NOW)
    record(
        "bad_tag alone: nothing known",
        sw(st, "gps") == "unknown" and st["registers"]["status"]["state"] == "never",
    )
    unv = dict(status_row(1), verified=0)
    record("an unverified status row feeds nothing", fold([unv], NOW)["fields"] == {})


def _test_registers(record: Record) -> None:
    st = fold([vrow(1, "sync", "", SYNC_OK), status_row(2)], NOW)
    r = st["registers"]
    record(
        "registers ok with raw, at and log_id",
        r["sync"] == {"state": "ok", "at": NOW - 1_000_000 + 1000, "log_id": 1, "raw": SYNC_OK}
        and r["status"] == {"state": "ok", "at": NOW - 1_000_000 + 2000, "log_id": 2, "raw": LIVE},
    )
    st = fold([vrow(1, "status", "", "ok v=4.40a s=GTDM")], NOW)
    record(
        "verified status that does not parse is unparsed, never ok, and feeds no field",
        st["registers"]["status"]["state"] == "unparsed"
        and st["registers"]["status"]["raw"] == "ok v=4.40a s=GTDM"
        and st["fields"] == {},
    )
    st = fold([status_row(1), vrow(2, "status", "", "ok v=4.40a s=GTDM")], NOW)
    record(
        "an unparsed newer status keeps older evidence but the register reads unparsed",
        st["registers"]["status"]["state"] == "unparsed" and fld(st, "heap_kb") == 115,
    )
    st = fold([vrow(1, "sync", "", "ok no counter here")], NOW)
    record("sync without ctr= is unparsed", st["registers"]["sync"]["state"] == "unparsed")
    st = fold([status_row(1), srow(2, "status", "", "waiting")], NOW)
    record(
        "register pending while a newer row of that command waits",
        st["registers"]["status"]["state"] == "pending"
        and st["registers"]["status"]["log_id"] == 1,
    )
    st = fold([srow(1, "sync", "", "queued")], NOW)
    record(
        "register pending with no prior verified row", st["registers"]["sync"]["state"] == "pending"
    )
    st = fold([srow(1, "status", "", "no_reply")], NOW)
    record(
        "a no_reply row alone leaves the register never",
        st["registers"]["status"]["state"] == "never",
    )


# --- fold: capability ------------------------------------------------------------------------


def _cap(rows: list[dict[str, Any]]) -> int:
    return int(fold(rows, NOW)["capability"])


def _test_capability(record: Record) -> None:
    record("capability defaults to 1", _cap([]) == 1 and _cap([status_row(1)]) == 1)
    sync2 = vrow(1, "sync", "", "ok ctr=1791268029 v=4.40a rm=2")
    record("sync rm=2 sets capability 2", _cap([sync2]) == 2)
    record("sync without rm= is capability 1", _cap([vrow(1, "sync", "", SYNC_OK)]) == 1)
    record(
        "capability NOT cleared by a later status without the token",
        _cap([sync2, status_row(2)]) == 2,
    )
    record(
        "capability: non-carrier reply with an rm= token does not set or clear it",
        _cap([sync2, status_row(2, LIVE + " rm=1")]) == 2
        and _cap([status_row(1, LIVE + " rm=3")]) == 1,
    )
    record(
        "capability: acks and errors do not touch it",
        _cap([sync2, vrow(2, "gps", "on", "ok gps=on"), vrow(3, "mesh", "on", "err failed")]) == 2,
    )
    record(
        "a later sync without the token clears it (carrier)",
        _cap([sync2, vrow(2, "sync", "", "ok ctr=1791268100 v=4.40a")]) == 1,
    )
    record(
        "firmware v= change between successive status replies resets capability",
        _cap([sync2, status_row(2, LIVE.replace("v=4.40a", "v=4.41a"))]) == 1,
    )
    record(
        "firmware v= change between sync and sync resets, then the new token applies",
        _cap([sync2, vrow(2, "sync", "", "ok ctr=1791268100 v=4.41a rm=2")]) == 2
        and _cap([sync2, vrow(2, "sync", "", "ok ctr=1791268100 v=4.41a")]) == 1,
    )
    record(
        "same version in status and sync is no change",
        _cap([sync2, status_row(2), vrow(3, "sync", "", "ok ctr=1791268100 v=4.40a rm=2")]) == 2,
    )
    record(
        "verified rebooting resets capability",
        _cap([sync2, vrow(2, "reboot", "", "ok rebooting")]) == 1,
    )
    record(
        "no_reply of a Phase B command resets capability",
        all(
            _cap([sync2, srow(2, cmd, "x", "no_reply")]) == 1
            for cmd in sorted(nas.PHASE_B_COMMANDS)
        ),
    )
    record(
        "no_reply of a Phase A command does not",
        _cap([sync2, srow(2, "gps", "on", "no_reply")]) == 2,
    )
    record(
        "a Phase B row still waiting does not reset",
        _cap([sync2, srow(2, "radio", "x", "waiting")]) == 2,
    )
    record(
        "a Phase B no_reply OLDER than the carrier does not undo it",
        _cap([srow(1, "radio", "x", "no_reply"), vrow(2, "sync", "", "ok ctr=1 v=4.40a rm=2")])
        == 2,
    )
    with patch.object(nas, "CAPABILITY_CARRIERS", frozenset({"sync", "status"})):
        record(
            "CAPABILITY_CARRIERS is the single switch: a carrier status without rm= clears it",
            _cap([sync2, status_row(2)]) == 1 and _cap([sync2, status_row(2, LIVE + " rm=2")]) == 2,
        )


def _test_injection(record: Record) -> None:
    calls: list[str] = []

    def fake(row: dict[str, Any], now_ms: int) -> str:
        calls.append(str(row["id"]))
        return "waiting"

    st = fold([status_row(1)], NOW, row_state=fake)
    record("fold accepts an injected row-state function", calls == ["1"] and st["fields"] == {})


def run_node_admin_state_tests() -> bool:
    results: list[tuple[str, bool]] = []

    def record(label: str, ok: bool) -> None:
        results.append((label, ok))
        print(f"{'PASS' if ok else 'FAIL'} | {label}")

    for group in (
        _test_tokenize,
        _test_parse_status,
        _test_round_trip,
        _test_snapshot,
        _test_order,
        _test_ack_effects,
        _test_reboot,
        _test_switch_states,
        _test_registers,
        _test_capability,
        _test_injection,
    ):
        try:
            group(record)
        except Exception as exc:  # a crashing group must fail the suite, loudly
            record(f"{group.__name__} raised {type(exc).__name__}: {exc}", False)

    passed = sum(1 for _, ok in results if ok)
    for label, ok in results:
        if not ok:
            print(f"FAIL | {label}")
    print(f"\nnode_admin_state Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if run_node_admin_state_tests() else 1)
