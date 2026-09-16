#!/usr/bin/env python3
"""Replay a recorded stall's HTTP call against a running MCProxy instance.

Re-issues the exact `method path?query` (with the recorded `body`, if any)
against `--base`, `--repeat` times, and prints the new run's p50/p95/max next
to the originally recorded duration and server context — so a coding agent
can tell whether a stall reproduces or was transient. `client_*` rows (webapp
stalls uploaded via `POST /api/stalls/client`) replay identically: their
`method`/`path`/`body` describe the same backend call the client made.

Usage:
    uv run python scripts/replay_stall.py --base http://mcapp.local --id 42
    uv run python scripts/replay_stall.py --json saved_row.json --repeat 20
    uv run python scripts/replay_stall.py --summary

See `doc/2026-09-15_1530-stall-tracking-plan.md` §5.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

_DEFAULT_BASE = "http://mcapp.local"
_FETCH_LIMIT = 2000
_TIMEOUT_S = 30.0
_SUMMARY_COLUMNS = ("path", "count", "p50", "p95", "p99", "max")


def _load_rows_from_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        rows = data.get("rows", [data])
    elif isinstance(data, list):
        rows = data
    else:
        rows = [data]
    return list(rows)


def _fetch_row_by_id(base: str, row_id: int) -> dict[str, Any] | None:
    resp = httpx.get(f"{base}/api/stalls", params={"limit": _FETCH_LIMIT}, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    for row in resp.json().get("rows", []):
        if row.get("id") == row_id:
            return dict(row)
    return None


def print_summary(base: str) -> None:
    resp = httpx.get(f"{base}/api/stalls/summary", timeout=_TIMEOUT_S)
    resp.raise_for_status()
    rows = resp.json().get("rows", [])
    if not rows:
        print("no summary rows")
        return
    widths = {c: max([len(c), *(len(str(r.get(c, ""))) for r in rows)]) for c in _SUMMARY_COLUMNS}
    header = "  ".join(c.ljust(widths[c]) for c in _SUMMARY_COLUMNS)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in _SUMMARY_COLUMNS))


def _replay_once(base: str, method: str, path: str, query: str, body: Any) -> tuple[int, float]:
    kwargs: dict[str, Any] = {
        "headers": {"X-Request-Id": f"replay-{uuid.uuid4().hex}"},
        "timeout": _TIMEOUT_S,
    }
    if query:
        kwargs["params"] = query
    if isinstance(body, dict | list):
        kwargs["json"] = body
    elif isinstance(body, str) and body:
        kwargs["content"] = body
    start = time.perf_counter()
    resp = httpx.request(method, f"{base}{path}", **kwargs)
    return resp.status_code, (time.perf_counter() - start) * 1000.0


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * p))
    return sorted_values[idx]


def replay_row(base: str, row: dict[str, Any], repeat: int) -> None:
    method = row.get("method") or "GET"
    path = row.get("path") or "/"
    query = row.get("query") or ""
    body = row.get("body")

    print(f"Replaying: {method} {path}{'?' + query if query else ''}")
    print(f"Recorded duration_ms={row.get('duration_ms')} status={row.get('status')}")
    context = row.get("context")
    if context:
        print("Recorded context:")
        print(json.dumps(context, indent=2, ensure_ascii=False))

    durations: list[float] = []
    statuses: dict[int, int] = {}
    for _ in range(max(1, repeat)):
        status, duration_ms = _replay_once(base, method, path, query, body)
        durations.append(duration_ms)
        statuses[status] = statuses.get(status, 0) + 1
    durations.sort()

    print(f"\nReplay runs: {len(durations)}")
    print(
        f"p50={_percentile(durations, 0.50):.1f}ms "
        f"p95={_percentile(durations, 0.95):.1f}ms "
        f"max={max(durations):.1f}ms"
    )
    print("statuses: " + ", ".join(f"{code}x{count}" for code, count in sorted(statuses.items())))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=_DEFAULT_BASE, help="MCProxy base URL")
    parser.add_argument("--id", type=int, dest="row_id", help="stall row id to fetch and replay")
    parser.add_argument("--json", type=Path, dest="json_file", help="saved row or {rows:[...]}")
    parser.add_argument("--repeat", type=int, default=10, help="replay iterations")
    parser.add_argument("--summary", action="store_true", help="print /api/stalls/summary and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = args.base.rstrip("/")

    if args.summary:
        print_summary(base)
        return 0

    if args.row_id is not None:
        row = _fetch_row_by_id(base, args.row_id)
        if row is None:
            msg = f"row id {args.row_id} not found in /api/stalls (limit={_FETCH_LIMIT})"
            print(msg, file=sys.stderr)
            return 1
    elif args.json_file is not None:
        rows = _load_rows_from_json(args.json_file)
        if not rows:
            print(f"no rows found in {args.json_file}", file=sys.stderr)
            return 1
        row = rows[0]
    else:
        build_parser().error("one of --id, --json or --summary is required")
        return 2

    replay_row(base, row, args.repeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
