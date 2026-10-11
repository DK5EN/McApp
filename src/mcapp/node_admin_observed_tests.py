"""Regression suite for Observed sessions (decoded RM1 traffic, read-only).

Plan: doc/2026-10-11_1400-rm1-observed-sessions-plan.md. Fully offline; frames are
built with the real `derive_key` / `rm_tag` / `rm_reply_tag`.

Cases:

  1. `parse_command` truth table (strict like `parse_reply`, never authenticates).
  2. `build_sessions`: pairing (in order, out of order, reply without command, command
     without reply), transport duplicate, `sent_ms`, fields, active/idle, `own`.
  3. `verified` is positive only; the tag never reaches the output; a forged frame
     arriving first never wins a slot from a genuine one (3h-3m).
  4. Storage `get_rm1_rows` and the route (200, guard 403, `hours` bounds).
  5. The REAL service: `observed()` and the live `node_admin:observed` event, built
     off the event loop and only from the frame's own pair (5h-5i).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import FastAPI

from . import node_admin_service, remote_cmd
from .commands.constants import has_console
from .node_admin_observed import build_sessions, split_fields
from .node_admin_service import NodeAdminService
from .secret_box import SecretBox
from .sqlite_storage import create_sqlite_storage
from .sse_routes.node_admin import build_node_admin_router
from .storage.constants import db_write

PASSWD = "secret"
KEY = remote_cmd.derive_key(PASSWD)
WRONG = remote_cmd.derive_key("other-pw")
TARGET = "DK5EN-90"
CMDR = "DK5EN-1"
FOREIGN = "DL1ABC"
NOW = 1_791_706_000_000
CTR = 1_791_705_897  # unix seconds, 2026-10-11


def cmd_text(
    ctr: int, line: str, *, target: str = TARGET, commander: str = CMDR, key: bytes = KEY
) -> str:
    return f"RM1 {ctr} {line} {remote_cmd.rm_tag(key, target, commander, ctr, line)}"


def reply_text(
    ctr: int, result: str, *, target: str = TARGET, commander: str = CMDR, key: bytes = KEY
) -> str:
    return f"RM1 {ctr} {result} {remote_cmd.rm_reply_tag(key, target, commander, ctr, result)}"


def cmd_row(ts: int, text: str, *, target: str = TARGET, commander: str = CMDR) -> dict[str, Any]:
    return {"src": commander, "dst": target, "msg": text, "timestamp": ts, "msg_id": None}


def rep_row(ts: int, text: str, *, target: str = TARGET, commander: str = CMDR) -> dict[str, Any]:
    return {"src": target, "dst": commander, "msg": text, "timestamp": ts, "msg_id": None}


def _tags(text: str) -> list[str]:
    return [t for t in text.split(" ") if len(t) == 16 and all(c in "0123456789abcdef" for c in t)]


def _case_parse(record: Any) -> None:
    tag = "0123456789abcdef"
    pc = remote_cmd.parse_command
    ok = pc(f"RM1 1791705897 txpower 1 {tag}")
    bare = pc(f"RM1 5 status {tag}")
    suffixed = pc(f"RM1 5 status {tag}{{042")
    record(
        "1a. parse_command: with args, bare, and with the UDP ack suffix",
        ok is not None
        and (ok.ctr, ok.cmd, ok.args, ok.tag) == (1791705897, "txpower", "1", tag)
        and bare is not None
        and (bare.cmd, bare.args) == ("status", "")
        and suffixed is not None
        and suffixed.cmd == "status",
    )
    sync = pc(f"RM1 0 sync {tag}")
    maxhop = pc(f"RM1 1791717866 maxhop {tag}")
    record(
        "1b. parse_command: ctr 0 and a command word outside the send allow-list",
        sync is not None and sync.ctr == 0 and maxhop is not None and maxhop.cmd == "maxhop",
    )
    bad = {
        "reply ok": f"RM1 5 ok m=list {tag}",
        "reply err": f"RM1 1791705897 err range 2..22 {tag}",
        "no tag": "RM1 5 status",
        "short tag": "RM1 5 status 0123456789abcde",
        "upper tag": "RM1 5 status 0123456789ABCDEF",
        "leading zero ctr": f"RM1 05 status {tag}",
        "ctr overflow": f"RM1 4294967296 status {tag}",
        "wrong proto": f"RM2 5 status {tag}",
        "upper cmd": f"RM1 5 STATUS {tag}",
        "non-ascii": f"RM1 5 status ü {tag}",
        "too long": "RM1 5 status " + "x" * 160 + f" {tag}",
        "not a string": cast(Any, None),
    }
    record(
        "1c. parse_command refuses replies and malformed frames",
        all(pc(v) is None for v in bad.values()),
    )
    record(
        "1d. verify_command: right key passes, wrong key and wrong orientation fail",
        remote_cmd.verify_command(KEY, TARGET, CMDR, cmd_text(7, "status")) is not None
        and remote_cmd.verify_command(WRONG, TARGET, CMDR, cmd_text(7, "status")) is None
        and remote_cmd.verify_command(KEY, CMDR, TARGET, cmd_text(7, "status")) is None,
    )


def _case_sessions(record: Any) -> None:
    t0 = CTR * 1000
    c = cmd_text(CTR, "txpower 1")
    r = reply_text(CTR, "err range 2..22")
    rows = [cmd_row(t0 + 100, c), rep_row(t0 + 9_000, r)]
    [s] = build_sessions(rows, NOW, {}, ())
    [ex] = s["exchanges"]
    record(
        "2a. command + reply pair into one exchange of one session",
        (s["commander"], s["target"]) == (CMDR, TARGET)
        and (ex["ctr"], ex["cmd"], ex["args"]) == (CTR, "txpower", "1")
        and ex["reply"]["status"] == "err"
        and ex["reply"]["result"] == "range 2..22"
        and ex["reply"]["latency_ms"] == 8_900
        and ex["no_reply"] is False
        and ex["sent_ms"] == t0,
    )
    [s2] = build_sessions(list(reversed(rows)), NOW, {}, ())
    record("2b. out-of-order rows give the same session", s2 == s)
    dup = [*rows, cmd_row(t0 + 200, c + "{042"), rep_row(t0 + 9_100, r + "{077")]
    [s3] = build_sessions(dup, NOW, {}, ())
    record(
        "2c. transport duplicates (suffix on the UDP copy) collapse",
        len(s3["exchanges"]) == 1 and s3["exchanges"][0]["reply"]["reply_ms"] == t0 + 9_000,
    )
    [s4] = build_sessions([rep_row(t0 + 9_000, r)], NOW, {}, ())
    ex4 = s4["exchanges"][0]
    record(
        "2d. a reply without its command still appears (cmd null)",
        ex4["cmd"] is None
        and ex4["command_ms"] is None
        and ex4["reply"] is not None
        and ex4["reply"]["latency_ms"] is None
        and ex4["no_reply"] is False,
    )
    old = NOW - 130_000
    fresh = NOW - 60_000
    [s5] = build_sessions([cmd_row(old, c)], NOW, {}, ())
    [s6] = build_sessions([cmd_row(fresh, c)], NOW, {}, ())
    record(
        "2e. command without reply: no_reply only after 120 s",
        s5["exchanges"][0]["no_reply"] is True
        and s5["exchanges"][0]["reply"] is None
        and s6["exchanges"][0]["no_reply"] is False,
    )
    edge = [
        build_sessions([cmd_row(NOW - d, c)], NOW, {}, ())[0]["active"] for d in (119_999, 120_000)
    ]
    record("2f. active is strictly younger than 120 s", edge == [True, False])
    small = cmd_text(41, "status")
    [s7] = build_sessions([cmd_row(NOW - 5_000, small)], NOW, {}, ())
    record(
        "2g. sent_ms: unix-seconds ctr * 1000, else the command row timestamp",
        s7["exchanges"][0]["sent_ms"] == NOW - 5_000 and s["exchanges"][0]["sent_ms"] == t0,
    )
    record(
        "2h. fields split on spaces and '=' (p=2/2/22, a=-, m=list); err text has none",
        split_fields("t=4 p=2/2/22 a=- m=list u=0/50")
        == [["t", "4"], ["p", "2/2/22"], ["a", "-"], ["m", "list"], ["u", "0/50"]]
        and split_fields("range 2..22") == []
        and ex["reply"]["fields"] == [],
    )
    st = reply_text(CTR + 5, "ok t=4 p=2/2/22 a=-")
    [s8] = build_sessions(
        [cmd_row(t0 + 5_100, cmd_text(CTR + 5, "status")), rep_row(t0 + 6_000, st)], NOW, {}, ()
    )
    record(
        "2i. an ok reply carries its fields",
        s8["exchanges"][0]["reply"]["fields"] == [["t", "4"], ["p", "2/2/22"], ["a", "-"]],
    )
    two = [
        cmd_row(t0 + 100, c),
        cmd_row(t0 + 20_000, cmd_text(CTR + 20, "status")),
        cmd_row(t0 + 1_000, cmd_text(CTR + 1, "status", commander="DL1ABC"), commander="DL1ABC"),
    ]
    out = build_sessions(two, NOW, {}, ())
    record(
        "2j. one session per (commander, target); sessions and exchanges newest first",
        len(out) == 2
        and [e["ctr"] for e in out[0]["exchanges"]] == [CTR + 20, CTR]
        and out[0]["commander"] == CMDR,
    )
    own_by_cmdr = build_sessions(two, NOW, {}, [CMDR])[0]["own"]
    own_by_tgt = build_sessions(two, NOW, {}, [TARGET])[0]["own"]
    foreign = build_sessions(two, NOW, {}, ["DK5EN-14"])[0]["own"]
    record(
        "2k. own = commander or target is one of own_calls",
        own_by_cmdr and own_by_tgt and not foreign,
    )
    sync1 = cmd_text(0, "sync")
    sync_rep = reply_text(0, "ok ctr=1791705000 v=2")
    out_sync = build_sessions(
        [cmd_row(t0, sync1), rep_row(t0 + 3_000, sync_rep), cmd_row(t0 + 60_000, sync1)],
        NOW,
        {},
        (),
    )
    record(
        "2l. repeated ctr 0 (sync) opens separate exchanges; the reply binds to the first",
        len(out_sync[0]["exchanges"]) == 2
        and sorted(e["reply"] is not None for e in out_sync[0]["exchanges"]) == [False, True],
    )
    junk: list[dict[str, Any]] = [
        {"src": CMDR, "dst": TARGET, "msg": "hello", "timestamp": t0},
        {"src": CMDR, "dst": TARGET, "msg": None, "timestamp": t0},
        {"src": "???", "dst": TARGET, "msg": c, "timestamp": t0},
    ]
    record("2m. non-RM1 and unparseable rows are skipped", build_sessions(junk, NOW, {}, ()) == [])


def _case_verified(record: Any) -> None:
    t0 = CTR * 1000
    c = cmd_text(CTR, "status")
    r = reply_text(CTR, "ok t=4 p=2/2/22")
    rows = [cmd_row(t0 + 100, c), rep_row(t0 + 5_000, r)]

    def ex_of(keys: dict[str, bytes], data: list[dict[str, Any]] = rows) -> dict[str, Any]:
        return cast(dict[str, Any], build_sessions(data, NOW, keys, ())[0]["exchanges"][0])

    good = ex_of({TARGET: KEY})
    record("3a. right key: verified is true", good.get("verified") is True)
    record(
        "3b. wrong key: the verified key is absent (never false)",
        "verified" not in ex_of({TARGET: WRONG}),
    )
    record("3c. no key: the verified key is absent", "verified" not in ex_of({}))
    record(
        "3d. a key for another target does not verify", "verified" not in ex_of({"DK5EN-91": KEY})
    )
    forged = [cmd_row(t0 + 100, c), rep_row(t0 + 5_000, r[:-1] + ("0" if r[-1] != "0" else "1"))]
    record(
        "3e. a forged reply tag with a command that verifies is not verified",
        "verified" not in ex_of({TARGET: KEY}, forged),
    )
    only_cmd = ex_of({TARGET: KEY}, rows[:1])
    record(
        "3f. a reply-less exchange verifies through its command", only_cmd.get("verified") is True
    )
    dumped = json.dumps(build_sessions(rows, NOW, {TARGET: KEY}, [CMDR]))
    tags = _tags(c) + _tags(r)
    record(
        "3g. no tag (16 hex) in the serialised output",
        len(tags) == 2 and all(t not in dumped for t in tags),
    )


def _case_forgery(record: Any) -> None:
    """A frame that fails the stored key never displaces one that passes (and vice versa)."""
    t0 = CTR * 1000
    bogus = "0" * 16
    c = cmd_text(CTR, "status")
    r = reply_text(CTR, "ok t=4 p=2/2/22")
    fc = f"RM1 {CTR} reboot {bogus}"
    fr = f"RM1 {CTR} ok t=9 p=0/0/0 {bogus}"
    keys = {TARGET: KEY}

    def ex_of(data: list[dict[str, Any]], k: dict[str, bytes]) -> dict[str, Any]:
        return cast(dict[str, Any], build_sessions(data, NOW, k, ())[0]["exchanges"][0])

    ex = ex_of(
        [cmd_row(t0 + 10, fc), cmd_row(t0 + 100, c), rep_row(t0 + 5_000, r)],
        keys,
    )
    record(
        "3h. forged command first, genuine command + reply after: genuine shown, verified",
        ex["cmd"] == "status" and ex.get("verified") is True and ex["command_ms"] == t0 + 100,
    )
    ex = ex_of([cmd_row(t0 + 10, fc), rep_row(t0 + 5_000, r)], keys)
    record(
        "3i. forged command + genuine reply only: not verified",
        ex["cmd"] == "reboot" and "verified" not in ex,
    )
    ex = ex_of([cmd_row(t0 + 100, c), rep_row(t0 + 4_000, fr), rep_row(t0 + 5_000, r)], keys)
    record(
        "3j. forged reply first, genuine reply after: genuine reply shown, verified",
        ex["reply"] is not None
        and ex["reply"]["fields"][0] == ["t", "4"]
        and ex["reply"]["reply_ms"] == t0 + 5_000
        and ex.get("verified") is True,
    )
    ex = ex_of(
        [
            cmd_row(t0 + 100, c),
            cmd_row(t0 + 200, fc),
            rep_row(t0 + 5_000, r),
            rep_row(t0 + 6_000, fr),
        ],
        keys,
    )
    record(
        "3k. genuine first, forged after: the genuine frames are kept, verified",
        ex["cmd"] == "status"
        and ex["reply"] is not None
        and ex["reply"]["fields"][0] == ["t", "4"]
        and ex.get("verified") is True,
    )
    forged_rows = [cmd_row(t0 + 10, fc), rep_row(t0 + 4_000, fr), rep_row(t0 + 5_000, r)]
    ex = ex_of(forged_rows, {})
    dumped = json.dumps(build_sessions(forged_rows, NOW, {}, ()))
    record(
        "3l. no key: first frame wins, verified absent everywhere",
        ex["cmd"] == "reboot"
        and ex["reply"]["fields"][0] == ["t", "9"]
        and '"verified"' not in dumped,
    )
    sync_c = cmd_text(0, "sync")
    sync_r = reply_text(0, "ok ctr=1791705000 v=2")
    forged_sync = f"RM1 0 ok ctr=1 v=2 {bogus}"
    sync_rows = [cmd_row(t0, sync_c), rep_row(t0 + 1_000, forged_sync), rep_row(t0 + 3_000, sync_r)]
    ex = ex_of(sync_rows, keys)
    record(
        "3m. ctr 0: a genuine sync reply replaces an unverified one in the same exchange",
        len(build_sessions(sync_rows, NOW, keys, ())[0]["exchanges"]) == 1
        and ex["reply"] is not None
        and ex["reply"]["fields"][0] == ["ctr", "1791705000"]
        and ex.get("verified") is True,
    )


async def _insert(db: Path, rows: list[tuple[int, str, str, str, str]]) -> None:
    def _run() -> None:
        with db_write(db) as conn:
            for i, (ts, src, dst, msg, typ) in enumerate(rows):
                conn.execute(
                    "INSERT INTO messages (msg_id, src, dst, msg, type, timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (f"M{i}", src, dst, msg, typ, ts),
                )

    await asyncio.to_thread(_run)


class _Provider:
    def __init__(self, svc: Any) -> None:
        self.node_admin_service = svc


class _FakeObserved:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def observed(self, hours: int) -> list[dict[str, Any]]:
        self.calls.append(hours)
        return [{"commander": CMDR, "target": TARGET, "exchanges": []}]


async def _case_route(record: Any) -> None:
    fake = _FakeObserved()
    app = FastAPI()
    app.include_router(build_node_admin_router(cast(Any, _Provider(fake)), lambda: ()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as cl:
        default = await cl.get("/api/node-admin/observed")
        week = await cl.get("/api/node-admin/observed", params={"hours": 168})
        zero = await cl.get("/api/node-admin/observed", params={"hours": 0})
        over = await cl.get("/api/node-admin/observed", params={"hours": 169})
        text = await cl.get("/api/node-admin/observed", params={"hours": "x"})
        evil = await cl.get("/api/node-admin/observed", headers={"host": "evil.example.com"})
        cross = await cl.get(
            "/api/node-admin/observed", headers={"origin": "http://evil.example.com"}
        )
    record(
        "4a. route: 200 with the list; hours defaults to 24 and passes 168 through",
        default.status_code == 200
        and default.json() == [{"commander": CMDR, "target": TARGET, "exchanges": []}]
        and fake.calls == [24, 168]
        and week.status_code == 200,
    )
    record(
        "4b. route: hours outside 1..168 or not a number is a 422, service not called",
        zero.status_code == over.status_code == text.status_code == 422 and fake.calls == [24, 168],
    )
    record(
        "4c. route: foreign Host and foreign Origin are 403 (existing guard)",
        evil.status_code == 403 and cross.status_code == 403 and fake.calls == [24, 168],
    )
    absent = FastAPI()
    absent.include_router(build_node_admin_router(cast(Any, _Provider(None)), lambda: ()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=absent), base_url="http://localhost"
    ) as cl:
        none = await cl.get("/api/node-admin/observed")
    record("4d. route: service absent is a 503", none.status_code == 503)


async def _case_live(
    record: Any,
    svc: NodeAdminService,
    storage: Any,
    bcasts: list[tuple[str, dict[str, Any]]],
    stored: list[str],
) -> None:
    """Live `node_admin:observed` events (5d-5i); `stored` are the texts already in the DB."""
    c, r = stored
    bcasts.clear()
    live_cmd = cmd_text(CTR + 50, "txpower 1", commander=FOREIGN)
    await svc.on_reply({"src": FOREIGN, "dst": TARGET, "msg": live_cmd + "{042", "type": "msg"})
    await svc.drain()
    seen = [p for e, p in bcasts if e == "node_admin:observed"]
    live_payloads = list(seen)
    record(
        "5d. live: a foreign COMMAND emits node_admin:observed with the updated session",
        len(seen) == 1
        and seen[0]["commander"] == FOREIGN
        and seen[0]["target"] == TARGET
        and seen[0]["exchanges"][0]["ctr"] == CTR + 50
        and seen[0]["exchanges"][0]["cmd"] == "txpower"
        and seen[0]["exchanges"][0]["reply"] is None
        and len(seen[0]["exchanges"]) == 2,
    )
    bcasts.clear()
    live_rep = reply_text(CTR + 50, "err range 2..22", commander=FOREIGN)
    await svc.on_reply({"src": TARGET, "dst": FOREIGN, "msg": live_rep, "type": "msg"})
    await svc.drain()
    seen = [p for e, p in bcasts if e == "node_admin:observed"]
    live_payloads += seen
    top = seen[0]["exchanges"][0] if seen else {}
    record(
        "5e. live: a foreign REPLY emits the same session with the reply attached",
        len(seen) == 1
        and seen[0]["commander"] == FOREIGN
        and top.get("reply", {}).get("status") == "err"
        and top.get("verified") is True,
    )
    bcasts.clear()
    await svc.on_reply({"src": FOREIGN, "dst": TARGET, "msg": "RM1 nonsense", "type": "msg"})
    await svc.on_reply({"src": FOREIGN, "dst": TARGET, "msg": "hello", "type": "msg"})
    await svc.drain()
    record(
        "5f. live: a non-frame emits no observed event",
        not [e for e, _ in bcasts if e == "node_admin:observed"],
    )
    # 5g asserts on the payloads of 5d/5e (collected BEFORE the clears above).
    dumped_live = [json.dumps(p) for p in live_payloads]
    secret_tags = _tags(live_cmd) + _tags(live_rep) + _tags(c) + _tags(r)
    raw_msgs = [live_cmd, live_rep, c, r]
    record(
        "5g. no tag (16 hex) and no raw msg in the live 5d/5e payloads",
        len(live_payloads) == 2
        and len(secret_tags) == 4
        and all(t not in d for d in dumped_live for t in secret_tags)
        and all(m not in d for d in dumped_live for m in raw_msgs)
        and all("RM1 " not in d for d in dumped_live),
    )

    await _case_live_pair(record, svc, storage, bcasts)


async def _case_live_pair(
    record: Any,
    svc: NodeAdminService,
    storage: Any,
    bcasts: list[tuple[str, dict[str, Any]]],
) -> None:
    """5h/5i: the live build runs off the loop and only sees the frame's own pair."""
    t0 = CTR * 1000
    other_cmdr, other_target = "DL2XYZ", "DK5EN-91"
    other = cmd_text(CTR + 1, "status", target=other_target, commander=other_cmdr)
    await _insert(storage.db_path, [(t0 + 7_000, other_cmdr, other_target, other, "msg")])
    main_thread = threading.get_ident()
    calls: list[tuple[int, list[Mapping[str, Any]]]] = []
    mod = cast(Any, node_admin_service)
    real_build = mod.build_sessions

    def spy(rows: Any, *args: Any, **kw: Any) -> list[dict[str, Any]]:
        listed = list(rows)
        calls.append((threading.get_ident(), listed))
        return cast(list[dict[str, Any]], real_build(listed, *args, **kw))

    mod.build_sessions = spy
    try:
        bcasts.clear()
        live2 = cmd_text(CTR + 60, "status", commander=FOREIGN)
        await svc.on_reply({"src": FOREIGN, "dst": TARGET, "msg": live2, "type": "msg"})
        await svc.drain()
        seen = [p for e, p in bcasts if e == "node_admin:observed"]
        [(live_thread, live_rows)] = calls[-1:]
        pair_calls = len(calls)
        await svc.observed(24)
        obs_thread = calls[-1][0]
    finally:
        mod.build_sessions = real_build
    pair_names = {FOREIGN, TARGET}
    record(
        "5h. live: build_sessions runs off the event loop, on the frame's pair rows only",
        pair_calls == 1
        and live_thread != main_thread
        and len(live_rows) >= 3
        and all({r_["src"], r_["dst"]} == pair_names for r_ in live_rows)
        and obs_thread != main_thread,
    )
    record(
        "5i. live: the pair's session is still broadcast with the new exchange",
        len(seen) == 1
        and (seen[0]["commander"], seen[0]["target"]) == (FOREIGN, TARGET)
        and seen[0]["exchanges"][0]["ctr"] == CTR + 60
        and seen[0]["exchanges"][0].get("verified") is True,
    )


async def _case_service(record: Any) -> None:
    bcasts: list[tuple[str, dict[str, Any]]] = []

    async def transmit(_t: str, _d: str, _m: str) -> str | None:
        raise AssertionError("observed sessions must never transmit")

    async def broadcast(event: str, payload: dict[str, Any]) -> None:
        bcasts.append((event, payload))

    async def no_sleep(_s: float) -> None:
        await asyncio.sleep(3600)

    t0 = CTR * 1000
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        storage = await create_sqlite_storage(tmp / "observed_test.db")
        box = SecretBox(key_path=tmp / "secret.key", binding=b"board:TEST")
        clock = {"now": NOW}
        svc = NodeAdminService(
            storage,
            box,
            transmit,
            lambda: "DK5EN-14",
            lambda: False,
            broadcast,
            clock_ms=lambda: clock["now"],
            sleep=no_sleep,
        )
        try:
            await svc.set_key(TARGET, PASSWD, 15)
            c = cmd_text(CTR, "status", commander=FOREIGN)
            r = reply_text(CTR, "ok t=4 p=2/2/22", commander=FOREIGN)
            await _insert(
                storage.db_path,
                [
                    (t0 + 100, FOREIGN, TARGET, c, "msg"),
                    (t0 + 5_000, TARGET, FOREIGN, r, "msg"),
                    (t0 + 5_100, TARGET, FOREIGN, r, "pos"),  # wrong type: ignored
                    (t0 + 6_000, "DL9XYZ", "20", "RM1 not a frame", "msg"),
                    (NOW - 200 * 3_600_000, FOREIGN, TARGET, c, "msg"),  # outside 24 h
                ],
            )
            rows = await storage.get_rm1_rows(NOW - 24 * 3_600_000)
            record(
                "5a. get_rm1_rows: type msg, strict RM1 shape, window, oldest first",
                [x["timestamp"] for x in rows] == [t0 + 100, t0 + 5_000],
            )
            sessions = await svc.observed(24)
            [ex] = sessions[0]["exchanges"]
            record(
                "5b. service.observed: foreign session, verified through the stored key, not own",
                len(sessions) == 1
                and sessions[0]["commander"] == FOREIGN
                and sessions[0]["own"] is False
                and ex["verified"] is True
                and ex["reply"]["fields"][0] == ["t", "4"],
            )
            week = await svc.observed(168)
            record(
                "5c. a 168 h window still excludes the 200 h old row",
                len(week[0]["exchanges"]) == 1,
            )

            await _case_live(record, svc, storage, bcasts, [c, r])
        finally:
            await svc.stop()
            await storage.close()


async def run_node_admin_observed_tests() -> bool:
    """Return True iff every Observed-sessions case passes."""
    if has_console:
        print("\n🧪 Testing node admin observed sessions:")
        print("=" * 55)

    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: Any) -> None:
        results.append((label, bool(ok)))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _case_parse(_record)
    _case_sessions(_record)
    _case_verified(_record)
    _case_forgery(_record)
    await _case_route(_record)
    await _case_service(_record)

    passed = sum(1 for _, ok in results if ok)
    if has_console or passed != len(results):
        for label, ok in results:
            if not ok:
                print(f"❌ FAIL | {label}")
        print(f"\n🧪 Node Admin Observed Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    _ok = asyncio.run(run_node_admin_observed_tests())
    print(f"node_admin_observed: {'PASS' if _ok else 'FAIL'}")
    raise SystemExit(0 if _ok else 1)
