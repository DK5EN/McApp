"""Built-in regression suite for `console_parse.py` (RF Monitor `frame.parsed`).

Pure-function suite: no I/O, no network, no node. Every line below is either a single
line lifted from the real dk5en-98 (ESP32) / dk5en-90 (RAK4631) meshlogger captures of
2026-10 (meshlogger prefix removed) or an example from `how-to-read-meshcom-logs.md`.

Cases cover every family, every `[LOG]` kind, packet-line field mapping (path order,
flags, LH marker, MOD split, ACK whole-byte hops), unit stripping, the noise set, each
severity rule, and the never-raise guarantee.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .commands.constants import has_console
from .console_parse import parse_console_line

Recorder = Callable[[str, bool], None]

_HEY = (
    "00:00:41 NEW-HEY 030 @ x1AE1E034 H04 S0 T0 M00 DK5EN-98>HG@R6; HW:43 MOD:8/8 FCS:06DB FW:40:a "
    "LH:AB"
)
_POS = (
    "00:00:01 MH-LoRa 115 ! x3BAC51B8 H00 S1 T0 M01 "
    "DD7MH-55,DB0MMR-12,DL2JA-2>*!4809.90N/01237.00EyPowerNode "
    "JN68HD#Hans/A=001535/N10/O=20.4/R=9;26386; HW:39 MOD:8/8 FCS:1BCC FW:40:a LH:AB"
)
_UDP = (
    "00:03:22 RX-UDP  051 : x6AC291FC H02 S1 T0 M00 OE1XAR-33>*:{CET}2026-10-04 22:03:22 HW:00 "
    "MOD:0/3 FCS:0BF9 FW:00:# LH:00"
)
_ACK = "08:02:52 TX-Lora  012 A x083DC0E9 H83 xC05311B0 01 00"
_LOG_RX = (
    "00:00:17 [LOG] 049 @ x1AE1E033 H03 S1 T0 M01 DK5EN-98,DL2JA-2>HG@R6;17,114,-18; "
    "HW:43 MOD:8/8 FCS:0B58 FW:40:a LH:AB RSSI:-114 SNR:-2 DUP:n OWN:e t=12066854"
)
_LOG_ACK = (
    "07:52:01 [LOG] 012 A x00F76491 H81 x03B50343 01 00 RSSI:-116 SNR:-4 DUP:n OWN:- t=40371047"
)
_STAT = (
    "00:04:11 [LOG] STAT util=6 rx=15541 tx=4030 newid=7 dup=3 err=2 txn=5 txfail=0 ringmax=1/20 "
    "drop=0/0/0/0/0 mh=6 heap=128904 trk=240/3 fw=40a/20260929 up=12300 t=12300155"
)


def _p(line: str) -> dict[str, Any]:
    r = parse_console_line(line)
    assert r is not None
    return dict(r)


def _stat_with(**over: str) -> str:
    out = _STAT
    for k, v in over.items():
        base = {"txfail": "txfail=0", "drop": "drop=0/0/0/0/0"}[k]
        out = out.replace(base, f"{k}={v}")
    return out


def _run(label: str, record: Recorder, check: Callable[[], object]) -> None:
    try:
        ok = bool(check())
    except Exception:  # a failing assertion/exception is a FAIL of that case, not of the suite
        ok = False
    record(label, ok)


def _test_packet_lines(record: Recorder) -> None:
    def hey() -> object:
        r = _p(_HEY)
        pk = r["packet"]
        return (
            r["family"] == "packet"
            and r["kind"] == "NEW-HEY"
            and r["node_time"] == "00:00:41"
            and pk["len"] == 30
            and pk["type"] == "@"
            and pk["msg_id"] == "1AE1E034"
            and pk["hops"] == 4
            and pk["server"] is False
            and pk["track"] is False
            and pk["mesh"] is False
            and pk["path"] == ["DK5EN-98"]
            and pk["dest"] == "HG"
            and pk["payload"] == "R6;"
            and pk["hw"] == 43
            and pk["fcs"] == "06DB"
            and pk["fw"] == 40
            and pk["fw_sub"] == "a"
            and pk["rssi"] is None
            and not r["noise"]
            and r["severity"] == "info"
        )

    _run("packet: NEW-HEY full mapping", record, hey)

    def pos() -> object:
        pk = _p(_POS)["packet"]
        return (
            pk["path"] == ["DD7MH-55", "DB0MMR-12", "DL2JA-2"]  # originator first
            and pk["dest"] == "*"
            and pk["type"] == "!"
            and pk["payload"].startswith("4809.90N/01237.00Ey")
            and pk["hops"] == 0
            and pk["server"] is True
            and pk["mesh"] is True
            and pk["hw"] == 39
        )

    _run("packet: path order, flags S1 M01, position payload", record, pos)

    def flags() -> object:
        pk = _p(
            "00:00:01 RX-LoRa2 020 : x1AE1E034 H02 S0 T0 M01 A>B:hi HW:43 MOD:8/8 FCS:06DB "
            "FW:40:a LH:AB"
        )["packet"]
        return (
            pk["hops"] == 2
            and pk["mesh"] is True
            and pk["server"] is False
            and pk["track"] is False
        )

    _run("packet: H02 S0 T0 M01 flags", record, flags)

    def track() -> object:
        pk = _p(
            "00:00:01 TX-LoRa  020 : x1AE1E034 H0A S1 T1 M00 A>B:hi HW:43 MOD:8/8 FCS:06DB "
            "FW:40:a LH:AB"
        )["packet"]
        return (
            pk["hops"] == 10
            and pk["track"] is True
            and pk["server"] is True
            and pk["mesh"] is False
        )

    _run("packet: hops is the LOW nibble only (H0A -> 10)", record, track)

    def lh() -> object:
        pk = _p(_HEY)["packet"]
        pk2 = _p(_HEY.replace("LH:AB", "LH:89"))["packet"]
        pk3 = _p(_UDP)["packet"]
        return pk["lh_hw"] == 43 and pk2["lh_hw"] == 9 and pk3["lh_hw"] == 0

    _run("packet: LH 0x80 marker removed (AB -> 43, 89 -> 9)", record, lh)

    def mod() -> object:
        pk = _p(_HEY)["packet"]
        pk2 = _p(_UDP)["packet"]
        return (
            pk["mod_country"] == 8
            and pk["mod"] == 8
            and pk2["mod_country"] == 0
            and pk2["mod"] == 3
            and pk2["fw"] == 0
            and pk2["fw_sub"] == "#"
        )

    _run("packet: MOD 8/8 and 0/3, FW:00:#", record, mod)

    def udp_text() -> object:
        r = _p(_UDP)
        pk = r["packet"]
        return (
            r["kind"] == "RX-UDP"
            and pk["type"] == ":"
            and pk["dest"] == "*"
            and pk["payload"].startswith("{CET}2026-10-04 22:03:22")
        )

    _run("packet: RX-UDP text frame, payload keeps ':'-free text", record, udp_text)

    def dm_payload() -> object:
        pk = _p(
            "00:01:02 RX-LoRa2 040 : xAABBCCDD H03 S0 T0 M01 DK5EN-1>DK5EN-98:hello there{087 "
            "HW:09 MOD:8/8 FCS:0001 FW:40:a LH:89"
        )["packet"]
        return pk["dest"] == "DK5EN-98" and pk["payload"] == "hello there{087"

    _run("packet: DM dest + payload split on the type char", record, dm_payload)

    def tags() -> object:
        kinds = []
        for tag in ("MH-LoRa", "RX-LoRa2", "TX-LoRa", "TX-UDP", "NEW-POS", "NEW-TXT", "NEW-PING"):
            r = _p(_HEY.replace("NEW-HEY", tag))
            kinds.append((r["family"], r["kind"]))
        return kinds == [
            ("packet", t)
            for t in ("MH-LoRa", "RX-LoRa2", "TX-LoRa", "TX-UDP", "NEW-POS", "NEW-TXT", "NEW-PING")
        ]

    _run("packet: every tag verbatim as kind", record, tags)

    def old_colon() -> object:
        r = _p(_HEY.replace("NEW-HEY 030", "MH-LoRa: 030"))
        return (
            r["kind"] == "MH-LoRa"
            and r["packet"]["len"] == 30
            and r["packet"]["msg_id"] == "1AE1E034"
        )

    _run("packet: old 'MH-LoRa:' with colon accepted (guide 6.11)", record, old_colon)

    def ack() -> object:
        r = _p(_ACK)
        pk = r["packet"]
        return (
            r["family"] == "packet"
            and r["kind"] == "TX-Lora"
            and pk["type"] == "A"
            and pk["msg_id"] == "083DC0E9"
            and pk["hops"] == 0x83  # whole byte 5
            and pk["acked_id"] == "C05311B0"
            and pk["server"] is None
            and pk["track"] is None
            and pk["mesh"] is None
            and pk["path"] == []
            and pk["dest"] is None
            and pk["payload"] is None
        )

    _run("packet: ACK line H83 whole byte, acked_id, nulls", record, ack)

    def ack_h81() -> object:
        return _p(_ACK.replace("H83", "H81"))["packet"]["hops"] == 0x81

    _run("packet: ACK H81 -> hops 129", record, ack_h81)

    def pkt_unparsable() -> object:
        r = _p("00:00:01 MH-LoRa 030 @ garbage")
        return (
            r["family"] == "packet"
            and r["kind"] == "MH-LoRa"
            and "packet" not in r
            and r["fields"] == {}
        )

    _run(
        "packet: matching tag but broken body -> family packet, no PacketLine",
        record,
        pkt_unparsable,
    )

    def msgid_no_x() -> object:
        return _p(_HEY.replace("x1AE1E034", "x1ae1e034"))["packet"]["msg_id"] == "1AE1E034"

    _run("packet: msg_id upper-cased, without the x", record, msgid_no_x)


def _test_log_kinds(record: Recorder) -> None:
    def rx() -> object:
        r = _p(_LOG_RX)
        pk = r["packet"]
        return (
            r["family"] == "log"
            and r["kind"] == "RX"
            and pk["tag"] == "[LOG]"
            and pk["rssi"] == -114
            and pk["snr"] == -2
            and pk["dup"] is False
            and pk["own_echo"] is True
            and pk["t_ms"] == 12066854
            and pk["path"] == ["DK5EN-98", "DL2JA-2"]
            and pk["lh_hw"] == 43
        )

    _run("log RX: tail RSSI/SNR/DUP:n/OWN:e/t", record, rx)

    def rx_dup() -> object:
        pk = _p(_LOG_RX.replace("DUP:n OWN:e", "DUP:d OWN:-"))["packet"]
        return pk["dup"] is True and pk["own_echo"] is False

    _run("log RX: DUP:d OWN:-", record, rx_dup)

    def rx_notail() -> object:
        pk = _p(
            "00:10:25 [LOG] 121 ! xE9FB709E H01 S0 T0 M01 A,B>*!4825.38N/01147.20E-/V=3 HW:43 "
            "MOD:8/8 FCS:1AC4 FW:40:a LH:AB RSSI:-113 SNR:-2"
        )["packet"]
        return pk["rssi"] == -113 and pk["snr"] == -2 and pk["dup"] is None and pk["t_ms"] is None

    _run("log RX: partial tail (RSSI/SNR only, nRF52 shape)", record, rx_notail)

    def ack() -> object:
        r = _p(_LOG_ACK)
        pk = r["packet"]
        return (
            r["kind"] == "ACK"
            and pk["hops"] == 0x81
            and pk["acked_id"] == "03B50343"
            and pk["msg_id"] == "00F76491"
            and pk["rssi"] == -116
            and pk["snr"] == -4
            and pk["t_ms"] == 40371047
        )

    _run("log ACK: H81 whole byte, acked_id, RSSI/SNR tail", record, ack)

    def rly() -> object:
        r = _p("00:00:01 [LOG] RLY x3BAC51B8 ! H00 q=gwcap prio=0 slot=-1")
        f = r["fields"]
        return (
            r["family"] == "log"
            and r["kind"] == "RLY"
            and f
            == {"msg_id": "3BAC51B8", "type": "!", "h": 0, "q": "gwcap", "prio": 0, "slot": -1}
            and r["severity"] == "info"
        )

    _run("log RLY: q kept as reason string, slot=-1 numeric", record, rly)

    def tx() -> object:
        r = _p(
            "00:00:45 [LOG] TX x1AE1E034 @ H04 prio=5 src=o wait=3770 q=1 cad=0 len=30 t=12094643"
        )
        f = r["fields"]
        return (
            r["kind"] == "TX"
            and f["msg_id"] == "1AE1E034"
            and f["prio"] == 5
            and f["src"] == "o"
            and f["wait"] == 3770
            and f["q"] == 1
            and f["cad"] == 0
            and f["len"] == 30
            and f["t"] == 12094643
        )

    _run("log TX", record, tx)

    def err() -> object:
        r = _p("00:01:09 [LOG] ERR rssi=-69 snr=1 len=100 ferr=-24 t=12118562")
        return (
            r["kind"] == "ERR"
            and r["fields"]["ferr"] == -24
            and r["fields"]["rssi"] == -69
            and r["severity"] == "warn"
        )

    _run("log ERR (warn)", record, err)

    def stat() -> object:
        r = _p(_STAT)
        f = r["fields"]
        return (
            r["kind"] == "STAT"
            and f["util"] == 6
            and f["rx"] == 15541
            and f["ringmax"] == "1/20"
            and f["drop"] == "0/0/0/0/0"
            and f["drop_by_prio"] == [0, 0, 0, 0, 0]
            and f["trk"] == "240/3"
            and f["fw"] == "40a/20260929"
            and f["heap"] == 128904
            and r["severity"] == "info"
        )

    _run("log STAT: fields, strings kept, drop_by_prio", record, stat)

    def stat_drop() -> object:
        r = _p(_stat_with(drop="0/0/22/0/7"))
        return (
            r["fields"]["drop_by_prio"] == [0, 0, 22, 0, 7] and r["fields"]["drop"] == "0/0/22/0/7"
        )

    _run("log STAT: drop_by_prio 0/0/22/0/7", record, stat_drop)

    def dm() -> object:
        r = _p(
            "00:04:11 [LOG] DM sent=0 echo=0 gwack=0 ack=0 giveup=0 giveuph=0 att=0 reack=0/0 "
            "rtt=0/0/0/0/0/0 ring=enq:5 ovw:0"
        )
        f = r["fields"]
        return (
            r["kind"] == "DM"
            and f["reack"] == "0/0"
            and f["rtt"] == "0/0/0/0/0/0"
            and f["ring"] == "enq:5"
            and f["ovw"] == 0
        )

    _run("log DM: slash values stay strings, ovw: key", record, dm)

    def mbox() -> object:
        r = _p(
            "00:04:11 [LOG] MBOX mode=heard used=0/50 act=1/20 stored=1 refr=0 deliv=0 ack=1 "
            "dropt=0 dropc=0 drops=0 peer=0 blk=0 sto=1/0"
        )
        return (
            r["kind"] == "MBOX"
            and r["fields"]["mode"] == "heard"
            and r["fields"]["used"] == "0/50"
            and r["fields"]["sto"] == "1/0"
        )

    _run("log MBOX", record, mbox)

    def gwi() -> object:
        r = _p("00:03:22 [LOG] GWI x6AC291FC : H02 from=OE1XAR-33 t=12251795")
        return (
            r["kind"] == "GWI"
            and r["fields"]["from"] == "OE1XAR-33"
            and r["fields"]["msg_id"] == "6AC291FC"
            and r["fields"]["t"] == 12251795
        )

    _run("log GWI", record, gwi)

    def gwu() -> object:
        r = _p("00:00:56 [LOG] GWU xA183638F @ H02 t=12105725")
        return (
            r["kind"] == "GWU"
            and r["fields"]["msg_id"] == "A183638F"
            and r["fields"]["t"] == 12105725
        )

    _run("log GWU", record, gwu)

    def unknown_kind() -> object:
        r = _p("00:00:56 [LOG] ZZZ foo=1")
        return r["family"] == "log" and r["kind"] is None

    _run("log: unknown kind -> kind null", record, unknown_kind)


def _test_mc_dbg(record: Recorder) -> None:
    def chan() -> object:
        r = _p("[MC-DBG] CHANNEL_UTIL rx=1198ms tx=0ms util=11%")
        return (
            r["family"] == "mc-dbg"
            and r["kind"] == "CHANNEL_UTIL"
            and r["fields"] == {"rx": 1198, "tx": 0, "util": 11}
            and r["node_time"] is None
            and not r["noise"]
        )

    _run("mc-dbg CHANNEL_UTIL: units ms/% stripped", record, chan)

    def chan_warn() -> object:
        return (
            _p("[MC-DBG] CHANNEL_UTIL rx=1198ms tx=0ms util=70%")["severity"] == "warn"
            and _p("[MC-DBG] CHANNEL_UTIL rx=1ms tx=0ms util=69%")["severity"] == "info"
        )

    _run("severity: CHANNEL_UTIL util >= 70 warns, 69 does not", record, chan_warn)

    def msgid_digits() -> object:
        r = _p("[MC-DBG] RX_DEDUP_NEW msg_id=58581144")
        r2 = _p("[MC-DBG] RX_DEDUP_ADD msg_id=58581144 srv=1 slot=18/100")
        return r["fields"]["msg_id"] == "58581144" and r2["fields"] == {
            "msg_id": "58581144",
            "srv": 1,
            "slot": "18/100",
        }

    _run("msg_id with only digits stays a string", record, msgid_digits)

    def msgid_hex() -> object:
        return _p("[MC-DBG] RX_DEDUP_DUP msg_id=E9FB709E slot=97")["fields"] == {
            "msg_id": "E9FB709E",
            "slot": 97,
        }

    _run("msg_id hex string, slot numeric", record, msgid_hex)

    def floats() -> object:
        r = _p("[MC-DBG] CRC_ERROR rssi=-69 snr=1 freq_err=-24.2 size=100 ts=12118561")
        return (
            r["fields"]["freq_err"] == -24.2
            and r["fields"]["size"] == 100
            and r["severity"] == "warn"
        )

    _run("mc-dbg CRC_ERROR: float freq_err (warn)", record, floats)

    def crc_payload() -> object:
        r = _p("[MC-DBG] CRC_PAYLOAD[100]: 40 8F 63 83")
        return r["kind"] == "CRC_PAYLOAD" and r["fields"] == {"n": 100, "hex": "408F6383"}

    _run("mc-dbg CRC_PAYLOAD[n]: hex joined (string, not number)", record, crc_payload)

    def ring_write() -> object:
        r = _p(
            "[MC-DBG] RING_WRITE slot=18 type=40 status=FF len=30 msg_id=1AE1E034 queued=0/20 "
            "src=auto_pos"
        )
        return (
            r["fields"]["queued"] == "0/20"
            and r["fields"]["src"] == "auto_pos"
            and r["fields"]["type"] == 40
        )

    _run("mc-dbg RING_WRITE", record, ring_write)

    def rx_timeout_fire() -> object:
        r = _p("[MC-DBG] RX_TIMEOUT_FIRE ts=12679588 wait=4570 delta=4572")
        return r["noise"] is True and r["fields"]["wait"] == 4570

    _run("mc-dbg RX_TIMEOUT_FIRE is noise", record, rx_timeout_fire)

    def dbg_nonnoise() -> object:
        kinds = [
            "CHANNEL_UTIL rx=1ms",
            "ONRXDONE_TIME ms=91",
            "RING_STATUS queued=0",
            "RX_DEDUP_NEW msg_id=1AE1E034",
            "CRC_ERROR rssi=1",
            "CAD_BUSY attempt=1 next_timeout=100",
        ]
        return all(not _p("[MC-DBG] " + k)["noise"] for k in kinds)

    _run("noise: informative mc-dbg events are not noise", record, dbg_nonnoise)

    def dbg_noise() -> object:
        evs = [
            "RX_TIMEOUT_FIRE ts=1",
            "RX_RESTART src=timeout state=0",
            "RX_RESTARTED src=after_tx state=0",
            "RX_FLAG_PROCESS ts=1",
            "TX_GATE_ENTER qlen=1 cad_attempt=0",
            "TX_START qlen=0",
            "CAD_FREE attempt=0",
            "RADIO_TX len=30 kind=msg",
            "RING_PRIO slot=18 prio=5",
        ]
        return all(_p("[MC-DBG] " + e)["noise"] for e in evs)

    _run("noise: the nine listed mc-dbg events", record, dbg_noise)


def _test_mc_other(record: Recorder) -> None:
    def sm() -> object:
        r = _p("[MC-SM] IDLE -> TX_PREPARE rc=0")
        return (
            r["family"] == "mc-sm"
            and r["kind"] == "transition"
            and r["fields"] == {"from": "IDLE", "to": "TX_PREPARE", "rc": 0}
            and r["noise"]
            and r["severity"] == "info"
        )

    _run("mc-sm: transition fields, always noise", record, sm)

    def sm_err() -> object:
        r = _p("[MC-SM] TX_DONE -> RX_LISTEN rc=-1")
        r2 = _p("[MC-SM] RX_LISTEN -> RX_PROCESS rc=-707")
        return (
            r["severity"] == "alarm"
            and r["fields"]["rc"] == -1
            and r["noise"] is True
            and r2["severity"] == "alarm"
        )

    _run("mc-sm: rc != 0 is an alarm (and still noise)", record, sm_err)

    def sm_exception() -> object:
        r = _p("[MC-SM] TX_PREPARE -> IDLE rc=-1")
        r2 = _p("[MC-SM] TX_PREPARE -> TX_ACTIVE rc=-1")
        return r["severity"] == "info" and r["fields"]["rc"] == -1 and r2["severity"] == "alarm"

    _run("mc-sm: TX_PREPARE -> IDLE rc=-1 is the documented back-off (info)", record, sm_exception)

    def test_frames() -> object:
        r = _p(
            "[MC-TEST] RX_FRAME len=65 rssi=-78 snr=6 "
            "hex=408937A49100444B35454E2D39303E484E4052333B4E333B00"
        )
        r2 = _p(
            "[MC-TEST] TX_FRAME len=30 "
            "hex=4034E0E11A14444B35454E2D39383E48474052363B002B8806DB28AB617E"
        )
        r3 = _p("[MC-TEST] RX_FRAME len=2 rssi=-1 snr=1 hex=1234")
        return (
            r["family"] == "mc-test"
            and r["kind"] == "RX_FRAME"
            and r["fields"]["len"] == 65
            and r["fields"]["rssi"] == -78
            and r["fields"]["snr"] == 6
            and r["fields"]["hex"].startswith("408937A491")
            and r2["kind"] == "TX_FRAME"
            and r2["fields"]["len"] == 30
            and "rssi" not in r2["fields"]
            and r3["fields"]["hex"] == "1234"  # all-digit hex must not become a number
            and "frame" not in r
        )

    _run("mc-test RX_FRAME/TX_FRAME: len/rssi/snr/hex, no frame", record, test_frames)

    def capture_dropped() -> object:
        r = _p("[MC-TEST] CAPTURE_DROPPED n=3 serial_bytes=512")
        return (
            r["kind"] == "CAPTURE_DROPPED"
            and r["fields"] == {"n": 3, "serial_bytes": 512}
            and r["severity"] == "warn"
        )

    _run("mc-test CAPTURE_DROPPED (warn)", record, capture_dropped)

    def stat_window() -> object:
        r = _p("[MC-STAT] t=300s qmax=5/20")
        return (
            r["family"] == "mc-stat"
            and r["kind"] == "window"
            and r["fields"] == {"t": 300, "qmax": "5/20"}
        )

    _run("mc-stat window: t=300s -> 300, qmax string", record, stat_window)

    def stat_cont() -> object:
        r = _p("  tx: p1=0 p2=0 p3=2 p4=0 p5=3 preempt=0")
        r2 = _p("  drop: p1=0 p2=0 p3=0 p4=0 p5=0")
        return (
            r["family"] == "mc-stat"
            and r["kind"] == "tx"
            and r["fields"] == {"p1": 0, "p2": 0, "p3": 2, "p4": 0, "p5": 3, "preempt": 0}
            and r2["family"] == "mc-stat"
            and r2["kind"] == "drop"
            and r2["fields"]["p5"] == 0
        )

    _run("mc-stat continuation lines (two leading spaces)", record, stat_cont)

    def stat_cont_needs_indent() -> object:
        return _p("tx: p1=0")["family"] == "other"

    _run("mc-stat continuation requires the two leading spaces", record, stat_cont_needs_indent)

    def prio() -> object:
        r = _p("[MC-PRIO] p5_lat_avg=3010ms p5_lat_max=5101ms p5_cnt=3")
        return (
            r["family"] == "mc-prio"
            and r["kind"] is None
            and r["fields"] == {"p5_lat_avg": 3010, "p5_lat_max": 5101, "p5_cnt": 3}
        )

    _run("mc-prio: kind null, ms stripped", record, prio)

    def hwm() -> object:
        r = _p("[MC-HWM] uptime=3600s queue_hwm=3/8 csma_hwm=4 trickle=120000ms")
        return r["family"] == "mc-hwm" and r["fields"] == {
            "uptime": 3600,
            "queue_hwm": "3/8",
            "csma_hwm": 4,
            "trickle": 120000,
        }

    _run("mc-hwm", record, hwm)

    def trickle() -> object:
        a = _p("[MC-TRICKLE] SEND consistent=0<k=2 interval=120000ms neighbors=6")
        b = _p("[MC-TRICKLE] TOPO_CHANGE neighbors=6->7 interval_reset=30000ms")
        return (
            a["family"] == "mc-trickle"
            and a["kind"] == "SEND"
            and a["fields"]["interval"] == 120000
            and a["fields"]["neighbors"] == 6
            and a["fields"]["consistent"] == 0
            and a["fields"]["k"] == 2
            and b["kind"] == "TOPO_CHANGE"
            and b["fields"]["neighbors"] == "6->7"
            and b["fields"]["interval_reset"] == 30000
        )

    _run("mc-trickle SEND / TOPO_CHANGE", record, trickle)

    def wdt() -> object:
        r = _p("[MC-WDT] TX_WATCHDOG fired after 15000ms - forcing RX recovery")
        return r["family"] == "mc-wdt" and r["kind"] == "TX_WATCHDOG" and r["severity"] == "alarm"

    _run("mc-wdt TX_WATCHDOG is an alarm", record, wdt)

    def warn_slow() -> object:
        r = _p("[MC-WARN] ONRXDONE_SLOW ms=91 threshold=50")
        return (
            r["family"] == "mc-warn"
            and r["kind"] == "ONRXDONE_SLOW"
            and r["severity"] == "info"
            and r["fields"] == {"ms": 91, "threshold": 50}
        )

    _run("mc-warn ONRXDONE_SLOW stays info", record, warn_slow)


def _test_severity(record: Recorder) -> None:
    def alarms() -> object:
        lines = [
            "[MC-DBG] RING_DROP_NEW slot=3 prio=5 type=40 msg_id=1AE1E034 (queue full)",
            "[MC-DBG] RING_OVERFLOW buf=tx",
            "[MC-DBG] RING_REJECT len=0 src=x",
            "[MC-DBG] RETRANSMIT_GIVEUP_DM msg_id=1AE1E034 dest=DK5EN-1",
            "[MC-DBG] RX_RESTART src=timeout state=-2",
            "[MC-DBG] RX_RESTARTED src=after_tx state=-5",
            "[MC-DBG] TX_DONE state=-1 ts=1",
        ]
        return all(_p(x)["severity"] == "alarm" for x in lines)

    _run(
        "severity alarm: ring drops/overflow/reject, giveup_dm, RX_RESTART*/TX_DONE state != 0",
        record,
        alarms,
    )

    def not_alarms() -> object:
        lines = [
            "[MC-DBG] RX_RESTART src=timeout state=0",
            "[MC-DBG] RX_RESTART src=timeout",  # RAK4631 shape: no state at all
            "[MC-DBG] TX_DONE state=0 ts=12095114",
        ]
        return all(_p(x)["severity"] == "info" for x in lines)

    _run("severity info: state=0 / absent state", record, not_alarms)

    def warns() -> object:
        lines = [
            "[MC-DBG] RX_OTHER_ERROR code=-5 rssi=-90 snr=1 size=10 ts=1",
            "[MC-DBG] CRC_ERROR rssi=-69 snr=1 freq_err=-24.2 size=100 ts=1",
            "[MC-DBG] RING_DROP_PRIO slot=1 prio=4 replaced_by_prio=2",
            "[MC-DBG] RING_DROP_STALE slot=1 age_s=200 msg_id=1AE1E034",
            "[MC-DBG] RETRANSMIT_GIVEUP retries=3",
            "00:32:45 [LOG] RLY x91A4340B : H04 q=full prio=0 slot=-1",
        ]
        return all(_p(x)["severity"] == "warn" for x in lines)

    _run("severity warn: error/drop/giveup events and RLY q=full", record, warns)

    def rly_other_q() -> object:
        return _p("00:32:45 [LOG] RLY x91A4340B : H04 q=tx prio=3 slot=4")["severity"] == "info"

    _run("severity: RLY q=tx stays info", record, rly_other_q)

    def stat_sev() -> object:
        return (
            _p(_stat_with(txfail="1"))["severity"] == "alarm"
            and _p(_stat_with(drop="1/0/0/0/0"))["severity"] == "alarm"
            and _p(_stat_with(drop="0/2/0/0/0"))["severity"] == "alarm"
            and _p(_stat_with(drop="0/0/22/0/7"))["severity"] == "warn"
            and _p(_stat_with(drop="0/0/0/1/0"))["severity"] == "warn"
            and _p(_stat_with(drop="1/0/3/0/0"))["severity"] == "alarm"
            and _p(_STAT)["severity"] == "info"
        )

    _run("severity: STAT txfail>0 / p1,p2 drops alarm, p3-p5 warn", record, stat_sev)

    def boot() -> object:
        a = _p("[BOOT] RESET_REASON=1 POWERON")
        b = _p("...BOOT RESET_REASON=3 SW")
        c = _p("[BOOT] RESET_REASON=4 PANIC")
        d = _p("[BOOT] RESET_REASON=7 TASK_WDT")
        e = _p("[BOOT] RESET_REASON=15 BROWNOUT")
        f = _p("[BOOT] RESET_REASON=5 INT_WDT")
        return (
            a["family"] == "boot"
            and a["fields"] == {"reset_reason": 1, "reason_name": "POWERON"}
            and a["severity"] == "info"
            and b["family"] == "boot"
            and b["fields"]["reason_name"] == "SW"
            and b["severity"] == "info"
            and all(x["severity"] == "alarm" for x in (c, d, e, f))
        )

    _run("boot: reset_reason/reason_name, PANIC/WDT/BROWNOUT alarm", record, boot)

    def boot_nrf() -> object:
        r = _p("[BOOT] RESETREAS=0x00000001")
        return (
            r["family"] == "boot"
            and r["fields"]["resetreas"] == "0x00000001"
            and r["severity"] == "info"
        )

    _run("boot: nRF52 RESETREAS", record, boot_nrf)

    def boot_ready_not_alarm() -> object:
        r = _p("[BOOT];ready;ms;7924;ip;1")
        return r["family"] == "boot" and r["fields"]["ms"] == 7924 and r["severity"] == "info"

    _run("boot: [BOOT];ready;... semicolon record", record, boot_ready_not_alarm)

    def safeboot_not_boot() -> object:
        return _p("[SAFEBOOT];wifi;pmf_off;rc;0")["family"] == "other"

    _run("[SAFEBOOT] is not a boot-reason line", record, safeboot_not_boot)


def _test_other_families(record: Recorder) -> None:
    def heap_esp() -> object:
        r = _p("[HEAP] 00:00:00 249432 244284 212980 (init)")
        r2 = _p("[HEAP] 22:11:00 130976 120456 118772 (mon)")
        return (
            r["family"] == "heap"
            and r["kind"] == "init"
            and r["fields"] == {"free": 249432, "minfree": 244284, "maxblock": 212980}
            and r["node_time"] == "00:00:00"
            and r2["kind"] == "mon"
            and r2["node_time"] == "22:11:00"
        )

    _run("heap: ESP32 layout, init/mon", record, heap_esp)

    def heap_nrf() -> object:
        r = _p("00:00:41;[HEAP];92580;82220;81908;(mon)")
        r2 = _p("[HEAP];00:00:41;92580;82220;81908;(init)")
        return (
            r["family"] == "heap"
            and r["kind"] == "mon"
            and r["node_time"] == "00:00:41"
            and r["fields"] == {"free": 92580, "minfree": 82220, "maxblock": 81908}
            and r2["kind"] == "init"
            and r2["fields"]["free"] == 92580
        )

    _run("heap: nRF52 ';' layouts (both orders)", record, heap_nrf)

    def lora_rx() -> object:
        r = _p(
            "[LoRa]...Received packet: RSSI:\t\t-113 dBm / SNR:\t\t-2 dB / Frequency "
            "error:\t620.97 Hz"
        )
        return (
            r["family"] == "lora"
            and r["kind"] == "received"
            and r["fields"] == {"rssi": -113, "snr": -2, "freq_err": 620.97}
            and not r["noise"]
        )

    _run("lora received: rssi/snr/freq_err", record, lora_rx)

    def lora_timeout() -> object:
        r = _p("00:00:06 [LoRa]...Receive Timeout, startReceive again with sucess")
        return (
            r["family"] == "lora"
            and r["kind"] == "timeout"
            and r["noise"] is True
            and r["node_time"] == "00:00:06"
        )

    _run("lora timeout is noise", record, lora_timeout)

    def server() -> object:
        k = _p("[KEEP]...KEEP0406B878DK5EN-98 4.40a20;232;262;26244;9;")
        d = _p("[DATA]...DATA0406B878DK5EN-98 4.40a-113  -203")
        b = _p("[BEAT]...Heartbeat from server received")
        g = _p("[GW];keep;tx;ok;1;ms;12061824")
        g2 = _p("[GW];rx;type;BEAT;len;20;ms;12061912")
        return (
            [x["family"] for x in (k, d, b, g, g2)] == ["server"] * 5
            and [x["kind"] for x in (k, d, b, g, g2)] == ["KEEP", "DATA", "BEAT", "GW", "GW"]
            and g["fields"] == {"event": "keep", "sub": "tx", "ok": 1, "ms": 12061824}
            and g2["fields"] == {"event": "rx", "type": "BEAT", "len": 20, "ms": 12061912}
        )

    _run("server: KEEP/DATA/BEAT/GW", record, server)

    def ext() -> object:
        o = _p(
            '[EXT] Out: {"src_type":"lora","type":"pos","src":"DD7MH-55,DL2JA-2","msg":""} Len: 300'
        )
        t = _p(
            '[EXT] Tele-Out: {"src_type":"lora","type":"tele","src":"DD7MH-55","batt":0} Len: 157'
        )
        s = _p("[EXT];tx;len;300;stack_hwm;4912;ms;12345")
        return (
            o["family"] == "ext"
            and o["kind"] == "out"
            and o["fields"]["len"] == 300
            and o["fields"]["src_type"] == "lora"
            and t["kind"] == "tele"
            and t["fields"]["len"] == 157
            and s["family"] == "ext"
            and s["kind"] is None
            and s["fields"]["stack_hwm"] == 4912
        )

    _run("ext: out / tele / semicolon form", record, ext)

    def net() -> object:
        w = _p(
            "[WIFI];link;up;rssi;-47;bssid;5A:AF:97:2E:2B:8B;chan;3;age_s;12056;got_ip_n;1;ip;1;ms;12063573"
        )
        n = _p("[NTP];ok;epoch;1790890658;rtt;105")
        t = _p("TimeClient now (UTC): 22:09:18")
        e = _p("[ETH];link;up")
        return (
            w["family"] == "net"
            and w["kind"] == "WIFI"
            and w["fields"]["rssi"] == -47
            and w["fields"]["bssid"] == "5A:AF:97:2E:2B:8B"
            and w["fields"]["chan"] == 3
            and n["family"] == "net"
            and n["kind"] == "NTP"
            and n["fields"]["epoch"] == 1790890658
            and n["fields"]["rtt"] == 105
            and t["family"] == "net"
            and t["kind"] == "time"
            and t["node_time"] is None
            and e["kind"] == "ETH"
        )

    _run("net: WIFI / NTP / ETH / TimeClient", record, net)

    def nbr() -> object:
        r = _p("[NBR]|EDGE|211|DL2JA-2|DB0ED-99|P|0|11|5")
        return r["family"] == "nbr" and r["kind"] is None and r["fields"] == {}

    _run("nbr: not parsed further", record, nbr)

    def other() -> object:
        a = _p("transmission finished!")
        b = _p("[INFO]...Check GRC 5 for own-node")
        c = _p("00:00:01 something unexpected")
        return (
            a["family"] == "other"
            and a["kind"] is None
            and a["fields"] == {}
            and b["family"] == "other"
            and c["family"] == "other"
            and c["node_time"] == "00:00:01"
            and a["severity"] == "info"
            and a["noise"] is False
        )

    _run("other: untagged chatter", record, other)

    def noise_words() -> object:
        words = [
            "receiveflag",
            "transmittedFlag",
            "OnTXDone",
            "OnRxDone",
            "[CHECK] radio.scanChannel() / 1",
            "00:00:01 MESH    : <no via info>return MESH=true",
            "00:00:17 checkOwnTx:1AE1E033 own_msg_id:1AE1E033 <1AE1E033> 00",
            "00:00:41 Insert own_msg_id:1AE1E034 <1AE1E034>",
        ]
        return all(_p(w)["noise"] is True and _p(w)["family"] == "other" for w in words)

    _run(
        "noise: bare firmware words, [CHECK], MESH, checkOwnTx, Insert own_msg_id",
        record,
        noise_words,
    )

    def not_noise() -> object:
        return not _p("MESHCOM info")["noise"] and not _p("OnRxError")["noise"]

    _run("noise: lookalikes are not noise", record, not_noise)


def _test_never_raises(record: Recorder) -> None:
    def empty() -> object:
        return (
            parse_console_line("") is None
            and parse_console_line("   ") is None
            and parse_console_line("\r\n") is None
        )

    _run("empty input -> None", record, empty)

    def garbage() -> object:
        samples = [
            "[",
            "]",
            "[]",
            "[LOG]",
            "[LOG] ",
            "[LOG] 1",
            "[LOG] 012 A",
            "[LOG] STAT drop=a/b/c/d/e txfail=x",
            "[MC-DBG]",
            "[MC-DBG] ",
            "[MC-DBG] lowercase junk",
            "[MC-SM] ->",
            "[MC-SM] a -> b rc=zz",
            "[HEAP]",
            "[HEAP] 1 2",
            "MH-LoRa",
            "MH-LoRa 1",
            "MH-LoRa 030 @",
            "00:00:00",
            "99:99:99 x",
            "=" * 50,
            ">" * 50,
            "\x00\x01\x02",
            "[EXT] Out: {not json} Len: x",
            "[EXT] Out: {" + "[" * 300,
            "[BOOT]",
            "[BOOT] RESET_REASON=",
            "...BOOT",
        ]
        for s in samples:
            r = parse_console_line(s)
            if r is None or r["family"] == "":
                return False
        return True

    _run("garbage and truncated lines never raise", record, garbage)

    def truncated_packet() -> object:
        for i in range(1, len(_LOG_RX)):
            if parse_console_line(_LOG_RX[:i]) is None and _LOG_RX[:i].strip():
                return False
        for i in range(1, len(_HEY)):
            if parse_console_line(_HEY[:i]) is None and _HEY[:i].strip():
                return False
        return True

    _run(
        "every prefix of a real packet / [LOG] line parses without raising",
        record,
        truncated_packet,
    )

    def long_line() -> object:
        r = _p("[MC-DBG] CRC_PAYLOAD[999]: " + "AB " * 700)
        r2 = _p("x" * 2000)
        r3 = _p(
            "00:00:01 MH-LoRa 115 ! x3BAC51B8 H00 S1 T0 M01 " + "A," * 1000 + "B>*!" + "y" * 2000
        )
        return r["kind"] == "CRC_PAYLOAD" and r2["family"] == "other" and r3["family"] == "packet"

    _run("2000-char lines", record, long_line)

    def non_ascii() -> object:
        r = _p(
            "00:00:01 RX-LoRa2 020 : x1AE1E034 H02 S0 T0 M01 DK5EN-1>*:Grüße aus München "
            "\U0001f600 HW:43 MOD:8/8 FCS:06DB FW:40:a LH:AB"
        )
        r2 = _p("�\x1a\x12DKNEN>*!N")
        return r["packet"]["payload"] == "Grüße aus München \U0001f600" and r2["family"] == "other"

    _run("non-ASCII payloads and binary junk", record, non_ascii)

    def cr() -> object:
        a = _p(_HEY + "\r")
        b = _p(_HEY + "\r\n")
        c = _p("[MC-SM] IDLE -> RX_LISTEN rc=0\r")
        return (
            a["packet"]["fw_sub"] == "a"
            and a["packet"]["lh_hw"] == 43
            and b == a
            and c["fields"]["rc"] == 0
        )

    _run("trailing \\r / \\r\\n tolerated", record, cr)

    def non_str() -> object:
        try:
            r = parse_console_line(None)  # type: ignore[arg-type]  # deliberately wrong type: never-raise probe
        except Exception:
            return False
        return r is None

    _run("non-str input does not raise", record, non_str)

    def wire_shape() -> object:
        r = _p(_LOG_RX)
        keys = {"family", "kind", "node_time", "noise", "severity", "fields", "packet"}
        pk_keys = set(r["packet"])
        return (
            set(r) == keys
            and "frame" not in r
            and len(pk_keys) == 24
            and r["severity"] in ("info", "warn", "alarm")
        )

    _run("wire shape: exact top-level keys, 24 PacketLine keys", record, wire_shape)


async def run_console_parse_tests() -> object:
    """Return True iff every console-line parser regression case passes."""
    if has_console:
        print("\nTesting console_parse (firmware console line parser):")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'PASS' if ok else 'FAIL'} | {label}")

    _test_packet_lines(_record)
    _test_log_kinds(_record)
    _test_mc_dbg(_record)
    _test_mc_other(_record)
    _test_severity(_record)
    _test_other_families(_record)
    _test_never_raises(_record)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if has_console:
        print(f"\nconsole_parse Summary: {passed}/{total} tests passed")
        print("=" * 55)
    return passed == total


if __name__ == "__main__":
    import asyncio

    raise SystemExit(0 if asyncio.run(run_console_parse_tests()) else 1)
