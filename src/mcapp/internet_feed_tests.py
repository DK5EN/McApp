"""Offline tests for the internet feed service (`internet_feed.py`).

A `httpx.MockTransport` plays meshmap; storage is a temp-file SQLite database;
the secret box uses a temp key and a fixed binding. No test touches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .commands.constants import has_console
from .internet_feed import (
    BACKOFF_MAX_S,
    BACKOFF_START_S,
    POSITION_RECONNECT_MIN_S,
    TOKENS_AD,
    IdentityInputs,
    InternetFeedService,
    build_identity,
    identity_key,
    map_feed_message,
)
from .internet_feed_types import STATUS_EVENT, WWW_MESSAGE_TOPIC
from .secret_box import SecretBox
from .sqlite_storage import SQLiteStorage, create_sqlite_storage
from .util import now_ms

TOKEN = "TOKEN-SECRET-xyz-0123456789"
WAIT_S = 10.0

Record = Callable[[str, bool], None]

PI_KEY = "DK5EN-98|PI|"  # identity_key of the default test identity (no wlan0 MAC file)


@dataclass
class Req:
    method: str
    path: str
    query: dict[str, str]
    auth: str | None
    body: Any


def _ring(env: Env, enc: str) -> Any:
    """Decrypted keyring from a stored `token_enc` (the AD is part of the storage contract)."""
    return json.loads(env.box.decrypt(enc, TOKENS_AD))


def sse(*events: tuple[str, Any, str | None]) -> bytes:
    out = ": ping\n\n"
    for name, data, eid in events:
        out += f"event: {name}\n"
        if eid:
            out += f"id: {eid}\n"
        out += f"data: {json.dumps(data)}\n\n"
    return out.encode()


class _Hang(httpx.AsyncByteStream):
    """Yields a prefix, then stays open until the client cancels."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._prefix
        await asyncio.Event().wait()


def hello(version: int = 1) -> tuple[str, Any, None]:
    return ("hello", {"feedVersion": version, "boxId": "b_1"}, None)


def hanging(*events: tuple[str, Any, str | None]) -> httpx.Response:
    return httpx.Response(200, stream=_Hang(sse(*events)))


def feed_ok(*events: tuple[str, Any, str | None]) -> httpx.Response:
    return httpx.Response(200, content=sse(*events))


def reg_201(box: str = "b_1", token: str = TOKEN) -> httpx.Response:
    return httpx.Response(201, json={"boxId": box, "token": token, "feedVersion": 1})


def reg_200(box: str = "b_1") -> httpx.Response:
    return httpx.Response(200, json={"boxId": box, "feedVersion": 1})


class Env:
    def __init__(
        self,
        tmp: Path,
        storage: SQLiteStorage,
        inputs: Callable[[], IdentityInputs],
        *,
        inject_sleep: bool = True,
        **svc_kwargs: Any,
    ) -> None:
        self.tmp = tmp
        self.storage = storage
        self.box = SecretBox(key_path=tmp / "secret.key", binding=b"board:TEST")
        self.requests: list[Req] = []
        self.register_script: list[Callable[[Req], httpx.Response]] = []
        self.feed_script: list[Callable[[Req], httpx.Response]] = []
        self.published: list[tuple[str, str, dict[str, Any]]] = []
        self.statuses: list[dict[str, Any]] = []
        self.sleeps: list[float] = []
        self.stop_after = 10**6
        self.stop = asyncio.Event()
        self.changed = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        # Gates: a request / publish blocks here until the test sets the event.
        self.register_gate: asyncio.Event | None = None
        self.publish_gate: asyncio.Event | None = None
        self.publish_started: list[dict[str, Any]] = []
        self.publish_raises = False
        # Contract server state (see contract_register).
        self.issued: dict[str, str] = {}
        self.tokenless: list[str] = []
        self.reclaims: list[str] = []
        self.rejected: list[str] = []
        self.svc = InternetFeedService(
            storage=storage,
            secret_box=self.box,
            publish=self._publish,
            emit_status=self._emit,
            identity_inputs=inputs,
            base_url="https://feed.test",
            transport=httpx.MockTransport(self._handle),
            sleep=self._sleep if inject_sleep else None,
            rng=random.Random(7),  # noqa: S311 - deterministic test jitter
            wlan_mac_path=tmp / "no-mac",
            model_path=tmp / "no-model",
            **svc_kwargs,
        )

    def contract_register(self, req: Req) -> httpx.Response:
        """Register endpoint per mcmap §3.2: a token is valid for ITS identity only."""
        key = identity_key(req.body)
        if req.auth:
            if self.issued.get(req.auth.removeprefix("Bearer ")) == key:
                return reg_200(f"box-{key}")
            self.rejected.append(key)
            return httpx.Response(401, json={})
        self.tokenless.append(key)
        if key in self.issued.values():
            self.reclaims.append(key)
        token = f"TOKEN-{len(self.issued) + 1}-{key}"
        self.issued[token] = key
        return reg_201(f"box-{key}", token)

    def script(
        self, kind: str, *responses: Callable[[Req], httpx.Response] | httpx.Response
    ) -> None:
        wrapped = [r if callable(r) else (lambda _q, r=r: r) for r in responses]
        (self.register_script if kind == "register" else self.feed_script).extend(wrapped)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        req = Req(
            request.method,
            request.url.path,
            dict(request.url.params),
            request.headers.get("authorization"),
            body,
        )
        self.requests.append(req)
        self.changed.set()
        is_register = request.url.path.endswith("/register")
        if is_register and self.register_gate is not None:
            await self.register_gate.wait()
        script = self.register_script if is_register else self.feed_script
        if not script:
            return httpx.Response(500)
        return (script.pop(0) if len(script) > 1 else script[0])(req)

    async def _publish(self, source: str, topic: str, data: dict[str, Any]) -> None:
        self.publish_started.append(data)
        self.changed.set()
        if self.publish_gate is not None:
            await self.publish_gate.wait()
        if self.publish_raises:
            msg = "publish boom"
            raise RuntimeError(msg)
        self.published.append((source, topic, data))
        self.changed.set()

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        assert event == STATUS_EVENT
        self.statuses.append(dict(payload))
        self.changed.set()

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self.stop_after:
            self.stop.set()
        await asyncio.sleep(0)

    def calls(self, suffix: str) -> list[Req]:
        return [r for r in self.requests if r.path.endswith(suffix)]

    async def configure(self, **fields: Any) -> None:
        await self.storage.update_internet_feed_state(**fields)

    def start(self) -> None:
        self.task = asyncio.create_task(self.svc.run(self.stop))
        self.task.add_done_callback(lambda _t: self.changed.set())

    async def wait(self, pred: Callable[[], bool]) -> None:
        async def _loop() -> None:
            while not pred():
                self.changed.clear()
                if pred():
                    return
                await self.changed.wait()

        await asyncio.wait_for(_loop(), WAIT_S)

    async def finish(self) -> None:
        self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, WAIT_S)

    def state(self) -> str:
        return str(self.svc.status()["state"])


async def _with_env(
    fn: Callable[[Env], Any],
    inputs: Callable[[], IdentityInputs] | None = None,
    **env_kwargs: Any,
) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        storage = await create_sqlite_storage(tmp / "feed_test.db")
        try:
            env = Env(
                tmp,
                storage,
                inputs or (lambda: IdentityInputs(my_callsign="dk5en-98")),
                **env_kwargs,
            )
            try:
                await fn(env)
            finally:
                await env.finish()
        finally:
            await storage.close()


# ── identity ───────────────────────────────────────────────────────────────


def _test_identity(record: Record) -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "mac").write_text("dc:a6:32:01:02:03\n")
        (tmp / "model").write_bytes(b"Raspberry Pi Zero 2 W Rev 1.0\x00")
        regs = {
            "I": {
                "TYP": "I",
                "CALL": "dk5en-98",
                "ID": 0x406B878,
                "FWVER": "4.40 a",
                "HWID": 43,
                "BPIN": "123456",
                "WIFIPW": "hunter2",
            },
            "IS1": {"TYP": "IS1", "BDATE": "2026-10-11 00:33:57"},
        }
        ble = build_identity(
            IdentityInputs(regs, True, "ac:a7:04:06:b8:79", "dk5en-98", "v2.1.10"),
            tmp / "mac",
            tmp / "model",
        )
        record(
            "F1 BLE identity fields",
            ble
            == {
                "idKind": "ble",
                "call": "DK5EN-98",
                "mac": "AC:A7:04:06:B8:79",
                "nodeId": "0x406B878",
                "firmware": "4.40 a",
                "build": "2026-10-11 00:33:57",
                "hardware": "43",
                "mcappVersion": "v2.1.10",
            },
        )
        text = json.dumps(ble)
        record(
            "F2 BLE identity never carries the PIN or other register keys",
            "BPIN" not in text and "123456" not in text and "hunter2" not in text,
        )
        pi = build_identity(
            IdentityInputs(regs, False, "ac:a7:04:06:b8:79", "dk5en-98", "v2.1.10"),
            tmp / "mac",
            tmp / "model",
        )
        record(
            "F3 Pi identity: wlan0 MAC, model without NULs, no node fields",
            pi
            == {
                "idKind": "pi",
                "call": "DK5EN-98",
                "mac": "DC:A6:32:01:02:03",
                "hardware": "Raspberry Pi Zero 2 W Rev 1.0",
                "mcappVersion": "v2.1.10",
            },
        )
        bare = build_identity(
            IdentityInputs({}, False, None, "DK5EN", "v1"), tmp / "none", tmp / "none"
        )
        record(
            "F4 Pi identity falls back when files are absent",
            bare
            == {"idKind": "pi", "call": "DK5EN", "hardware": "Pi Zero 2W", "mcappVersion": "v1"},
        )
        no_mac = build_identity(
            IdentityInputs(regs, True, None, "DK5EN", "v1"), tmp / "mac", tmp / "model"
        )
        record("F5 BLE connected but no MAC -> Pi identity", no_mac["idKind"] == "pi")
        for label, addr in (
            ("F6 CoreBluetooth UUID address -> no mac key", "5f1c2d3e-1a2b-4c3d-8e9f-0123456789ab"),
            ("F8 garbage address -> no mac key", "not-a-mac"),
        ):
            odd = build_identity(
                IdentityInputs(regs, True, addr, "dk5en-98", "v1"), tmp / "mac", tmp / "model"
            )
            record(label, odd["idKind"] == "ble" and "mac" not in odd)
        dash = build_identity(
            IdentityInputs(regs, True, "ac-a7-04-06-b8-79", "dk5en-98", "v1"),
            tmp / "mac",
            tmp / "model",
        )
        record(
            "F7 dash-separated MAC still sent, upper-cased", dash.get("mac") == "AC-A7-04-06-B8-79"
        )


# ── mapper ─────────────────────────────────────────────────────────────────


def _msg(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "msgId": "687b4273",
        "from": "DF4ND-99",
        "via": "DF4ND-99",
        "to": "20",
        "text": "hello",
        "timeUnix": 1791703597,
    }
    base.update(over)
    return base


def _test_mapper(record: Record) -> None:
    ok = map_feed_message(_msg())
    record(
        "M1 mapped fields (upper-case msg_id, ms timestamp, www, msg type)",
        ok
        == {
            "src": "DF4ND-99",
            "dst": "20",
            "msg": "hello",
            "msg_id": "687B4273",
            "timestamp": 1791703597000,
            "type": "msg",
            "src_type": "www",
        },
    )
    m = map_feed_message(_msg(via="NET"))
    record("M2 via NET is dropped", m is not None and m["src"] == "DF4ND-99")
    m = map_feed_message(_msg(via="df4nd-99"))
    record("M3 via == from (case-insensitive) is dropped", m is not None and m["src"] == "DF4ND-99")
    m = map_feed_message(_msg(via="OE1ABC-12"))
    record("M4 real via is appended", m is not None and m["src"] == "DF4ND-99,OE1ABC-12")
    m = map_feed_message(_msg(via="not a call"))
    record("M5 non-callsign via is dropped", m is not None and m["src"] == "DF4ND-99")
    m = map_feed_message(_msg(via=None))
    record("M6 absent via leaves src alone", m is not None and m["src"] == "DF4ND-99")
    m = map_feed_message(_msg(**{"from": "OE1ABC-5"}, via="OE1ABC-5"))
    record("M7 SSID kept exactly as received", m is not None and m["src"] == "OE1ABC-5")
    bad: list[dict[str, Any]] = [
        _msg(msgId="123"),
        _msg(msgId="ZZZZZZZZ"),
        _msg(msgId=None),
        _msg(**{"from": ""}),
        _msg(to=" "),
        _msg(text=""),
        _msg(timeUnix="1791703597"),
        _msg(timeUnix=True),
        _msg(timeUnix=None),
    ]
    record("M8 malformed messages map to None", all(map_feed_message(b) is None for b in bad))
    record(
        "M9 non-dict maps to None", map_feed_message("x") is None and map_feed_message(None) is None
    )


# ── service cases ──────────────────────────────────────────────────────────


async def _case_register_and_batch(env: Env, record: Record) -> None:
    batch = (
        "batch",
        {
            "seq": 1,
            "messages": [_msg(), _msg(msgId="BAD"), _msg(msgId="1AE1E057", via="NET", to="*")],
            "positions": [{"call": "X"}],
            "removed": ["Y"],
        },
        "1791703630:687B4273",
    )
    env.script("register", reg_201(), reg_200(), reg_200())
    env.script("feed", feed_ok(hello(), batch), feed_ok(hello()))
    env.stop_after = 2
    await env.configure(enabled=True, groups=["20", "262"], broadcast=True)
    env.start()
    await env.wait(lambda: env.task is not None and env.task.done())
    st = await env.storage.get_internet_feed_state()
    reg1, reg2 = env.calls("/register")[:2]
    record(
        "R1 first register carries no Authorization",
        reg1.auth is None and reg1.body["idKind"] == "pi",
    )
    enc = st["token_enc"]
    record(
        "R2 201 token stored encrypted under its AD",
        isinstance(enc, str)
        and enc.startswith("v1:")
        and TOKEN not in enc
        and _ring(env, enc) == {PI_KEY: {"box_id": "b_1", "token": TOKEN}}
        and st["box_id"] == "b_1",
    )
    record(
        "R3 check-in after reconnect carries the stored token (kept across reconnect)",
        reg2.auth == f"Bearer {TOKEN}" and st["token_enc"] == enc,
    )
    feed1, feed2 = env.calls("/feed")[:2]
    record(
        "R4 feed query: groups, bcast, pos=off, no since on first connect; Bearer",
        feed1.query == {"groups": "20,262", "bcast": "1", "pos": "off"}
        and feed1.auth == f"Bearer {TOKEN}",
    )
    record(
        "R5 publish via www / www_message for each valid message, in order",
        [(s, t, d["msg_id"]) for s, t, d in env.published]
        == [("www", WWW_MESSAGE_TOPIC, "687B4273"), ("www", WWW_MESSAGE_TOPIC, "1AE1E057")]
        and env.published[1][2]["src"] == "DF4ND-99",
    )
    record(
        "R6 cursor persisted from id: and next connect sends since=",
        st["cursor"] == "1791703630:687B4273" and feed2.query.get("since") == "1791703630:687B4273",
    )
    record(
        "R7 last_batch_ms persisted",
        isinstance(st["last_batch_ms"], int) and st["last_batch_ms"] > 0,
    )
    record(
        "R8 identity_json stored",
        json.loads(st["identity_json"]).get("idKind") == "pi",
    )


async def _case_401(env: Env, record: Record) -> None:
    env.script(
        "register",
        reg_201(token="OLD-TOKEN-1"),
        lambda _r: httpx.Response(401, json={}),
        reg_201(box="b_2", token="NEW-TOKEN-2"),
        reg_200("b_2"),
    )
    env.script("feed", feed_ok(hello()))
    env.stop_after = 2
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.task is not None and env.task.done())
    regs = env.calls("/register")
    st = await env.storage.get_internet_feed_state()
    record(
        "A1 401 on check-in: token dropped, one token-less re-register",
        regs[1].auth == "Bearer OLD-TOKEN-1" and regs[2].auth is None,
    )
    feeds = env.calls("/feed")
    record(
        "A2 feed after re-register uses the new token and box",
        feeds[1].auth == "Bearer NEW-TOKEN-2"
        and st["box_id"] == "b_2"
        and _ring(env, st["token_enc"]) == {PI_KEY: {"box_id": "b_2", "token": "NEW-TOKEN-2"}},
    )


async def _case_halts(env: Env, record: Record, event: str, label: str) -> None:
    env.script(
        "register", reg_201(), reg_201(token="SECOND-TOKEN") if event == "revoked" else reg_200()
    )
    env.script("feed", feed_ok(hello(), (event, {}, None)))
    await env.configure(enabled=True)
    env.start()
    want = "revoked" if event == "revoked" else "error"
    await env.wait(lambda: env.state() == want)
    n = len(env.requests)
    await asyncio.sleep(0)
    st = await env.storage.get_internet_feed_state()
    status = env.svc.status()
    if event == "revoked":
        record(
            f"{label}a revoked: state revoked, token and box dropped",
            st["token_enc"] is None and st["box_id"] is None,
        )
    else:
        record(f"{label}a superseded: state error 'superseded'", status["error"] == "superseded")
    record(
        f"{label}b loop stops: no reconnect, no sleep",
        len(env.requests) == n == 2 and not env.sleeps,
    )
    # a later apply_settings re-arms
    env.script("feed", feed_ok(hello()))
    await env.svc.apply_settings(
        {"enabled": True, "groups": [], "broadcast": False, "pos_mode": "off", "radius_km": 300}
    )
    await env.wait(lambda: len(env.calls("/feed")) >= 2)
    record(f"{label}c apply_settings re-arms a halted loop", len(env.calls("/feed")) >= 2)


async def _case_version(env: Env, record: Record) -> None:
    env.script("register", reg_201())
    env.script("feed", feed_ok(hello(2)))
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.state() == "error")
    status = env.svc.status()
    record(
        "V1 unknown feedVersion major: error state, clear text, no retry",
        "feedVersion" in str(status["error"]) and len(env.calls("/feed")) == 1 and not env.sleeps,
    )
    st = await env.storage.get_internet_feed_state()
    record("V2 error persisted to last_error", "feedVersion" in str(st["last_error"]))


async def _case_backoff(env: Env, record: Record) -> None:
    env.script("register", lambda _r: httpx.Response(503, json={}))
    env.stop_after = 10
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.task is not None and env.task.done())
    expected = [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0, 300.0]
    within = all(0.8 * b <= s <= 1.2 * b for b, s in zip(expected, env.sleeps, strict=True))
    record(
        "B1 backoff 5 s doubling to 300 s with +-20% jitter",
        within and BACKOFF_START_S == 5.0 and BACKOFF_MAX_S == 300.0 and len(set(env.sleeps)) > 5,
    )
    record(
        "B2 transient failure shows error state", env.svc.status()["error"] == "register HTTP 503"
    )


async def _case_retry_after(env: Env, record: Record) -> None:
    env.script(
        "register",
        lambda _r: httpx.Response(429, json={"retryAfterS": 120}),
        lambda _r: httpx.Response(503, headers={"Retry-After": "90"}),
        lambda _r: httpx.Response(503, json={}),
    )
    env.stop_after = 3
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.task is not None and env.task.done())
    record(
        "B3 retryAfterS (body) and Retry-After (header) are a floor",
        env.sleeps[0] >= 120 and env.sleeps[1] >= 90 and env.sleeps[2] < 90,
    )


async def _case_backoff_reset(env: Env, record: Record) -> None:
    env.script("register", lambda _r: httpx.Response(503, json={}), reg_201(), reg_200())
    env.script("feed", feed_ok(hello()))
    env.stop_after = 3
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.task is not None and env.task.done())
    record(
        "B4 backoff resets after a successful hello",
        0.8 * 5 <= env.sleeps[0] <= 1.2 * 5 and 0.8 * 5 <= env.sleeps[2] <= 1.2 * 5,
    )


async def _case_disabled(env: Env, record: Record) -> None:
    gets = 0
    real_get = env.storage.get_internet_feed_state
    seen = asyncio.Event()

    async def counting() -> dict[str, Any]:
        nonlocal gets
        gets += 1
        if gets >= 2:
            seen.set()
        return await real_get()

    env.storage.get_internet_feed_state = counting  # type: ignore[method-assign]  # test spy
    env.start()
    await asyncio.wait_for(seen.wait(), WAIT_S)
    for _ in range(5):
        await asyncio.sleep(0)
    record("D1 disabled: no HTTP at all", env.requests == [] and env.state() == "off")
    await env.finish()
    record("D2 disabled loop ends on stop", env.task is not None and env.task.done())


async def _case_apply_settings(env: Env, record: Record) -> None:
    env.script("register", reg_201(), reg_200(), reg_200())
    env.script(
        "feed",
        lambda _r: hanging(hello()),
        lambda _r: hanging(hello()),
    )
    await env.configure(enabled=True, groups=["20"], broadcast=False)
    env.start()
    await env.wait(lambda: env.state() == "connected")
    settings = await env.svc.apply_settings(
        {
            "enabled": True,
            "groups": ["20", "262"],
            "broadcast": True,
            "pos_mode": "off",
            "radius_km": 300,
        }
    )
    await env.wait(lambda: len(env.calls("/feed")) >= 2)
    second = env.calls("/feed")[1]
    record(
        "S1 apply_settings reconnects with the new subscription",
        second.query["groups"] == "20,262" and second.query["bcast"] == "1",
    )
    record(
        "S2 apply_settings persists and marks configured",
        settings["groups"] == ["20", "262"]
        and settings["configured"] is True
        and settings["registered"] is True,
    )
    await env.wait(lambda: env.state() == "connected" and len(env.calls("/feed")) == 2)
    await env.svc.apply_settings(
        {
            "enabled": False,
            "groups": ["20"],
            "broadcast": False,
            "pos_mode": "off",
            "radius_km": 300,
        }
    )
    await env.wait(lambda: env.state() == "off")
    n = len(env.requests)
    for _ in range(5):
        await asyncio.sleep(0)
    record("S3 disabling goes idle and stops HTTP", env.state() == "off" and len(env.requests) == n)
    settings = await env.svc.forget()
    st = await env.storage.get_internet_feed_state()
    record(
        "S4 forget clears box and token",
        st["box_id"] is None and st["token_enc"] is None and settings["registered"] is False,
    )


async def _eventually(pred: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    """True once `pred` holds; False on timeout (a failed check, not a crashed case)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


def _settings(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "groups": [],
        "broadcast": False,
        "pos_mode": "off",
        "radius_km": 300,
    }
    base.update(over)
    return base


async def _case_cancel_mid_publish(env: Env, record: Record) -> None:
    """A publish that began must finish even when the session is cancelled around it."""
    env.script("register", reg_201(), reg_200())
    env.script(
        "feed",
        lambda _r: hanging(hello(), ("batch", {"messages": [_msg()]}, "1:687B4273")),
        lambda _r: hanging(hello()),
        lambda _r: hanging(hello(), ("batch", {"messages": [_msg(msgId="1AE1E057")]}, "2:X")),
    )
    env.publish_gate = asyncio.Event()
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: len(env.publish_started) == 1)
    await env.svc.apply_settings(_settings(groups=["20"]))  # cancels the session
    for _ in range(10):
        await asyncio.sleep(0)
    env.publish_gate.set()
    done = await _eventually(lambda: len(env.published) == 1)
    record("P1 publish in flight when the session is cancelled still completes", done)
    st = await env.storage.get_internet_feed_state()
    record("P2 cancelled batch does not advance the cursor", st["cursor"] is None)
    env.publish_raises = True
    env.publish_gate = asyncio.Event()
    logs: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logs.append(record.getMessage())

    handler = Capture(level=logging.ERROR)
    logging.getLogger().addHandler(handler)
    try:
        await env.wait(lambda: env.state() == "connected" and len(env.calls("/feed")) == 2)
        await env.svc.apply_settings(_settings(groups=["20", "262"]))
        await env.wait(lambda: len(env.publish_started) == 2)
        await env.svc.apply_settings(_settings(enabled=False))
        for _ in range(10):
            await asyncio.sleep(0)
        env.publish_gate.set()
        logged = await _eventually(lambda: any("publish failed" in x for x in logs))
    finally:
        logging.getLogger().removeHandler(handler)
    record("P3 failure of a shielded publish after the cancel is still logged", logged)


async def _case_pause_ignores_identity_hints(env: Env, record: Record) -> None:
    # retryAfterS 100 x delay_scale 0.005 = a real 0.5 s pause (no injected sleep).
    env.script("register", lambda _r: httpx.Response(429, json={"retryAfterS": 100}))
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: len(env.calls("/register")) == 1)
    await env.wait(lambda: env.state() == "error")
    t0 = asyncio.get_running_loop().time()
    for _ in range(6):
        env.svc.notify_identity_changed()
        await asyncio.sleep(0.03)
    record(
        "W1 identity hints do not end a Retry-After pause (no extra register)",
        len(env.calls("/register")) == 1,
    )
    await env.svc.apply_settings(_settings())
    ended = await _eventually(lambda: len(env.calls("/register")) == 2, 1.0)
    elapsed = asyncio.get_running_loop().time() - t0
    record("W2 apply_settings ends the pause at once", ended and elapsed < 0.4)


async def _case_forget_race(env: Env, record: Record) -> None:
    """forget() while a register is in flight must not be undone by that register."""
    env.script("register", reg_201(token="STALE-TOKEN"), reg_201(token="FRESH-TOKEN"))
    env.script("feed", lambda _r: hanging(hello()))
    env.register_gate = asyncio.Event()
    await env.configure(enabled=True)
    real = env.storage.update_internet_feed_state
    cleared = asyncio.Event()
    release_forget = asyncio.Event()
    stale_written: list[bool] = []

    async def spy(**fields: Any) -> None:
        await real(**fields)
        if fields.get("token_enc"):
            stale_written.append("STALE-TOKEN" in env.box.decrypt(fields["token_enc"], TOKENS_AD))
        elif "identity_json" in fields and fields["identity_json"] is None:
            cleared.set()
            await release_forget.wait()  # the clear is done; forget() has not returned yet

    env.storage.update_internet_feed_state = spy  # type: ignore[method-assign]  # test spy
    env.start()
    await env.wait(lambda: len(env.calls("/register")) == 1)
    forget_task = asyncio.create_task(env.svc.forget())
    await asyncio.wait_for(cleared.wait(), WAIT_S)
    env.register_gate.set()  # the in-flight register now answers 201 with STALE-TOKEN
    await _eventually(lambda: bool(stale_written), 0.3)  # old code writes it back here
    release_forget.set()
    await forget_task
    await env.wait(lambda: env.state() == "connected")
    st = await env.storage.get_internet_feed_state()
    record(
        "G1 register in flight during forget() does not write its token back",
        True not in stale_written
        and "STALE-TOKEN" not in env.box.decrypt(st["token_enc"], TOKENS_AD),
    )
    record(
        "G2 after forget the next register is token-less and its token is kept",
        len(env.calls("/register")) == 2
        and env.calls("/register")[1].auth is None
        and "FRESH-TOKEN" in env.box.decrypt(st["token_enc"], TOKENS_AD),
    )


async def _case_lost_settings(env: Env, record: Record) -> None:
    """apply_settings landing while run() reads state must not be consumed."""
    env.script("register", reg_201(), reg_200())
    env.script("feed", lambda _r: hanging(hello()), lambda _r: hanging(hello()))
    await env.configure(enabled=True, groups=["20"])
    real = env.storage.get_internet_feed_state
    calls = 0
    guard = False

    async def spy() -> dict[str, Any]:
        nonlocal calls, guard
        out = await real()
        if guard:
            return out
        calls += 1
        if calls == 2:  # the loop-top read of the first iteration
            guard = True
            await env.svc.apply_settings(_settings(groups=["20", "262"]))
            guard = False
        return out  # the stale read: groups == ["20"]

    env.storage.get_internet_feed_state = spy  # type: ignore[method-assign]  # test spy
    env.start()
    ok = await _eventually(
        lambda: any(r.query.get("groups") == "20,262" for r in env.calls("/feed")), 3.0
    )
    record("L1 apply_settings during run()'s state read still reaches the connection", ok)


async def _case_fresh_install_register_not_cancelled(env: Env, record: Record) -> None:
    """Without a stored identity, an identity hint must not cancel a register in flight."""
    env.script("register", reg_201())
    env.script("feed", lambda _r: hanging(hello()))
    env.register_gate = asyncio.Event()
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: len(env.calls("/register")) == 1)
    env.svc.notify_identity_changed()  # BLE status event, identity unchanged
    for _ in range(20):
        await asyncio.sleep(0.005)
    env.register_gate.set()
    await env.wait(lambda: env.state() == "connected")
    record(
        "D3 identity hint with an unchanged identity leaves a fresh-install register alone",
        len(env.calls("/register")) == 1,
    )


BLE_MAC = "ac:a7:04:06:b8:79"
BLE_KEY = "DK5EN-98|BLE|AC:A7:04:06:B8:79"


def _to_ble(inputs: IdentityInputs, call: str = "DK5EN-98", fwver: str = "4.40") -> None:
    inputs.ble_connected = True
    inputs.ble_mac = BLE_MAC
    inputs.registers = {"I": {"CALL": call, "ID": 5, "FWVER": fwver, "HWID": 3}}


def _to_pi(inputs: IdentityInputs) -> None:
    inputs.ble_connected = False
    inputs.ble_mac = None
    inputs.registers = {}  # the register cache is wiped on a BLE disconnect


async def _seed(env: Env, ring: dict[str, dict[str, str]], serves: bool = True) -> None:
    """Pre-load the keyring as an earlier process would have left it."""
    if serves:
        for key, entry in ring.items():
            env.issued[entry["token"]] = key
    box_id = next(reversed(ring.values()))["box_id"] if ring else None
    await env.configure(
        enabled=True, token_enc=env.box.encrypt(json.dumps(ring), TOKENS_AD), box_id=box_id
    )


async def _case_identity_change(env: Env, record: Record, inputs: IdentityInputs) -> None:
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.state() == "connected")
    _to_ble(inputs)
    env.svc.notify_identity_changed()
    await env.wait(lambda: len(env.calls("/feed")) >= 2)
    regs = env.calls("/register")
    record(
        "I1 identity change registers the new identity without the old identity's token",
        regs[1].body["idKind"] == "ble" and regs[1].auth is None,
    )
    _to_pi(inputs)
    env.svc.notify_identity_changed()
    await env.wait(lambda: len(env.calls("/feed")) >= 3)
    regs = env.calls("/register")
    pi_token = next(tok for tok, key in env.issued.items() if key == PI_KEY)
    record(
        "I2 switching back to pi reuses the pi token: Bearer check-in, no reclaim, no 401",
        regs[2].auth == f"Bearer {pi_token}"
        and env.reclaims == []
        and env.rejected == []
        and len(env.tokenless) == 2,
    )
    ring = _ring(env, (await env.storage.get_internet_feed_state())["token_enc"])
    record(
        "I3 keyring holds one token per identity",
        sorted(ring) == sorted([PI_KEY, BLE_KEY]) and list(ring)[-1] == PI_KEY,
    )


async def _case_restart_with_ble_token(env: Env, record: Record, inputs: IdentityInputs) -> None:
    _to_ble(inputs)
    await _seed(env, {BLE_KEY: {"box_id": "box-ble", "token": "TOK-BLE"}})
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    env.start()
    await env.wait(lambda: env.state() == "connected")
    reg = env.calls("/register")[0]
    record(
        "K1 restart with a stored BLE token: Bearer check-in, no token-less register",
        reg.auth == "Bearer TOK-BLE" and env.tokenless == [] and env.rejected == [],
    )
    feed = env.calls("/feed")[0]
    st = await env.storage.get_internet_feed_state()
    record(
        "K2 feed uses the BLE identity's token; box_id column is the identity in use",
        feed.auth == "Bearer TOK-BLE" and st["box_id"] == f"box-{BLE_KEY}",
    )


async def _wait_feeds(env: Env, n: int) -> None:
    await env.wait(lambda: len(env.calls("/feed")) >= n)


async def _case_keyring_bound(env: Env, record: Record, inputs: IdentityInputs) -> None:
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: len(env.calls("/feed")) >= 1)
    for i in range(4):
        inputs.my_callsign = f"DK5EN-{i}"
        env.svc.notify_identity_changed()
        await _wait_feeds(env, i + 2)
    ring = _ring(env, (await env.storage.get_internet_feed_state())["token_enc"])
    record(
        "K3 keyring is bounded to 4 identities, least recently used dropped",
        list(ring) == [f"DK5EN-{i}|PI|" for i in range(4)],
    )


async def _case_401_drops_only_that_identity(env: Env, record: Record) -> None:
    await _seed(env, {BLE_KEY: {"box_id": "box-ble", "token": "TOK-BLE"}})
    ring = _ring(env, (await env.storage.get_internet_feed_state())["token_enc"])
    ring[PI_KEY] = {"box_id": "box-pi", "token": "TOK-PI-STALE"}  # meshmap no longer knows it
    await env.configure(token_enc=env.box.encrypt(json.dumps(ring), TOKENS_AD))
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    env.start()
    await env.wait(lambda: env.state() == "connected")
    regs = env.calls("/register")
    ring = _ring(env, (await env.storage.get_internet_feed_state())["token_enc"])
    record(
        "K4 401 drops only that identity's token; the other identity's entry survives",
        regs[0].auth == "Bearer TOK-PI-STALE"
        and regs[1].auth is None
        and ring[BLE_KEY] == {"box_id": "box-ble", "token": "TOK-BLE"}
        and ring[PI_KEY]["token"] != "TOK-PI-STALE",
    )


async def _case_legacy_token(env: Env, record: Record) -> None:
    legacy = env.box.encrypt("LEGACY-SECRET-TOKEN", "internet_feed.token:b_1")
    await env.configure(enabled=True, token_enc=legacy, box_id="b_1")
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    logs: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logs.append(record.getMessage())

    handler = Capture(level=logging.DEBUG)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        env.start()
        await env.wait(lambda: env.state() == "connected")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    st = await env.storage.get_internet_feed_state()
    record(
        "K5 legacy / undecryptable token_enc: empty keyring, token-less register, replaced",
        env.calls("/register")[0].auth is None
        and PI_KEY in _ring(env, st["token_enc"])
        and any("cannot be read" in x for x in logs)
        and "LEGACY-SECRET-TOKEN" not in "\n".join(logs),
    )


async def _case_field_change_no_reregister(
    env: Env, record: Record, inputs: IdentityInputs
) -> None:
    _to_ble(inputs)
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.state() == "connected")
    _to_ble(inputs, fwver="4.41")  # same identity key, refreshed firmware field
    env.svc.notify_identity_changed()
    for _ in range(20):
        await asyncio.sleep(0.005)
    record(
        "K6 hint with the same identity key (firmware field) does not re-register",
        len(env.calls("/register")) == 1 and len(env.calls("/feed")) == 1,
    )


async def _case_holdoff_ble_arrives(env: Env, record: Record, inputs: IdentityInputs) -> None:
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.state() == "connecting")
    for _ in range(20):
        await asyncio.sleep(0.005)
    held = env.calls("/register") == []
    # apply_settings must not be blocked by (or restart) the hold-off
    await env.svc.apply_settings(_settings())
    for _ in range(20):
        await asyncio.sleep(0.005)
    still_held = env.calls("/register") == []
    _to_ble(inputs)
    env.svc.notify_identity_changed()
    await env.wait(lambda: len(env.calls("/register")) >= 1)
    record(
        "O1 BLE expected, identity still pi: no register until the BLE identity appears",
        held and still_held,
    )
    record(
        "O2 first register after the hold-off is the BLE identity",
        env.calls("/register")[0].body["idKind"] == "ble" and env.tokenless == [BLE_KEY],
    )


async def _case_holdoff_timeout(env: Env, record: Record) -> None:
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True)
    t0 = asyncio.get_running_loop().time()
    env.start()
    await env.wait(lambda: len(env.calls("/register")) >= 1)
    waited = asyncio.get_running_loop().time() - t0
    record(
        "O3 hold-off timeout: registers as pi after the wait",
        env.calls("/register")[0].body["idKind"] == "pi" and waited >= 0.2,
    )


async def _case_holdoff_stop(env: Env, record: Record) -> None:
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: env.state() == "connecting")
    await asyncio.wait_for(env.finish(), 1.0)
    record(
        "O4 stop during the hold-off ends the loop at once, nothing registered",
        env.task is not None and env.task.done() and env.calls("/register") == [],
    )


async def _case_holdoff_not_expected(env: Env, record: Record) -> None:
    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True)
    env.start()
    await env.wait(lambda: len(env.calls("/register")) >= 1)
    record(
        "O5 BLE disabled: no hold-off, registers as pi at once",
        env.calls("/register")[0].body["idKind"] == "pi",
    )


async def _case_secrecy(env: Env, record: Record) -> None:
    env.script("register", reg_201(), reg_200())
    env.script("feed", feed_ok(hello(), ("batch", {"messages": [_msg()]}, "1:AAAAAAAA")))
    env.stop_after = 2
    logs: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logs.append(record.getMessage())
            if record.exc_info:
                logs.append(repr(record.exc_info[1]))

    root = logging.getLogger()
    handler = Capture(level=logging.DEBUG)
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        await env.configure(enabled=True)
        env.start()
        await env.wait(lambda: env.task is not None and env.task.done())
        settings = await env.svc.get_settings()
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    blob = json.dumps([env.svc.status(), settings, env.statuses])
    record("T1 token not in status(), get_settings() or emitted status", TOKEN not in blob)
    record("T2 token not in any log line", TOKEN not in "\n".join(logs) and bool(logs))
    st = await env.storage.get_internet_feed_state()
    record(
        "T3 get_settings exposes no token field",
        "token" not in " ".join(settings)
        and "token_enc" not in settings
        and st["token_enc"] is not None,
    )
    record(
        "T4 status emitted on transitions",
        [s["state"] for s in env.statuses][:2] == ["connecting", "connected"],
    )


# ── positions (wave B1b) ───────────────────────────────────────────────────


class _Rec:
    """Station position source and `on_positions` sink for the positions cases."""

    def __init__(self) -> None:
        self.station: tuple[float, float] | None = None
        self.calls: list[dict[str, Any]] = []
        self.raises = False
        self.hook: Callable[[], Any] | None = None
        self.changed = asyncio.Event()
        self.clock_offset_ms = 0  # injected service clock = real time + this
        self.decisions: list[str | None] = []  # every `_station_move` verdict, in order
        self.decided = asyncio.Event()

    def clock(self) -> int:
        return now_ms() + self.clock_offset_ms

    def station_fn(self) -> tuple[float, float] | None:
        return self.station

    async def cb(self, payload: dict[str, Any]) -> None:
        self.calls.append(payload)
        self.changed.set()
        if self.hook is not None:
            await self.hook()
        if self.raises:
            msg = "positions boom"
            raise RuntimeError(msg)

    def empties(self) -> int:
        return sum(1 for c in self.calls if c == {"reset": True, "positions": [], "removed": []})


CENTRE = (50.1, 8.5)


async def _spin(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _case_pos_query(env: Env, record: Record, rec: _Rec) -> None:
    env.script("register", reg_201(), reg_200(), reg_200(), reg_200())
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True, pos_mode="off")
    env.start()
    await env.wait(lambda: env.state() == "connected")
    q = env.calls("/feed")[-1].query
    record("P1 pos=off query carries no centre", q["pos"] == "off" and "lat" not in q)
    s = await env.svc.get_settings()
    record(
        "P1b off: positions effective off, no note",
        s["positions"] == {"effective": "off", "note": None},
    )
    await env.svc.apply_settings(_settings(pos_mode="world"))
    await _wait_feeds(env, 2)
    q = env.calls("/feed")[-1].query
    record(
        "P2 pos=world: no lat/lon/radiusKm",
        q["pos"] == "world" and not {"lat", "lon", "radiusKm"} & set(q),
    )
    rec.station = CENTRE
    await env.svc.apply_settings(_settings(pos_mode="radius", radius_km=250))
    await _wait_feeds(env, 3)
    q = env.calls("/feed")[-1].query
    record(
        "P3 pos=radius sends 6-decimal lat/lon and radiusKm",
        q["pos"] == "radius"
        and q["lat"] == "50.100000"
        and q["lon"] == "8.500000"
        and q["radiusKm"] == "250",
    )
    s = await env.svc.get_settings()
    record(
        "P3b radius: positions effective radius",
        s["positions"] == {"effective": "radius", "note": None} and s["pos_mode"] == "radius",
    )
    rec.station = None
    await env.svc.apply_settings(_settings(pos_mode="radius", radius_km=250))
    await _wait_feeds(env, 4)
    q = env.calls("/feed")[-1].query
    s = await env.svc.get_settings()
    record(
        "P4 radius without a station position connects pos=off and says why",
        q["pos"] == "off"
        and "lat" not in q
        and s["positions"]["effective"] == "off"
        and "station position" in str(s["positions"]["note"])
        and s["pos_mode"] == "radius",
    )


async def _case_pos_events(env: Env, record: Record, rec: _Rec) -> None:
    seen_at_cb: list[tuple[int, Any]] = []

    async def hook() -> None:
        st = await env.storage.get_internet_feed_state()
        seen_at_cb.append((len(env.published), st["cursor"]))

    rec.hook = hook
    env.script("register", reg_201())
    env.script(
        "feed",
        lambda _r: hanging(
            hello(),
            ("positions", {"reset": True, "positions": [{"call": "A"}, {"call": "B"}]}, None),
            (
                "batch",
                {
                    "messages": [_msg()],
                    "positions": [{"call": "C"}],
                    "removed": ["A"],
                },
                "1:ONE",
            ),
            (
                "batch",
                {"messages": [_msg(msgId="1AE1E057")], "positions": "x", "removed": 5},
                "2:TWO",
            ),
            ("batch", {"messages": [], "positions": [], "removed": []}, "3:THREE"),
        ),
    )
    await env.configure(enabled=True, pos_mode="world")
    env.start()
    await env.wait(lambda: len(env.published) >= 2)
    await env.wait(lambda: (env.svc.status()["last_batch_ms"] or 0) > 0)
    st = await env.storage.get_internet_feed_state()
    await _eventually(lambda: st is not None and len(rec.calls) >= 2)
    record(
        "P5 positions event -> on_positions reset with the list",
        rec.calls[0] == {"reset": True, "positions": [{"call": "A"}, {"call": "B"}], "removed": []},
    )
    record(
        "P6 batch positions/removed -> callback after its publishes, before the cursor",
        rec.calls[1] == {"reset": False, "positions": [{"call": "C"}], "removed": ["A"]}
        and seen_at_cb[1] == (1, None),
    )
    await _eventually(lambda: env.svc.status()["last_batch_ms"] is not None)
    for _ in range(3):
        await _spin()
    record(
        "P7 malformed / empty batch fields -> no callback",
        len(rec.calls) == 2,
    )


async def _case_pos_idle(env: Env, record: Record, rec: _Rec) -> None:
    env.script("register", reg_201(), reg_200(), reg_200(), reg_200())
    env.script(
        "feed",
        lambda _r: hanging(
            hello(), ("positions", {"reset": True, "positions": [{"call": "A"}]}, None)
        ),
    )
    await env.configure(enabled=True, pos_mode="world")
    env.start()
    await _eventually(lambda: len(rec.calls) >= 1, 5.0)
    await env.svc.apply_settings(_settings(pos_mode="off"))
    ok = await _eventually(lambda: rec.empties() == 1)
    record("P8 pos_mode switched to off empties the cache once", ok)
    await _wait_feeds(env, 2)
    await _spin()
    record("P8b ... and not again on the off reconnect", rec.empties() == 1)
    await env.svc.apply_settings(_settings(pos_mode="world"))
    await _eventually(lambda: rec.calls[-1].get("positions") == [{"call": "A"}], 5.0)
    await env.svc.apply_settings(_settings(enabled=False, pos_mode="world"))
    ok = await _eventually(lambda: rec.empties() == 2)
    await _spin()
    record("P9 disabling empties the cache once", ok and rec.empties() == 2)
    await env.configure(enabled=True)
    await env.svc.apply_settings(_settings(pos_mode="world"))
    await _eventually(lambda: rec.calls[-1].get("positions") == [{"call": "A"}], 5.0)
    await env.svc.forget()
    ok = await _eventually(lambda: rec.empties() == 3)
    record("P10 forget empties the cache", ok)


async def _case_pos_station_moved(env: Env, record: Record, rec: _Rec) -> None:
    rec.station = CENTRE
    orig_move = env.svc._station_move

    def spy() -> str | None:
        verdict = orig_move()
        rec.decisions.append(verdict)
        rec.decided.set()
        return verdict

    env.svc._station_move = spy  # type: ignore[method-assign]

    async def decided(n: int) -> None:
        """Block (event-driven) until the service has judged `n` hints in total."""
        while len(rec.decisions) < n:
            rec.decided.clear()
            if len(rec.decisions) >= n:
                return
            await asyncio.wait_for(rec.decided.wait(), WAIT_S)

    env.script("register", env.contract_register)
    env.script("feed", lambda _r: hanging(hello()))
    await env.configure(enabled=True, pos_mode="radius", radius_km=300)
    env.start()
    await env.wait(lambda: env.state() == "connected")
    rec.clock_offset_ms += (POSITION_RECONNECT_MIN_S + 100) * 1000  # register window is open
    rec.station = (CENTRE[0] + 0.009, CENTRE[1])  # ~1 km
    env.svc.notify_identity_changed()
    await decided(1)
    record(
        "P11 station moved ~1 km: judged 'not moved', no reconnect",
        rec.decisions == [None] and len(env.calls("/feed")) == 1,
    )
    rec.station = (CENTRE[0] + 0.054, CENTRE[1])  # ~6 km from the connect centre
    env.svc.notify_identity_changed()
    await _wait_feeds(env, 2)
    q = env.calls("/feed")[-1].query
    record(
        "P12 station moved ~6 km: reconnect with the new lat/lon (exactly one more /feed)",
        q["lat"] == "50.154000" and q["lon"] == "8.500000" and len(env.calls("/feed")) == 2,
    )
    record(
        "P12b ... without backoff or identity churn (no sleep, same identity)",
        env.sleeps == [] and len(env.tokenless) == 1 and env.rejected == [],
    )
    await env.wait(lambda: env.state() == "connected")
    # P14: a second move 2 min after the reconnect is deferred, applied when the window opens.
    rec.clock_offset_ms += 120 * 1000
    rec.station = (CENTRE[0] + 0.108, CENTRE[1])  # ~6 km from the new centre
    n = len(rec.decisions)
    env.svc.notify_identity_changed()
    await decided(n + 1)
    record(
        "P14 second move inside the window: judged 'moved' but deferred (pending, no reconnect)",
        rec.decisions[-1] == "moved" and env.svc._move_pending and len(env.calls("/feed")) == 2,
    )
    rec.clock_offset_ms += (POSITION_RECONNECT_MIN_S - 120) * 1000  # window opens now
    await _wait_feeds(env, 3)  # the timer, not another hint, re-checks
    q = env.calls("/feed")[-1].query
    record(
        "P14b ... applied when the window opens, with the position then current",
        q["lat"] == "50.208000" and len(env.calls("/feed")) == 3 and not env.svc._move_pending,
    )
    await env.wait(lambda: env.state() == "connected")
    # P13 (exempt from the window): a position APPEARING for a pending radius reconnects at once.
    rec.station = None
    await env.svc.apply_settings(_settings(pos_mode="radius"))
    await _wait_feeds(env, 4)
    rec.station = CENTRE  # the session started moments ago: window closed
    env.svc.notify_identity_changed()
    await _wait_feeds(env, 5)
    q = env.calls("/feed")[-1].query
    record(
        "P13 a station position appearing for a pending radius reconnects despite the window",
        env.calls("/feed")[3].query["pos"] == "off"
        and q["pos"] == "radius"
        and q["lat"] == "50.100000",
    )


async def _case_pos_callback_raises(env: Env, record: Record, rec: _Rec) -> None:
    rec.raises = True
    env.script("register", reg_201())
    env.script(
        "feed",
        lambda _r: hanging(
            hello(),
            ("positions", {"reset": True, "positions": [{"call": "A"}]}, None),
            ("batch", {"messages": [_msg()], "positions": [{"call": "B"}]}, "1:ONE"),
            ("batch", {"messages": [_msg(msgId="1AE1E057")]}, "2:TWO"),
        ),
    )
    await env.configure(enabled=True, pos_mode="world")
    env.start()
    await env.wait(lambda: len(env.published) >= 2)
    st = await env.storage.get_internet_feed_state()
    await _eventually(lambda: len(rec.calls) >= 2)
    st = await env.storage.get_internet_feed_state()
    record(
        "P15 a raising on_positions does not kill the session or the later batches",
        env.state() == "connected" and len(rec.calls) == 2 and st["cursor"] == "2:TWO",
    )


async def run_internet_feed_tests() -> bool:
    if has_console:
        print("\n🧪 Testing internet feed service:")
        print("=" * 55)
    results: list[tuple[str, bool]] = []

    def record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _test_identity(record)
    _test_mapper(record)

    cases: list[Callable[[Env], Any]] = [
        lambda e: _case_register_and_batch(e, record),
        lambda e: _case_401(e, record),
        lambda e: _case_halts(e, record, "superseded", "H1"),
        lambda e: _case_halts(e, record, "revoked", "H2"),
        lambda e: _case_version(e, record),
        lambda e: _case_backoff(e, record),
        lambda e: _case_retry_after(e, record),
        lambda e: _case_backoff_reset(e, record),
        lambda e: _case_disabled(e, record),
        lambda e: _case_apply_settings(e, record),
        lambda e: _case_secrecy(e, record),
        lambda e: _case_cancel_mid_publish(e, record),
        lambda e: _case_forget_race(e, record),
        lambda e: _case_lost_settings(e, record),
        lambda e: _case_fresh_install_register_not_cancelled(e, record),
        lambda e: _case_401_drops_only_that_identity(e, record),
        lambda e: _case_legacy_token(e, record),
    ]
    for case in cases:
        try:
            await _with_env(case)
        except Exception as exc:
            record(f"case crashed: {type(exc).__name__}: {exc}", False)
    try:
        await _with_env(
            lambda e: _case_pause_ignores_identity_hints(e, record),
            inject_sleep=False,
            delay_scale=0.005,
        )
    except Exception as exc:
        record(f"pause case crashed: {type(exc).__name__}: {exc}", False)

    async def with_shared(
        fn: Callable[[Env, IdentityInputs], Any], expected: bool = False, **env_kwargs: Any
    ) -> None:
        shared = IdentityInputs(my_callsign="DK5EN-98", ble_expected=expected)
        try:
            await _with_env(lambda e: fn(e, shared), lambda: shared, **env_kwargs)
        except Exception as exc:
            record(f"{fn.__name__} crashed: {type(exc).__name__}: {exc}", False)

    await with_shared(lambda e, i: _case_identity_change(e, record, i))
    await with_shared(lambda e, i: _case_restart_with_ble_token(e, record, i))
    await with_shared(lambda e, i: _case_keyring_bound(e, record, i))
    await with_shared(lambda e, i: _case_field_change_no_reregister(e, record, i))
    await with_shared(
        lambda e, i: _case_holdoff_ble_arrives(e, record, i), True, startup_holdoff_s=30.0
    )
    await with_shared(lambda e, _i: _case_holdoff_timeout(e, record), True, startup_holdoff_s=0.2)
    await with_shared(lambda e, _i: _case_holdoff_stop(e, record), True, startup_holdoff_s=30.0)
    await with_shared(lambda e, _i: _case_holdoff_not_expected(e, record), False)

    async def with_rec(fn: Callable[[Env, _Rec], Any]) -> None:
        rec = _Rec()
        try:
            await _with_env(
                lambda e: fn(e, rec),
                station_position=rec.station_fn,
                on_positions=rec.cb,
                clock=rec.clock,
                delay_scale=0.0002,  # a deferred move's 480 s window is a real 0.1 s timer
            )
        except Exception as exc:
            record(f"{fn.__name__} crashed: {type(exc).__name__}: {exc}", False)

    await with_rec(lambda e, r: _case_pos_query(e, record, r))
    await with_rec(lambda e, r: _case_pos_events(e, record, r))
    await with_rec(lambda e, r: _case_pos_idle(e, record, r))
    await with_rec(lambda e, r: _case_pos_station_moved(e, record, r))
    await with_rec(lambda e, r: _case_pos_callback_raises(e, record, r))

    passed = sum(1 for _, ok in results if ok)
    if has_console or passed != len(results):
        for label, ok in results:
            if not ok:
                print(f"❌ FAIL | {label}")
        print(f"\n🧪 Internet feed Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run_internet_feed_tests()) else 1)
