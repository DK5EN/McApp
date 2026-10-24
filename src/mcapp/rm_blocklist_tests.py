"""Replay of the RM1 `blockcall` corpus (`rm_blocklist_vectors.json`) through `rm_blocklist`.

The corpus is canonical HERE and hand-copied to the webapp
(`src/utils/__tests__/rm_blocklist_vectors.json`), which pins its own sha256.
Re-capture `_VECTORS_EXPECTED_SHA256` in the SAME change as any edit and copy the
file over; nothing syncs it for you. Sections `normalize`/`parse`/`match`/`reply`
mirror the firmware (`blocked_calls.cpp`, `rm_format.h`); `build`/`guard` are
McApp's own sender rules. Every vector of every section runs.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from .rm_blocklist import (
    ARGS_MAX,
    LIST_MAX,
    BlocklistError,
    BlockReply,
    build_args,
    matches,
    normalize,
    parse_list,
    parse_reply_body,
    write_refusal,
)

_VECTORS_PATH = Path(__file__).parent / "rm_blocklist_vectors.json"
_VECTORS_EXPECTED_SHA256 = "8b8bc5ff88de2b9c71d78e204e64a1ccc16e334f2bc6ea0e668991208265e43e"


def _build_outcome(vector: dict[str, Any]) -> tuple[bool, str]:
    """Run one `build` vector; returns (ok, detail)."""
    try:
        args = build_args(vector["calls"], vector["own"], vector["target"])
    except BlocklistError as err:
        if vector["ok"]:
            return False, f"unexpected {err.code} ({err.entry})"
        want = (vector["error"], vector.get("entry"), vector.get("length"))
        return (err.code, err.entry, err.length) == want, f"got {(err.code, err.entry, err.length)}"
    if not vector["ok"]:
        return False, f"expected {vector['error']}, got {args!r}"
    return args == vector["args"], f"got {args!r}"


def _known(raw: dict[str, Any] | None) -> BlockReply | None:
    if raw is None:
        return None
    return BlockReply(tuple(raw["calls"]), raw["omitted"])


def run_rm_blocklist_tests() -> bool:
    """Replay the whole corpus plus a few non-corpus cases; True when all pass."""
    raw = _VECTORS_PATH.read_bytes()
    corpus: dict[str, Any] = json.loads(raw)
    results: list[tuple[str, bool]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        results.append((label, ok))
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}" + ("" if ok else f" | {detail}"))

    check(
        "blockcall corpus: sha256 pin (v1) — re-capture on any edit, copy to the webapp",
        hashlib.sha256(raw).hexdigest() == _VECTORS_EXPECTED_SHA256,
        hashlib.sha256(raw).hexdigest(),
    )
    check(
        "constants match corpus",
        (corpus["args_max"], corpus["list_max"]) == (ARGS_MAX, LIST_MAX),
    )

    for v in corpus["normalize"]:
        got = normalize(v["in"])
        check(f"normalize {v['in']!r} -> {v['out']!r}", got == v["out"], f"got {got!r}")

    for v in corpus["parse"]:
        csv, bad = parse_list(v["in"])
        want = (v["csv"], None) if v["ok"] else (None, v["bad"])
        check(f"parse {v['in'][:40]!r}", (csv, bad) == want, f"got {(csv, bad)!r}")

    for v in corpus["match"]:
        got_match = matches(v["csv"], v["call"])
        check(
            f"match {v['csv']!r} vs {v['call']!r} -> {v['blocked']}",
            got_match == v["blocked"],
            f"got {got_match}",
        )

    for v in corpus["reply"]:
        reply = parse_reply_body(v["body"])
        want_reply = None if v["calls"] is None else BlockReply(tuple(v["calls"]), v["omitted"])
        check(f"reply {v['body'][:40]!r}", reply == want_reply, f"got {reply!r}")

    built: list[str] = []
    for i, v in enumerate(corpus["build"]):
        ok, detail = _build_outcome(v)
        check(f"build #{i} {v['calls'][:3]!r} -> {v.get('args') or v.get('error')}", ok, detail)
        if v["ok"]:
            built.append(v["args"])

    for v in corpus["guard"]:
        refusal = write_refusal(_known(v["known"]), v["args"])
        want_refusal = None if v["allowed"] else v["reason"]
        check(
            f"guard known={v['known']} args={v['args']!r} -> {want_refusal}",
            refusal == want_refusal,
            f"got {refusal!r}",
        )

    # Non-corpus cases.
    clipped = "DK5EN" + " " * 30 + "X"
    check("parse: 23+ char clipped token stays invalid", parse_list(clipped)[0] is None)
    check(
        "parse: clipped token is reported, not silently dropped",
        parse_list(clipped)[1] is not None,
    )
    check(
        "build_args never exceeds ARGS_MAX (corpus property)",
        all(len(a) <= ARGS_MAX for a in built) and bool(built),
        str(built),
    )
    check("guard: 'NONE' compares case-insensitively", write_refusal(None, "NONE") is None)

    all_ok = all(ok for _, ok in results)
    print(f"    rm_blocklist: {'PASS' if all_ok else 'FAIL'} ({len(results)} cases)")
    return all_ok
