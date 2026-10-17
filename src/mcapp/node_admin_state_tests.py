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
from typing import Any, cast
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


def _test_tx_min_and_led_flash(record: Record) -> None:
    three = "ok v=4.40a up=75 bat=0 heap=112 s=GtdMwl p=2/0/22"
    st = fold([status_row(1, three)], NOW)
    record(
        "p=2/0/22: tx_power 2, tx_power_min 0, tx_power_max 22",
        fld(st, "tx_power") == 2 and fld(st, "tx_power_min") == 0 and fld(st, "tx_power_max") == 22,
    )
    info = parse_status(three)
    record("p=2/0/22: StatusInfo.tx_min == 0", info is not None and info.tx_min == 0)
    two = fold([status_row(1, LIVE)], NOW)
    record(
        "p=2/22 (two-part) still works, no tx_power_min",
        fld(two, "tx_power") == 2
        and fld(two, "tx_power_max") == 22
        and "tx_power_min" not in two["fields"],
    )
    neg = parse_status("ok v=4.40a s=GTDMW p=-3/-9/22")
    record(
        "p=-3/-9/22 parses (negative min)",
        neg is not None and (neg.tx_cur, neg.tx_min, neg.tx_max) == (-3, -9, 22),
    )
    bad = parse_status("ok v=4.40a s=GTDMW p=1/2/3/4")
    record("p= with four parts is ignored", bad is not None and bad.tx_cur is None)

    # LED: three states
    led_off = "ok v=4.40a up=1 bat=1 heap=1 s=gtdmwl p=2/0/22"
    led_up = "ok v=4.40a up=1 bat=1 heap=1 s=gtdmwL p=2/0/22"
    st = fold([vrow(1, "led", "flash", "ok led=flash")], NOW)
    record("led ack 'ok led=flash' sets state flash", sw(st, "led") == "flash")
    st = fold([vrow(1, "led", "on", "ok led=on"), vrow(2, "led", "flash", "err failed")], NOW)
    record(
        "led flash 'err failed' leaves the earlier state (no invented opposite)",
        sw(st, "led") == "on",
    )
    st = fold([vrow(1, "led", "flash", "err failed")], NOW)
    record("led flash 'err failed' with no evidence stays unknown", sw(st, "led") == "unknown")
    st = fold([status_row(1, led_up)], NOW)
    record("status L alone -> on_or_flash", sw(st, "led") == "on_or_flash")
    st = fold([status_row(1, led_off)], NOW)
    record("status l alone -> off", sw(st, "led") == "off")
    st = fold([vrow(1, "led", "flash", "ok led=flash"), status_row(2, led_up)], NOW)
    record("led ack flash then status L -> flash", sw(st, "led") == "flash")
    st = fold([vrow(1, "led", "on", "ok led=on"), status_row(2, led_up)], NOW)
    record("led ack on then status L -> on", sw(st, "led") == "on")
    st = fold([vrow(1, "led", "flash", "ok led=flash"), status_row(2, led_off)], NOW)
    record("led ack flash then status l -> off", sw(st, "led") == "off")
    st = fold(
        [
            vrow(1, "led", "flash", "ok led=flash"),
            vrow(2, "reboot", "", "ok rebooting"),
            status_row(3, led_up),
        ],
        NOW,
    )
    record(
        "led flash, reboot, status L -> on_or_flash (ack predates the reboot)",
        sw(st, "led") == "on_or_flash",
    )
    st = fold([vrow(1, "led", "flash", "ok led=flash"), vrow(2, "reboot", "", "ok rebooting")], NOW)
    record("led flash then reboot -> off", sw(st, "led") == "off")

    # A reboot McApp never saw (power cycle, watchdog, another sender): the status dates the boot.
    t_ack = NOW - 3_000_000
    t_status = t_ack + 600_000  # ten minutes later
    up2 = "ok v=4.40a up=2 bat=1 heap=1 s=gtdmwL p=2/0/22"
    up75 = "ok v=4.40a up=75 bat=1 heap=1 s=gtdmwL p=2/0/22"
    no_up = "ok v=4.40a bat=1 heap=1 s=gtdmwL p=2/0/22"
    ack = vrow(1, "led", "flash", "ok led=flash", t_ack)
    st = fold([ack, status_row(2, up2, t_status)], NOW)
    record(
        "led flash ack, status L ten minutes later with up=2 (node booted after the ack) "
        "-> on_or_flash",
        sw(st, "led") == "on_or_flash",
    )
    st = fold([ack, status_row(2, up75, t_status)], NOW)
    record(
        "led flash ack, status L ten minutes later with up=75 (no boot since) -> flash",
        sw(st, "led") == "flash",
    )
    st = fold([ack, status_row(2, no_up, t_status)], NOW)
    record(
        "led flash ack, status L without up= (ack cannot be dated against the boot) -> on_or_flash",
        sw(st, "led") == "on_or_flash",
    )


def _test_injection(record: Record) -> None:
    calls: list[str] = []

    def fake(row: dict[str, Any], now_ms: int) -> str:
        calls.append(str(row["id"]))
        return "waiting"

    st = fold([status_row(1)], NOW, row_state=fake)
    record("fold accepts an injected row-state function", calls == ["1"] and st["fields"] == {})


def fold_any(rows: list[dict[str, Any]], now: int) -> dict[str, Any]:
    """`fold` with an untyped result: the read fields hold JSON-shaped values."""
    return cast(dict[str, Any], fold(rows, now))


def _test_read_fields(record: Record) -> None:
    """Extended read/write commands (wave 2): shapes from the firmware corpus boundaries."""
    import json
    from pathlib import Path

    corpus = json.loads((Path(__file__).parent / "remote_cmd_vectors.json").read_text())
    by_ctr = {r["ctr"]: r["result"] for r in corpus["replies"]}

    def one(cmd: str, args: str, result: str) -> Any:
        return fold_any([vrow(1, cmd, args, result)], NOW)["fields"].get(cmd)

    st = fold_any([vrow(1, "radio", "", by_ctr[80])], NOW)["fields"]
    record(
        "radio: f/sf/cr/bw parsed, p= sets tx_power min/max like status",
        st["radio"]["value"] == {"f": 999.999, "sf": 99, "cr": 99, "bw": 999.99}
        and (st["tx_power"]["value"], st["tx_power_min"]["value"]) == (-99, -99)
        and st["tx_power_max"]["value"] == -99,
    )
    st = fold_any([vrow(1, "radio", "", "ok f=433.175 sf=11 cr=5 bw=250 p=10/22")], NOW)["fields"]
    record(
        "radio: 2-part p= sets cur/max and no minimum",
        st["radio"]["value"] == {"f": 433.175, "sf": 11, "cr": 5, "bw": 250.0}
        and st["tx_power"]["value"] == 10
        and st["tx_power_max"]["value"] == 22
        and "tx_power_min" not in st,
    )
    sens = one("sens", "", by_ctr[81])
    record(
        "sens: boundary values; '-' is None; err unsupported marks the field",
        sens["value"] == {"t": -99.9, "h": 100.0, "p": 1099.9, "t2": -99.9}
        and one("sens", "", "ok t=21.4 h=45 p=1013.2 t2=-")["value"]["t2"] is None
        and one("sens", "", "err unsupported")["value"] == {"unsupported": True},
    )
    record(
        "name / atxt: free text verbatim after the key, spaces kept, '-' is empty",
        one("name", "", by_ctr[85])["value"] == "N" * 19
        and one("atxt", "", "ok a=MeshCom Garten  x")["value"] == "MeshCom Garten  x"
        and one("name", "Martin", "ok n=Martin")["value"] == "Martin"
        and one("atxt", "none", "ok a=-")["value"] == "",
    )
    record(
        "pos: lat lon alt src (read and write ack share the shape)",
        one("pos", "", by_ctr[87])["value"]
        == {"lat": -89.99999, "lon": -179.99999, "alt": 40000, "src": "nofix"}
        and one("pos", "48.4 11.7 492", "ok 48.40000 11.70000 492 set")["value"]["src"] == "set",
    )
    record(
        "txq: counters stay verbatim strings",
        one("txq", "", by_ctr[82])["value"]
        == {
            "q_used": 65535,
            "q_cap": 65535,
            "bp": "qrt",
            "tx": "4294M",
            "rt": "4294M",
            "dr": "4294M",
            "u": 100,
        },
    )
    mbox = one("mbox", "", by_ctr[83])
    record(
        "mbox: boundary values; err unsupported marks the field",
        mbox["value"]
        == {
            "m": "heard",
            "u_used": 65535,
            "u_slots": 65535,
            "b": 999999,
            "a_used": 65535,
            "a_ceiling": 65535,
            "st": "4294M",
            "dl": "4294M",
            "ak": "4294M",
            "dr": "4294M",
            "bl": "4294M",
            "nt": "4294M",
        }
        and one("mbox", "", "err unsupported")["value"] == {"unsupported": True},
    )
    record(
        "maxhop and chgled",
        one("maxhop", "", by_ctr[84])["value"] == {"t": 99, "p": 99}
        and one("chgled", "auto", by_ctr[98])["value"] == "auto"
        and one("chgled", "off", "ok chgled=off")["value"] == "off"
        and one("chgled", "bogus", "ok chgled=bogus") is None,
    )

    # mh pages
    r1 = vrow(1, "mh", "0", "ok 5 2 AA1AA 3 BB2BB 4")
    r2 = vrow(2, "mh", "2", "ok 5 4 CC3CC 5 DD4DD 6")
    r3 = vrow(3, "mh", "4", "ok 5 - EE5EE 7")
    mh = fold_any([r1, r2, r3], NOW)["fields"]["mh"]
    record(
        "mh pages: a page at the previous next appends, start unchanged, next - ends",
        mh["value"]
        == {
            "total": 5,
            "next": None,
            "start": 0,
            "rows": [
                {"call": c, "min": m}
                for c, m in (("AA1AA", 3), ("BB2BB", 4), ("CC3CC", 5), ("DD4DD", 6), ("EE5EE", 7))
            ],
        }
        and mh["log_id"] == 3,
    )
    again = fold_any([r1, r2, vrow(3, "mh", "0", "ok 5 2 ZZ9ZZ 1")], NOW)["fields"]["mh"]["value"]
    skip = fold_any([r1, vrow(2, "mh", "4", "ok 5 - EE5EE 7")], NOW)["fields"]["mh"]["value"]
    record(
        "mh pages: any other row restarts (start = row arg, rows = that page); 0 - is empty",
        again["start"] == 0
        and again["rows"] == [{"call": "ZZ9ZZ", "min": 1}]
        and again["next"] == 2
        and skip["start"] == 4
        and len(skip["rows"]) == 1
        and one("mh", "0", "ok 0 -")["value"] == {"total": 0, "next": None, "start": 0, "rows": []},
    )
    reboot_between = fold_any([r1, vrow(2, "reboot", "", "ok rebooting"), r2], NOW)["fields"]["mh"]
    record(
        "mh pages: a page after a reboot restarts instead of extending a stale list",
        reboot_between["value"]["start"] == 2 and len(reboot_between["value"]["rows"]) == 2,
    )

    # mh <call>
    direct = vrow(1, "mh", "dk5en-98", by_ctr[89])
    routed = vrow(2, "mh", "DL2JA-2", by_ctr[90])
    unknown = vrow(3, "mh", "XX0XX", "err unknown")
    mc = fold_any([direct, routed, unknown], NOW)["fields"]["mh_call"]
    d = mc["value"].get("DK5EN-98", {})
    rt = mc["value"].get("DL2JA-2", {})
    record(
        "mh <call>: direct numbers, routed rc/v, unknown; merged by upper-case call",
        d.get("kind") == "direct"
        and d["la"] == -90.0
        and d["di"] == 9999.9
        and d["t"] == 65535
        and rt["kind"] == "routed"
        and rt["rc_snr"] == -99.9
        and rt["rc_call"] == "DK5EN-001"
        and len(rt["v"]) == 5
        and rt["m"] == 1
        and mc["value"]["XX0XX"] == {"kind": "unknown"}
        and mc["log_id"] == 3,
    )
    dash = fold_any([vrow(1, "mh", "DL2JA-2", "ok r h=3 k=2 g=0 m=- rc=- t=3 v=-")], NOW)["fields"][
        "mh_call"
    ]["value"]["DL2JA-2"]
    record(
        "mh <call>: '-' maps to None / empty via list",
        dash["m"] is None
        and dash["rc_snr"] is None
        and dash["rc_call"] is None
        and dash["v"] == [],
    )

    # malformed / err replies are not evidence
    bad = [
        ("radio", "", "ok f=abc sf=11 cr=5 bw=250 p=1/2"),
        ("sens", "", "ok t=1 h=2"),
        ("pos", "", "ok 48.4 11.7 492 bogus"),
        ("pos", "48.4 11.7 492", "ok 48.4 11.7 492 bogus"),
        ("txq", "", "ok q=3/20 bp=zzz tx=1 rt=1 dr=1 u=1"),
        ("mbox", "", "ok m=heard u=1/2"),
        ("maxhop", "", "ok t=4"),
        ("name", "", "ok x=Martin"),
        ("name", "Martin", "ok x=Martin"),
        ("mh", "0", "ok 5 2 AA1AA"),
        ("name", "", "err text"),
        ("pos", "", "err gps"),
        ("radio", "", "err range"),
        ("chgled", "auto", "err range"),
    ]
    clean = all(fold_any([vrow(1, c, a, r)], NOW)["fields"].get(c) is None for c, a, r in bad)
    keep = fold_any([vrow(1, "name", "", "ok n=Old"), vrow(2, "name", "X", "ok garbage")], NOW)
    record(
        "malformed or err replies never raise and never change a field",
        clean
        and keep["fields"]["name"]["value"] == "Old"
        and keep["fields"]["name"]["log_id"] == 1,
    )
    # not-verified rows are not evidence
    unver = fold_any([srow(1, "name", "", "bad_tag")], NOW)["fields"]
    record("an unverified read row is not evidence", "name" not in unver)


def _test_read_markers(record: Record) -> None:
    """`?` calls and the `err hidden` / `err unsupported` markers (advisor rework of wave 2)."""

    def one(cmd: str, args: str, result: str) -> Any:
        return fold_any([vrow(1, cmd, args, result)], NOW)["fields"].get(cmd)

    # `?` calls (firmware rm_format.h: a non-call byte or a >9-char call is emitted as `?`)
    q = one("mh", "0", "ok 3 - AA1AA 3 ? 0 BB2BB 4")
    record(
        "mh page with a `?` row (churned out / over-long call) folds as 3 rows incl. call '?'",
        q is not None
        and [r["call"] for r in q["value"]["rows"]] == ["AA1AA", "?", "BB2BB"]
        and q["value"]["rows"][1]["min"] == 0
        and one("mh", "0", "ok 1 - A?B 1")["value"]["rows"] == [{"call": "A?B", "min": 1}]
        and one("mh", "0", "ok 1 - TOOLONGCALL 1") is None,
    )
    qr_row = "ok r h=3 k=2 g=0 m=1 rc=17.8@? t=3 v=?,DK5EN-1"
    qr = fold_any([vrow(1, "mh", "DL2JA-2", qr_row)], NOW)["fields"].get("mh_call")
    qv = one("mh", "DL2JA-2", "ok r h=3 k=2 g=0 m=1 rc=-9@ABCDEFGHIJ t=3 v=-")
    record(
        "mh <call> routed: rc call `?` and a `?` via hop parse; an over-long rc call is rejected",
        qr is not None
        and qr["value"]["DL2JA-2"]
        == {
            "kind": "routed",
            "h": 3,
            "k": 2,
            "g": 0,
            "m": 1,
            "t": 3,
            "rc_snr": 17.8,
            "rc_call": "?",
            "v": ["?", "DK5EN-1"],
        }
        and qv is None,
    )

    # err hidden / err unsupported replace the field; a later ack replaces them again
    hid = fold_any(
        [vrow(1, "pos", "", "err hidden"), vrow(2, "chgled", "on", "err unsupported")], NOW
    )["fields"]
    again_pos = fold_any(
        [vrow(1, "pos", "", "err hidden"), vrow(2, "pos", "", "ok 48.40000 11.70000 492 set")], NOW
    )["fields"]["pos"]["value"]
    record(
        "pos `err hidden` -> {hidden: True}; chgled `err unsupported` -> {unsupported: True}; "
        "a later successful pos replaces the marker",
        hid["pos"]["value"] == {"hidden": True}
        and hid["chgled"]["value"] == {"unsupported": True}
        and again_pos["src"] == "set"
        and one("pos", "", "err hidden")["log_id"] == 1,
    )


def _test_status_keeps_reads(record: Record) -> None:
    """Regression: `_status` used to wipe the whole fields map (`self.fields = {}`)."""
    rows = [
        vrow(1, "radio", "", "ok f=433.175 sf=11 cr=5 bw=250 p=10/22"),
        vrow(2, "name", "", "ok n=Martin"),
        status_row(3),
    ]
    fields = fold_any(rows, NOW)["fields"]
    record(
        "a status after a radio / name read keeps those fields",
        fields.get("radio", {}).get("value", {}).get("sf") == 11
        and fields.get("name", {}).get("value") == "Martin"
        and fields["tx_power"]["log_id"] == 3,
    )
    only_status = fold_any(
        [vrow(1, "radio", "", "ok f=433.175 sf=11 cr=5 bw=250 p=10/3/22"), status_row(2, FW_OLD)],
        NOW,
    )
    record(
        "a status resets status-owned fields (tx_power_min from the radio read does not outlive "
        "a status without one)",
        "tx_power_min" not in only_status["fields"] and "radio" in only_status["fields"],
    )
    after = fold_any([*rows, vrow(4, "reboot", "", "ok rebooting")], NOW)["fields"]
    record(
        "a reboot marks the read fields stale",
        after["radio"]["stale"] and after["name"]["stale"] and after["firmware"]["stale"],
    )


def _test_net_upd(record: Record) -> None:
    """`net` / `upd` read parsers (firmware grammar, reply of 2026-10-17)."""

    def one(cmd: str, args: str, result: str) -> Any:
        return fold_any([vrow(1, cmd, args, result)], NOW)["fields"].get(cmd)

    full = one("net", "", "ok mtu=1280 srv=OE net=auto eff=hamnet")
    record(
        "net: full reply parsed",
        full["value"] == {"mtu": 1280, "srv": "OE", "net": "auto", "eff": "hamnet"},
    )
    record(
        "net: '-' and out-of-range / unknown values read as None",
        one("net", "", "ok mtu=1280 srv=OE net=auto eff=-")["value"]["eff"] is None
        and one("net", "", "ok mtu=100 srv=oe net=x eff=inet")["value"]
        == {"mtu": None, "srv": None, "net": None, "eff": "inet"},
    )
    record(
        "net: garbage leaves no field",
        one("net", "", "ok blah") is None and one("net", "", "ok mtu=1 srv=x net=y eff=z") is None,
    )
    upd = one("upd", "", "ok m=notify c=prod n=1 a=v4.41a st=- b=0 e=-")
    record(
        "upd read: full reply parsed",
        upd["value"]
        == {
            "mode": "notify",
            "chan": "prod",
            "newer": True,
            "avail": "v4.41a",
            "staged": None,
            "busy": False,
            "go": None,
            "err": None,
            "check_min": None,
        },
    )
    live = one("upd", "", "ok m=off c=prod n=0 a=- st=v4.40a.10.17 b=0 g=1 ck=- e=-")["value"]
    record(
        "upd read: bench reply (staged, g=1, '-' for none) parsed",
        (live["avail"], live["staged"], live["go"], live["err"], live["check_min"])
        == (None, "v4.40a.10.17", True, None, None),
    )
    record(
        "net: bench reply parsed, eff=- is None",
        one("net", "", "ok mtu=1280 srv=OE net=auto eff=-")["value"]
        == {"mtu": 1280, "srv": "OE", "net": "auto", "eff": None},
    )
    more = one("upd", "", "ok m=auto c=dev n=0 a=v4.41a st=v4.41a b=1 e=nowifi ck=12")["value"]
    record(
        "upd read: st / b / e / ck parsed",
        (more["staged"], more["busy"], more["err"], more["check_min"])
        == ("v4.41a", True, "nowifi", 12)
        and (more["mode"], more["chan"], more["newer"]) == ("auto", "dev", False),
    )
    bad = one("upd", "", "ok m=bogus c=x n=2 a=bad/tag st=- b=9 e=- ck=abc")
    record(
        "upd read: invalid values dropped, nothing parsed leaves no field",
        bad is None and one("upd", "", "ok m=bogus n=1")["value"]["newer"] is True,
    )
    base = [vrow(1, "upd", "", "ok m=off c=prod n=1 a=v4.41a st=- b=0 e=-")]
    after = fold_any(
        [
            *base,
            # key=value results on purpose: without the args guard they WOULD parse
            vrow(2, "upd", "check", "ok m=auto n=0 a=-"),
            vrow(3, "upd", "go v4.41a", "ok m=auto n=0 a=-"),
        ],
        NOW,
    )["fields"]["upd"]
    record(
        "upd check / go replies do not touch fields['upd']",
        after["value"]["avail"] == "v4.41a" and after["value"]["mode"] == "off",
    )
    no_args = fold_any([*base, {**vrow(2, "upd", "", "ok m=auto n=0 a=-"), "args": None}], NOW)[
        "fields"
    ]["upd"]
    record("upd with args None is a read like ''", no_args["value"]["mode"] == "auto")
    newest = fold_any(
        [*base, vrow(2, "upd", "", "ok m=auto c=prod n=0 a=- st=- b=0 e=-")],
        NOW,
    )["fields"]["upd"]["value"]
    record(
        "upd read: the newer verified read wins (send order)",
        newest["mode"] == "auto" and newest["newer"] is False and newest["avail"] is None,
    )


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
        _test_tx_min_and_led_flash,
        _test_injection,
        _test_read_fields,
        _test_net_upd,
        _test_read_markers,
        _test_status_keeps_reads,
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
