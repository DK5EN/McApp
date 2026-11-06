#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial"]
# ///
"""Unit tests for node.py parsers and builders (no serial port is opened)."""

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import node

SAMPLE = """\
[WIFI]...now listening at IP 192.168.68.71, UDP port 1990
--MeshCom 4.40a (build: Oct 31 2026 / 09:29:29)
...UPDATE: none
...Call: <DK5EN-1> ...ID 433A8968 ...NODE 43 <HELTEC_V3> ...UTC-OFF 1.000000 [GPS]
...EXTUDP off ...EXT IP  ...NOPMOTHER off
"""


class FakeSerial:
    def __init__(self, reply: bytes = b"") -> None:
        self.written = b""
        self.reply = reply

    def write(self, b: bytes) -> int:
        self.written += b
        return len(b)

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        r, self.reply = self.reply, b""
        return r

    def close(self) -> None:
        pass


class T(unittest.TestCase):
    def test_parse_info(self) -> None:
        i = node.parse_info(SAMPLE)
        assert i["call"] == "DK5EN-1"
        assert i["firmware"] == "4.40a"
        assert i["build"] == "Oct 31 2026 / 09:29:29"
        assert i["node_ip"] == "192.168.68.71"
        assert i["extudp"] == "off"
        assert i["ext_ip"] == ""

    def test_parse_extip_set(self) -> None:
        i = node.parse_info("...EXTUDP on ...EXT IP 192.168.68.58 ...NOPMOTHER off\n")
        assert i["extudp"] == "on"
        assert i["ext_ip"] == "192.168.68.58"

    def test_builders(self) -> None:
        assert node.build_command("--info") == b"--info\r\n"
        assert node.build_send("DL1ABC-1", "hi there") == ":{DL1ABC-1} hi there"
        ns = argparse.Namespace(ip="192.168.68.58")
        assert node.build_cmds("extudp-set", ns) == ["--extudpip 192.168.68.58"]
        assert node.build_cmds("extudp-clear", ns) == ["--extudpip none"]
        assert node.build_cmds("reboot", ns) == ["--reboot"]

    def test_ip_validation(self) -> None:
        assert node.valid_ipv4("10.0.0.1")
        assert not node.valid_ipv4("300.0.0.1")
        assert not node.valid_ipv4("a.b.c.d")

    def test_check_expected(self) -> None:
        ns = argparse.Namespace(ip="1.2.3.4")
        assert node.check_expected("extudp-set", ns, {"ext_ip": "1.2.3.4"}) is None
        assert node.check_expected("extudp-set", ns, {"ext_ip": ""}) is not None
        assert node.check_expected("extudp-on", ns, {"extudp": "off"}) is not None

    def test_console_info_fake_serial(self) -> None:
        class Echoing(FakeSerial):
            def write(self, b: bytes) -> int:
                self.reply = SAMPLE.encode()
                return super().write(b)

        ser = Echoing()
        info = node.Console("x", ser=ser).info(wait=0.3)
        assert ser.written == b"--info\r\n"
        assert info["call"] == "DK5EN-1"


if __name__ == "__main__":
    unittest.main()
