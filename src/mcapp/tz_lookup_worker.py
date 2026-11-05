"""Standalone time-zone lookup, run as a short-lived child process.

Invoked as ``sys.executable -I <this file> <lat> <lon>`` by ``/api/timezone``.
It deliberately imports nothing from ``mcapp``: the point of the subprocess is
that numpy and TimezoneFinder (~20 MB resident) live only for the lookup.

stdout: the IANA zone name, or an empty line when no zone covers the point.
Exit codes: 0 ok, 2 bad arguments, 3 timezonefinder not importable.
"""

from __future__ import annotations

import sys

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_NOT_INSTALLED = 3
ARGC = 3


def main(argv: list[str]) -> int:
    if len(argv) != ARGC:
        return EXIT_BAD_ARGS
    try:
        lat = float(argv[1])
        lon = float(argv[2])
    except ValueError:
        return EXIT_BAD_ARGS
    try:
        from timezonefinder import TimezoneFinder  # noqa: PLC0415 - heavy, child-only
    except ImportError:
        return EXIT_NOT_INSTALLED
    name = TimezoneFinder().timezone_at(lat=lat, lng=lon)
    sys.stdout.write((name or "") + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
