"""Regression suite for the Node Admin service (`node_admin_service.py`, RM1 remote admin).

Plan: doc/2026-10-05_1000-node-admin-ui-concept-and-plan.md §5, §6.1 B1. Fully
offline and transmits nothing: a real SQLite storage on a temp DB, a real
`SecretBox` on a temp key file, and recorders for `transmit`, `broadcast`, the
clock and `sleep` (a virtual-time scheduler: `FakeTime.advance`).

Required cases (each one fails on a named mutation, see the task log):

  a. WIRING REPLAY: every command vector of `remote_cmd_vectors.json` goes
     through the real `set_key` + `send`/`sync` and must leave the service as
     exactly `(dst, dm_text)`; every reply vector, bare and with the UDP `{087`
     suffix, must verify an open row through `on_reply`.
  b. Reply guards: wrong tag, forged sync reply carrying ctr 4294967295,
     unknown ctr, foreign source, foreign destination, relay-path source.
  c. Monotone state: verified is terminal with one broadcast; bad tag then
     genuine ends verified; BLE + UDP copies racing act exactly once.
  d. Fake clock: 119 999 ms waiting, 120 000 ms no reply, a late reply flips
     it, the startup sweep abandons stale rows and a late reply still flips them.
  e. Auto-sync and spacing: the first send publishes only the sync; the command
     follows a verified sync reply by >= 10 s; 90 s of silence DROPS it with a
     warning; in-flight, 10 s and lockout refusals.
  f. Re-ask: byte-identical, no counter, no new row, refusal rules.
  g. AAD and case, unreadable key.
  h. The password never appears in logs, broadcasts, frames, rows or files.
  i. Refusals burn no counter; a failed transmit keeps its row.
  j. Pure helpers, prefilter, counter floor, key validation.
  m. W0 regressions (remote-view campaign).
  n. W2a: post-silence cool-down, foreign-sender pause, sync gate drop, re-flashed
     node recovery, refusals before allocation, the `/state` document.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from . import remote_cmd
from .node_admin_service import (
    SYNC_DROPPED,
    NodeAdminService,
    _lockout_until_ms,
    compute_state,
)
from .node_admin_types import NodeAdminBusyError, NodeAdminUnavailableError
from .node_admin_types import NodeAdminService as NodeAdminServiceProtocol
from .secret_box import SecretBox, SecretBoxError
from .sqlite_storage import SQLiteStorage, create_sqlite_storage
from .storage.constants import db_write

Record = Callable[[str, bool], None]

T0 = 1_791_000_000_000
TARGET = "DK5EN-90"
ATTACHED = "DK5EN-14"
PASSWD = "secret"
SENTINEL = "Zq9!pw-sentnl"  # 13 chars: a valid node password
SETTLE_S = 0.05

_CORRUPT_SQL = "UPDATE node_admin_keys SET password_enc = ?"
_CORRUPT_TOKEN = "v1:" + "A" * 48  # well-formed base64, fails the GCM tag
_VECTORS = Path(__file__).with_name("remote_cmd_vectors.json")


# ── harness ────────────────────────────────────────────────────────────────


class FakeTime:
    """Virtual clock + `sleep`: a sleeper wakes when `advance` reaches its deadline."""

    def __init__(self) -> None:
        self.now = T0
        self.sleepers: list[tuple[int, asyncio.Future[None]]] = []

    def clock(self) -> int:
        return self.now

    async def sleep(self, seconds: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (self.now + round(seconds * 1000), fut)
        self.sleepers.append(entry)
        try:
            await fut
        finally:
            self.sleepers.remove(entry)

    def pending_deltas(self) -> list[int]:
        return [d - self.now for d, f in self.sleepers if not f.done()]

    async def advance(self, ms: int) -> None:
        target = self.now + ms
        while True:
            due = sorted(
                (e for e in self.sleepers if e[0] <= target and not e[1].done()),
                key=lambda e: e[0],
            )
            if not due:
                break
            deadline, fut = due[0]
            self.now = max(self.now, deadline)
            fut.set_result(None)
            await settle()
        self.now = target
        await settle()


async def settle() -> None:
    await asyncio.sleep(SETTLE_S)


async def until(cond: Callable[[], bool], limit_s: float = 3.0) -> bool:
    end = asyncio.get_running_loop().time() + limit_s
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


async def raises(exc: type[BaseException], coro: Awaitable[object]) -> bool:
    try:
        await coro
    except exc:
        return True
    except Exception:
        return False
    return False


def seed_ctr(db_path: Path, target: str, ctr: int) -> None:
    with db_write(db_path) as conn:
        conn.execute("UPDATE node_admin_state SET ctr = ? WHERE target_call = ?", (ctr, target))


async def seed_row(env: Env, handed_off_at: int, cmd: str = "status") -> dict[str, Any]:
    """A command row written straight to storage: counter allocated, hand-off stamped, no frame.

    The service itself can no longer produce two silent rows less than 5 min apart (the
    post-silence cool-down refuses the second frame), but the lockout guard stays a backstop
    for what it cannot see; its tests seed the rows the node may have counted.
    """
    row = await env.storage.allocate_node_admin_command(
        TARGET, ATTACHED, cmd, None, "udp", handed_off_at, 0, lambda c: f"RM1 {c} {cmd} seeded"
    )
    await env.storage.mark_node_admin_handed_off(row["id"], handed_off_at)
    return {**row, "log_id": row["id"]}


def reply_text(passwd: str, target: str, src_call: str, ctr: int, result: str) -> str:
    key = remote_cmd.derive_key(passwd)
    return f"RM1 {ctr} {result} {remote_cmd.rm_reply_tag(key, target, src_call, ctr, result)}"


def break_tag(text: str) -> str:
    head, tag = text[:-16], text[-16:]
    return head + ("0" if tag[0] != "0" else "1") + tag[1:]


class Env:
    def __init__(  # noqa: PLR0913 - harness knobs
        self,
        storage: SQLiteStorage,
        box: SecretBox,
        tmp: Path,
        *,
        attached: str | None,
        ble: bool,
        unix: int,
    ) -> None:
        self.storage = storage
        self.box = box
        self.tmp = tmp
        self.time = FakeTime()
        self.attached = attached
        self.ble = ble
        self.unix = unix
        self.tx: list[tuple[str, str, str]] = []  # (transport, dst, msg)
        self.tx_result: str | None = None
        self.tx_raise = False
        self.tx_gate: asyncio.Event | None = None
        self.bcasts: list[tuple[str, dict[str, Any]]] = []
        self.svc = self.new_service()

    async def _transmit(self, transport: str, dst: str, msg: str) -> str | None:
        self.tx.append((transport, dst, msg))
        if self.tx_gate is not None:
            await self.tx_gate.wait()
        if self.tx_raise:
            raise RuntimeError("transmit exploded")
        return self.tx_result

    async def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        self.bcasts.append((event, dict(payload)))

    def new_service(self) -> NodeAdminService:
        return NodeAdminService(
            self.storage,
            self.box,
            self._transmit,
            lambda: self.attached,
            lambda: self.ble,
            self._broadcast,
            clock_ms=self.time.clock,
            unix_s=lambda: self.unix,
            sleep=self.time.sleep,
        )

    async def setup(
        self, target: str = TARGET, passwd: str = PASSWD, *, tx_max: int = 15, synced: bool = True
    ) -> None:
        await self.svc.set_key(target, passwd, tx_max)
        if synced:
            self.svc._synced.add(target)

    async def rows(self, target: str = TARGET) -> list[dict[str, Any]]:
        return await self.svc.history(target, 100)

    async def state_of(self, row_id: int, target: str = TARGET) -> str | None:
        for r in await self.rows(target):
            if r["id"] == row_id:
                return str(r["state"])
        return None

    async def target_info(self, target: str = TARGET) -> dict[str, Any]:
        for t in await self.svc.list_targets():
            if t["target"] == target:
                return t
        raise AssertionError(f"no target {target}")

    async def hwm(self, target: str = TARGET) -> int:
        return int((await self.target_info(target))["last_hwm"])

    async def ctr(self, target: str = TARGET) -> int:
        return int((await self.target_info(target))["ctr"])

    def frame(  # noqa: PLR0913 - one knob per frame field
        self,
        ctr: int,
        result: str = "ok status",
        *,
        suffix: str = "",
        src: str = TARGET,
        dst: str = ATTACHED,
        passwd: str = PASSWD,
        bad: bool = False,
        src_call: str = ATTACHED,
    ) -> dict[str, Any]:
        text = reply_text(passwd, TARGET, src_call, ctr, result)
        if bad:
            text = break_tag(text)
        return {"src": src, "dst": dst, "msg": text + suffix, "type": "msg", "src_type": "udp"}

    async def reply(self, ctr: int, result: str = "ok status", **kw: Any) -> None:
        await self.svc.on_reply(self.frame(ctr, result, **kw))

    def bc_for(self, row_id: int) -> list[dict[str, Any]]:
        return [p for e, p in self.bcasts if e == "node_admin:reply" and p.get("id") == row_id]

    async def send(
        self, cmd: str = "status", args: str = "", transport: str = "udp"
    ) -> dict[str, Any]:
        return await self.svc.send(TARGET, cmd, args, transport)

    async def send_after_cooldown(self, cmd: str = "status", args: str = "") -> dict[str, Any]:
        """Wait out the post-silence hold (300 s after the last hand-off), then send."""
        info = (await self.state())["cooldown_until"]
        if info is not None:
            await self.time.advance(int(info) - self.time.now)
        return await self.send(cmd, args)

    async def state(self, target: str = TARGET) -> dict[str, Any]:
        return await self.svc.state(target)


@contextlib.asynccontextmanager
async def make_env(
    *, attached: str | None = ATTACHED, ble: bool = False, unix: int = 0
) -> AsyncIterator[Env]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        storage = await create_sqlite_storage(tmp / "node_admin_service_test.db")
        box = SecretBox(key_path=tmp / "secret.key", binding=b"board:TEST")
        env = Env(storage, box, tmp, attached=attached, ble=ble, unix=unix)
        try:
            yield env
        finally:
            await env.svc.stop()
            await storage.close()


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


# ── a. wiring replay ───────────────────────────────────────────────────────


async def case_a_wiring(record: Record) -> None:
    vectors = json.loads(_VECTORS.read_text(encoding="utf-8"))
    bad: list[str] = []
    for v in vectors["commands"]:
        async with make_env(attached=v["src"], unix=0) as env:
            await env.setup(v["dst"], v["passwd"], tx_max=30)
            if v["ctr"] == 0:
                got = await env.svc.sync(v["dst"])
            else:
                seed_ctr(env.storage.db_path, v["dst"], v["ctr"] - 1)
                got = await env.svc.send(v["dst"], v["cmd"], v["args"], "udp")
            await env.svc.drain()
            if [(d, m) for _, d, m in env.tx] != [(v["dst"], v["dm_text"])]:
                bad.append(f"{v['dm_text']!r} -> {env.tx!r}")
            if got["log_id"] < 1:
                bad.append(f"no log id for {v['dm_text']!r}")
            if v["ctr"] and got["ctr"] != v["ctr"]:
                bad.append(f"ctr {got['ctr']} != {v['ctr']}")
    record(f"a1. all {len(vectors['commands'])} command vectors leave as (dst, dm_text)", not bad)
    if bad:
        print("      ", bad[:3])


async def case_a_replies(record: Record) -> None:
    vectors = json.loads(_VECTORS.read_text(encoding="utf-8"))
    bad: list[str] = []
    for v in vectors["replies"]:
        for suffix in ("", "{087"):
            async with make_env(attached=v["src"], unix=0) as env:
                await env.setup(v["dst"], v["passwd"], tx_max=30)
                if v["ctr"] == 0:
                    sent = await env.svc.sync(v["dst"])
                else:
                    seed_ctr(env.storage.db_path, v["dst"], v["ctr"] - 1)
                    sent = await env.svc.send(v["dst"], "status", "", "udp")
                await env.svc.drain()
                frame = {"src": v["dst"], "dst": v["src"], "msg": v["reply_text"] + suffix}
                await env.svc.on_reply(frame)
                await env.svc.drain()
                rows = await env.svc.history(v["dst"], 10)
                row = next(r for r in rows if r["id"] == sent["log_id"])
                if row["state"] != "verified" or row["reply_text"] != v["reply_text"]:
                    bad.append(f"{v['reply_text']!r}{suffix!r} -> {row['state']}")
    record(
        f"a2. all {len(vectors['replies'])} reply vectors verify, bare and with the {{NNN suffix",
        not bad,
    )
    if bad:
        print("      ", bad[:3])


# ── b. reply guards ────────────────────────────────────────────────────────


async def case_b_guards(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.reply(r["ctr"], bad=True)
        await env.svc.drain()
        row = (await env.rows())[0]
        record(
            "b1. wrong tag -> bad_tag, hwm unmoved, counted as rejected",
            row["state"] == "bad_tag" and await env.hwm() == 0 and env.svc.rejected_replies == 1,
        )
        await env.reply(r["ctr"], passwd="nope-wrong-pw")
        await env.svc.drain()
        record(
            "b1b. a reply under another key is also bad_tag",
            await env.state_of(r["log_id"]) == "bad_tag",
        )

    async with make_env() as env:
        await env.setup(synced=False)
        await env.send()
        await settle()
        forged = env.frame(0, "ok ctr=4294967295 v=4.40a", bad=True)
        await env.svc.on_reply(forged)
        await settle()
        sync_row = (await env.rows())[0]
        info = await env.target_info()
        record(
            "b2. forged sync reply (ctr=4294967295, bad tag): bad_tag, hwm 0, no last_sync, "
            "gate not released, command not sent",
            sync_row["cmd"] == "sync"
            and sync_row["state"] == "bad_tag"
            and info["last_hwm"] == 0
            and info["last_sync_at"] is None
            and TARGET in env.svc._pending
            and not env.svc._pending[TARGET].released.is_set()
            and len(env.tx) == 1
            and 10_000 not in env.time.pending_deltas(),
        )
        await env.reply(0, "ok ctr=5 v=4.40a")
        await until(lambda: 10_000 in env.time.pending_deltas())
        record(
            "b2b. the genuine sync reply after a forged one still verifies and releases the gate",
            await env.state_of(sync_row["id"]) == "verified" and await env.hwm() == 5,
        )

    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        before = len(env.bcasts)
        await env.reply(99)
        await env.reply(0)
        await env.svc.drain()
        rows = await env.rows()
        record(
            "b3. unknown ctr (valid tag) changes no row and emits nothing",
            len(rows) == 1 and rows[0]["state"] == "waiting" and len(env.bcasts) == before,
        )
        await env.reply(r["ctr"], src="DK5EN-91")
        await env.svc.drain()
        record(
            "b4. reply from a non-target src is ignored",
            await env.state_of(r["log_id"]) == "waiting",
        )
        await env.reply(r["ctr"], dst="DL1ABC")
        await env.svc.drain()
        record(
            "b5. reply whose dst is not the row's src_call is ignored",
            await env.state_of(r["log_id"]) == "waiting",
        )
        await env.reply(r["ctr"], src="DK5EN-90,DL1ABC")
        await env.svc.drain()
        record(
            "b6. relay-path src still correlates",
            await env.state_of(r["log_id"]) == "verified",
        )

    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.reply(r["ctr"], dst="DL1ABC,DK5EN-14")
        await env.svc.drain()
        record(
            "b7. via-routed dst resolves to its last component",
            await env.state_of(r["log_id"]) == "verified",
        )


# ── c. monotone state ──────────────────────────────────────────────────────


async def case_c_monotone(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.reply(r["ctr"])
        await env.svc.drain()
        n = len(env.bc_for(r["log_id"]))
        await env.reply(r["ctr"], "ok forged", bad=True)
        await env.reply(r["ctr"], "ok forged", bad=True, suffix="{012")
        await env.svc.drain()
        verified_events = [p for p in env.bc_for(r["log_id"]) if p["state"] == "verified"]
        row = (await env.rows())[0]
        record(
            "c1. verified then forged bad-tag stays verified, exactly one verified broadcast",
            row["state"] == "verified"
            and row["reply_text"] == reply_text(PASSWD, TARGET, ATTACHED, r["ctr"], "ok status")
            and len(verified_events) == 1
            and len(env.bc_for(r["log_id"])) == n
            and env.svc.rejected_replies == 0,
        )

    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.reply(r["ctr"], bad=True)
        await env.svc.drain()
        s1 = await env.state_of(r["log_id"])
        await env.reply(r["ctr"])
        await env.svc.drain()
        states = [p["state"] for p in env.bc_for(r["log_id"])]
        record(
            "c2. bad tag then genuine ends verified (waiting, bad_tag, verified)",
            s1 == "bad_tag"
            and await env.state_of(r["log_id"]) == "verified"
            and states == ["waiting", "bad_tag", "verified"]
            and await env.hwm() == r["ctr"],
        )

    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        real_apply = env.storage.apply_node_admin_reply
        real_raise = env.storage.raise_node_admin_hwm
        raises_seen: list[int] = []

        async def slow_apply(*a: Any, **kw: Any) -> bool:
            await asyncio.sleep(0.05)  # both copies are past find+verify before either writes
            return await real_apply(*a, **kw)

        async def spy_raise(target: str, hwm: int, sync_at_ms: int | None = None) -> None:
            raises_seen.append(hwm)
            await real_raise(target, hwm, sync_at_ms)

        env.storage.apply_node_admin_reply = slow_apply  # type: ignore[method-assign]  # test double
        env.storage.raise_node_admin_hwm = spy_raise  # type: ignore[method-assign]  # test double
        await asyncio.gather(
            env.svc.on_reply(env.frame(r["ctr"])),
            env.svc.on_reply(env.frame(r["ctr"], suffix="{087")),
        )
        await env.svc.drain()
        verified_events = [p for p in env.bc_for(r["log_id"]) if p["state"] == "verified"]
        record(
            "c3. BLE + UDP copies racing: one verified transition, one broadcast, one hwm raise",
            len(verified_events) == 1 and raises_seen == [r["ctr"]],
        )


# ── d. fake clock ──────────────────────────────────────────────────────────


async def case_d_clock(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.time.advance(119_999)
        s1 = await env.state_of(r["log_id"])
        await env.time.advance(1)
        s2 = await env.state_of(r["log_id"])
        await env.reply(r["ctr"])
        await env.svc.drain()
        s3 = await env.state_of(r["log_id"])
        record(
            "d1. 119_999 ms waiting, 120_000 ms no_reply, a late verified reply flips it",
            (s1, s2, s3) == ("waiting", "no_reply", "verified"),
        )

    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        await env.reply(a["ctr"])
        await env.svc.drain()
        await env.time.advance(20_000)
        b = await env.send()
        await env.svc.drain()
        await env.time.advance(1_000)
        await env.svc.start()
        sa, sb = await env.state_of(a["log_id"]), await env.state_of(b["log_id"])
        await env.reply(b["ctr"])
        await env.svc.drain()
        record(
            "d2. start() abandons the open row, keeps the verified one; a late reply flips it",
            (sa, sb) == ("verified", "abandoned") and await env.state_of(b["log_id"]) == "verified",
        )

    # abandoned wins over a recent hand-off but never over a verdict
    row: dict[str, Any] = {"handed_off_at": T0, "result": "abandoned", "reply_text": None}
    record(
        "d3. compute_state: abandoned only without a reply",
        compute_state(row, T0 + 1) == "abandoned"
        and compute_state({**row, "reply_text": "x", "verified": 0}, T0 + 1) == "bad_tag",
    )


# ── e. auto-sync and spacing ───────────────────────────────────────────────


async def case_e_sync_spacing(record: Record) -> None:
    key = remote_cmd.derive_key(PASSWD)
    async with make_env() as env:
        await env.setup(synced=False)
        res = await env.send()
        await until(lambda: len(env.tx) == 1)
        rows = await env.rows()
        busy = await raises(NodeAdminBusyError, env.send())
        record(
            "e1. first send publishes only the sync: one sync row, no counter, pending",
            res.get("pending") is True
            and res["ctr"] == 0
            and res["text"].startswith("RM1 0 sync ")
            and [m for _, _, m in env.tx] == [res["text"]]
            and len(rows) == 1
            and rows[0]["cmd"] == "sync"
            and rows[0]["args"] is None
            and await env.ctr() == 0
            and busy,
        )
        await env.time.advance(3_000)
        await env.reply(0, "ok ctr=5 v=4.40a")
        await until(lambda: 10_000 in env.time.pending_deltas())
        await settle()
        held = len(env.tx) == 1
        await env.time.advance(9_999)
        held_still = len(env.tx) == 1
        await env.time.advance(1)
        await until(lambda: len(env.tx) == 2)
        want = remote_cmd.build_command_text(key, TARGET, ATTACHED, 6, "status", "", 15)
        rows = await env.rows()
        record(
            "e2. command goes out only after a verified sync reply AND 10 s of sleep after it",
            held
            and held_still
            and len(env.tx) == 2
            and env.tx[1][2] == want
            and await env.hwm() == 5
            and len(rows) == 2
            and rows[0]["cmd"] == "status"
            and rows[0]["ctr"] == 6,
        )
        await until(lambda: any(p.get("cmd") == "status" for _, p in env.bcasts))
        cmd_events = [p for _, p in env.bcasts if p.get("cmd") == "status"]
        record(
            "e2b. the follow-on command row is broadcast, without a warning",
            len(cmd_events) == 1 and "warning" not in cmd_events[0],
        )
        # after the sync the target is synced: the next send is direct (after spacing + reply)
        await env.reply(6)
        await env.svc.drain()
        await env.time.advance(10_000)
        again = await env.send()
        await env.svc.drain()
        record("e2c. a synced target sends directly", again["ctr"] == 7 and "pending" not in again)


async def case_e_timeout_inflight(record: Record) -> None:
    async with make_env() as env:
        await env.setup(synced=False)
        await env.send()
        await until(lambda: len(env.tx) == 1)
        sync_id = (await env.rows())[0]["id"]
        await env.time.advance(89_999)
        held = TARGET in env.svc._pending and len(env.tx) == 1
        await env.time.advance(1)
        await until(lambda: TARGET not in env.svc._pending)
        await until(lambda: any("warning" in p for _, p in env.bcasts))
        warned = [p for _, p in env.bcasts if "warning" in p]
        record(
            "e3. 90 s without a sync reply DROPS the held command: no frame, no counter, "
            "no row, 'command dropped' warning on the sync row, target not synced",
            held
            and len(env.tx) == 1
            and await env.ctr() == 0
            and len(await env.rows()) == 1
            and len(warned) == 1
            and warned[0]["id"] == sync_id
            and warned[0]["warning"] == SYNC_DROPPED
            and warned[0]["warning"].startswith("command dropped: ")
            and TARGET not in env.svc._synced
            and not any(p.get("cmd") == "status" for _, p in env.bcasts),
        )
        # A genuine sync reply that arrives after the drop (still inside the 120 s window)
        # marks the target synced, but never revives the dropped command.
        await env.time.advance(10_000)
        await env.reply(0, "ok ctr=5 v=4.40a")
        await env.svc.drain()
        record(
            "e3b. a late verified sync marks the target synced; the dropped command is not revived",
            await env.state_of(sync_id) == "verified"
            and TARGET in env.svc._synced
            and len(env.tx) == 1
            and await env.ctr() == 0,
        )

    async with make_env() as env:
        await env.setup()
        env.tx_gate = asyncio.Event()
        a = await env.send()
        await until(lambda: len(env.tx) == 1)
        queued = await env.state_of(a["log_id"]) == "queued"
        busy_queued = await raises(NodeAdminBusyError, env.send())
        env.tx_gate.set()
        await env.svc.drain()
        await env.time.advance(15_000)
        busy_waiting = await raises(NodeAdminBusyError, env.send())
        await env.time.advance(105_000)
        ok = await env.send_after_cooldown()
        await env.svc.drain()
        record(
            "e4. one command in flight per target: queued and waiting refuse, no_reply frees it "
            "(after the cool-down)",
            queued and busy_queued and busy_waiting and ok["ctr"] == 2 and len(env.tx) == 2,
        )


async def case_e_dropped(record: Record) -> None:
    for label, drop_ble in (("BLE dropped", True), ("key deleted", False)):
        async with make_env(ble=True) as env:
            await env.setup(synced=False)
            await env.send(transport="ble")
            await until(lambda: len(env.tx) == 1)
            sync_id = (await env.rows())[0]["id"]
            await env.reply(0, "ok ctr=5 v=4.40a")
            await until(lambda: 10_000 in env.time.pending_deltas())
            if drop_ble:
                env.ble = False
            else:
                await env.svc.delete_key(TARGET)
            await env.time.advance(10_000)
            await until(lambda: any("warning" in p for _, p in env.bcasts))
            warned = [p for _, p in env.bcasts if "warning" in p]
            record(
                f"e8. {label} before the gate opens: the dropped command is broadcast",
                len(env.tx) == 1
                and len(warned) == 1
                and warned[0]["id"] == sync_id
                and warned[0]["warning"].startswith("command dropped: ")
                and TARGET not in env.svc._pending,
            )


async def case_e_spacing(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        await env.time.advance(5_000)
        await env.reply(a["ctr"])
        await env.svc.drain()
        await env.time.advance(9_999)
        early = await raises(NodeAdminBusyError, env.send())
        await env.time.advance(1)
        late = await env.send()
        record(
            "e5. a send < 10 s after the previous REPLY is refused (anchor is the reply)",
            early and late["ctr"] == a["ctr"] + 1,
        )

    async with make_env() as env:
        await env.setup()
        await env.send()
        await env.svc.drain()
        await env.time.advance(125_000)
        second = await seed_row(env, env.time.now)
        await env.time.advance(125_000)
        locked = await raises(NodeAdminBusyError, env.send())
        until_ms = (await env.target_info())["possible_lockout_until_ms"]
        await env.time.advance(174_999)
        still = await raises(NodeAdminBusyError, env.send())
        await env.time.advance(1)
        freed = await env.send()
        record(
            "e6. two no_reply rows within 300 s: possible lockout until 300 s after the later one",
            locked
            and until_ms == T0 + 125_000 + 300_000
            and still
            and freed["ctr"] == second["ctr"] + 1
            and (await env.target_info())["possible_lockout_until_ms"] is None,
        )

    async with make_env() as env:
        await env.setup(synced=False)
        await env.send()
        await settle()
        sync_row = (await env.rows())[0]
        await env.svc.stop()
        record(
            "e7. stop() cancels a pending auto-sync cleanly",
            sync_row["cmd"] == "sync" and not env.svc._pending and not env.svc._tasks,
        )


# ── f. re-ask ──────────────────────────────────────────────────────────────


async def case_f_reask(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        too_early = await raises(NodeAdminBusyError, env.svc.reask(a["log_id"]))
        await env.time.advance(59_000)
        still_early = await raises(NodeAdminBusyError, env.svc.reask(a["log_id"]))
        record("f1a. re-ask refused before RM_REASK_MIN_MS (reply still in flight)", still_early)
        await env.time.advance(2_000)
        ctr_before = await env.ctr()
        res = await env.svc.reask(a["log_id"])
        await env.svc.drain()
        rows = await env.rows()
        record(
            "f1. re-ask resends the byte-identical frame: no counter, no new row, window restarted",
            too_early
            and res == {"log_id": a["log_id"]}
            and len(env.tx) == 2
            and env.tx[1][2] == env.tx[0][2]
            and await env.ctr() == ctr_before
            and len(rows) == 1
            and rows[0]["handed_off_at"] == env.time.now
            and rows[0]["state"] == "waiting",
        )
        again = await raises(NodeAdminBusyError, env.svc.reask(a["log_id"]))
        record("f2. a second re-ask inside 10 s is refused", again)
        unknown = await raises(ValueError, env.svc.reask(9999))
        record("f2b. an unknown log id is a ValueError", unknown)

    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        await env.time.advance(300_000)
        b = await env.send()
        await env.svc.drain()
        await env.time.advance(11_000)
        not_newest = await raises(NodeAdminBusyError, env.svc.reask(a["log_id"]))
        await env.reply(b["ctr"])
        await env.svc.drain()
        await env.time.advance(11_000)
        verified_row = await raises(NodeAdminBusyError, env.svc.reask(b["log_id"]))
        record("f3. refused unless newest", not_newest)
        record("f4. refused for a verified row", verified_row)


async def case_f_reask_rules(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        r = await env.send("reboot")
        await env.svc.drain()
        await env.time.advance(11_000)
        record(
            "f5. refused for reboot", await raises(NodeAdminBusyError, env.svc.reask(r["log_id"]))
        )

    async with make_env() as env:
        await env.setup()
        s = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(11_000)
        record(
            "f6. refused for a sync row",
            await raises(NodeAdminBusyError, env.svc.reask(s["log_id"])),
        )

    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.time.advance(599_999)
        ok = await env.svc.reask(r["log_id"])
        await env.svc.drain()
        record("f7. allowed at 599_999 ms after the hand-off", ok == {"log_id": r["log_id"]})

    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.time.advance(600_000)
        record(
            "f8. refused after 10 min", await raises(NodeAdminBusyError, env.svc.reask(r["log_id"]))
        )


# ── g. AAD, case, unreadable key ───────────────────────────────────────────


async def case_g_aad_unreadable(record: Record) -> None:
    async with make_env() as env:
        await env.svc.set_key("dk5en-90", PASSWD, None)
        env.svc._synced.add(TARGET)
        token = await env.storage.get_node_admin_key(TARGET)
        wrong_aad = False
        try:
            env.box.decrypt(token or "", "node_admin.password:dk5en-90")
        except SecretBoxError:
            wrong_aad = True
        r = await env.svc.send("dk5en-90", "status", "", "udp")
        await env.svc.drain()
        key = remote_cmd.derive_key(PASSWD)
        record(
            "g1. lower-case set_key + mixed-case send work; the AAD is the upper-case target",
            token is not None
            and env.box.decrypt(token, "node_admin.password:DK5EN-90") == PASSWD
            and wrong_aad
            and env.tx
            == [("udp", TARGET, remote_cmd.build_command_text(key, TARGET, ATTACHED, 1, "status"))]
            and r["ctr"] == 1
            and (await env.target_info())["has_key"],
        )
        await env.time.advance(20_000)
        await env.reply(1)
        await env.svc.drain()
        await env.time.advance(11_000)
        await env.svc.set_key(TARGET, "other-pass", None)  # cache must be invalidated
        env.svc._synced.add(TARGET)  # set_key clears it (case m4); this case tests the key cache
        await env.svc.send(TARGET, "status", "", "udp")
        await env.svc.drain()
        new_key = remote_cmd.derive_key("other-pass")
        record(
            "g2. replacing the key invalidates the cached HMAC key",
            env.tx[-1][2] == remote_cmd.build_command_text(new_key, TARGET, ATTACHED, 2, "status"),
        )
        await env.svc.delete_key(TARGET)
        info = await env.target_info()
        record(
            "g3. delete_key keeps the counter state",
            info["has_key"] is False and info["ctr"] == 2,
        )
        record("g3b. send without a key is a ValueError", await raises(ValueError, env.send()))

    async with make_env() as env:
        await env.setup()
        with db_write(env.storage.db_path) as conn:
            conn.execute(_CORRUPT_SQL, (_CORRUPT_TOKEN,))
        fresh = env.new_service()
        env.svc = fresh
        fresh._synced.add(TARGET)
        info = await env.target_info()
        refused = await raises(NodeAdminUnavailableError, env.send())
        record(
            "g4. undecryptable key: key_unreadable, send raises Unavailable, no counter, no frame",
            info["has_key"] is True
            and info["key_unreadable"] is True
            and refused
            and await env.ctr() == 0
            and env.tx == []
            and await env.rows() == [],
        )
        await env.svc.on_reply(env.frame(1))
        await env.svc.drain()
        record("g5. a reply with an unreadable key is ignored without raising", True)


# ── h. password sentinel ───────────────────────────────────────────────────


async def case_h_sentinel(record: Record) -> None:
    cap = _Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        async with make_env() as env:
            await env.setup(passwd=SENTINEL)
            a = await env.send()
            await env.svc.drain()
            await env.reply(a["ctr"], passwd=SENTINEL)
            await env.svc.drain()
            await env.time.advance(11_000)
            b = await env.send("txpower", "10")
            await env.svc.drain()
            await env.time.advance(61_000)
            await env.svc.reask(b["log_id"])
            await env.svc.drain()
            await env.reply(b["ctr"], bad=True, passwd=SENTINEL)
            await env.svc.drain()
            err_text = ""
            try:
                await env.svc.set_key(TARGET, SENTINEL + "x" * 5, 15)
            except ValueError as exc:
                err_text = str(exc)
            with db_write(env.storage.db_path) as conn:
                conn.execute(_CORRUPT_SQL, (_CORRUPT_TOKEN,))
            broken = env.new_service()
            env.svc = broken
            with contextlib.suppress(NodeAdminUnavailableError):
                await env.send()
            await broken.list_targets()
            await broken.on_reply(env.frame(1, passwd=SENTINEL))
            await broken.drain()
            blobs = [
                "\n".join(cap.lines),
                repr(env.bcasts),
                repr(env.tx),
                repr(await env.svc.history(None, 100)),
                err_text,
            ]
            blobs.extend(p.read_bytes().decode("latin-1") for p in env.tmp.iterdir() if p.is_file())
            leaked = [i for i, b_ in enumerate(blobs) if SENTINEL in b_]
            record(
                "h1. the password is in no log line, broadcast, frame, row, error text or file",
                not leaked and len(cap.lines) > 5,
            )
            if leaked:
                print("       leaked in blob(s)", leaked)
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)


# ── i. refusals and transmit failures ──────────────────────────────────────


async def _clean(env: Env, target: str = TARGET) -> bool:
    return await env.ctr(target) == 0 and await env.rows(target) == [] and env.tx == []


async def case_i_refusals(record: Record) -> None:
    async with make_env(attached=None) as env:
        await env.setup()
        a = await raises(NodeAdminUnavailableError, env.send())
        record(
            "i1. unknown attached call -> Unavailable, nothing consumed", a and await _clean(env)
        )

    async with make_env(ble=False) as env:
        await env.setup()
        a = await raises(NodeAdminUnavailableError, env.send(transport="ble"))
        record(
            "i2. explicit ble while disconnected -> Unavailable, nothing consumed",
            a and await _clean(env),
        )

    async with make_env(attached=TARGET) as env:
        await env.setup()
        a = await raises(ValueError, env.send())
        record(
            "i3. target == attached call -> ValueError, nothing consumed", a and await _clean(env)
        )

    async with make_env() as env:
        await env.setup()
        checks = [
            await raises(ValueError, env.send("txpower", "16")),  # above tx_max 15
            await raises(ValueError, env.send("format-disk")),
            await raises(ValueError, env.send("gps", "maybe")),
            await raises(ValueError, env.svc.send("not a call", "status", "", "udp")),
            await raises(ValueError, env.send(transport="carrier-pigeon")),
        ]
        record(
            "i4. invalid input -> ValueError, nothing consumed", all(checks) and await _clean(env)
        )


async def case_i_transmit(record: Record) -> None:
    async with make_env() as env:
        await env.setup(target="DK5EN-91")
        a = await raises(ValueError, env.send())  # no key for TARGET
        record(
            "i5. no stored key -> ValueError, nothing consumed", a and await _clean(env, "DK5EN-91")
        )

    async with make_env(ble=True) as env:
        await env.setup()
        await env.send(transport="auto")
        await env.svc.drain()
        env.ble = False
        await env.reply(1)
        await env.svc.drain()
        await env.time.advance(11_000)
        await env.send(transport="auto")
        await env.svc.drain()
        record(
            "i6. auto picks ble when connected, udp otherwise",
            [t for t, _, _ in env.tx] == ["ble", "udp"],
        )

    async with make_env() as env:
        await env.setup()
        env.tx_result = "BLE not connected"
        first = await env.send()
        await env.svc.drain()
        rows = await env.rows()
        ctr_after = await env.ctr()
        env.tx_result = None
        b = await env.send()  # a refused hand-off never reached the air: no 10 s spacing
        await env.svc.drain()
        record(
            "i7. failed transmit: row kept as send_failed with the reason, counter stays consumed",
            len(rows) == 1
            and rows[0]["state"] == "send_failed"
            and rows[0]["send_error"] == "BLE not connected"
            and ctr_after == 1
            and b["ctr"] == 2
            and first["ctr"] == 1,
        )
        failed_bc = env.bc_for(first["log_id"])[-1]
        sent_bc = env.bc_for(b["log_id"])[-1]
        failed_tag = rows[0]["text"][-16:]
        record(
            "i7b. a failed row's broadcast carries no signed tag; a sent row's broadcast does",
            failed_bc["state"] == "send_failed"
            and failed_bc["text"] == "RM1 1 status"
            and failed_tag not in repr(failed_bc)
            and sent_bc["text"] == env.tx[-1][2]
            and sent_bc["text"][-16:] != "",
        )
        # history() is behind the guarded /api/node-admin routes and keeps the full text
        record("i7c. history() keeps the full stored text", rows[0]["text"].endswith(failed_tag))

    async with make_env() as env:
        await env.setup()
        env.tx_raise = True
        await env.send()
        await env.svc.drain()
        row = (await env.rows())[0]
        record(
            "i8. a raising transmit is recorded as send_failed, never escapes",
            row["state"] == "send_failed" and row["send_error"] == "transmit failed",
        )

    async with make_env() as env:
        await env.setup()
        env.tx_gate = asyncio.Event()
        res = await asyncio.wait_for(env.send(), timeout=2)
        record(
            "i9. send() returns while the transmit is still blocked",
            set(res) == {"log_id", "ctr", "text"} and (await env.rows())[0]["state"] == "queued",
        )
        env.tx_gate.set()
        await env.svc.drain()


# ── j. pure helpers, prefilter, floor, validation ──────────────────────────


async def case_j_misc(record: Record) -> None:
    base: dict[str, Any] = {"handed_off_at": T0, "send_error": None, "verified": None}
    record(
        "j1. compute_state table",
        compute_state({**base, "handed_off_at": None}, T0) == "queued"
        and compute_state({**base, "send_error": "x"}, T0) == "send_failed"
        and compute_state(base, T0 + 119_999) == "waiting"
        and compute_state(base, T0 + 120_000) == "no_reply"
        and compute_state({**base, "verified": 1, "send_error": "x"}, T0) == "verified"
        and compute_state({**base, "verified": 0, "reply_text": "t"}, T0) == "bad_tag",
    )
    unstamped: dict[str, Any] = {"handed_off_at": None, "sent_at": T0}
    record(
        "j1b. a queued row ages out from sent_at when its hand-off was never stamped",
        compute_state(unstamped, T0 + 119_999) == "queued"
        and compute_state(unstamped, T0 + 120_000) == "no_reply",
    )

    async with make_env() as env:
        await env.setup()
        for msg in (None, 5, "hello", "RM1", "rm1 1 ok x", "RM1 " + "x" * 300):
            await env.svc.on_reply({"src": TARGET, "dst": ATTACHED, "msg": msg})
        none_spawned = not env.svc._tasks
        await env.svc.on_reply(env.frame(1))
        spawned = bool(env.svc._tasks)
        await env.svc.drain()
        record(
            "j2. on_reply: cheap prefilter spawns nothing, an RM1 frame spawns a task",
            none_spawned and spawned,
        )

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("db down")

        env.storage.find_node_admin_log_row = boom  # type: ignore[method-assign]  # test double
        await env.svc.on_reply(env.frame(1))
        await env.svc.drain()
        record("j3. a raising task body never escapes on_reply/drain", True)

    async with make_env(unix=1_791_000_000) as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        record("j4. counter floor: unix time wins over stored + 1", r["ctr"] == 1_791_000_000)

    async with make_env(unix=1_000_000) as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        record("j5. clock before 2024: floor disabled, counter is stored + 1", r["ctr"] == 1)

    async with make_env() as env:
        results = [
            await raises(ValueError, env.svc.set_key(TARGET, "x" * 15, None)),
            await raises(ValueError, env.svc.set_key(TARGET, " lead", None)),
            await raises(ValueError, env.svc.set_key(TARGET, "none", None)),
            await raises(ValueError, env.svc.set_key(TARGET, "ok", 31)),
            await raises(ValueError, env.svc.set_key(TARGET, "ok", -1)),
            await raises(ValueError, env.svc.set_key("TOOLONGCALL-99", "ok", None)),
        ]
        await env.svc.set_key(TARGET, "ok", 0)
        await env.svc.set_key(TARGET, "ok", None)  # None keeps tx_max
        record(
            "j6. set_key validates password, tx_max 0..30 and the callsign; None keeps tx_max",
            all(results) and (await env.target_info())["tx_max"] == 0,
        )
        proto: NodeAdminServiceProtocol = env.svc
        record("j7. conforms to the Protocol", await proto.history(None, 5) == [])


# ── m. W0 regressions (remote-view campaign, doc/2026-10-06_1100-node-admin-remote-view-*) ─

STATUS_P1515 = "ok v=4.40a up=125 bat=87 heap=212 s=GTDMWL p=15/15 led=0"


async def case_m_reask(record: Record) -> None:
    # V1a: the lockout guard applies to re-ask exactly as to send.
    async with make_env() as env:
        await env.setup()
        await env.send("status")
        await env.svc.drain()
        await env.time.advance(130_000)
        s2 = await seed_row(env, env.time.now)
        await env.time.advance(130_000)
        rows = await env.svc._storage.node_admin_history(TARGET, 100)
        until = _lockout_until_ms(rows, env.time.now)
        send_refused = await raises(NodeAdminBusyError, env.send("status"))
        tx_before = len(env.tx)
        refused = await raises(NodeAdminBusyError, env.svc.reask(s2["log_id"]))
        await env.svc.drain()
        record(
            "m1. re-ask is refused while the lockout guard is active (no frame sent)",
            until is not None
            and until > env.time.now
            and send_refused
            and refused
            and len(env.tx) == tx_before,
        )
        await env.time.advance(until - env.time.now + 1 if until else 0)
        allowed = await env.svc.reask(s2["log_id"])
        await env.svc.drain()
        record(
            "m1b. the same re-ask goes out once the guard has expired",
            allowed == {"log_id": s2["log_id"]} and len(env.tx) == tx_before + 1,
        )

    # V1b: one re-ask per row; the 10 min cache window is never extended by it.
    async with make_env() as env:
        await env.setup()
        a = await env.send("status")
        await env.svc.drain()
        await env.time.advance(61_000)
        await env.svc.reask(a["log_id"])
        await env.svc.drain()
        outcomes: list[bool] = []
        for _ in range(12):  # every 61 s, 12 min past the first hand-off
            await env.time.advance(61_000)
            outcomes.append(await raises(NodeAdminBusyError, env.svc.reask(a["log_id"])))
        await env.svc.drain()
        record(
            "m2. at most one re-ask per row: 12 further attempts every 61 s are all refused",
            all(outcomes) and len(env.tx) == 2,
        )


async def case_m_sync_binding(record: Record) -> None:
    res = "ok ctr=5 v=4.40a"
    # V3: failed sync A, later sync B, both transport copies of B's reply.
    async with make_env() as env:
        await env.setup(synced=False)
        a = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(300_000)
        b = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(15_000)
        await env.reply(0, res)  # UDP copy
        await env.svc.drain()
        await env.reply(0, res, suffix="{087")  # second copy ~100 ms later
        await env.svc.drain()
        await env.reply(0, res)
        await env.svc.drain()
        states = {r["id"]: r["state"] for r in await env.rows()}
        a_row = next(r for r in await env.rows() if r["id"] == a["log_id"])
        record(
            "m3. two copies of sync B's reply: A stays no_reply, B verified, one broadcast for B",
            states[a["log_id"]] == "no_reply"
            and a_row["reply_text"] is None
            and states[b["log_id"]] == "verified"
            and len([p for p in env.bc_for(b["log_id"]) if p["state"] == "verified"]) == 1
            and not [p for p in env.bc_for(a["log_id"]) if p["state"] == "verified"],
        )

    # The duplicate guard on its own: A is a sync the transport refused (still inside
    # its window, reply_at empty), B follows at once; B's second copy must not bind A.
    async with make_env() as env:
        await env.setup(synced=False)
        env.tx_result = "no route"
        a = await env.svc.sync(TARGET)
        await env.svc.drain()
        env.tx_result = None
        await env.time.advance(1_000)
        b = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(15_000)
        await env.reply(0, res)
        await env.svc.drain()
        await env.reply(0, res, suffix="{087")
        await env.svc.drain()
        states = {r["id"]: r["state"] for r in await env.rows()}
        record(
            "m3b. the second copy is dropped even when an older open sync row is inside its window",
            states[a["log_id"]] == "send_failed"
            and states[b["log_id"]] == "verified"
            and len([p for p in env.bc_for(b["log_id"]) if p["state"] == "verified"]) == 1
            and not [p for p in env.bc_for(a["log_id"]) if p["state"] == "verified"],
        )


async def case_m_sync_window(record: Record) -> None:
    res = "ok ctr=5 v=4.40a"
    # A reply to a sync whose 120 s window is over binds nothing.
    async with make_env() as env:
        await env.setup(synced=False)
        a = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(120_000)
        await env.reply(0, res)
        await env.svc.drain()
        record(
            "m3c. a sync reply after the 120 s window verifies nothing",
            await env.state_of(a["log_id"]) == "no_reply" and await env.hwm() == 0,
        )

    # ctr=<hwm> below the stored hwm is a stale or replayed reply.
    async with make_env() as env:
        await env.setup(synced=False)
        await env.storage.raise_node_admin_hwm(TARGET, 10)
        a = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(15_000)
        await env.reply(0, "ok ctr=9 v=4.40a")
        await env.svc.drain()
        stale_ignored = await env.state_of(a["log_id"]) == "waiting"
        await env.reply(0, "ok ctr=10 v=4.40a")
        await env.svc.drain()
        record(
            "m3d. sync reply with ctr below last_hwm is ignored, ctr == last_hwm verifies",
            stale_ignored
            and await env.state_of(a["log_id"]) == "verified"
            and await env.hwm() == 10,
        )


async def case_m_result_max(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.time.advance(20_000)
        await env.reply(r["ctr"], "ok " + "x" * 105)  # 108 characters
        await env.svc.drain()
        record(
            "m4. a 108-character result verifies the row (RESULT_MAX)",
            await env.state_of(r["log_id"]) == "verified",
        )

    async with make_env() as env:
        await env.setup()
        r = await env.send()
        await env.svc.drain()
        await env.time.advance(20_000)
        cap = _Capture()
        svc_log = logging.getLogger("mcapp.node_admin_service")
        svc_log.addHandler(cap)
        try:
            over = env.frame(r["ctr"], "ok " + "x" * 106)  # 109 characters
            await env.svc.on_reply(over)
            await env.svc.on_reply(env.frame(r["ctr"], "ok " + "x" * 106, suffix="{087"))
            await env.svc.on_reply(
                {**over, "msg": "RM1 1 ok " + "y" * 190 + " 0123456789abcdef"}
            )  # over the prefilter
            await env.svc.on_reply({**over, "msg": "RM1 7 status 0123456789abcdef"})  # a command
            await env.svc.drain()
        finally:
            svc_log.removeHandler(cap)
        tag = over["msg"][-16:]
        warned = [ln for ln in cap.lines if "dropped" in ln]
        record(
            "m5. an RM1 reply the parser refuses logs one WARNING with the reason, never the tag",
            await env.state_of(r["log_id"]) == "waiting"
            and sum("result length 109 exceeds 108" in ln for ln in warned) == 2
            and sum("wire length" in ln for ln in warned) == 1
            and len(warned) == 3
            and not any(tag in ln for ln in cap.lines),
        )


async def case_m_txpower_bound(record: Record) -> None:
    async def counts(env: Env) -> tuple[int, int, int]:
        return await env.ctr(), len(await env.rows()), len(env.tx)

    async with make_env() as env:
        await env.setup(tx_max=30)
        s = await env.send("status")
        await env.svc.drain()
        await env.time.advance(20_000)
        await env.reply(s["ctr"], STATUS_P1515)
        await env.svc.drain()
        await env.time.advance(11_000)
        before = await counts(env)
        refused, sentence = False, ""
        try:
            await env.send("txpower", "20")
        except ValueError as exc:
            refused, sentence = True, str(exc)
        except Exception:  # an unfixed service fails the record below, not the whole case
            refused = False
        after = await counts(env)
        record(
            "m6. txpower above the node's p=<cur>/<max> maximum is refused (ValueError naming "
            "15), no counter allocated, no row, no frame",
            refused and "15" in sentence and before == after,
        )
        try:
            ok = await env.send("txpower", "15")
            goes_out = ok["ctr"] == before[0] + 1
        except Exception:  # an unfixed service still has the refused frame in flight
            goes_out = False
        await env.svc.drain()
        record("m6b. txpower at the node's maximum still goes out", goes_out)

    async with make_env() as env:
        await env.setup(tx_max=30)
        r = await env.send("txpower", "25")
        await env.svc.drain()
        record(
            "m6c. no known node maximum: the key's tx_max alone bounds txpower (as before)",
            r["ctr"] == 1,
        )

    # The newest verified status that CARRIES p= decides; a newer old-form status does not lift it.
    async with make_env() as env:
        await env.setup(tx_max=30)
        s1 = await env.send("status")
        await env.svc.drain()
        await env.time.advance(20_000)
        await env.reply(s1["ctr"], STATUS_P1515)
        await env.svc.drain()
        await env.time.advance(11_000)
        s2 = await env.send("status")
        await env.svc.drain()
        await env.time.advance(20_000)
        await env.reply(s2["ctr"], "ok v=4.40a up=125 bat=87 heap=212 gw=0 mesh=1")
        await env.svc.drain()
        await env.time.advance(11_000)
        record(
            "m6d. a newer verified status without p= leaves the earlier maximum in force",
            await raises(ValueError, env.send("txpower", "16")),
        )


async def case_m_txpower_held(record: Record) -> None:
    # Held behind the auto-sync: the maximum becomes known while the command waits.
    async with make_env() as env:
        await env.setup(tx_max=30)
        s = await env.send("status")
        await env.svc.drain()
        await env.time.advance(300_000)  # S: no_reply, cool-down over
        env.svc._synced.discard(TARGET)
        ctr_before = await env.ctr()
        held = await env.send("txpower", "20")
        await settle()
        accepted_pending = held.get("pending") is True
        await env.reply(s["ctr"], STATUS_P1515)  # the late reply to S names the maximum
        await settle()
        await env.reply(0, "ok ctr=5 v=4.40a")
        await until(lambda: 10_000 in env.time.pending_deltas())
        await env.time.advance(10_000)
        await env.svc.drain()
        dropped = [
            p
            for e, p in env.bcasts
            if e == "node_admin:reply" and str(p.get("warning", "")).startswith("command dropped")
        ]
        record(
            "m7. a txpower held behind the auto-sync is refused at release: no frame, no counter, "
            "no row, 'command dropped' broadcast",
            accepted_pending
            and not any(m.split(" ")[2] == "txpower" for _, _, m in env.tx)
            and await env.ctr() == ctr_before
            and len(await env.rows()) == 2
            and len(dropped) == 1
            and "15" in str(dropped[0]["warning"]),
        )


async def case_m_synced_and_broadcast(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        had = TARGET in env.svc._synced
        await env.svc.set_key(TARGET, "other-pass", None)
        cleared = TARGET not in env.svc._synced
        r = await env.send("status")
        await settle()
        record(
            "m8. set_key clears the target from _synced: the next command syncs first",
            had and cleared and r.get("pending") is True and r["ctr"] == 0,
        )

    # V5b: a late verified reply to a row beyond the newest 100 still emits its event.
    async with make_env() as env:
        await env.setup()
        old = await env.send("status")
        await env.svc.drain()
        for i in range(105):
            await env.storage.insert_node_admin_sync_row(
                TARGET, ATTACHED, "udp", env.time.now + i, "RM1 0 sync x"
            )
        await env.time.advance(20_000)
        await env.reply(old["ctr"])
        await env.svc.drain()
        verified = [p for p in env.bc_for(old["log_id"]) if p["state"] == "verified"]
        record(
            "m9. a verified reply to a row older than the newest 100 still broadcasts it",
            len(verified) == 1 and await env.state_of(old["log_id"]) in {None, "verified"},
        )

    # V5c: re-ask by id works for a row beyond the newest 100 of ALL targets.
    async with make_env() as env:
        await env.setup()
        old = await env.send("status")
        await env.svc.drain()
        for i in range(105):
            await env.storage.insert_node_admin_sync_row(
                "DK5EN-91", ATTACHED, "udp", env.time.now + i, "RM1 0 sync x"
            )
        await env.time.advance(61_000)
        try:
            out: dict[str, Any] | None = await env.svc.reask(old["log_id"])
        except ValueError:
            out = None
        await env.svc.drain()
        record(
            "m10. re-ask resolves its row by id, not by the newest 100 rows of all targets",
            out == {"log_id": old["log_id"]},
        )


# ── n. W2a: cool-down, foreign pause, stale mark, /state ──────────────────

FOREIGN_CMD = "RM1 4242 gps on 0123456789abcdef"
STALE_WARNING = (
    "The node reports counter mark {hwm}, below the last known {stored}. "
    "If it was re-flashed, enter its password again in Settings > Remote nodes."
)


def foreign(src: str, dst: str, msg: str = FOREIGN_CMD) -> dict[str, Any]:
    return {"src": src, "dst": dst, "msg": msg, "type": "msg", "src_type": "udp"}


async def counts(env: Env) -> tuple[int, int, int]:
    """(allocated counter, log rows, frames transmitted): a refusal must leave all three alone."""
    return await env.ctr(), len(await env.rows()), len(env.tx)


async def busy_text(coro: Awaitable[object]) -> str | None:
    """Text of the NodeAdminBusyError (the router maps it to 409); None when none was raised."""
    try:
        await coro
    except NodeAdminBusyError as exc:
        return str(exc)
    except Exception:
        return None
    return None


async def case_n_cooldown(record: Record) -> None:
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        await env.time.advance(120_000)  # no_reply; hand-off was T0
        before = await counts(env)
        send_text = await busy_text(env.send())
        sync_text = await busy_text(env.svc.sync(TARGET))
        st = await env.state()
        record(
            "n1. 120 s after a silent row a send AND a sync are refused (Busy -> 409) "
            "before any counter, row or frame",
            send_text is not None
            and sync_text is not None
            and "5 minutes" in send_text
            and "180 s" in send_text
            and before == await counts(env)
            and st["cooldown_until"] == T0 + 300_000
            and st["next_allowed_at"] == T0 + 300_000,
        )
        await env.time.advance(179_999)
        edge = await busy_text(env.send())
        edge_ok = edge is not None and "1 s" in edge and before == await counts(env)
        await env.time.advance(1)
        again = await env.send()
        st2 = await env.state()
        record(
            "n1b. refused at 299 999 ms after the hand-off, accepted at 300 000 ms",
            edge_ok and again["ctr"] == a["ctr"] + 1 and st2["cooldown_until"] is None,
        )


async def case_n_cooldown_more(record: Record) -> None:
    # an abandoned row (a restart before the reply) holds the target as well
    async with make_env() as env:
        await env.setup()
        await env.send()
        await env.svc.drain()
        await env.time.advance(1_000)
        await env.svc.start()
        before = await counts(env)
        await env.time.advance(20_000)
        refused = await busy_text(env.send())
        await env.time.advance(278_999)
        edge = await busy_text(env.send())
        await env.time.advance(1)
        ok = await env.send()
        record(
            "n1c. an abandoned row holds the target until 300 s after its hand-off",
            refused is not None
            and edge is not None
            and ok["ctr"] == 2
            and before[0] == 1
            and len(await env.rows()) == 2,
        )

    # re-ask is exempt: it replays the same ctr and the node answers from its cache
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        await env.time.advance(125_000)
        new_frame = await busy_text(env.send())
        ctr_before = await env.ctr()
        res = await env.svc.reask(a["log_id"])
        await env.svc.drain()
        record(
            "n1d. during the cool-down a NEW frame is refused but re-ask of the same ctr goes out",
            new_frame is not None
            and res == {"log_id": a["log_id"]}
            and len(env.tx) == 2
            and env.tx[1][2] == env.tx[0][2]
            and await env.ctr() == ctr_before,
        )

    # a late verified reply ends the silence: nothing holds the target but the 10 s spacing
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        await env.time.advance(125_000)
        await env.reply(a["ctr"])
        await env.svc.drain()
        early = await busy_text(env.send())
        await env.time.advance(10_000)
        ok = await env.send()
        record(
            "n1e. a late verified reply lifts the cool-down (only the 10 s spacing remains)",
            early is not None and "one frame per 10 s" in early and ok["ctr"] == a["ctr"] + 1,
        )


async def case_n_foreign(record: Record) -> None:
    cap = _Capture()
    svc_log = logging.getLogger("mcapp.node_admin_service")
    async with make_env() as env:
        await env.setup()
        svc_log.addHandler(cap)
        svc_log.setLevel(logging.INFO)
        try:
            await env.svc.on_reply(foreign("DL1ABC", TARGET))  # another SysOp's command
            await env.svc.drain()
            st = await env.state()
            before = await counts(env)
            text = await busy_text(env.send())
            sync_text = await busy_text(env.svc.sync(TARGET))
            await env.time.advance(30_000)
            await env.svc.on_reply(  # ... and the node's answer to that other SysOp
                foreign(TARGET, "DL1ABC", "RM1 4242 ok gps=on 0123456789abcdef")
            )
            await env.svc.drain()
        finally:
            svc_log.removeHandler(cap)
        st2 = await env.state()
        record(
            "n2. another station's RM1 command to the target pauses it 120 s: send and sync "
            "refused (Busy -> 409) before any counter, row or frame",
            st["foreign_until"] == T0 + 120_000
            and st["next_allowed_at"] == T0 + 120_000
            and text is not None
            and "Another station is managing DK5EN-90" in text
            and "120 s" in text
            and sync_text is not None
            and before == await counts(env),
        )
        record(
            "n2b. the node's reply to another station extends the pause; INFO is logged once",
            st2["foreign_until"] == T0 + 150_000
            and len([ln for ln in cap.lines if "RM1 frame of another station" in ln]) == 1
            and not any("0123456789abcdef" in ln for ln in cap.lines),
        )
        await env.time.advance(119_999)
        edge = await busy_text(env.send())
        await env.time.advance(1)
        ok = await env.send()
        record(
            "n2c. refused until foreign_until, accepted at it",
            edge is not None and ok["ctr"] == 1 and (await env.state())["foreign_until"] is None,
        )


async def case_n_foreign_own(record: Record) -> None:
    # our own traffic and unrelated frames never pause
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        for frame in (
            foreign(ATTACHED, TARGET),  # the echo of our own command
            foreign(TARGET, ATTACHED, "RM1 99 ok status 0123456789abcdef"),  # reply to us
            env.frame(a["ctr"]),  # our genuine reply
            env.frame(a["ctr"], suffix="{087"),  # its second transport copy
            foreign("DL1ABC", "DL2XYZ"),  # two unrelated stations
            foreign("DL1ABC", "DK5EN-91"),  # a target we hold no key for
            foreign("DL1ABC", "20"),  # a group
        ):
            await env.svc.on_reply(frame)
        await env.svc.drain()
        env.attached = "DK5EN-15"  # the attached node changed: old rows still name our old call
        await env.svc.on_reply(foreign(TARGET, ATTACHED, "RM1 98 ok status 0123456789abcdef"))
        await env.svc.drain()
        record(
            "n2d. our own echo, replies to us (both copies), unrelated frames and a changed "
            "attached node never start a pause",
            (await env.state())["foreign_until"] is None,
        )


async def case_n_foreign_held(record: Record) -> None:
    # held behind the auto-sync: a pause that starts while the command waits drops it
    async with make_env() as env:
        await env.setup(synced=False)
        await env.send()
        await until(lambda: len(env.tx) == 1)
        await env.svc.on_reply(foreign("DL1ABC", TARGET))
        await settle()  # the gate is parked on its 90 s sleep: drain() would wait for it
        await env.time.advance(3_000)
        await env.reply(0, "ok ctr=5 v=4.40a")
        await until(lambda: 10_000 in env.time.pending_deltas())
        await env.time.advance(10_000)
        await env.svc.drain()
        dropped = [
            p
            for e, p in env.bcasts
            if e == "node_admin:reply" and str(p.get("warning", "")).startswith("command dropped")
        ]
        record(
            "n2e. a command held behind the sync is refused at release during a pause: "
            "no frame, no counter, no row, 'command dropped' broadcast",
            len(env.tx) == 1
            and await env.ctr() == 0
            and len(await env.rows()) == 1
            and len(dropped) == 1
            and "Another station" in str(dropped[0]["warning"]),
        )

    # re-ask is a frame too: another sender's command has replaced the node's reply cache
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        await env.time.advance(61_000)
        await env.svc.on_reply(foreign("DL1ABC", TARGET))
        await env.svc.drain()
        refused = await busy_text(env.svc.reask(a["log_id"]))
        sent = len(env.tx)
        await env.time.advance(120_000)
        res = await env.svc.reask(a["log_id"])
        await env.svc.drain()
        record(
            "n2f. re-ask is refused during a foreign pause and allowed after it",
            refused is not None
            and sent == 1
            and res == {"log_id": a["log_id"]}
            and len(env.tx) == 2,
        )


async def case_n_sync_and_hwm(record: Record) -> None:
    # only a verified SYNC marks a target synced in this process
    async with make_env() as env:
        await env.setup()
        a = await env.send()
        await env.svc.drain()
        fresh = env.new_service()  # a restart: nothing is synced yet
        env.svc = fresh
        await env.reply(a["ctr"])
        await env.svc.drain()
        record(
            "n3. a late verified reply to an old COMMAND does not mark the target synced",
            await env.state_of(a["log_id"]) == "verified"
            and TARGET not in fresh._synced
            and (await env.state())["connected"] is False,
        )
        await env.time.advance(11_000)
        sent = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(15_000)
        await env.reply(0, "ok ctr=5 v=4.40a")
        await env.svc.drain()
        record(
            "n3b. a verified sync does",
            await env.state_of(sent["log_id"]) == "verified"
            and (await env.state())["connected"] is True,
        )

    # a re-flashed node answers sync with a mark below the stored one
    async with make_env() as env:
        await env.setup(synced=False)
        await env.storage.raise_node_admin_hwm(TARGET, 10)
        seed_ctr(env.storage.db_path, TARGET, 50)
        a = await env.svc.sync(TARGET)
        await env.svc.drain()
        await env.time.advance(15_000)
        await env.reply(0, "ok ctr=3 v=4.40a", bad=True)  # forged: no operator-facing claim
        await env.svc.drain()
        forged_warned = [p for p in env.bc_for(a["log_id"]) if "warning" in p]
        await env.reply(0, "ok ctr=3 v=4.40a")
        await env.svc.drain()
        await env.reply(0, "ok ctr=3 v=4.40a", suffix="{087")  # the second transport copy
        await env.svc.drain()
        warned = [p for p in env.bc_for(a["log_id"]) if "warning" in p]
        record(
            "n4. a verified sync reply below last_hwm is ignored (row open, hwm kept) but "
            "announced once, with the recovery hint",
            not forged_warned
            and len(warned) == 1
            and warned[0]["warning"] == STALE_WARNING.format(hwm=3, stored=10)
            and warned[0]["state"] in {"waiting", "bad_tag"}
            and warned[0]["verified"] != 1
            and await env.hwm() == 10
            and TARGET not in env.svc._synced,
        )
        ctr_before = await env.ctr()
        await env.svc.set_key(TARGET, PASSWD, 15)
        record(
            "n4b. set_key resets last_hwm to 0 and keeps the counter",
            await env.hwm() == 0 and await env.ctr() == ctr_before,
        )
        await env.reply(0, "ok ctr=3 v=4.40a")
        await env.svc.drain()
        record(
            "n4c. after the reset the same reply verifies and the new mark is stored",
            await env.state_of(a["log_id"]) == "verified"
            and await env.hwm() == 3
            and TARGET in env.svc._synced,
        )
        await env.time.advance(11_000)
        nxt = await env.send()
        record(
            "n4d. the next counter continues above the old one (hwm reset or not)",
            ctr_before == 50 and nxt["ctr"] == 51,
        )


async def case_n_state(record: Record) -> None:
    keys = [
        "target",
        "now_ms",
        "as_of_id",
        "connected",
        "in_flight",
        "next_allowed_at",
        "cooldown_until",
        "foreign_until",
        "possible_lockout_until_ms",
        "capability",
        "has_led",
        "fields",
        "switches",
        "registers",
    ]
    async with make_env() as env:
        await env.setup(synced=False)
        empty = await env.state()
        record(
            "n5. /state of a keyed target without rows: Appendix A keys in order, nothing known",
            list(empty) == keys
            and empty["target"] == TARGET
            and empty["now_ms"] == T0
            and empty["as_of_id"] == 0
            and empty["connected"] is False
            and empty["in_flight"] is None
            and empty["next_allowed_at"] is None
            and empty["cooldown_until"] is None
            and empty["foreign_until"] is None
            and empty["possible_lockout_until_ms"] is None
            and empty["capability"] == 1
            and empty["has_led"] is None
            and empty["fields"] == {}
            and set(empty["switches"]) == {"gps", "track", "display", "mesh", "gateway", "led"}
            and {v["state"] for v in empty["switches"].values()} == {"unknown"}
            and {k: v["state"] for k, v in empty["registers"].items()}
            == {"sync": "never", "status": "never"},
        )
        lower = await env.svc.state("dk5en-90")
        record(
            "n5b. the target is canonicalised; an invalid or entirely unknown call is a ValueError",
            lower["target"] == TARGET
            and await raises(ValueError, env.svc.state("not a call"))
            and await raises(ValueError, env.svc.state("DL9ZZZ-1")),
        )

    async with make_env() as env:
        await env.setup()
        a = await env.send("gps", "on")
        await env.svc.drain()
        busy = await env.state()
        record(
            "n5c. in_flight names the newest queued/waiting row; connected follows _synced; "
            "next_allowed_at is hand-off + 10 s",
            busy["in_flight"] == {"log_id": a["log_id"], "cmd": "gps", "args": "on"}
            and busy["connected"] is True
            and busy["next_allowed_at"] == T0 + 10_000
            and busy["as_of_id"] == a["log_id"]
            and busy["switches"]["gps"]["state"] == "pending",
        )
        await env.time.advance(20_000)
        await env.reply(a["ctr"], "ok gps=on")
        await env.svc.drain()
        done = await env.state()
        await env.time.advance(11_000)
        idle = await env.state()
        record(
            "n5d. a verified reply ends in_flight, feeds the fold and re-anchors the 10 s "
            "spacing on the reply; idle -> next_allowed_at null",
            done["in_flight"] is None
            and done["switches"]["gps"]["state"] == "on"
            and done["switches"]["gps"]["log_id"] == a["log_id"]
            and done["next_allowed_at"] == T0 + 30_000
            and idle["next_allowed_at"] is None
            and idle["now_ms"] == T0 + 31_000,
        )

    # the newest verified row per (cmd, args) survives however many rows were logged since
    async with make_env() as env:
        await env.setup()
        s1 = await env.send("status")
        await env.svc.drain()
        await env.time.advance(20_000)
        await env.reply(s1["ctr"], STATUS_P1515)
        await env.svc.drain()
        for i in range(205):
            await env.storage.insert_node_admin_sync_row(
                TARGET, ATTACHED, "udp", env.time.now + i, "RM1 0 sync x"
            )
        st = await env.state()
        record(
            "n5e. an old verified status beyond the newest 200 rows still feeds /state",
            st["fields"].get("tx_power", {}).get("value") == 15
            and st["registers"]["status"]["state"] == "ok"
            and st["switches"]["gps"]["state"] == "on",
        )

    # lockout guard and foreign pause both feed next_allowed_at
    async with make_env() as env:
        await env.setup()
        await env.send()
        await env.svc.drain()
        await env.time.advance(125_000)
        await seed_row(env, env.time.now)
        await env.time.advance(125_000)
        st = await env.state()
        record(
            "n5f. possible_lockout_until_ms and cooldown_until are reported; "
            "next_allowed_at is the latest of them",
            st["possible_lockout_until_ms"] == T0 + 125_000 + 300_000
            and st["cooldown_until"] == T0 + 125_000 + 300_000
            and st["next_allowed_at"] == T0 + 125_000 + 300_000,
        )
        await env.svc.on_reply(foreign("DL1ABC", TARGET))
        await env.svc.drain()
        st2 = await env.state()
        record(
            "n5g. a later foreign pause moves next_allowed_at",
            st2["foreign_until"] == env.time.now + 120_000
            and st2["next_allowed_at"] == max(st["next_allowed_at"], st2["foreign_until"]),
        )


# ── runner ─────────────────────────────────────────────────────────────────


async def case_k_start_never_blocks(record: Record) -> None:
    """The feature is always on, so `start()` runs on every box at boot: a failing
    housekeeping query (locked DB, I/O error, missing table) must never propagate and
    stop mcapp from starting."""

    async def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("database is locked")

    for name in ("abandon_stale_node_admin_rows", "prune_node_admin_log"):
        async with make_env() as env:
            cap = _Capture()
            svc_log = logging.getLogger("mcapp.node_admin_service")
            svc_log.addHandler(cap)
            setattr(env.storage, name, boom)
            try:
                try:
                    await env.svc.start()
                    returned = True
                except Exception:
                    returned = False
            finally:
                svc_log.removeHandler(cap)
            record(f"k: start() returns normally when {name} raises", returned)
            record(
                f"k: the failure of {name} is logged, not swallowed silently",
                any("housekeeping failed" in line for line in cap.lines),
            )


async def run_node_admin_service_tests() -> bool:
    results: list[tuple[str, bool]] = []

    def record(label: str, ok: bool) -> None:
        results.append((label, ok))
        print(f"{'PASS' if ok else 'FAIL'} | {label}")

    cases: list[Callable[[Record], Awaitable[None]]] = [
        case_a_wiring,
        case_a_replies,
        case_b_guards,
        case_c_monotone,
        case_d_clock,
        case_e_sync_spacing,
        case_e_timeout_inflight,
        case_e_spacing,
        case_e_dropped,
        case_f_reask,
        case_f_reask_rules,
        case_g_aad_unreadable,
        case_h_sentinel,
        case_i_refusals,
        case_i_transmit,
        case_j_misc,
        case_k_start_never_blocks,
        case_m_reask,
        case_m_sync_binding,
        case_m_sync_window,
        case_m_result_max,
        case_m_txpower_bound,
        case_m_txpower_held,
        case_m_synced_and_broadcast,
        case_n_cooldown,
        case_n_cooldown_more,
        case_n_foreign,
        case_n_foreign_own,
        case_n_foreign_held,
        case_n_sync_and_hwm,
        case_n_state,
    ]
    for case in cases:
        try:
            await asyncio.wait_for(case(record), timeout=120)
        except Exception:
            record(f"{case.__name__} raised", False)
            traceback.print_exc()
    passed = sum(1 for _, ok in results if ok)
    ok_all = passed == len(results)
    print(f"\nNode admin service: {passed}/{len(results)} passed")
    print(f"node_admin_service: {'PASS' if ok_all else 'FAIL'}")
    return ok_all


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run_node_admin_service_tests()) else 1)
