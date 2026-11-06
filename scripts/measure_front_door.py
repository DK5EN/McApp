#!/usr/bin/env python3
"""Measure what the Caddy front door puts on the wire, for before/after comparisons.

Run from a LAN client with a curl that has HTTP/3 (Homebrew curl on macOS):

    uv run python scripts/measure_front_door.py --ip 192.168.68.74 --runs 10

It only issues GET requests and prints one markdown table row per measurement. The
SSE row covers the connect burst: time from request start until `proxy:summary`
appeared, which is the event right behind the large `proxy:initial`. Its wire bytes
are read from curl over a fixed window, so they include any live traffic in that window.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import time
from dataclasses import dataclass

HOST = "mcapp.local"
ENCODINGS = "gzip, deflate, br, zstd"
API_PATHS = (
    "/api/status",
    "/api/monitor/frames",
    "/api/stalls?limit=200",
)
STATIC_PATHS = (
    "/webapp/",
    "/webapp/assets/litertlm_wasm_internal.wasm",
)
BURST_MARKER = b"event: proxy:summary"
BURST_WINDOW_S = 4.0
BURST_TIMEOUT_S = 40.0
WRITE_OUT_FIELDS = 4


@dataclass(frozen=True)
class Sample:
    seconds: float
    wire_bytes: int
    encoding: str
    protocol: str


def _base_args(curl: str, ip: str, protocol: str, limit_rate: str | None) -> list[str]:
    args = [curl, "-k", "-s", "--resolve", f"{HOST}:443:{ip}"]
    args.append("--http3-only" if protocol == "h3" else "--http2")
    # A real browser's list, without --compressed, so curl reports wire bytes untouched.
    args += ["-H", f"Accept-Encoding: {ENCODINGS}"]
    if limit_rate:
        args += ["--limit-rate", limit_rate]
    return args


def fetch(curl: str, ip: str, protocol: str, path: str, limit_rate: str | None) -> Sample:
    write_out = "%{time_total} %{size_download} %{http_version} %header{content-encoding}"
    args = _base_args(curl, ip, protocol, limit_rate)
    args += ["-o", "/dev/null", "-w", write_out, f"https://{HOST}{path}"]
    result = subprocess.run(args, capture_output=True, text=True, check=True)  # noqa: S603 - argv built here, target is the operator's own box
    fields = result.stdout.split()
    encoding = fields[3] if len(fields) == WRITE_OUT_FIELDS else "-"
    return Sample(float(fields[0]), int(fields[1]), encoding, fields[2])


def fetch_burst(curl: str, ip: str, protocol: str, limit_rate: str | None) -> Sample:
    """Time the connect burst on decoded output, then read wire bytes from curl."""
    args = _base_args(curl, ip, protocol, limit_rate)
    args += ["-N", "--compressed", "--max-time", str(BURST_TIMEOUT_S), "-o", "-"]
    args += ["-D", "/dev/stderr", f"https://{HOST}/events"]
    # --compressed adds its own Accept-Encoding, which the explicit -H above overrides.
    start = time.perf_counter()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603 - argv built here, target is the operator's own box
    if proc.stdout is None:
        raise RuntimeError("curl started without a stdout pipe")
    seen = b""
    elapsed = BURST_TIMEOUT_S
    while chunk := proc.stdout.read1(65536):
        seen = seen[-64:] + chunk
        if BURST_MARKER in seen:
            elapsed = time.perf_counter() - start
            break
    proc.kill()
    _, stderr = proc.communicate()
    headers = stderr.decode(errors="replace").lower().splitlines()
    encoding = next(
        (h.split(":", 1)[1].strip() for h in headers if h.startswith("content-encoding:")), "-"
    )

    window = _base_args(curl, ip, protocol, limit_rate)
    window += ["-N", "--max-time", str(BURST_WINDOW_S), "-o", "/dev/null"]
    window += ["-w", "%{size_download}", f"https://{HOST}/events"]
    # curl exits 28 on the expected --max-time, so the return code is not checked.
    result = subprocess.run(window, capture_output=True, text=True, check=False)  # noqa: S603 - argv built here, target is the operator's own box
    return Sample(elapsed, int(result.stdout.strip()), encoding, protocol)


def row(label: str, protocol: str, rate: str | None, samples: list[Sample]) -> str:
    seconds = sorted(s.seconds for s in samples)
    median_ms = statistics.median(seconds) * 1000
    worst_ms = seconds[-1] * 1000
    wire = statistics.median(s.wire_bytes for s in samples)
    return (
        f"| {label} | {protocol} | {rate or 'LAN'} | {samples[0].encoding} "
        f"| {wire:.0f} | {median_ms:.0f} | {worst_ms:.0f} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True, help="LAN address of the box")
    parser.add_argument("--curl", default="/opt/homebrew/opt/curl/bin/curl")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--slow-rate", default="125k", help="curl --limit-rate for the slow link rows"
    )
    args = parser.parse_args()

    print("| Target | Proto | Link | Encoding | Wire bytes | Median ms | Worst ms |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for protocol in ("h2", "h3"):
        for rate in (None, args.slow_rate):
            burst = [fetch_burst(args.curl, args.ip, protocol, rate) for _ in range(args.runs)]
            print(row("/events connect burst", protocol, rate, burst), flush=True)
            for path in API_PATHS:
                samples = [
                    fetch(args.curl, args.ip, protocol, path, rate) for _ in range(args.runs)
                ]
                print(row(path, protocol, rate, samples), flush=True)
        for path in STATIC_PATHS:
            runs = 3 if path.endswith(".wasm") else args.runs
            samples = [fetch(args.curl, args.ip, protocol, path, None) for _ in range(runs)]
            print(row(path, protocol, None, samples), flush=True)


if __name__ == "__main__":
    main()
