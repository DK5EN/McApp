#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial"]
# ///
"""Manage a MeshCom node over its USB serial console (container test campaign)."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

import serial

DEFAULT_PORT = "/dev/cu.usbserial-0001"
BAUD = 115200
_OCTET_MAX = 255
BOOT_WAIT_S = 12.0  # WiFi reports its IP ~7.5 s after the reset that opening the port triggers

_FW_RE = re.compile(r"--MeshCom\s+(\S+)\s+\(build:\s*([^)]*)\)")
_CALL_RE = re.compile(r"Call:\s*<([^>]*)>")
_EXTUDP_RE = re.compile(r"EXTUDP\s+(on|off)\b")
_EXTIP_RE = re.compile(r"EXT IP\s+(\S*)\s*(?=\.\.\.|$)")
_NODEIP_RE = re.compile(r"now listening at IP\s+(\d+\.\d+\.\d+\.\d+)")
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def parse_info(text: str) -> dict[str, Any]:
    """Parse console text (the --info block plus boot lines) into a dict."""
    out: dict[str, Any] = {
        "call": None,
        "firmware": None,
        "build": None,
        "node_ip": None,
        "extudp": None,
        "ext_ip": None,
    }
    for line in text.splitlines():
        if m := _FW_RE.search(line):
            out["firmware"], out["build"] = m.group(1), m.group(2).strip()
        if m := _CALL_RE.search(line):
            out["call"] = m.group(1)
        if m := _EXTUDP_RE.search(line):
            out["extudp"] = m.group(1)
            e = _EXTIP_RE.search(line)
            out["ext_ip"] = e.group(1) if e and e.group(1) else ""
        if m := _NODEIP_RE.search(line):
            out["node_ip"] = m.group(1)
    return out


def valid_ipv4(ip: str) -> bool:
    m = _IPV4_RE.match(ip)
    return bool(m) and all(int(g) <= _OCTET_MAX for g in m.groups())


def build_command(cmd: str) -> bytes:
    """A console command line as written to the port (CRLF terminated)."""
    return cmd.encode("utf-8") + b"\r\n"


def build_send(dest: str, text: str) -> str:
    """DM/group: ':{DEST} text' (loop_functions.cpp sendMessage, '::' is console text)."""
    return f":{{{dest}}} {text}"


_FIXED = {
    "extudp-on": "--extudp on",
    "extudp-off": "--extudp off",
    "extudp-clear": "--extudpip none",
    "reboot": "--reboot",
}


def build_cmds(name: str, args: argparse.Namespace) -> list[str]:
    if name in _FIXED:
        return [_FIXED[name]]
    if name == "extudp-set":
        if not valid_ipv4(args.ip):
            raise SystemExit(f"invalid IPv4 address: {args.ip}")
        return [f"--extudpip {args.ip}"]
    if name == "send":
        return [build_send(args.dest, args.text)]
    if name == "raw":
        return [args.command]
    raise SystemExit(f"no command for {name}")


def check_expected(name: str, args: argparse.Namespace, info: dict[str, Any]) -> str | None:
    """Return a mismatch description, or None if the --info state matches."""
    if name == "extudp-set" and info.get("ext_ip") != args.ip:
        return f"ext_ip is {info.get('ext_ip')!r}, expected {args.ip!r}"
    if name == "extudp-on" and info.get("extudp") != "on":
        return f"extudp is {info.get('extudp')!r}, expected 'on'"
    if name == "extudp-off" and info.get("extudp") != "off":
        return f"extudp is {info.get('extudp')!r}, expected 'off'"
    if name == "extudp-clear" and info.get("ext_ip"):
        return f"ext_ip is {info.get('ext_ip')!r}, expected empty"
    return None


class Console:
    def __init__(self, port: str, boot_wait: float = BOOT_WAIT_S, ser: Any = None) -> None:
        if ser is None:
            ser = serial.Serial(port, BAUD, timeout=0.2)
            time.sleep(boot_wait)
        self.ser = ser

    def drain(self) -> str:
        data = self.ser.read(65536)
        return data.decode("utf-8", errors="replace")

    def write(self, cmd: str) -> None:
        self.ser.write(build_command(cmd))
        self.ser.flush()

    def read_for(self, seconds: float) -> str:
        end = time.monotonic() + seconds
        buf = ""
        while time.monotonic() < end:
            buf += self.drain()
        return buf

    def info(self, wait: float = 3.0) -> dict[str, Any]:
        # Keep the boot output: the node IP only appears in the WiFi boot line
        # ("now listening at IP ..."), never in the --info block itself.
        boot = self.drain()
        self.write("--info")
        return parse_info(boot + self.read_for(wait))

    def close(self) -> None:
        self.ser.close()


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", default=DEFAULT_PORT)
    p.add_argument("--dry-run", action="store_true", help="print bytes, open nothing")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info")
    s.add_argument("--wait", type=float, default=3.0)
    s = sub.add_parser("extudp-set")
    s.add_argument("ip")
    for n in ("extudp-on", "extudp-off", "extudp-clear", "reboot"):
        sub.add_parser(n)
    s = sub.add_parser("send")
    s.add_argument("dest")
    s.add_argument("text")
    s = sub.add_parser("raw")
    s.add_argument("command")
    s = sub.add_parser("monitor")
    s.add_argument("--seconds", type=float, required=True)
    s.add_argument("--grep")
    return p


def run_live(con: Console, args: argparse.Namespace, cmds: list[str]) -> int:
    if args.cmd == "info":
        print(json.dumps(con.info(args.wait), indent=2))
        return 0
    if args.cmd == "monitor":
        pat = re.compile(args.grep) if args.grep else None
        for line in con.read_for(args.seconds).splitlines():
            if pat is None or pat.search(line):
                print(line)
        return 0
    for c in cmds:
        con.write(c)
    if args.cmd == "reboot":
        return 0
    reply = con.read_for(1.5)
    if args.cmd in ("send", "raw"):
        print(reply)
        return 0
    info = con.info()
    print(json.dumps(info, indent=2))
    bad = check_expected(args.cmd, args, info)
    if bad:
        print(f"MISMATCH: {bad}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.cmd == "info":
        cmds = ["--info"]
    elif args.cmd == "monitor":
        cmds = []
    else:
        cmds = build_cmds(args.cmd, args)

    if args.dry_run:
        for c in cmds:
            print(repr(build_command(c)))
        if args.cmd.startswith("extudp-"):
            print(repr(build_command("--info")), "(verification)")
        return 0

    con = Console(args.port)
    try:
        return run_live(con, args, cmds)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
