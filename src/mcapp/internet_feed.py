"""McApp internet feed service: group/broadcast messages from meshmap.

Registers this box with meshmap (TOFU token, stored encrypted), subscribes to
`GET /api/v2/box/feed` over SSE and republishes every mapped message on the
`www_message` topic. Inert while the operator has not enabled it. Contract:
mcmap `docs/proposals/data-mcapp-box-feed.md`; design:
`doc/2026-10-11_1005-internet-feed-concept.md` §4.2-§4.4.

Wave A: positions are not requested (`pos=off`) and `positions` / `removed` in
a batch are ignored.

The token is a credential: it is never logged, never part of `status()` or
`get_settings()`, and never in an error text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from .internet_feed_types import STATUS_EVENT, WWW_MESSAGE_TOPIC
from .secret_box import SecretBoxError
from .util import now_ms

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://meshmap.oevsv.at"
BASE_URL_ENV = "MCAPP_INTERNET_FEED_URL"
SUPPORTED_FEED_MAJOR = 1

BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0
BACKOFF_JITTER = 0.2
READ_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 15.0

# One SecretBox blob holds every identity's token (the keyring); fixed associated data.
TOKENS_AD = "internet_feed.tokens"
KEYRING_MAX = 4
# BLE registers arrive some seconds after start; registering as `pi` first would cost a
# token-less re-register (a `reclaim` at meshmap) for the BLE identity right after.
STARTUP_HOLDOFF_S = 30.0
STARTUP_HOLDOFF_POLL_S = 1.0
PUBLISH_DRAIN_S = 10.0
WLAN_MAC_PATH = Path("/sys/class/net/wlan0/address")
MODEL_PATH = Path("/proc/device-tree/model")
PI_FALLBACK_HARDWARE = "Pi Zero 2W"

_MSG_ID_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
# Same shape as commands.constants.CALLSIGN_TARGET_RE (not imported: that package is heavy).
_CALLSIGN_RE = re.compile(r"^(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]{3,8}(-\d{1,2})?$")


class InternetFeedStorage(Protocol):
    async def get_internet_feed_state(self) -> dict[str, Any]: ...

    async def update_internet_feed_state(self, **fields: Any) -> None: ...


class SecretBoxLike(Protocol):
    def encrypt(self, plaintext: str, associated_data: str) -> str: ...

    def decrypt(self, token: str, associated_data: str) -> str: ...


@dataclass
class IdentityInputs:
    registers: dict[str, Any] = field(default_factory=dict)
    ble_connected: bool = False
    ble_mac: str | None = None
    my_callsign: str = ""
    mcapp_version: str = ""
    ble_expected: bool = False  # BLE mode is not disabled: a BLE identity should appear soon


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().replace(b"\x00", b"").decode("utf-8", "replace").strip() or None
    except OSError:
        return None


def build_identity(
    inputs: IdentityInputs,
    wlan_mac_path: Path = WLAN_MAC_PATH,
    model_path: Path = MODEL_PATH,
) -> dict[str, Any]:
    """Identity the box reports (mcmap §3.1). Whitelist only: the `I` register holds the BLE PIN."""
    reg_i = inputs.registers.get("I")
    reg_i = reg_i if isinstance(reg_i, dict) else {}
    reg_is1 = inputs.registers.get("IS1")
    reg_is1 = reg_is1 if isinstance(reg_is1, dict) else {}
    node_call = reg_i.get("CALL")
    identity: dict[str, Any]
    if inputs.ble_connected and isinstance(node_call, str) and node_call and inputs.ble_mac:
        identity = {
            "idKind": "ble",
            "call": node_call.strip().upper(),
            "mac": inputs.ble_mac.strip().upper(),
        }
        node_id = reg_i.get("ID")
        if isinstance(node_id, int) and not isinstance(node_id, bool) and node_id >= 0:
            identity["nodeId"] = f"0x{node_id:X}"
        fwver = reg_i.get("FWVER")
        if fwver not in (None, ""):
            identity["firmware"] = str(fwver)
        bdate = reg_is1.get("BDATE")
        if bdate not in (None, ""):
            identity["build"] = str(bdate)
        hwid = reg_i.get("HWID")
        if hwid not in (None, ""):
            identity["hardware"] = str(hwid)
    else:
        identity = {"idKind": "pi", "call": inputs.my_callsign.strip().upper()}
        mac = _read_text(wlan_mac_path)
        if mac:
            identity["mac"] = mac.upper()
        identity["hardware"] = _read_text(model_path) or PI_FALLBACK_HARDWARE
    identity["mcappVersion"] = inputs.mcapp_version
    return identity


def identity_key(identity: dict[str, Any]) -> str:
    """mcmap's identity key `(call, idKind, mac)`, upper-cased (contract §3.2)."""
    return "|".join(str(identity.get(k) or "").strip().upper() for k in ("call", "idKind", "mac"))


def map_feed_message(m: Any) -> dict[str, Any] | None:  # noqa: PLR0911 - flat validation guards
    """Feed `Message` -> router dict, or None for anything malformed."""
    if not isinstance(m, dict):
        return None
    msg_id = m.get("msgId")
    src = m.get("from")
    dst = m.get("to")
    text = m.get("text")
    time_unix = m.get("timeUnix")
    if not isinstance(msg_id, str) or not _MSG_ID_RE.match(msg_id):
        return None
    if not isinstance(src, str) or not src.strip():
        return None
    if not isinstance(dst, str) or not dst.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if isinstance(time_unix, bool) or not isinstance(time_unix, int) or time_unix < 0:
        return None
    path = src
    via = m.get("via")
    if (
        isinstance(via, str)
        and via.upper() != src.upper()
        and _CALLSIGN_RE.match(via.strip().upper())
    ):
        path = f"{src},{via.strip()}"
    return {
        "src": path,
        "dst": dst.strip(),
        "msg": text,
        "msg_id": msg_id.upper(),
        "timestamp": time_unix * 1000,
        "type": "msg",
        "src_type": "www",
    }


class _Retry(Exception):  # noqa: N818 - control-flow signal, not an error
    def __init__(self, error: str, floor_s: float = 0.0) -> None:
        super().__init__(error)
        self.error = error
        self.floor_s = floor_s


class _Restart(Exception):  # noqa: N818 - control-flow signal, not an error
    """The session's settings were replaced underneath it (forget): start over, no backoff."""


class _Halt(Exception):  # noqa: N818 - control-flow signal, not an error
    def __init__(self, state: str, error: str | None, drop_token: bool = False) -> None:
        super().__init__(error or state)
        self.state = state
        self.error = error
        self.drop_token = drop_token


def _retry_floor(resp: httpx.Response) -> float:
    floor = 0.0
    header = resp.headers.get("retry-after")
    if header:
        with contextlib.suppress(ValueError):
            floor = max(floor, float(header))
    try:
        body = resp.json()
    except ValueError:
        return floor
    if isinstance(body, dict):
        val = body.get("retryAfterS")
        if isinstance(val, int | float) and not isinstance(val, bool):
            floor = max(floor, float(val))
    return floor


class InternetFeedService:
    def __init__(  # noqa: PLR0913 - every collaborator is injectable for tests
        self,
        *,
        storage: InternetFeedStorage,
        secret_box: SecretBoxLike,
        publish: Callable[[str, str, dict[str, Any]], Awaitable[None]],
        emit_status: Callable[[str, dict[str, Any]], Awaitable[None]],
        identity_inputs: Callable[[], IdentityInputs],
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], int] = now_ms,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
        wlan_mac_path: Path = WLAN_MAC_PATH,
        model_path: Path = MODEL_PATH,
        delay_scale: float = 1.0,
        startup_holdoff_s: float = STARTUP_HOLDOFF_S,
    ) -> None:
        self._delay_scale = delay_scale  # test-only: shrinks real (non-injected) pauses
        self._holdoff_s = startup_holdoff_s
        self._storage = storage
        self._box = secret_box
        self._publish = publish
        self._emit_status = emit_status
        self._identity_inputs = identity_inputs
        url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        self._base = url.rstrip("/")
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - backoff jitter, not security
        self._wlan_mac_path = wlan_mac_path
        self._model_path = model_path

        self._client: httpx.AsyncClient | None = None
        self._stop: asyncio.Event | None = None
        self._wake = asyncio.Event()
        self._reconnect = False
        self._identity_dirty = False
        self._rearm = False
        self._halted = False
        self._backoff_s = BACKOFF_START_S
        self._gen = 0  # bumped by forget(); a connect from an older generation never persists
        self._session_gen = 0
        self._session_identity: dict[str, Any] | None = None
        self._session_key: str | None = None
        self._identity_hint = asyncio.Event()
        self._holdoff_deadline: float | None = None
        self._holdoff_done = False
        self._inflight: set[asyncio.Future[None]] = set()

        self._state = "off"
        self._since_ms = clock()
        self._error: str | None = None
        self._last_batch_ms: int | None = None

    # ── InternetFeedApi ────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "since_ms": self._since_ms,
            "last_batch_ms": self._last_batch_ms,
            "error": self._error,
        }

    async def get_settings(self) -> dict[str, Any]:
        st = await self._storage.get_internet_feed_state()
        return {
            "enabled": bool(st["enabled"]),
            "groups": list(st["groups"]),
            "broadcast": bool(st["broadcast"]),
            "pos_mode": st["pos_mode"],
            "radius_km": st["radius_km"],
            "configured": bool(st["configured"]),
            "status": {**self.status(), "last_connected_ms": st.get("last_connected_ms")},
            "registered": bool(st.get("box_id")),
        }

    async def apply_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        await self._storage.update_internet_feed_state(
            enabled=bool(settings["enabled"]),
            groups=[str(g) for g in settings["groups"]],
            broadcast=bool(settings["broadcast"]),
            pos_mode=settings["pos_mode"],
            radius_km=settings["radius_km"],
            configured=True,
        )
        self._rearm_and_wake()
        if not settings["enabled"]:
            await self._set_state("off")
        return await self.get_settings()

    async def forget(self) -> dict[str, Any]:
        self._gen += 1  # before the clear: an in-flight register must not write back after it
        await self._storage.update_internet_feed_state(
            box_id=None, token_enc=None, identity_json=None
        )
        self._rearm_and_wake()
        st = await self._storage.get_internet_feed_state()
        if not st["enabled"]:
            await self._set_state("off")
        return await self.get_settings()

    def notify_identity_changed(self) -> None:
        self._identity_dirty = True
        self._identity_hint.set()
        self._wake.set()

    # ── state ──────────────────────────────────────────────────────────

    def _rearm_and_wake(self) -> None:
        self._reconnect = True
        self._rearm = True
        self._backoff_s = BACKOFF_START_S
        self._wake.set()

    async def _set_state(self, state: str, error: str | None = None) -> None:
        if (state, error) == (self._state, self._error):
            return
        self._state, self._error, self._since_ms = state, error, self._clock()
        if error is not None:
            await self._storage.update_internet_feed_state(
                last_error=error, last_error_ms=self._since_ms
            )
        elif state == "connected":
            await self._storage.update_internet_feed_state(last_error=None)
        await self._emit()

    async def _emit(self) -> None:
        try:
            await self._emit_status(STATUS_EVENT, self.status())
        except Exception:
            logger.exception("internet feed status emit failed")

    def _identity(self) -> dict[str, Any]:
        return build_identity(self._identity_inputs(), self._wlan_mac_path, self._model_path)

    # ── loop ───────────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        self._stop = stop_event
        if self._transport is not None:
            self._client = httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout(), follow_redirects=False
            )
        else:
            ctx = await asyncio.to_thread(httpx.create_ssl_context)
            self._client = httpx.AsyncClient(
                verify=ctx, timeout=self._timeout(), follow_redirects=False
            )
        try:
            st0 = await self._storage.get_internet_feed_state()
            self._last_batch_ms = st0.get("last_batch_ms")
            while not stop_event.is_set():
                # Capture-and-clear BEFORE the read: an apply_settings landing during the
                # await sets the flags again and reaches the session as an immediate reconnect.
                rearm = self._rearm
                self._rearm = self._reconnect = False
                state = await self._storage.get_internet_feed_state()
                if not state["enabled"]:
                    self._halted = False
                    await self._set_state("off")
                    await self._idle()
                    continue
                if self._halted:
                    if not rearm:
                        await self._idle()
                        continue
                    self._halted = False
                await self._run_session(state)
        finally:
            # A shutdown cancel can land inside the drain; the client still closes.
            try:
                await self._drain_publishes(honor_stop=False)
            finally:
                await self._client.aclose()
                self._client = None

    @staticmethod
    def _timeout() -> httpx.Timeout:
        return httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)

    async def _idle(self) -> None:
        assert self._stop is not None  # noqa: S101 - set by run()
        if self._wake.is_set():
            self._wake.clear()
            return
        await self._wait_for_wake(None)

    async def _wait_for_wake(self, timeout_s: float | None) -> None:
        """Return on wake, stop or timeout. Clears the wake event."""
        assert self._stop is not None  # noqa: S101 - set by run()
        stop_task = asyncio.ensure_future(self._stop.wait())
        wake_task = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
        self._wake.clear()

    async def _pause(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
            return
        await self._wait_for_reconnect(seconds * self._delay_scale)

    async def _wait_for_reconnect(self, timeout_s: float) -> None:
        """Backoff pause: ends on a settings change (`_reconnect`), stop or timeout.

        Identity hints fire on every BLE status event; letting them end the pause would
        defeat the backoff and Retry-After. They only leave `_identity_dirty` set.
        """
        assert self._stop is not None  # noqa: S101 - set by run()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not (self._reconnect or self._stop.is_set()):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await self._wait_for_wake(remaining)

    def _next_delay(self, floor_s: float) -> float:
        base = self._backoff_s
        self._backoff_s = min(BACKOFF_MAX_S, base * 2)
        jitter = self._rng.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
        return max(floor_s, base * jitter)

    async def _run_session(self, state: dict[str, Any]) -> None:  # noqa: PLR0912 - wake/stop/outcome race
        assert self._stop is not None  # noqa: S101 - set by run()
        await self._drain_publishes(honor_stop=True)
        self._identity_dirty = False  # the connect below rebuilds the identity anyway
        task = asyncio.ensure_future(self._connect_once(state))
        stop_task = asyncio.ensure_future(self._stop.wait())
        outcome: BaseException | None = None
        try:
            while True:
                wake_task = asyncio.ensure_future(self._wake.wait())
                try:
                    done, _ = await asyncio.wait(
                        {task, stop_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    wake_task.cancel()
                if task in done:
                    outcome = task.exception()
                    break
                if stop_task in done:
                    await self._cancel(task)
                    return
                self._wake.clear()
                if self._reconnect:
                    await self._cancel(task)
                    return
                if self._identity_dirty:
                    self._identity_dirty = False
                    if self._identity_changed():
                        await self._cancel(task)
                        return
        finally:
            stop_task.cancel()
            if not task.done():
                await self._cancel(task)
        if isinstance(outcome, _Restart):
            # forget() is mid-flight; its rearm ends this wait, the 5 s only bounds a lost one.
            await self._wait_for_reconnect(5.0)
            return
        if isinstance(outcome, _Halt):
            await self._halt(outcome)
        elif isinstance(outcome, _Retry):
            await self._set_state("error", outcome.error)
            await self._pause(self._next_delay(outcome.floor_s))
        elif outcome is not None:
            logger.error("internet feed session crashed", exc_info=outcome)
            await self._set_state("error", "internal error")
            await self._pause(self._next_delay(0.0))
        else:
            await self._set_state("error", "connection closed")
            await self._pause(self._next_delay(0.0))

    @staticmethod
    async def _cancel(task: asyncio.Future[Any]) -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _identity_changed(self) -> bool:
        """The identity KEY differs from the one the current session connected with.

        Compared in memory, not against the stored JSON: on a fresh install nothing is
        stored until the register answers, and every hint would cancel it. Only the key
        `(call, idKind, mac)` counts: other fields (firmware, build) are refreshed by the
        next check-in anyway, and re-registering for them would burn meshmap's 10/h budget.
        """
        current = self._session_identity
        return current is not None and identity_key(current) != identity_key(self._identity())

    async def _halt(self, halt: _Halt) -> None:
        self._halted = True
        if halt.drop_token:
            await self._drop_entry()
        await self._set_state(halt.state, halt.error)

    async def _drain_publishes(self, *, honor_stop: bool) -> None:
        """Let publishes that already began finish before the next session replays them."""
        pending = {t for t in self._inflight if not t.done()}
        if not pending:
            return
        all_done: asyncio.Future[Any] = asyncio.ensure_future(asyncio.wait(pending))
        waiters: set[asyncio.Future[Any]] = {all_done}
        if honor_stop and self._stop is not None:
            waiters.add(asyncio.ensure_future(self._stop.wait()))
        try:
            await asyncio.wait(
                waiters, timeout=PUBLISH_DRAIN_S, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for w in waiters:
                w.cancel()

    # ── one connection ─────────────────────────────────────────────────

    async def _connect_once(self, state: dict[str, Any]) -> None:
        assert self._client is not None  # noqa: S101 - set by run()
        await self._set_state("connecting")
        self._session_gen = self._gen
        self._session_identity = self._session_key = None
        try:
            await self._startup_holdoff()
            token = await self._register()
            await self._stream(state, token)
        except httpx.HTTPError as exc:
            raise _Retry(f"network error ({type(exc).__name__})") from None
        except OSError as exc:
            raise _Retry(f"network error ({type(exc).__name__})") from None

    async def _startup_holdoff(self) -> None:
        """Before the process's first register, give BLE time to supply the real identity.

        The BLE registers are not hydrated yet at startup, so the identity would be `pi`,
        then `ble` seconds later: two identities, a 401 and a token-less (reclaim) register
        in between. Waits (re-checking on identity hints) for the identity to become `ble`,
        up to `startup_holdoff_s`, then proceeds with whatever is current. A cancel from
        stop / settings ends it at once; the deadline survives such a restart.
        """
        if self._holdoff_done:
            return
        if self._identity_inputs().ble_expected:
            loop = asyncio.get_running_loop()
            if self._holdoff_deadline is None:
                self._holdoff_deadline = loop.time() + self._holdoff_s
            while self._identity()["idKind"] != "ble":
                remaining = self._holdoff_deadline - loop.time()
                if remaining <= 0:
                    logger.info("internet feed: no BLE identity yet, registering as pi")
                    break
                self._identity_hint.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._identity_hint.wait(), min(remaining, STARTUP_HOLDOFF_POLL_S)
                    )
        self._holdoff_done = True

    # The keyring: ONE encrypted JSON object {identity_key: {box_id, token}} in `token_enc`,
    # least recently used first. mcmap answers 401 to a token presented with another identity
    # (contract §3.2), so each identity keeps its own token; a single slot meant a token-less
    # register (a `reclaim` at meshmap) on every pi <-> ble switch.

    def _keyring(self, state: dict[str, Any]) -> dict[str, dict[str, str]]:
        enc = state.get("token_enc")
        if not enc:
            return {}
        try:
            raw = json.loads(self._box.decrypt(enc, TOKENS_AD))
        except (SecretBoxError, ValueError):
            logger.warning("stored feed tokens cannot be read; registering fresh")
            return {}
        ring: dict[str, dict[str, str]] = {}
        if isinstance(raw, dict):
            for key, entry in raw.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                box_id, token = entry.get("box_id"), entry.get("token")
                if isinstance(box_id, str) and isinstance(token, str) and token:
                    ring[key] = {"box_id": box_id, "token": token}
        return ring

    def _pack_keyring(self, ring: dict[str, dict[str, str]]) -> str | None:
        return self._box.encrypt(json.dumps(ring), TOKENS_AD) if ring else None

    async def _drop_entry(self) -> None:
        """Forget the token of the identity this session used; other identities keep theirs."""
        key = self._session_key
        if key is None or self._session_gen != self._gen:
            return
        ring = self._keyring(await self._storage.get_internet_feed_state())
        ring.pop(key, None)
        if self._session_gen != self._gen:
            return
        await self._storage.update_internet_feed_state(
            box_id=None, token_enc=self._pack_keyring(ring)
        )

    async def _post_register(self, identity: dict[str, Any], token: str | None) -> httpx.Response:
        assert self._client is not None  # noqa: S101 - set by run()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return await self._client.post(
            f"{self._base}/api/v2/box/register", json=identity, headers=headers
        )

    async def _register(self) -> str:
        identity = self._identity()
        key = identity_key(identity)
        self._session_identity, self._session_key = identity, key
        ring = self._keyring(await self._storage.get_internet_feed_state())
        before = list(ring.items())
        entry = ring.get(key)
        token = entry["token"] if entry else None
        resp = await self._post_register(identity, token)
        if resp.status_code == httpx.codes.UNAUTHORIZED and token:
            await self._drop_entry()
            ring.pop(key, None)
            before = list(ring.items())
            token = None
            resp = await self._post_register(identity, None)
        if resp.status_code in (httpx.codes.TOO_MANY_REQUESTS, httpx.codes.SERVICE_UNAVAILABLE):
            raise _Retry(f"register HTTP {resp.status_code}", _retry_floor(resp))
        if resp.status_code not in (httpx.codes.OK, httpx.codes.CREATED):
            raise _Retry(f"register HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        box_id = body.get("boxId") if isinstance(body, dict) else None
        if not isinstance(box_id, str) or not box_id:
            raise _Retry("register: malformed response")
        if resp.status_code == httpx.codes.CREATED or token is None:
            new_token = body.get("token") if isinstance(body, dict) else None
            if not isinstance(new_token, str) or not new_token:
                raise _Retry("register: no token issued")
            token = new_token
        ring.pop(key, None)
        ring[key] = {"box_id": box_id, "token": token}  # most recently used last
        while len(ring) > KEYRING_MAX:
            ring.pop(next(iter(ring)))
        fields: dict[str, Any] = {
            "box_id": box_id,
            "identity_json": json.dumps(identity, sort_keys=True),
        }
        if list(ring.items()) != before:
            fields["token_enc"] = self._pack_keyring(ring)
        if self._session_gen != self._gen:  # forget() ran while the POST was in flight
            raise _Restart
        await self._storage.update_internet_feed_state(**fields)
        return token

    async def _stream(self, state: dict[str, Any], token: str) -> None:
        assert self._client is not None  # noqa: S101 - set by run()
        fresh = await self._storage.get_internet_feed_state()
        params: dict[str, str] = {
            "bcast": "1" if state["broadcast"] else "0",
            "pos": "off",  # wave B
        }
        if state["groups"]:
            params["groups"] = ",".join(state["groups"])
        if fresh.get("cursor"):
            params["since"] = str(fresh["cursor"])
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        async with self._client.stream(
            "GET", f"{self._base}/api/v2/box/feed", params=params, headers=headers
        ) as resp:
            if resp.status_code != httpx.codes.OK:
                await resp.aread()
                if resp.status_code == httpx.codes.UNAUTHORIZED:
                    await self._drop_entry()
                    raise _Retry("feed HTTP 401")
                floor = (
                    _retry_floor(resp)
                    if resp.status_code
                    in (httpx.codes.TOO_MANY_REQUESTS, httpx.codes.SERVICE_UNAVAILABLE)
                    else 0.0
                )
                raise _Retry(f"feed HTTP {resp.status_code}", floor)
            event: str | None = None
            data: list[str] = []
            event_id: str | None = None
            async for line in resp.aiter_lines():
                if line == "":
                    if data or event:
                        await self._dispatch(event or "message", "\n".join(data), event_id)
                    event, data, event_id = None, [], None
                    continue
                if line.startswith(":"):
                    continue
                name, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if name == "event":
                    event = value
                elif name == "data":
                    data.append(value)
                elif name == "id":
                    event_id = value

    async def _dispatch(self, event: str, raw: str, event_id: str | None) -> None:
        if event == "superseded":
            raise _Halt("error", "superseded")
        if event == "revoked":
            raise _Halt("revoked", "token revoked", drop_token=True)
        if event not in ("hello", "batch"):
            return
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            logger.warning("internet feed: undecodable %s event ignored", event)
            return
        if not isinstance(payload, dict):
            return
        if event == "hello":
            await self._on_hello(payload)
        else:
            await self._on_batch(payload, event_id)

    async def _on_hello(self, payload: dict[str, Any]) -> None:
        version = payload.get("feedVersion")
        if isinstance(version, bool) or not isinstance(version, int | float):
            raise _Halt("error", "unsupported feedVersion (missing)")
        if int(version) != SUPPORTED_FEED_MAJOR:
            raise _Halt("error", f"unsupported feedVersion {version}")
        self._backoff_s = BACKOFF_START_S
        await self._storage.update_internet_feed_state(last_connected_ms=self._clock())
        await self._set_state("connected")

    async def _on_batch(self, payload: dict[str, Any], event_id: str | None) -> None:
        messages = payload.get("messages")
        for raw in messages if isinstance(messages, list) else []:
            mapped = map_feed_message(raw)
            if mapped is None:
                continue
            await self._publish_one(mapped)
        self._last_batch_ms = self._clock()
        fields: dict[str, Any] = {"last_batch_ms": self._last_batch_ms}
        if event_id:
            fields["cursor"] = event_id
        await self._storage.update_internet_feed_state(**fields)
        await self._emit()

    async def _publish_one(self, mapped: dict[str, Any]) -> None:
        """Publish to completion even if this session is cancelled meanwhile.

        `store_message` claims `(sender, msg_id)` in memory before its first await; a cancel
        between that claim and the INSERT would leave a claim with no row, and the replay
        (cursor not advanced) would then be dropped as a duplicate. The shielded task keeps
        running; its failure is logged by the done-callback, whoever is still waiting.
        """
        task: asyncio.Future[None] = asyncio.ensure_future(
            self._publish("www", WWW_MESSAGE_TOPIC, mapped)
        )
        self._inflight.add(task)
        msg_id = str(mapped["msg_id"])

        def _done(t: asyncio.Future[None]) -> None:
            self._inflight.discard(t)
            if not t.cancelled() and (exc := t.exception()) is not None:
                logger.error("internet feed publish failed (msg_id=%s)", msg_id, exc_info=exc)

        task.add_done_callback(_done)
        with contextlib.suppress(Exception):  # already logged by _done
            await asyncio.shield(task)
