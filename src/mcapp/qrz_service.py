"""QRZ.com callsign lookup service: one background loop, hard-capped.

Budget rules (doc/2026-10-04_0848-qrz-callsign-lookup-plan.md §4):

- at most `DAILY_CAP` lookups per rolling 24 h, counted from a ledger row
  written BEFORE the request goes out, so a crash or timeout still counts;
- reaching the cap suspends lookups for 24 h, persisted across restarts;
- at most one request per `MIN_INTERVAL_MS`, logins included, persisted too;
- exponential backoff on rate limiting and transient failures;
- a rejected login stops the service until new credentials arrive, because
  retrying a wrong password is what gets an account locked.

`step()` performs at most one request and returns how long to wait before the
next one; `run()` is only the sleep loop around it, so tests drive `step()`
with a fake clock and transport and never sleep.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from .qrz_client import (
    QRZ_AGENT,
    QRZ_XML_URL,
    Outcome,
    QrzResponse,
    base_callsign,
    first_name,
    is_lookup_candidate,
    parse_response,
    qth_from_addr2,
)
from .secret_box import SecretBox, SecretBoxError
from .util import PLACEHOLDER_CALLSIGN_BASES, now_ms

if TYPE_CHECKING:
    from .sqlite_storage import SQLiteStorage

logger = logging.getLogger(__name__)

_HOUR_MS = 3_600_000
_DAY_MS = 24 * _HOUR_MS

DAILY_CAP = 50
WINDOW_MS = _DAY_MS
SUSPEND_MS = _DAY_MS
MIN_INTERVAL_MS = 30_000
BACKOFF_BASE_MS = 60_000
BACKOFF_MAX_MS = 6 * _HOUR_MS
REFRESH_FOUND_MS = 90 * _DAY_MS
REFRESH_NOT_FOUND_MS = 7 * _DAY_MS
ACTIVITY_WINDOW_MS = 30 * _DAY_MS
LEDGER_RETENTION_MS = 2 * _DAY_MS
IDLE_POLL_MS = 5 * 60_000
HTTP_TIMEOUT_S = 15.0
# A second session loss in a row without a successful lookup in between is not
# a timeout any more but something about this client; back off instead of
# spending the budget on login/lookup pairs.
SESSION_ERRORS_BEFORE_BACKOFF = 2

_RATE_LIMIT_HTTP = frozenset({429, 503})

# `SubExp` on a free account; a subscriber gets the expiry date instead.
_NON_SUBSCRIBER = "non-subscriber"
_SERVER_COUNT_REASON = "QRZ reports"

# `qrz_state.suspend_reason`: why the running suspension was set.
SUSPEND_CAP = "cap"
SUSPEND_SERVER_COUNT = "server_count"
SUSPEND_REFUSED = "refused"

# Called with {BASE: {first_name, qth, country}} whenever a lookup finds data;
# main.py fans it out as the SSE event `proxy:callsign_info`.
InfoListener = Callable[[dict[str, dict[str, str | None]]], Awaitable[None]]


def _aad(username: str) -> str:
    return f"qrz.password:{username}"


def _is_free_tier(sub_exp: str | None) -> bool:
    """True unless QRZ said the account is a subscriber. Fails closed: an
    unknown `SubExp` is treated as free, so the Count gate stays on."""
    return sub_exp is None or sub_exp.strip().lower() == _NON_SUBSCRIBER


def _account_tier(sub_exp: str | None) -> str | None:
    """'subscriber' or 'free' for the settings card; None before any login."""
    if sub_exp is None:
        return None
    return "free" if _is_free_tier(sub_exp) else "subscriber"


def _retry_after_ms(response: httpx.Response) -> int:
    try:
        seconds = int(response.headers.get("retry-after", "0"))
    except ValueError:
        return 0
    return max(0, min(seconds * 1000, _DAY_MS))


class QrzLookupService:
    def __init__(
        self,
        storage: SQLiteStorage,
        secret_box: SecretBox,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], int] = now_ms,
        on_info: InfoListener | None = None,
    ) -> None:
        self._storage = storage
        self._on_info = on_info
        self._box = secret_box
        self._transport = transport
        self._clock = clock
        self._wake = asyncio.Event()
        # In memory only: a restart logs in again, which costs no lookup.
        self._session_key: str | None = None
        self._session_errors = 0
        # Bumped whenever credentials change, so a request that was in flight
        # with the old ones cannot write its verdict over the new ones.
        self._cred_gen = 0
        self._nothing_due = False

    def set_info_listener(self, listener: InfoListener | None) -> None:
        """Wire the live push after construction: the SSE manager is built
        after this service in build_app."""
        self._on_info = listener

    # ── API surface ────────────────────────────────────────────────────────

    async def set_credentials(self, username: str, password: str) -> None:
        user = username.strip().upper()
        if not user or not password:
            msg = "username and password are required"
            raise ValueError(msg)
        token = await asyncio.to_thread(self._box.encrypt, password, _aad(user))
        self._cred_gen += 1
        self._session_key = None
        self._session_errors = 0
        await self._storage.update_qrz_state(
            username=user,
            password_enc=token,
            enabled=1,
            auth_failed=0,
            last_login_ms=None,
            last_error=None,
            last_error_ms=None,
        )
        logger.info("QRZ credentials stored for %s", user)
        self._wake.set()

    async def clear_credentials(self) -> None:
        self._cred_gen += 1
        self._session_key = None
        await self._storage.update_qrz_state(
            username=None,
            password_enc=None,
            auth_failed=0,
            last_login_ms=None,
            last_error=None,
            last_error_ms=None,
        )
        logger.info("QRZ credentials removed")
        self._wake.set()

    async def set_enabled(self, enabled: bool) -> None:
        await self._storage.update_qrz_state(enabled=1 if enabled else 0)
        self._wake.set()

    async def status(self) -> dict[str, Any]:
        state = await self._storage.get_qrz_state()
        now = self._clock()
        lookups = await self._storage.count_qrz_lookups_since(now - WINDOW_MS)
        cached = await self._storage.count_callsign_info()
        suspended = state["suspended_until_ms"]
        backoff = state["backoff_until_ms"]
        suspended = suspended if suspended and suspended > now else None
        backoff = backoff if backoff and backoff > now else None
        readable = await self._credentials_readable(state)
        label = self._state_label(state, readable, suspended, backoff)
        next_at = None
        if label in {"verifying", "active", "idle", "suspended", "backoff"}:
            next_at = max(
                (state["last_request_ms"] or 0) + MIN_INTERVAL_MS, suspended or 0, backoff or 0
            )
        return {
            "configured": bool(state["username"] and state["password_enc"]),
            "enabled": bool(state["enabled"]),
            "username": state["username"],
            "state": label,
            "lookups_24h": lookups,
            "daily_cap": DAILY_CAP,
            "suspended_until": suspended,
            "backoff_until": backoff,
            "next_request_at": next_at,
            "server_count": state["server_count"],
            "subscription": state["subscription"],
            "account_tier": _account_tier(state["subscription"]),
            "last_login_at": state["last_login_ms"],
            "last_error": state["last_error"],
            "last_error_at": state["last_error_ms"],
            "cached_found": cached["found"],
            "cached_not_found": cached["not_found"],
        }

    def _state_label(
        self, state: dict[str, Any], readable: bool, suspended: int | None, backoff: int | None
    ) -> str:
        checks: list[tuple[bool, str]] = [
            (not (state["username"] and state["password_enc"]), "unconfigured"),
            (not readable, "credentials_unreadable"),
            (bool(state["auth_failed"]), "auth_failed"),
            (not state["enabled"], "disabled"),
            (suspended is not None, "suspended"),
            (backoff is not None, "backoff"),
            (state["last_login_ms"] is None, "verifying"),
            (self._nothing_due, "idle"),
        ]
        return next((label for hit, label in checks if hit), "active")

    async def _credentials_readable(self, state: dict[str, Any]) -> bool:
        if not (state["username"] and state["password_enc"]):
            return True
        try:
            await asyncio.to_thread(
                self._box.decrypt, state["password_enc"], _aad(state["username"])
            )
        except SecretBoxError:
            return False
        return True

    # ── Loop ───────────────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                delay_ms = await self.step()
            except Exception:
                logger.exception("QRZ lookup step failed")
                delay_ms = IDLE_POLL_MS
            self._wake.clear()
            stop_wait = asyncio.ensure_future(stop_event.wait())
            wake_wait = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait(
                    {stop_wait, wake_wait},
                    timeout=max(delay_ms, 1000) / 1000,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                stop_wait.cancel()
                wake_wait.cancel()
        logger.debug("QRZ lookup service stopped")

    async def step(self) -> int:  # noqa: PLR0911 - one early return per gate, in gate order
        """Run at most one request; return ms until the next step is useful."""
        state = await self._storage.get_qrz_state()
        now = self._clock()
        if not (state["username"] and state["password_enc"]):
            return IDLE_POLL_MS
        if not state["enabled"] or state["auth_failed"]:
            return IDLE_POLL_MS
        try:
            password = await asyncio.to_thread(
                self._box.decrypt, state["password_enc"], _aad(state["username"])
            )
        except SecretBoxError:
            logger.warning("QRZ password cannot be decrypted on this install; re-enter it")
            return IDLE_POLL_MS

        if await self._count_suspension_obsolete(state, now):
            await self._storage.update_qrz_state(
                suspended_until_ms=None, suspend_reason=None, last_error=None, last_error_ms=None
            )
            state["suspended_until_ms"] = None
            logger.info("QRZ suspension from the server Count lifted: subscriber account")
        for until in (state["suspended_until_ms"], state["backoff_until_ms"]):
            if until and int(until) > now:
                return int(until) - now
        next_allowed = int(state["last_request_ms"] or 0) + MIN_INTERVAL_MS
        if next_allowed > now:
            return next_allowed - now

        await self._storage.prune_qrz_lookups(now - LEDGER_RETENTION_MS)
        used = await self._storage.count_qrz_lookups_since(now - WINDOW_MS)
        if used >= DAILY_CAP:
            await self._suspend(now, f"daily cap of {DAILY_CAP} lookups reached", SUSPEND_CAP)
            return SUSPEND_MS

        if self._session_key is None:
            await self._login(state["username"], password, now)
            return MIN_INTERVAL_MS

        callsign = await self._next_due(now)
        if callsign is None:
            self._nothing_due = True
            return IDLE_POLL_MS
        self._nothing_due = False
        await self._lookup(callsign, now, used)
        return MIN_INTERVAL_MS

    # ── Requests ───────────────────────────────────────────────────────────

    async def _post(self, data: dict[str, str]) -> httpx.Response:
        # POST with a form body, never a query string: httpx logs request URLs,
        # and the URL would carry the password or the session key.
        async with httpx.AsyncClient(transport=self._transport, timeout=HTTP_TIMEOUT_S) as client:
            return await client.post(QRZ_XML_URL, data=data)

    async def _send(self, data: dict[str, str], *, is_login: bool) -> QrzResponse | int:
        """The parsed reply, or a backoff hint in ms when there is none."""
        try:
            response = await self._post(data)
        except httpx.HTTPError as exc:
            await self._backoff(self._clock(), f"network error: {type(exc).__name__}", 0)
            return -1
        if response.status_code != 200:  # noqa: PLR2004 - HTTP OK
            hint = _retry_after_ms(response) if response.status_code in _RATE_LIMIT_HTTP else 0
            await self._backoff(self._clock(), f"HTTP {response.status_code}", hint)
            return -1
        return parse_response(response.text, is_login=is_login)

    async def _login(self, username: str, password: str, now: int) -> None:
        gen = self._cred_gen
        await self._storage.update_qrz_state(last_request_ms=now)
        result = await self._send(
            {"username": username, "password": password, "agent": QRZ_AGENT}, is_login=True
        )
        if not isinstance(result, QrzResponse) or gen != self._cred_gen:
            return
        await self._record_session_info(result)
        if result.outcome is Outcome.LOGGED_IN:
            self._session_key = result.key
            await self._storage.update_qrz_state(
                last_login_ms=now, backoff_level=0, backoff_until_ms=None
            )
            logger.info("QRZ login ok (%s)", result.sub_exp or "subscription unknown")
            await self._check_server_count(result, now)
        elif result.outcome is Outcome.AUTH_FAILED:
            await self._storage.update_qrz_state(
                auth_failed=1, last_error=result.error or "login rejected", last_error_ms=now
            )
            logger.warning("QRZ login rejected: %s — stopped until new credentials", result.error)
        else:
            await self._handle_failure(result, now)

    async def _lookup(self, callsign: str, now: int, used_before: int) -> None:
        gen = self._cred_gen
        await self._storage.record_qrz_lookup(now, callsign)
        await self._storage.update_qrz_state(last_request_ms=now)
        if used_before + 1 >= DAILY_CAP:
            await self._suspend(now, f"daily cap of {DAILY_CAP} lookups reached", SUSPEND_CAP)
        result = await self._send(
            {"s": self._session_key or "", "callsign": callsign}, is_login=False
        )
        if not isinstance(result, QrzResponse):
            await self._storage.set_qrz_lookup_outcome(now, callsign, "transport_error")
            return
        await self._storage.set_qrz_lookup_outcome(now, callsign, result.outcome.value)
        if gen != self._cred_gen:
            return
        await self._record_session_info(result)

        if result.outcome is Outcome.FOUND:
            rec = result.record
            info = {
                "first_name": first_name(rec.get("fname")),
                "qth": qth_from_addr2(rec.get("addr2")),
                "country": rec.get("country"),
            }
            await self._storage.upsert_callsign_info(callsign, "found", now, raw=rec, **info)
            await self._publish(callsign, info)
            self._session_errors = 0
            await self._storage.update_qrz_state(backoff_level=0, backoff_until_ms=None)
        elif result.outcome is Outcome.NOT_FOUND:
            index = await self._storage.get_callsign_info_index()
            if callsign in index:
                await self._storage.touch_callsign_info(callsign, now)
            else:
                await self._storage.upsert_callsign_info(callsign, "not_found", now)
            self._session_errors = 0
            await self._storage.update_qrz_state(backoff_level=0, backoff_until_ms=None)
        elif result.outcome is Outcome.SESSION_INVALID:
            self._session_key = None
            self._session_errors += 1
            await self._note_error(now, result.error or "session invalid")
            if self._session_errors >= SESSION_ERRORS_BEFORE_BACKOFF:
                await self._backoff(now, result.error or "repeated session loss", 0)
        else:
            await self._handle_failure(result, now)
        await self._check_server_count(result, now)

    async def _publish(self, callsign: str, info: dict[str, str | None]) -> None:
        # Same filter as get_callsign_info_map: an entry with neither name nor
        # QTH is never on the wire, live or in the connect snapshot.
        if self._on_info is None or not (info["first_name"] or info["qth"]):
            return
        try:
            await self._on_info({callsign: info})
        except Exception:
            # A broadcast failure must never count as a lookup failure.
            logger.warning("callsign_info broadcast failed", exc_info=True)

    # ── State transitions ─────────────────────────────────────────────────

    async def _handle_failure(self, result: QrzResponse, now: int) -> None:
        if result.outcome is Outcome.REFUSED:
            self._session_key = None
            await self._suspend(now, result.error or "Connection refused", SUSPEND_REFUSED)
        else:
            await self._backoff(now, result.error or result.outcome.value, 0)

    async def _record_session_info(self, result: QrzResponse) -> None:
        fields: dict[str, Any] = {}
        if result.count is not None:
            fields["server_count"] = result.count
        if result.sub_exp:
            fields["subscription"] = result.sub_exp
        if fields:
            await self._storage.update_qrz_state(**fields)

    async def _check_server_count(self, result: QrzResponse, now: int) -> None:
        # QRZ's own 24 h tally includes other software using the same account;
        # at the cap we stop too, which keeps the account under the free tier.
        # Not on a subscriber: there is no free tier to protect, and its Count
        # is not a 24 h tally — DM3KS's login reported 77678 while QRZ's own
        # account page showed 1 XML lookup that day and an unlimited limit
        # (2026-10-04). Gating on it suspended every login, forever. Our own
        # ledger cap still applies to subscribers.
        if result.count is None or result.count < DAILY_CAP:
            return
        sub_exp = result.sub_exp or (await self._storage.get_qrz_state())["subscription"]
        if _is_free_tier(sub_exp):
            await self._suspend(
                now, f"{_SERVER_COUNT_REASON} {result.count} lookups in 24 h", SUSPEND_SERVER_COUNT
            )

    async def _count_suspension_obsolete(self, state: dict[str, Any], now: int) -> bool:
        """A running suspension set by the server Count on an account known to
        be a subscriber — written before the gate learned about `SubExp`."""
        until = state["suspended_until_ms"]
        if not until or int(until) <= now or _is_free_tier(state["subscription"]):
            return False
        reason = state["suspend_reason"]
        if reason is not None:
            return bool(reason == SUSPEND_SERVER_COUNT)
        # Written before `suspend_reason` existed (v2.1.1/v2.1.2): infer it.
        # The Count message names it outright. A replaced password clears
        # `last_error` but not the suspension — DM3KS's exact state — and
        # then only our own ledger can still have caused it, so lift unless
        # the ledger is at the cap. A refusal hidden the same way costs one
        # login that QRZ refuses again, never a lookup.
        error = state["last_error"]
        if error is not None:
            return str(error).startswith(_SERVER_COUNT_REASON)
        used = await self._storage.count_qrz_lookups_since(now - WINDOW_MS)
        return used < DAILY_CAP

    async def _suspend(self, now: int, reason: str, kind: str) -> None:
        await self._storage.update_qrz_state(
            suspended_until_ms=now + SUSPEND_MS,
            suspend_reason=kind,
            last_error=reason,
            last_error_ms=now,
        )
        logger.warning("QRZ lookups suspended for 24 h: %s", reason)

    async def _backoff(self, now: int, reason: str, hint_ms: int) -> None:
        state = await self._storage.get_qrz_state()
        level = int(state["backoff_level"] or 0)
        delay = min(BACKOFF_BASE_MS * (2**level), BACKOFF_MAX_MS)
        delay = max(delay, hint_ms)
        await self._storage.update_qrz_state(
            backoff_level=level + 1,
            backoff_until_ms=now + delay,
            last_error=reason,
            last_error_ms=now,
        )
        logger.warning("QRZ backoff %d s (level %d): %s", delay // 1000, level + 1, reason)

    async def _note_error(self, now: int, reason: str) -> None:
        await self._storage.update_qrz_state(last_error=reason, last_error_ms=now)

    # ── Candidates ────────────────────────────────────────────────────────

    async def _next_due(self, now: int) -> str | None:
        """Never-looked-up first, then stale refreshes; chat partners before
        stations only heard; most recent first within each group."""
        activity = await self._storage.get_recent_callsign_activity(now - ACTIVITY_WINDOW_MS)
        merged: dict[str, tuple[int, int]] = {}
        for row in activity:
            base = base_callsign(row["callsign"] or "")
            if not is_lookup_candidate(base) or base in PLACEHOLDER_CALLSIGN_BASES:
                continue
            prev_ts, prev_chatted = merged.get(base, (0, 0))
            merged[base] = (
                max(int(row["last_ts"] or 0), prev_ts),
                max(int(row["chatted"] or 0), prev_chatted),
            )
        index = await self._storage.get_callsign_info_index()

        def due_rank(base: str) -> int | None:
            cached = index.get(base)
            if cached is None:
                return 0
            status, fetched_at = cached
            ttl = REFRESH_FOUND_MS if status == "found" else REFRESH_NOT_FOUND_MS
            return 1 if now - fetched_at >= ttl else None

        due = [(rank, base) for base in merged if (rank := due_rank(base)) is not None]
        if not due:
            return None
        due.sort(key=lambda item: (item[0], -merged[item[1]][1], -merged[item[1]][0]))
        return due[0][1]
