"""Regression suite for the QRZ.com callsign lookup (issue #14).

Plan: doc/2026-10-04_0848-qrz-callsign-lookup-plan.md. Fully offline: QRZ is
an `httpx.MockTransport`, time is a fake clock, every scenario gets a
throwaway SQLite DB and a throwaway install key.

Coverage:
  S. `secret_box`: round trip, the key file is 0600, and decryption fails on
     another board, under another username, after tampering, and with a
     regenerated install key. Board-serial source precedence.
  P. `qrz_client`: every row of the plan's §3 table, against the live response
     shape (xmlns) and the spec's own examples (no xmlns); name/QTH
     normalisation; callsign candidate shape.
  Q. `QrzLookupService`: 30 s spacing for every request, the 50/24 h hard cap
     and its 24 h suspension (also across a restart), the ledger counting a
     lookup whose request failed, exponential backoff with Retry-After, the
     session-loss rules, a fixed 1 h back-off after a refusal or rate limit, a
     rejected login stopping all traffic, QRZ's own Count (a login-time baseline
     that never suspends, ignored when implausible, a stale Count suspension
     lifted), cache refresh windows, candidate selection, POST-only transport,
     the in-flight credential-change race, the API routes never returning the
     password, the live `proxy:callsign_info` push and its connect-burst
     snapshot, and the greppable login/lookup/refusal log lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from .commands.constants import has_console
from .qrz_client import (
    Outcome,
    first_name,
    is_lookup_candidate,
    parse_response,
    qth_from_addr2,
)
from .qrz_service import (
    BACKOFF_BASE_MS,
    BACKOFF_MAX_MS,
    DAILY_CAP,
    MIN_INTERVAL_MS,
    REFRESH_FOUND_MS,
    REFRESH_NOT_FOUND_MS,
    REFUSAL_BACKOFF_MS,
    SERVER_COUNT_PLAUSIBLE_MAX,
    SUSPEND_MS,
    WINDOW_MS,
    QrzLookupService,
)
from .qrz_service import logger as service_logger
from .secret_box import SecretBox, SecretBoxError, board_binding
from .sqlite_storage import SQLiteStorage, create_sqlite_storage
from .sse_handler import SSEManager
from .sse_routes.qrz import build_qrz_router

Record = Callable[[str, bool], None]

T0 = 1_791_000_000_000  # fixed epoch ms for the fake clock
PASSWORD = "correct horse battery"

_NS = ' xmlns="http://xmldata.qrz.com"'


def _session_xml(
    *,
    key: str | None = "KEY1",
    count: int | None = 1,
    error: str | None = None,
    ns: bool = True,
    callsign: dict[str, str] | None = None,
) -> str:
    parts = [f'<?xml version="1.0" ?><QRZDatabase version="1.36"{_NS if ns else ""}>']
    if callsign:
        fields = "".join(f"<{k}>{v}</{k}>" for k, v in callsign.items())
        parts.append(f"<Callsign>{fields}</Callsign>")
    parts.append("<Session>")
    if error:
        parts.append(f"<Error>{error}</Error>")
    if key:
        parts.append(f"<Key>{key}</Key>")
    parts.append(f"<Count>{count}</Count>" if count is not None else "")
    parts.append("<SubExp>non-subscriber</SubExp>")
    parts.append("<GMTime>Sun Oct  4 06:37:15 2026</GMTime></Session></QRZDatabase>")
    return "".join(parts)


def _with_sub_exp(body: str, sub_exp: str | None) -> str:
    """Swap the free-tier `SubExp` for a subscriber's expiry date, or drop it."""
    tag = f"<SubExp>{sub_exp}</SubExp>" if sub_exp is not None else ""
    return body.replace("<SubExp>non-subscriber</SubExp>", tag)


DK5EN = {"call": "DK5EN", "fname": "Martin Stefan", "name": "Werner", "addr2": "Freising",
         "state": "BY", "country": "Germany"}  # fmt: skip


class FakeClock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakeQrz:
    """Scripted QRZ endpoint. `responder(form)` returns (status, body) or
    raises; every request is logged with the clock time it arrived at."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.requests: list[tuple[int, dict[str, str], httpx.Request]] = []
        self.responder: Callable[[dict[str, str]], tuple[int, str, dict[str, str]]] = self.default
        self.count = 0
        self.report_count = True
        self.sub_exp: str | None = "non-subscriber"

    def rolling_count(self) -> int:
        """QRZ's Count: lookups in the last 24 h, plus `count` as lookups
        another program made with the same account."""
        if not self.report_count:
            return 0
        since = self.clock.now - WINDOW_MS
        return self.count + sum(1 for t, f, _ in self.requests if "callsign" in f and t > since)

    def default(self, form: dict[str, str]) -> tuple[int, str, dict[str, str]]:
        if "username" in form:
            return 200, _with_sub_exp(_session_xml(count=self.rolling_count()), self.sub_exp), {}
        call = form["callsign"]
        body = _session_xml(count=self.rolling_count(), callsign={**DK5EN, "call": call})
        return 200, _with_sub_exp(body, self.sub_exp), {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        self.requests.append((self.clock.now, form, request))
        status, body, headers = self.responder(form)
        return httpx.Response(status, text=body, headers=headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def lookups(self) -> list[tuple[int, dict[str, str], httpx.Request]]:
        return [r for r in self.requests if "callsign" in r[1]]

    def logins(self) -> list[tuple[int, dict[str, str], httpx.Request]]:
        return [r for r in self.requests if "username" in r[1]]


class ScriptedQrz:
    """Responder answering every login and lookup with one scripted `Count`
    (None omits the element), so the Count rules are testable without the
    rolling tally FakeQrz keeps. `count` may be changed between steps."""

    def __init__(
        self, count: int | None, *, sub_exp: str | None = "non-subscriber", key: str = "KEY1"
    ) -> None:
        self.count = count
        self.sub_exp = sub_exp
        self.key = key

    def __call__(self, form: dict[str, str]) -> tuple[int, str, dict[str, str]]:
        callsign = None if "username" in form else {**DK5EN, "call": form["callsign"]}
        body = _session_xml(key=self.key, count=self.count, callsign=callsign)
        return 200, _with_sub_exp(body, self.sub_exp), {}


@contextmanager
def _captured_logs() -> Iterator[list[logging.LogRecord]]:
    """Every record the service (and httpx, which logs request URLs) emits."""
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler(level=logging.DEBUG)
    loggers = [service_logger, logging.getLogger("httpx")]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
    try:
        yield records
    finally:
        for lg, level in zip(loggers, previous, strict=True):
            lg.removeHandler(handler)
            lg.setLevel(level)


def _lines(records: list[logging.LogRecord], level: int | None = None) -> list[str]:
    return [r.getMessage() for r in records if level is None or r.levelno == level]


async def _seed_stations(env: Env, n: int) -> None:
    """`n` distinct, valid, never-looked-up callsigns, newest first."""
    for i in range(n):
        await env.add_heard(f"DL{i % 10}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}X", T0 - i)


class Env:
    def __init__(self, tmp: Path, storage: SQLiteStorage) -> None:
        self.tmp = tmp
        self.storage = storage
        self.clock = FakeClock()
        self.qrz = FakeQrz(self.clock)
        self.box = SecretBox(key_path=tmp / "secret.key", binding=b"board:TEST")
        self.service = self.new_service()

    def new_service(self) -> QrzLookupService:
        return QrzLookupService(
            self.storage, self.box, transport=self.qrz.transport, clock=self.clock
        )

    async def run_for(self, duration_ms: int, max_steps: int = 10_000) -> None:
        """Drive step() like run() would, advancing the fake clock by each
        returned delay, until `duration_ms` has elapsed."""
        end = self.clock.now + duration_ms
        for _ in range(max_steps):
            if self.clock.now >= end:
                return
            delay = await self.service.step()
            self.clock.now = min(end, self.clock.now + max(delay, 1))

    async def add_heard(self, callsign: str, ts: int, *, chatted: bool = False) -> None:
        if chatted:
            await self.storage._mutate(
                "INSERT INTO messages (src, dst, msg, type, timestamp)"
                " VALUES (?, '20', 'hi', 'msg', ?)",
                (callsign, ts),
            )
        else:
            await self.storage._mutate(
                "INSERT OR REPLACE INTO station_positions (callsign, last_seen) VALUES (?, ?)",
                (callsign, ts),
            )


async def _with_env(fn: Callable[[Env], Awaitable[None]]) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        storage = await create_sqlite_storage(tmp / "qrz_test.db")
        try:
            await fn(Env(tmp, storage))
        finally:
            await storage.close()


# ── S: secret_box ──────────────────────────────────────────────────────────


def _raises(fn: Callable[[], object]) -> bool:
    try:
        fn()
    except SecretBoxError:
        return True
    return False


def _test_secret_box(record: Record) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        key = Path(tmp_dir) / "state" / "secret.key"
        box = SecretBox(key_path=key, binding=b"board:AAA")
        token = box.encrypt(PASSWORD, "qrz.password:DK5EN")
        record(
            "S1. round trip; ciphertext carries no plaintext; key file is 0600",
            box.decrypt(token, "qrz.password:DK5EN") == PASSWORD
            and PASSWORD not in token
            and token.startswith("v1:")
            and (key.stat().st_mode & 0o777) == 0o600,
        )
        record(
            "S2. same key file on another board cannot decrypt",
            _raises(
                lambda: SecretBox(key_path=key, binding=b"board:BBB").decrypt(
                    token, "qrz.password:DK5EN"
                )
            ),
        )
        record(
            "S3. a ciphertext cannot be replayed under another username",
            _raises(lambda: box.decrypt(token, "qrz.password:OE5HWN")),
        )
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        record(
            "S4. a tampered ciphertext is rejected",
            _raises(lambda: box.decrypt(tampered, "qrz.password:DK5EN")),
        )
        key.unlink()
        record(
            "S5. a regenerated install key cannot decrypt the old secret",
            _raises(
                lambda: SecretBox(key_path=key, binding=b"board:AAA").decrypt(
                    token, "qrz.password:DK5EN"
                )
            ),
        )
        key.chmod(0o644)
        SecretBox(key_path=key, binding=b"board:AAA").encrypt("x", "y")
        record(
            "S6. a pre-existing wider key file is narrowed to 0600",
            (key.stat().st_mode & 0o777) == 0o600,
        )

        root = Path(tmp_dir)
        dt = root / "serial-number"
        cpu = root / "cpuinfo"
        mid = root / "machine-id"
        dt.write_bytes(b"00000000abcd1234\x00")
        cpu.write_text("processor\t: 0\nSerial\t\t: 00000000ffff0000\n")
        mid.write_text("feedface\n")
        record(
            "S7. board binding prefers the devicetree serial, then cpuinfo, then machine-id",
            board_binding((dt, cpu), mid) == b"board:00000000abcd1234"
            and board_binding((root / "missing", cpu), mid) == b"board:00000000ffff0000"
            and board_binding((root / "missing",), mid) == b"machine-id:feedface"
            and board_binding((root / "missing",), root / "missing2") == b"",
        )


# ── P: qrz_client ──────────────────────────────────────────────────────────


def _test_parser(record: Record) -> None:
    login = parse_response(_session_xml(count=0), is_login=True)
    record(
        "P1. login with key -> LOGGED_IN, Count and SubExp read",
        login.outcome is Outcome.LOGGED_IN
        and login.key == "KEY1"
        and login.count == 0
        and login.sub_exp == "non-subscriber",
    )
    bad = parse_response(_session_xml(key=None, error="Username/password incorrect"), is_login=True)
    record("P2. login error without key -> AUTH_FAILED", bad.outcome is Outcome.AUTH_FAILED)
    hit = parse_response(_session_xml(callsign=DK5EN), is_login=False)
    record(
        "P3. lookup with Callsign node -> FOUND with every field",
        hit.outcome is Outcome.FOUND
        and hit.record["fname"] == "Martin Stefan"
        and hit.record["addr2"] == "Freising",
    )
    nf = parse_response(_session_xml(error="Not found: g1srdd", ns=False), is_login=False)
    record(
        "P4. spec example 'Not found' WITH key -> NOT_FOUND (session kept)",
        nf.outcome is Outcome.NOT_FOUND and nf.key == "KEY1",
    )
    st = parse_response(_session_xml(key=None, error="Session Timeout", ns=False), is_login=False)
    record(
        "P5. spec example 'Session Timeout' without key -> SESSION_INVALID",
        st.outcome is Outcome.SESSION_INVALID,
    )
    record(
        "P6. 'Connection refused' -> REFUSED on login and on lookup",
        parse_response(_session_xml(key=None, error="Connection refused"), is_login=True).outcome
        is Outcome.REFUSED
        and parse_response(
            _session_xml(key=None, error="Connection refused"), is_login=False
        ).outcome
        is Outcome.REFUSED,
    )
    record(
        "P7. a limit/exceeded error -> RATE_LIMITED, not AUTH_FAILED",
        parse_response(
            _session_xml(key=None, error="Daily lookup limit exceeded"), is_login=True
        ).outcome
        is Outcome.RATE_LIMITED,
    )
    record(
        "P8. garbage and a Session-less document -> MALFORMED",
        parse_response("<html>oops", is_login=False).outcome is Outcome.MALFORMED
        and parse_response("<QRZDatabase/>", is_login=False).outcome is Outcome.MALFORMED,
    )
    record(
        "P9. first name and QTH normalisation (measured live values)",
        first_name("Martin Stefan") == "Martin"
        and first_name("Helmut") == "Helmut"
        and first_name("") is None
        and qth_from_addr2("A-4060 Leonding") == "Leonding"
        and qth_from_addr2("D-85354 Freising") == "Freising"
        and qth_from_addr2("85354 Freising") == "Freising"
        and qth_from_addr2("Leonding 4060") == "Leonding"
        and qth_from_addr2("Freising") == "Freising"
        and qth_from_addr2("Bad Tölz") == "Bad Tölz"
        and qth_from_addr2(None) is None,
    )
    record(
        "P10. candidate shape: real calls yes, aliases/groups/garbage no",
        all(is_lookup_candidate(c) for c in ("DK5EN", "OE5HWN", "W1AW", "9A1AA", "2E0ABC", "DL1A"))
        and not any(
            is_lookup_candidate(c) for c in ("WLNK", "20", "TEST", "", "DK5EN-98", "ABCDEFG1")
        ),
    )


# ── Q: service ─────────────────────────────────────────────────────────────


async def _test_unconfigured(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN-98", T0)
    await env.run_for(10 * 60_000)
    status = await env.service.status()
    record(
        "Q1. unconfigured: no request at all, state 'unconfigured'",
        not env.qrz.requests and status["state"] == "unconfigured" and not status["configured"],
    )


async def _test_spacing_and_encryption(env: Env, record: Record) -> None:
    for i, call in enumerate(("DK5EN-98", "OE5HWN-12", "DL1ABC", "DK3PB-1", "OE1KBC-12")):
        await env.add_heard(call, T0 - i * 1000)
    await env.service.set_credentials("dk5en", PASSWORD)
    state = await env.storage.get_qrz_state()
    status = await env.service.status()
    record(
        "Q2. password stored encrypted, username normalised, status never echoes it",
        state["username"] == "DK5EN"
        and PASSWORD not in (state["password_enc"] or "")
        and env.box.decrypt(state["password_enc"], "qrz.password:DK5EN") == PASSWORD
        and PASSWORD not in repr(status)
        and status["state"] == "verifying",
    )
    await env.run_for(10 * 60_000)
    times = [t for t, _, _ in env.qrz.requests]
    gaps = [b - a for a, b in pairwise(times)]
    record(
        "Q3. every request (login included) is >= 30 s after the previous one",
        len(times) == 6 and all(g >= MIN_INTERVAL_MS for g in gaps),
    )
    req = env.qrz.requests[0][2]
    record(
        "Q4. credentials and session key travel as a POST form body, never in the URL",
        all(r.method == "POST" and not r.url.query for _, _, r in env.qrz.requests)
        and b"password=" in req.content,
    )
    info = await env.storage.get_callsign_info_map()
    record(
        "Q5. found entries cached by base callsign with name and QTH",
        info.get("DK5EN") == {"first_name": "Martin", "qth": "Freising", "country": "Germany"}
        and set(info) == {"DK5EN", "OE5HWN", "DL1ABC", "DK3PB", "OE1KBC"},
    )
    record(
        "Q6. state is 'idle' once nothing is due", (await env.service.status())["state"] == "idle"
    )


async def _test_daily_cap(env: Env, record: Record) -> None:
    for i in range(200):
        await env.add_heard(f"DL{i % 10}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}X", T0 - i)
    # Count reporting off: our own cap must hold without QRZ's tally behind it.
    env.qrz.report_count = False
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(WINDOW_MS)
    first_day = env.qrz.lookups()
    status = await env.service.status()
    record(
        "Q7. hard cap: exactly 50 lookups in the first 24 h, then suspended",
        len(first_day) == DAILY_CAP
        and status["state"] == "suspended"
        and status["lookups_24h"] == DAILY_CAP,
    )
    fiftieth = first_day[-1][0]
    record(
        "Q8. the suspension runs 24 h from the 50th lookup",
        status["suspended_until"] == fiftieth + SUSPEND_MS,
    )
    # Restart: a fresh service on the same DB must still be suspended.
    env.service = env.new_service()
    env.clock.now = fiftieth + SUSPEND_MS - 60_000
    await env.run_for(50_000)
    record("Q9. suspension survives a restart", len(env.qrz.lookups()) == DAILY_CAP)
    await env.run_for(3 * WINDOW_MS)
    stamps = [t for t, _, _ in env.qrz.lookups()]
    worst = max(sum(1 for s in stamps if t <= s < t + WINDOW_MS) for t in stamps)
    record(
        "Q10. resumes after 24 h and no rolling 24 h window ever exceeds the cap",
        len(stamps) > DAILY_CAP and worst <= DAILY_CAP,
    )


async def _test_spacing_under_early_wakeups(env: Env, record: Record) -> None:
    """The 30 s gate must hold even when step() is called far too often —
    a wake event or a restart re-enters it without waiting the returned delay."""
    for i in range(20):
        await env.add_heard(f"DL{i % 10}{chr(65 + i)}BC", T0 - i)
    await env.service.set_credentials("DK5EN", PASSWORD)
    for _ in range(600):
        await env.service.step()
        env.clock.now += 1000
    times = [t for t, _, _ in env.qrz.requests]
    record(
        "Q3b. step() called every second still sends at most one request per 30 s",
        len(times) == 20 and all(b - a >= MIN_INTERVAL_MS for a, b in pairwise(times)),
    )


async def _test_cap_precheck(env: Env, record: Record) -> None:
    """The ledger alone enforces the cap: 50 lookups already in the window and
    no suspension on record (e.g. a DB restored without qrz_state) must still
    send nothing."""
    await env.add_heard("DK5EN", T0)
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(MIN_INTERVAL_MS)  # login only
    for i in range(DAILY_CAP):
        await env.storage.record_qrz_lookup(T0 - WINDOW_MS + 60_000 + i, f"DL{i}XX")
    await env.storage.update_qrz_state(suspended_until_ms=None)
    await env.run_for(10 * 60_000)
    status = await env.service.status()
    record(
        "Q30. 50 ledger rows in the window block lookups even without a stored suspension",
        not env.qrz.lookups() and status["state"] == "suspended",
    )


async def _test_aad_binds_username(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    await env.service.set_credentials("DK5EN", PASSWORD)
    # An attacker with DB write access swaps the username next to the blob.
    await env.storage.update_qrz_state(username="OE5HWN")
    await env.run_for(10 * 60_000)
    status = await env.service.status()
    record(
        "Q31. a ciphertext moved under another username does not decrypt",
        status["state"] == "credentials_unreadable" and not env.qrz.requests,
    )


async def _test_live_push(env: Env, record: Record) -> None:
    """Each hit is pushed once as a one-entry delta; misses push nothing; a
    broadcast that raises never turns a hit into a lookup failure."""
    await env.add_heard("DK5EN", T0)
    await env.add_heard("NO1BODY", T0 - 1)
    await env.add_heard("OE5HWN", T0 - 2)
    pushed: list[dict[str, dict[str, str | None]]] = []

    async def listener(info: dict[str, dict[str, str | None]]) -> None:
        pushed.append(info)
        if "OE5HWN" in info:
            raise RuntimeError("client queue full")

    env.service.set_info_listener(listener)
    env.qrz.responder = lambda form: (
        (200, _session_xml(), {})
        if "username" in form
        else (200, _session_xml(error="Not found: NO1BODY"), {})
        if form["callsign"] == "NO1BODY"
        else (200, _session_xml(callsign={**DK5EN, "call": form["callsign"]}), {})
    )
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(10 * 60_000)
    state = await env.storage.get_qrz_state()
    info = await env.storage.get_callsign_info_map()
    record(
        "Q32. each hit pushed once as a one-entry delta, a miss pushes nothing",
        pushed
        == [
            {"DK5EN": {"first_name": "Martin", "qth": "Freising", "country": "Germany"}},
            {"OE5HWN": {"first_name": "Martin", "qth": "Freising", "country": "Germany"}},
        ],
    )
    record(
        "Q33. a failing broadcast neither loses the entry nor triggers backoff",
        "OE5HWN" in info and state["backoff_until_ms"] is None,
    )


async def _test_connect_snapshot(env: Env, record: Record) -> None:
    """Every SSE connect carries the full map as `proxy:callsign_info`,
    `{}` included, so a client never waits for the next hit to learn names."""

    class _Router:
        def __init__(self, storage: SQLiteStorage) -> None:
            self.storage_handler = storage
            self.my_callsign = "DK5EN"
            self.filter_history_row = None

        def subscribe(self, _topic: str, _handler: Any) -> None:
            return

        def get_protocol(self, _name: str) -> Any:
            return None

    async def burst() -> str:
        manager = SSEManager(host="127.0.0.1", port=0, message_router=_Router(env.storage))
        return "".join([chunk async for chunk in manager.initial_events("c")])

    empty = await burst()
    await env.storage.upsert_callsign_info(
        "DK5EN", "found", T0, first_name="Martin", qth="Freising", country="Germany"
    )
    await env.storage.upsert_callsign_info("NO1BODY", "not_found", T0)
    full = await burst()

    def payload(text: str) -> Any:
        block = text.split("event: proxy:callsign_info", 1)[1]
        data_line = next(ln for ln in block.splitlines() if ln.startswith("data: "))
        return json.loads(data_line.removeprefix("data: "))

    record(
        "Q34. connect burst carries the snapshot: {} when empty, found entries only",
        payload(empty) == {}
        and payload(full)
        == {"DK5EN": {"first_name": "Martin", "qth": "Freising", "country": "Germany"}},
    )


async def _test_ledger_counts_failed_requests(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(MIN_INTERVAL_MS)  # login

    def explode(form: dict[str, str]) -> tuple[int, str, dict[str, str]]:
        raise httpx.ConnectError("no route", request=httpx.Request("POST", "https://x"))

    env.qrz.responder = explode
    await env.service.step()
    used = await env.storage.count_qrz_lookups_since(T0 - WINDOW_MS)
    rows = await env.storage._query("SELECT outcome FROM qrz_lookups")
    record(
        "Q11. a lookup whose request failed still counts against the cap",
        used == 1 and rows == [{"outcome": "transport_error"}],
    )


async def _test_backoff(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    await env.service.set_credentials("DK5EN", PASSWORD)
    env.qrz.responder = lambda form: (429, "", {})
    delays: list[int] = []
    for _ in range(12):
        await env.service.step()
        state = await env.storage.get_qrz_state()
        until = state["backoff_until_ms"]
        if until and until > env.clock.now:
            delays.append(until - env.clock.now)
            env.clock.now = until
        else:
            env.clock.now += MIN_INTERVAL_MS
    record(
        "Q12. HTTP 429 backs off exponentially from 60 s, capped at 6 h",
        delays[:4]
        == [BACKOFF_BASE_MS, 2 * BACKOFF_BASE_MS, 4 * BACKOFF_BASE_MS, 8 * BACKOFF_BASE_MS]
        and max(delays) == BACKOFF_MAX_MS
        and all(b >= a for a, b in pairwise(delays)),
    )
    env.qrz.responder = env.qrz.default
    await env.run_for(2 * MIN_INTERVAL_MS)
    state = await env.storage.get_qrz_state()
    record(
        "Q13. a successful response resets the backoff",
        state["backoff_level"] == 0 and state["backoff_until_ms"] is None,
    )

    env.qrz.responder = lambda form: (503, "", {"Retry-After": "7200"})
    await env.storage.update_qrz_state(last_request_ms=None)
    env.service._session_key = "KEY1"
    await env.storage._mutate("DELETE FROM callsign_info")
    await env.service.step()
    state = await env.storage.get_qrz_state()
    record(
        "Q14. Retry-After wins when it is longer than the backoff step",
        state["backoff_until_ms"] - env.clock.now == 7_200_000,
    )


async def _test_session_rules(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    await env.add_heard("OE5HWN", T0 - 1)
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(MIN_INTERVAL_MS)  # login
    env.qrz.responder = lambda form: (
        (200, _session_xml(key="KEY2"), {})
        if "username" in form
        else (200, _session_xml(key=None, error="Session Timeout"), {})
    )
    await env.run_for(2 * MIN_INTERVAL_MS)  # lookup -> session lost, then re-login
    record(
        "Q15. a lost session (no Key) triggers a re-login at the next slot",
        len(env.qrz.logins()) == 2
        and (await env.storage.get_qrz_state())["backoff_until_ms"] is None,
    )
    await env.run_for(MIN_INTERVAL_MS)  # second session loss in a row
    state = await env.storage.get_qrz_state()
    record(
        "Q16. a second session loss in a row backs off",
        (state["backoff_until_ms"] or 0) > env.clock.now,
    )


async def _test_refused(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    env.qrz.responder = lambda form: (200, _session_xml(key=None, error="Connection refused"), {})
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(REFUSAL_BACKOFF_MS - 60_000)
    status = await env.service.status()
    record(
        "Q17. 'Connection refused' at login backs off 1 h (not 24 h) after one attempt",
        len(env.qrz.requests) == 1
        and status["state"] == "backoff"
        and status["backoff_until"] == T0 + REFUSAL_BACKOFF_MS
        and status["suspended_until"] is None,
    )


async def _test_auth_failed(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    env.qrz.responder = lambda form: (
        200,
        _session_xml(key=None, error="Username/password incorrect"),
        {},
    )
    await env.service.set_credentials("DK5EN", "wrong")
    await env.run_for(3 * WINDOW_MS)
    status = await env.service.status()
    record(
        "Q18. a rejected login stops all traffic until new credentials",
        len(env.qrz.requests) == 1
        and status["state"] == "auth_failed"
        and status["last_error"] == "Username/password incorrect",
    )
    env.qrz.responder = env.qrz.default
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(2 * MIN_INTERVAL_MS)
    record("Q19. new credentials clear auth_failed and resume", len(env.qrz.lookups()) == 1)


async def _test_server_count(env: Env, record: Record) -> None:
    await _seed_stations(env, 20)
    env.qrz.count = DAILY_CAP + 10  # another program already used the account
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(10 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q20. Count above our cap suspends nothing: baseline spent, 31 left after 9 lookups",
        len(env.qrz.lookups()) == 9
        and status["state"] != "suspended"
        and status["server_count"] >= DAILY_CAP + 10
        and status["account_tier"] == "free"
        and status["server_budget_left"] == 100 - (DAILY_CAP + 10) - 9,
    )


SUBSCRIBER = "Thu Oct 21 15:45:16 2027"


async def _test_server_count_subscriber(env: Env, record: Record) -> None:
    """A subscriber's Count is not a 24 h tally: DM3KS's login reported 77678
    while QRZ's account page showed 1 XML lookup that day, and gating on it
    suspended every login, forever. QRZ does not limit a subscriber's XML
    lookups per day, so our own ledger cap does not apply either."""
    for i in range(DAILY_CAP + 10):
        await env.add_heard(f"DL{i % 10}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}X", T0 - i)
    env.qrz.sub_exp = SUBSCRIBER
    env.qrz.count = 77678
    await env.service.set_credentials("DM3KS", PASSWORD)
    await env.run_for(10 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q20b. a subscriber's Count above the cap does not suspend; lookups run",
        len(env.qrz.lookups()) == 9
        and status["state"] != "suspended"
        and status["server_count"] >= 77678
        and status["account_tier"] == "subscriber",
    )
    await env.run_for(WINDOW_MS)
    stamps = [t for t, _, _ in env.qrz.lookups()]
    status = await env.service.status()
    record(
        "Q20c. a subscriber has no daily cap: all 60 due stations looked up in 24 h",
        len([t for t in stamps if t < T0 + WINDOW_MS]) == DAILY_CAP + 10
        and status["state"] == "idle"
        and status["daily_cap"] is None,
    )


async def _test_login_reasserts_tier(env: Env, record: Record) -> None:
    """A stored "subscriber" from an earlier login must not lift the cap when
    the current login no longer reports a subscription."""
    for i in range(DAILY_CAP + 10):
        await env.add_heard(f"DL{i % 10}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}X", T0 - i)
    env.qrz.report_count = False
    await env.service.set_credentials("DM3KS", PASSWORD)
    await env.storage.update_qrz_state(subscription=SUBSCRIBER)
    env.qrz.sub_exp = None
    await env.run_for(WINDOW_MS)
    status = await env.service.status()
    record(
        "Q20j. a login without SubExp resets the tier: the 50/24 h cap applies again",
        len(env.qrz.lookups()) == DAILY_CAP
        and status["state"] == "suspended"
        and status["daily_cap"] == DAILY_CAP,
    )


async def _test_lift_cap_suspension(env: Env, record: Record) -> None:
    """A ledger-cap suspension written by v2.1.3 or earlier on a subscriber is
    lifted, by recorded reason and by the pre-v34 cap text alike."""
    env.qrz.sub_exp = SUBSCRIBER
    await _seed_suspension(
        env, reason=f"daily cap of {DAILY_CAP} lookups reached", subscription=SUBSCRIBER
    )
    for i in range(DAILY_CAP):
        await env.storage.record_qrz_lookup(T0 - 3_600_000 + i, f"DL{i % 10}XX")
    await env.run_for(3 * MIN_INTERVAL_MS)
    by_text = len(env.qrz.lookups()) == 1
    await env.storage.update_qrz_state(
        suspended_until_ms=T0 + SUSPEND_MS, suspend_reason="cap", last_error=None
    )
    await env.service.set_credentials("DM3KS", PASSWORD)
    env.qrz.requests.clear()
    await env.storage._mutate("DELETE FROM callsign_info", ())
    await env.run_for(3 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q20k. a ledger-cap suspension on a subscriber is lifted and lookups resume",
        by_text and len(env.qrz.lookups()) == 1 and status["state"] != "suspended",
    )


async def _test_server_count_unknown_tier(env: Env, record: Record) -> None:
    await _seed_stations(env, 20)
    env.qrz.sub_exp = None
    env.qrz.count = DAILY_CAP + 10
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(10 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q20d. no SubExp at all fails closed: the free-tier rules (cap, baseline) still apply",
        len(env.qrz.lookups()) == 9
        and status["state"] != "suspended"
        and status["daily_cap"] == DAILY_CAP
        and status["server_budget_left"] == 100 - (DAILY_CAP + 10) - 9,
    )


async def _seed_suspension(
    env: Env, *, reason: str | None, subscription: str, kind: str | None = None
) -> None:
    """A running suspension as an earlier step left it; `kind` None is a row
    written before `suspend_reason` existed (v2.1.1/v2.1.2)."""
    await env.add_heard("DK5EN", T0)
    await env.service.set_credentials("DM3KS", PASSWORD)
    await env.storage.update_qrz_state(
        suspended_until_ms=T0 + SUSPEND_MS - 3_600_000,
        suspend_reason=kind,
        last_error=reason,
        last_error_ms=T0 - 3_600_000 if reason else None,
        subscription=subscription,
    )


async def _test_lift_count_suspension(env: Env, record: Record) -> None:
    """The box already carrying a Count suspension from the old gate recovers
    on the update instead of waiting it out."""
    env.qrz.sub_exp = SUBSCRIBER
    env.qrz.count = 77678
    await _seed_suspension(env, reason="QRZ reports 77678 lookups in 24 h", subscription=SUBSCRIBER)
    await env.run_for(3 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q20e. a stored Count suspension on a subscriber is lifted and lookups resume",
        len(env.qrz.lookups()) == 1
        and status["state"] != "suspended"
        and status["last_error"] is None,
    )


async def _test_lift_after_replaced_credentials(env: Env, record: Record) -> None:
    """Replacing the password clears `last_error` but not the suspension; the
    lift must not depend on the text that just vanished."""
    env.qrz.sub_exp = SUBSCRIBER
    env.qrz.count = 77678
    await _seed_suspension(
        env,
        reason="QRZ reports 77678 lookups in 24 h",
        subscription=SUBSCRIBER,
        kind="server_count",
    )
    await env.service.set_credentials("DM3KS", PASSWORD)
    await env.run_for(3 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q20g. a Count suspension is lifted on a subscriber after the password was replaced",
        len(env.qrz.lookups()) == 1 and status["state"] != "suspended",
    )


async def _test_lift_legacy_cleared_reason(env: Env, record: Record) -> None:
    """DM3KS after updating to v2.1.2: no `suspend_reason` (older row), and
    `last_error` already cleared by a password replace. Only our own ledger
    could have caused it, and the ledger is empty."""
    env.qrz.sub_exp = SUBSCRIBER
    env.qrz.count = 77678
    await _seed_suspension(env, reason=None, subscription=SUBSCRIBER)
    await env.run_for(3 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q20h. a pre-v34 suspension with its reason cleared is lifted on a subscriber",
        len(env.qrz.lookups()) == 1 and status["state"] != "suspended",
    )


async def _test_suspend_records_reason(env: Env, record: Record) -> None:
    reasons: list[str | None] = []
    env.qrz.count = DAILY_CAP + 10
    await env.add_heard("DK5EN", T0)
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(MIN_INTERVAL_MS)
    reasons.append((await env.storage.get_qrz_state())["suspend_reason"])  # Count: none
    for i in range(DAILY_CAP):
        await env.storage.record_qrz_lookup(env.clock.now - 60_000 + i, f"DL{i}XX")
    await env.run_for(2 * MIN_INTERVAL_MS)
    reasons.append((await env.storage.get_qrz_state())["suspend_reason"])  # ledger cap
    await env.storage._mutate("DELETE FROM qrz_lookups")
    await env.storage.update_qrz_state(suspended_until_ms=None, suspend_reason=None)
    env.qrz.responder = lambda form: (200, _session_xml(key=None, error="Connection refused"), {})
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(2 * MIN_INTERVAL_MS)
    state = await env.storage.get_qrz_state()
    reasons.append(state["suspend_reason"])  # refusal: a back-off, not a suspension
    record(
        "Q20i. only the ledger cap records a suspension reason; Count and refusal do not suspend",
        reasons == [None, "cap", None]
        and state["suspended_until_ms"] is None
        and (state["backoff_until_ms"] or 0) > env.clock.now,
    )


async def _test_keep_other_suspensions(env: Env, record: Record) -> None:
    """The lift is narrow: a ledger-cap suspension on a free account and a
    refusal on a subscriber stay. (A Count suspension used to be in this list;
    it is lifted on every tier since the Count gate was retired, see Q43.)"""
    await _seed_suspension(
        env, reason=f"daily cap of {DAILY_CAP} lookups reached", subscription="non-subscriber"
    )
    await env.run_for(3 * MIN_INTERVAL_MS)
    ledger_kept = not env.qrz.requests
    # Reason recorded on a subscriber, then the password replaced: refusal stays.
    await env.storage.update_qrz_state(
        suspend_reason="refused", last_error=None, subscription=SUBSCRIBER
    )
    await env.service.set_credentials("DM3KS", PASSWORD)
    await env.run_for(3 * MIN_INTERVAL_MS)
    refused_kept = not env.qrz.requests
    # Pre-v34 row on a subscriber whose text names a refusal.
    await env.storage.update_qrz_state(suspend_reason=None, last_error="Connection refused")
    await env.run_for(3 * MIN_INTERVAL_MS)
    state = await env.storage.get_qrz_state()
    legacy_kept = (
        not env.qrz.requests and state["suspended_until_ms"] == T0 + SUSPEND_MS - 3_600_000
    )
    record(
        "Q20f. a free-tier cap suspension and refusal suspensions are never lifted",
        ledger_kept and refused_kept and legacy_kept,
    )


# ── Q40+: QRZ's Count is a login-time observation, never a suspension ─────
#
# 2026-10-05 09:42: the service woke from its own 24 h cap suspension, logged
# in, QRZ reported Count 50 against a ledger of 0/50, and the old gate
# suspended it for another 24 h. Count is read only at login and nothing looks
# up while suspended, so a Count that does not decay wedged it for good.


async def _test_count_at_cap_does_not_suspend(env: Env, record: Record) -> None:
    await _seed_stations(env, 60)
    env.qrz.responder = ScriptedQrz(50)
    with _captured_logs() as logs:
        await env.service.set_credentials("DK5EN", PASSWORD)
        await env.run_for(MIN_INTERVAL_MS)  # login only
        at_login = await env.service.status()
        await env.run_for(10 * MIN_INTERVAL_MS)
    status = await env.service.status()
    state = await env.storage.get_qrz_state()
    record(
        "Q40a. Count 50 on a free account: login line says budget_today=50, status pins "
        "server_count_ignored False and server_budget_left 50",
        at_login["server_count"] == 50
        and at_login["server_count_ignored"] is False
        and at_login["server_budget_left"] == 50
        # Before any later step: the stale-suspension lift must not be what hides a gate.
        and at_login["state"] != "suspended"
        and at_login["suspended_until"] is None
        and any(
            ln
            == "QRZ login ok sub=non-subscriber count=50 plausible=yes ledger=0/50 budget_today=50"
            for ln in _lines(logs, logging.INFO)
        ),
    )
    record(
        "Q40b. Count 50 on a free account: no suspension, lookups proceed",
        len(env.qrz.lookups()) == 10
        and status["state"] != "suspended"
        and state["suspended_until_ms"] is None
        and state["suspend_reason"] is None
        and status["server_budget_left"] == 50 - 10
        and not any("suspended" in ln for ln in _lines(logs, logging.WARNING)),
    )


async def _test_implausible_count_ignored(env: Env, record: Record) -> None:
    await _seed_stations(env, 60)
    script = ScriptedQrz(77678)
    env.qrz.responder = script
    with _captured_logs() as logs:
        await env.service.set_credentials("DK5EN", PASSWORD)
        await env.run_for(MIN_INTERVAL_MS)
        at_login = await env.service.status()
        await env.run_for(10 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q41a. Count 77678 on a free account: ignored, no suspension, budget is the ledger's 50",
        status["server_count"] == 77678
        and status["server_count_ignored"] is True
        and status["server_budget_left"] is None
        and status["state"] != "suspended"
        and len(env.qrz.lookups()) == 10
        and any(
            "count=77678 plausible=no ledger=0/50 budget_today=50" in ln
            for ln in _lines(logs, logging.INFO)
        )
        and at_login["server_budget_left"] is None
        and at_login["suspended_until"] is None
        and not any("suspended" in ln for ln in _lines(logs, logging.WARNING)),
    )
    # The boundary: 99 is a tally with one lookup left, 100 is not a tally.
    results: dict[int, tuple[object, object]] = {}
    for count in (99, 100):
        script.count = count
        env.service._session_key = None  # a lost session: the next step logs in again
        env.clock.now += MIN_INTERVAL_MS
        await env.service.step()
        st = await env.service.status()
        results[count] = (st["server_count_ignored"], st["server_budget_left"])
    record(
        "Q41b. plausibility boundary: Count 99 is used (1 left), Count 100 is ignored",
        SERVER_COUNT_PLAUSIBLE_MAX == 100
        and results[99] == (False, 1)
        and results[100] == (True, None),
    )


async def _test_server_budget_backoff(env: Env, record: Record) -> None:
    await _seed_stations(env, 60)
    script = ScriptedQrz(70)
    env.qrz.responder = script
    with _captured_logs() as logs:
        await env.service.set_credentials("DK5EN", PASSWORD)
        await env.run_for(35 * MIN_INTERVAL_MS)
    state = await env.storage.get_qrz_state()
    status = await env.service.status()
    stopped_at = T0 + 31 * MIN_INTERVAL_MS
    record(
        "Q42a. Count 70: exactly 30 lookups, the 31st does not happen",
        len(env.qrz.lookups()) == 30 and len(env.qrz.logins()) == 1,
    )
    record(
        "Q42b. the budget stop is a 1 h back-off with the budget reason, not a 24 h suspension",
        state["backoff_until_ms"] == stopped_at + REFUSAL_BACKOFF_MS
        and state["suspended_until_ms"] is None
        and state["last_error"] == "QRZ budget used up (QRZ reports 70)"
        and status["state"] == "backoff"
        and status["server_budget_left"] == 0
        and env.service._session_key is None
        and any("QRZ server budget used up" in ln for ln in _lines(logs, logging.WARNING)),
    )
    # After the back-off the service logs in again and Count is re-read.
    script.count = 90
    env.clock.now = (state["backoff_until_ms"] or 0) - 1
    await env.service.step()
    record(
        "Q42c. nothing is sent before the back-off ends",
        len(env.qrz.logins()) == 1 and len(env.qrz.lookups()) == 30,
    )
    env.clock.now = state["backoff_until_ms"] or env.clock.now
    with _captured_logs() as logs2:
        await env.run_for(14 * MIN_INTERVAL_MS)
    record(
        "Q42d. after 1 h it logs in again, takes the NEW Count as baseline (90 -> 10 left, "
        "ledger 20) and stops again after exactly 10 more lookups",
        len(env.qrz.logins()) == 2
        and len(env.qrz.lookups()) == 40
        and any("count=90 plausible=yes ledger=30/50 budget_today=10" in ln for ln in _lines(logs2))
        and (await env.storage.get_qrz_state())["last_error"]
        == "QRZ budget used up (QRZ reports 90)",
    )


async def _test_stale_count_suspension_lifted(env: Env, record: Record) -> None:
    """The live box on 2026-10-05: free account, Count 50, ledger EMPTY, a
    server_count suspension with ~11 h to run."""
    await _seed_stations(env, 20)
    env.qrz.responder = ScriptedQrz(50)
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.storage.update_qrz_state(
        subscription="non-subscriber",
        server_count=50,
        suspend_reason="server_count",
        suspended_until_ms=T0 + 11 * 3_600_000,
        last_error="QRZ reports 50 lookups in 24 h",
        last_error_ms=T0 - 13 * 3_600_000,
    )
    with _captured_logs() as logs:
        await env.run_for(3 * MIN_INTERVAL_MS)
    state = await env.storage.get_qrz_state()
    first = len(env.qrz.lookups())
    record(
        "Q43a. the live row (free, server_count suspension, empty ledger) is lifted at the "
        "next step and lookups resume",
        first >= 1
        and state["suspended_until_ms"] is None
        and state["suspend_reason"] is None
        and state["last_error"] is None
        and state["last_error_ms"] is None
        and any("stale Count suspension lifted" in ln for ln in _lines(logs, logging.INFO)),
    )
    # Same row as written before `suspend_reason` existed: recognised by its text.
    await env.storage.update_qrz_state(
        suspend_reason=None,
        suspended_until_ms=env.clock.now + 11 * 3_600_000,
        last_error="QRZ reports 50 lookups in 24 h",
        last_error_ms=env.clock.now,
    )
    await env.run_for(3 * MIN_INTERVAL_MS)
    state = await env.storage.get_qrz_state()
    record(
        "Q43b. the same suspension without suspend_reason is lifted by its error text",
        len(env.qrz.lookups()) > first
        and state["suspended_until_ms"] is None
        and state["last_error"] is None,
    )


def _ledger_cap_case(count: int | None, label: str) -> Callable[[Env, Record], Awaitable[None]]:
    async def case(env: Env, record: Record) -> None:
        await _seed_stations(env, 200)
        env.qrz.responder = ScriptedQrz(count)
        with _captured_logs() as logs:
            await env.service.set_credentials("DK5EN", PASSWORD)
            await env.run_for(WINDOW_MS)
        first_day = env.qrz.lookups()
        state = await env.storage.get_qrz_state()
        status = await env.service.status()
        record(
            f"{label}. Count {count}: the ledger cap alone still stops at 50 per 24 h and "
            "suspends 24 h from the 50th lookup",
            len(first_day) == DAILY_CAP
            and status["state"] == "suspended"
            and state["suspend_reason"] == "cap"
            and state["suspended_until_ms"] == first_day[-1][0] + SUSPEND_MS
            and state["backoff_until_ms"] is None
            and any(
                f"count={'none' if count is None else count} plausible="
                f"{'no' if count is None else 'yes'} ledger=0/50" in ln
                for ln in _lines(logs, logging.INFO)
            ),
        )

    return case


def _refusal_case(
    error: str, label: str, *, at_login: bool
) -> Callable[[Env, Record], Awaitable[None]]:
    async def case(env: Env, record: Record) -> None:
        await env.add_heard("DK5EN", T0)
        script = ScriptedQrz(7, key="SESSIONKEY-4F9A7C")
        refuse: tuple[int, str, dict[str, str]] = (
            200,
            _session_xml(key=None, count=7, error=error),
            {},
        )

        def respond(form: dict[str, str]) -> tuple[int, str, dict[str, str]]:
            if "username" in form and not at_login:
                return script(form)
            return refuse

        env.qrz.responder = respond
        with _captured_logs() as logs:
            await env.service.set_credentials("DK5EN", PASSWORD)
            await env.service.step()  # login (refused, or ok)
            if not at_login:
                env.clock.now += MIN_INTERVAL_MS
                await env.service.step()  # the lookup, refused
            first_at = env.clock.now
            state = await env.storage.get_qrz_state()
            sent = len(env.qrz.requests)
            first_ok = (
                state["backoff_until_ms"] == first_at + REFUSAL_BACKOFF_MS
                and state["backoff_level"] == 0
                and state["suspended_until_ms"] is None
                and state["suspend_reason"] is None
                and state["last_error"] == error
                and env.service._session_key is None
            )
            env.clock.now = first_at + REFUSAL_BACKOFF_MS - 1
            await env.service.step()
            quiet = len(env.qrz.requests) == sent
            env.clock.now = first_at + REFUSAL_BACKOFF_MS
            await env.service.step()  # exactly 1 h later: through login again
            relogin = len(env.qrz.logins()) == 2
            if not at_login:
                env.clock.now += MIN_INTERVAL_MS
                await env.service.step()  # refused again
            second_at = env.clock.now
            state2 = await env.storage.get_qrz_state()
        stage = "login" if at_login else "lookup"
        warning = next((ln for ln in _lines(logs, logging.WARNING) if error in ln), "")
        record(
            f"{label}. {error!r} at {stage}: fixed 1 h back-off (level 0, no suspension), "
            "session dropped, silence until it ends, then a login",
            first_ok and quiet and relogin,
        )
        record(
            f"{label}/2. the second refusal backs off exactly 1 h again (fixed, not exponential)",
            state2["backoff_until_ms"] == second_at + REFUSAL_BACKOFF_MS
            and state2["backoff_level"] == 0,
        )
        record(
            f"{label}/3. the WARNING carries the raw error, http status, Count, ledger and tier",
            f"error='{error}'" in warning
            and "http=200" in warning
            and "count=7" in warning
            and f"ledger={0 if at_login else 1}/50" in warning
            and "tier=free" in warning
            and all("SESSIONKEY" not in ln and PASSWORD not in ln for ln in _lines(logs)),
        )

    return case


async def _test_subscriber_ignores_count(env: Env, record: Record) -> None:
    """A Count of 99 would leave a free account one lookup; a subscriber has no
    cap and no Count gate at all, so every due station is looked up."""
    await _seed_stations(env, 120)
    env.qrz.responder = ScriptedQrz(99, sub_exp=SUBSCRIBER)
    with _captured_logs() as logs:
        await env.service.set_credentials("DM3KS", PASSWORD)
        await env.run_for(WINDOW_MS)
    status = await env.service.status()
    state = await env.storage.get_qrz_state()
    record(
        "Q46. a subscriber with Count 99: no cap, no back-off, no budget, all 120 looked up",
        len(env.qrz.lookups()) == 120
        and status["server_budget_left"] is None
        and status["daily_cap"] is None
        and state["backoff_until_ms"] is None
        and state["suspended_until_ms"] is None
        and any(
            f"sub={SUBSCRIBER} count=99 plausible=yes ledger=0/none budget_today=none" in ln
            for ln in _lines(logs, logging.INFO)
        ),
    )


async def _test_baseline_math(env: Env, record: Record) -> None:
    await _seed_stations(env, 40)
    script = ScriptedQrz(40)
    env.qrz.responder = script
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(11 * MIN_INTERVAL_MS)  # login + 10 lookups
    status = await env.service.status()
    record(
        "Q47a. baseline 40 + 10 lookups since login leaves 50",
        len(env.qrz.lookups()) == 10 and status["server_budget_left"] == 50,
    )
    # A lost session: the next step logs in again and Count is re-read.
    script.count = 80
    env.service._session_key = None
    with _captured_logs() as logs:
        await env.run_for(MIN_INTERVAL_MS)
    status = await env.service.status()
    after_login = status["server_budget_left"]
    await env.run_for(5 * MIN_INTERVAL_MS)
    status = await env.service.status()
    record(
        "Q47b. a second login refreshes the baseline and restarts the since-login count "
        "(80 -> 20 left, then 15 after 5 lookups; ledger 10/50 is not the server's tally)",
        len(env.qrz.logins()) == 2
        and after_login == 20
        and status["server_budget_left"] == 15
        and any("count=80 plausible=yes ledger=10/50 budget_today=20" in ln for ln in _lines(logs)),
    )


async def _test_session_refresh(env: Env, record: Record) -> None:
    """The baseline is a login-time Count but the ledger rows added to it never
    age out; a session older than 24 h logs in again instead."""
    await _seed_stations(env, 20)
    script = ScriptedQrz(40)
    env.qrz.responder = script
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(3 * MIN_INTERVAL_MS)  # login + 2 lookups
    first_login = T0
    # (3) younger than 24 h: the session is kept, and the 30 s gate stays in force.
    env.clock.now = first_login + WINDOW_MS - 1
    await env.service.step()
    young_ok = len(env.qrz.logins()) == 1 and len(env.qrz.lookups()) == 3
    env.clock.now = first_login + WINDOW_MS
    before = len(env.qrz.requests)
    await env.service.step()  # 24 h old, but 1 ms after the last request
    gate_ok = len(env.qrz.requests) == before
    record(
        "Q49a. a session younger than 24 h is not renewed, and the 30 s gate holds at 24 h",
        young_ok and gate_ok,
    )
    # (1) older than 24 h: log in again; the new Count is the baseline.
    script.count = 80
    env.clock.now += MIN_INTERVAL_MS
    with _captured_logs() as logs:
        await env.service.step()
    status = await env.service.status()
    record(
        "Q49b. a session older than 24 h logs in again (INFO line) and uses the new baseline "
        "(80 -> 20 left, earlier lookups are outside the ledger window)",
        len(env.qrz.logins()) == 2
        and status["server_budget_left"] == 20
        and any(
            "QRZ session older than 24 h, logging in again to refresh the baseline" in ln
            for ln in _lines(logs, logging.INFO)
        ),
    )


async def _test_session_refresh_two_days(env: Env, record: Record) -> None:
    """Without the refresh, baseline 0 + 100 lookups since the first login ends
    the third day in a spurious 'budget used up' pause."""
    await _seed_stations(env, 200)
    env.qrz.responder = ScriptedQrz(0)
    with _captured_logs() as logs:
        await env.service.set_credentials("DK5EN", PASSWORD)
        await env.run_for(3 * WINDOW_MS)
    state = await env.storage.get_qrz_state()
    record(
        "Q49c. three days at the 50/day cap with Count 0 never hit 'budget used up'",
        len(env.qrz.lookups()) > 100
        and len(env.qrz.logins()) >= 3
        and not any("budget used up" in ln for ln in _lines(logs, logging.WARNING))
        and "budget used up" not in str(state["last_error"]),
    )


async def _test_session_refresh_subscriber(env: Env, record: Record) -> None:
    """A subscriber has no baseline: its session is kept, as before."""
    await _seed_stations(env, 20)
    env.qrz.responder = ScriptedQrz(5, sub_exp=SUBSCRIBER)
    await env.service.set_credentials("DM3KS", PASSWORD)
    await env.run_for(2 * MIN_INTERVAL_MS)
    env.clock.now = T0 + WINDOW_MS + 1
    with _captured_logs() as logs:
        await env.service.step()
    record(
        "Q49d. a subscriber's session is not renewed after 24 h (nothing to refresh)",
        len(env.qrz.logins()) == 1
        and len(env.qrz.lookups()) == 2
        and not any("older than 24 h" in ln for ln in _lines(logs)),
    )


async def _test_log_lines(env: Env, record: Record) -> None:
    key = "SESSIONKEY-4F9A7C"
    await _seed_stations(env, 5)
    script = ScriptedQrz(40, key=key)
    env.qrz.responder = script
    with _captured_logs() as logs:
        await env.service.set_credentials("DK5EN", PASSWORD)
        await env.run_for(2 * MIN_INTERVAL_MS)  # login + one lookup
        callsign = env.qrz.lookups()[0][1]["callsign"]
        env.qrz.responder = lambda form: (
            200,
            _session_xml(key=None, count=41, error="Connection refused"),
            {},
        )
        await env.run_for(MIN_INTERVAL_MS)  # refused lookup
    info = _lines(logs, logging.INFO)
    warnings = _lines(logs, logging.WARNING)
    record(
        "Q48a. login INFO line carries sub, count, plausible, ledger and budget_today",
        "QRZ login ok sub=non-subscriber count=40 plausible=yes ledger=0/50 budget_today=50"
        in info,
    )
    record(
        "Q48b. every lookup answers with an INFO line: call, outcome, count, ledger used/cap",
        f"QRZ lookup {callsign} -> found count=40 ledger=1/50" in info
        and any(re.fullmatch(r"QRZ lookup \w+ -> refused count=41 ledger=2/50", ln) for ln in info),
    )
    record(
        "Q48c. the refusal WARNING carries error, http, count, ledger and tier",
        any(
            re.fullmatch(
                r"QRZ refused: error='Connection refused' http=200 count=41 ledger=2/50 "
                r"tier=free; backing off 3600 s",
                ln,
            )
            for ln in warnings
        ),
    )
    everything = " | ".join(_lines(logs)) + " | ".join(str(r.args) + str(r.exc_text) for r in logs)
    record(
        "Q48d. neither the password nor the session key appears in any captured log",
        PASSWORD not in everything and key not in everything and len(logs) > 4,
    )


async def _test_refresh(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    await env.add_heard("NO1BODY", T0 - 1)
    env.qrz.responder = lambda form: (
        (200, _session_xml(), {})
        if "username" in form
        else (200, _session_xml(callsign=DK5EN), {})
        if form["callsign"] == "DK5EN"
        else (200, _session_xml(error=f"Not found: {form['callsign']}"), {})
    )
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(10 * 60_000)
    index = await env.storage.get_callsign_info_index()
    record(
        "Q21. 'Not found' is cached as not_found, each call looked up once",
        index.get("NO1BODY", ("", 0))[0] == "not_found" and len(env.qrz.lookups()) == 2,
    )
    # Keep both stations "recently heard" as time advances.
    for days in (REFRESH_NOT_FOUND_MS, REFRESH_FOUND_MS):
        env.clock.now = T0 + days + 60_000
        await env.add_heard("DK5EN", env.clock.now)
        await env.add_heard("NO1BODY", env.clock.now)
        await env.run_for(10 * 60_000)
    looked_up = [f["callsign"] for _, f, _ in env.qrz.lookups()]
    # Day 7: only the not_found entry is due. Day 90: the found entry is due,
    # and so is the not_found one again (its day-7 check is older than 7 days).
    record(
        "Q22. not_found refreshed after 7 days, found only after 90 days",
        looked_up == ["DK5EN", "NO1BODY", "NO1BODY", "DK5EN", "NO1BODY"],
    )
    env.qrz.responder = lambda form: (
        (200, _session_xml(), {})
        if "username" in form
        else (200, _session_xml(error="Not found: DK5EN"), {})
    )
    env.clock.now += REFRESH_FOUND_MS + 60_000
    await env.add_heard("DK5EN", env.clock.now)
    await env.run_for(10 * 60_000)
    info = await env.storage.get_callsign_info_map()
    record(
        "Q23. a refresh answered 'Not found' keeps the name already on file",
        info.get("DK5EN", {}).get("first_name") == "Martin",
    )


async def _test_candidates(env: Env, record: Record) -> None:
    await env.add_heard("OE5HWN-12", T0)  # heard only, newest
    await env.add_heard("DK5EN-98", T0 - 5000, chatted=True)  # chatted, older
    await env.add_heard("DK5EN-12", T0 - 6000)  # same base, other SSID
    await env.add_heard("WLNK-1", T0 - 100, chatted=True)  # alias
    await env.add_heard("XX0XXX-00", T0 - 200)  # placeholder
    await env.add_heard("OLD1AB", T0 - 31 * 24 * 3_600_000)  # outside 30 days
    await env.service.set_credentials("DK5EN", PASSWORD)
    await env.run_for(10 * 60_000)
    looked_up = [f["callsign"] for _, f, _ in env.qrz.lookups()]
    record(
        "Q24. chat partners first, bases deduplicated, aliases/placeholders/stale skipped",
        looked_up == ["DK5EN", "OE5HWN"],
    )


async def _test_inflight_race(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    old_responder = env.qrz.responder

    def reject_then_switch(form: dict[str, str]) -> tuple[int, str, dict[str, str]]:
        # The operator saves new credentials while the old login is in flight.
        env.service._cred_gen += 1
        env.qrz.responder = old_responder
        return 200, _session_xml(key=None, error="Username/password incorrect"), {}

    env.qrz.responder = reject_then_switch
    await env.service.set_credentials("DK5EN", "old")
    await env.service.step()
    state = await env.storage.get_qrz_state()
    record("Q25. a verdict for superseded credentials is discarded", state["auth_failed"] == 0)


async def _test_unreadable(env: Env, record: Record) -> None:
    await env.add_heard("DK5EN", T0)
    await env.service.set_credentials("DK5EN", PASSWORD)
    env.box = SecretBox(key_path=env.tmp / "secret.key", binding=b"board:OTHER")
    env.service = env.new_service()
    await env.run_for(10 * 60_000)
    status = await env.service.status()
    record(
        "Q26. SD card on another board: state credentials_unreadable, no request",
        status["state"] == "credentials_unreadable" and not env.qrz.requests,
    )


async def _test_routes(env: Env, record: Record) -> None:
    class _Manager:
        def __init__(self, svc: QrzLookupService | None, storage: SQLiteStorage) -> None:
            self.qrz_service = svc
            self._storage = storage

        def require_storage(self) -> SQLiteStorage:
            return self._storage

    app = FastAPI()
    app.include_router(build_qrz_router(_Manager(env.service, env.storage)))  # type: ignore[arg-type]  # duck-typed manager stub
    bodies: list[str] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        put = await client.put(
            "/api/qrz/credentials", json={"username": "dk5en", "password": PASSWORD}
        )
        bodies.append(put.text)
        bodies.append((await client.get("/api/qrz/status")).text)
        off = await client.put("/api/qrz/enabled", json={"enabled": False})
        bodies.append(off.text)
        bad = await client.put("/api/qrz/credentials", json={"username": "DK5EN", "password": ""})
        deleted = await client.delete("/api/qrz/credentials")
        bodies.append(deleted.text)
    record(
        "Q27. routes: PUT/enable/DELETE work and no response ever contains the password",
        put.status_code == 200
        and put.json()["username"] == "DK5EN"
        and off.json()["state"] == "disabled"
        and bad.status_code == 422
        and deleted.json()["state"] == "unconfigured"
        and all(PASSWORD not in b for b in bodies),
    )
    app503 = FastAPI()
    app503.include_router(build_qrz_router(_Manager(None, env.storage)))  # type: ignore[arg-type]  # duck-typed manager stub
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app503), base_url="http://t"
    ) as client:
        record(
            "Q28. no service wired -> 503", (await client.get("/api/qrz/status")).status_code == 503
        )


async def _test_run_loop_wakes(env: Env, record: Record) -> None:
    """run() must react to new credentials without waiting out its idle poll."""
    await env.add_heard("DK5EN", T0)
    stop = asyncio.Event()
    task = asyncio.create_task(env.service.run(stop))
    await asyncio.sleep(0.05)
    await env.service.set_credentials("DK5EN", PASSWORD)
    for _ in range(100):
        if env.qrz.logins():
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    record(
        "Q29. run() wakes on new credentials and stops on stop_event", len(env.qrz.logins()) == 1
    )


async def run_qrz_tests() -> bool:
    if has_console:
        print("\n🧪 Testing QRZ callsign lookup:")
        print("=" * 55)
    results: list[tuple[str, bool]] = []

    def record(label: str, ok: bool) -> None:
        results.append((label, ok))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    _test_secret_box(record)
    _test_parser(record)
    for case in (
        _test_unconfigured,
        _test_spacing_and_encryption,
        _test_spacing_under_early_wakeups,
        _test_cap_precheck,
        _test_aad_binds_username,
        _test_daily_cap,
        _test_ledger_counts_failed_requests,
        _test_backoff,
        _test_session_rules,
        _test_refused,
        _test_auth_failed,
        _test_server_count,
        _test_server_count_subscriber,
        _test_server_count_unknown_tier,
        _test_count_at_cap_does_not_suspend,
        _test_implausible_count_ignored,
        _test_server_budget_backoff,
        _test_stale_count_suspension_lifted,
        _ledger_cap_case(0, "Q44a"),
        _ledger_cap_case(None, "Q44b"),
        _refusal_case("Connection refused", "Q45a", at_login=True),
        _refusal_case("Connection refused", "Q45b", at_login=False),
        _refusal_case("Daily lookup limit exceeded", "Q45c", at_login=True),
        _refusal_case("Daily lookup limit exceeded", "Q45d", at_login=False),
        _test_subscriber_ignores_count,
        _test_baseline_math,
        _test_session_refresh,
        _test_session_refresh_two_days,
        _test_session_refresh_subscriber,
        _test_log_lines,
        _test_login_reasserts_tier,
        _test_lift_cap_suspension,
        _test_lift_count_suspension,
        _test_lift_after_replaced_credentials,
        _test_lift_legacy_cleared_reason,
        _test_suspend_records_reason,
        _test_keep_other_suspensions,
        _test_refresh,
        _test_candidates,
        _test_inflight_race,
        _test_unreadable,
        _test_routes,
        _test_run_loop_wakes,
        _test_live_push,
        _test_connect_snapshot,
    ):
        await _with_env(lambda env, case=case: case(env, record))  # type: ignore[misc]  # default-arg binding of the loop variable

    passed = sum(1 for _, ok in results if ok)
    if has_console or passed != len(results):
        for label, ok in results:
            if not ok:
                print(f"❌ FAIL | {label}")
        print(f"\n🧪 QRZ Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(run_qrz_tests()) else 1)
