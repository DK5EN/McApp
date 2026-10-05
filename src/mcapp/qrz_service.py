"""QRZ.com callsign lookup service: one background loop, hard-capped.

Budget rules (doc/2026-10-04_0848-qrz-callsign-lookup-plan.md §4):

- on a free account at most `DAILY_CAP` lookups per rolling 24 h, counted
  from a ledger row written BEFORE the request goes out, so a crash or
  timeout still counts; a paid subscription has no daily limit, so only the
  request spacing below bounds it;
- reaching the cap suspends lookups for 24 h, persisted across restarts;
- QRZ's own `Count` never suspends anything. At each login on a free account a
  plausible Count (0 <= Count < `SERVER_COUNT_PLAUSIBLE_MAX`) becomes an in-memory
  baseline; the lookups recorded since that login are added to it and the sum is
  held under QRZ's free limit. A Count that is not a plausible 24 h tally (QRZ
  reports thousands on some accounts) is ignored. Used up, the service backs off
  `REFUSAL_BACKOFF_MS` and re-logs in, which refreshes the baseline; so does a
  session older than 24 h, whose baseline no longer describes QRZ's tally;
- at most one request per `MIN_INTERVAL_MS`, logins included, persisted too;
- a refusal or an explicit rate limit from QRZ backs off a fixed `REFUSAL_BACKOFF_MS`
  (not 24 h: nothing proves it lasts that long), any other transient failure backs
  off exponentially;
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
# QRZ's free-account limit per 24 h, and the largest Count that can still be a
# tally of it. Free accounts have been seen reporting Count in the thousands
# (a counter that is not a 24 h tally); such a value is ignored, not obeyed.
SERVER_FREE_LIMIT = 100
SERVER_COUNT_PLAUSIBLE_MAX = SERVER_FREE_LIMIT
# Back-off after a refusal, an explicit rate limit or an exhausted server-side
# budget. The login that follows re-reads QRZ's Count.
REFUSAL_BACKOFF_MS = _HOUR_MS
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
_CAP_REASON = f"daily cap of {DAILY_CAP} lookups reached"

# `qrz_state.suspend_reason`: why the running suspension was set. Only `cap` is
# still written; `server_count` and `refused` exist on boxes updated from
# v2.1.1-v2.1.5, and a `server_count` one is lifted at the next step.
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
    unknown `SubExp` is treated as free, so the free-tier rules (ledger cap, baseline) stay on."""
    return not sub_exp or sub_exp.strip().lower() in {"", _NON_SUBSCRIBER}


def _daily_cap(sub_exp: str | None) -> int | None:
    """Our ledger cap: `DAILY_CAP` on a free (or unknown) account, None on a
    subscriber, whose XML lookups QRZ does not limit per day."""
    return DAILY_CAP if _is_free_tier(sub_exp) else None


def _account_tier(sub_exp: str | None) -> str | None:
    """'subscriber' or 'free' for the settings card; None before any login."""
    if sub_exp is None:
        return None
    return "free" if _is_free_tier(sub_exp) else "subscriber"


def _fmt(value: object) -> str:
    """Log rendering of an optional number: 'none' rather than 'None'."""
    return "none" if value is None else str(value)


def _count_plausible(count: int | None) -> bool:
    return count is not None and 0 <= count < SERVER_COUNT_PLAUSIBLE_MAX


def _is_count_suspension(state: dict[str, Any]) -> bool:
    """A suspension set by the retired Count gate: by recorded reason, or, on a
    row from before `suspend_reason` existed, by its error text."""
    reason = state["suspend_reason"]
    if reason is not None:
        return bool(reason == SUSPEND_SERVER_COUNT)
    return str(state["last_error"] or "").startswith(_SERVER_COUNT_REASON)


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
        # Also in memory, for the same reason: set by every successful login on a
        # free account from a plausible Count, so a restart or a lost session
        # always refreshes it. None means "no usable server-side tally".
        self._server_baseline: int | None = None
        # Raw Count of the last login of this process; `status()` judges
        # plausibility on it so the flag cannot disagree with the baseline.
        self._login_seen = False
        self._login_count: int | None = None
        # Bumped whenever credentials change, so a request that was in flight
        # with the old ones cannot write its verdict over the new ones.
        self._cred_gen = 0
        self._nothing_due = False
        self._last_http_status: int | None = None

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
        self._forget_session()
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
        self._forget_session()
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

    def _forget_session(self) -> None:
        self._session_key = None
        self._server_baseline = None
        self._login_seen = False
        self._login_count = None

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
        observed = self._login_count if self._login_seen else state["server_count"]
        since_login = await self._lookups_since_login(state)
        server_left = self._server_remaining(since_login)
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
            "daily_cap": _daily_cap(state["subscription"]),
            "suspended_until": suspended,
            "backoff_until": backoff,
            "next_request_at": next_at,
            "server_count": state["server_count"],
            "server_count_ignored": None if observed is None else not _count_plausible(observed),
            "server_budget_left": None if server_left is None else max(0, server_left),
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

        await self._lift_obsolete_suspension(state, now)
        for until in (state["suspended_until_ms"], state["backoff_until_ms"]):
            if until and int(until) > now:
                return int(until) - now
        next_allowed = int(state["last_request_ms"] or 0) + MIN_INTERVAL_MS
        if next_allowed > now:
            return next_allowed - now

        await self._storage.prune_qrz_lookups(now - LEDGER_RETENTION_MS)
        used = await self._storage.count_qrz_lookups_since(now - WINDOW_MS)
        cap = _daily_cap(state["subscription"])
        if cap is not None and used >= cap:
            await self._suspend(now, _CAP_REASON, SUSPEND_CAP)
            return SUSPEND_MS

        if self._session_key is not None and self._baseline_expired(state, now):
            logger.info("QRZ session older than 24 h, logging in again to refresh the baseline")
            self._session_key = None
        if self._session_key is None:
            await self._login(state["username"], password, now)
            return MIN_INTERVAL_MS

        callsign = await self._next_due(now)
        if callsign is None:
            self._nothing_due = True
            return IDLE_POLL_MS
        self._nothing_due = False
        if await self._stop_if_server_budget_used(state, now, used, cap):
            return REFUSAL_BACKOFF_MS
        await self._lookup(callsign, now, used, cap)
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
        self._last_http_status = response.status_code
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
            # The login is authoritative for the tier: a reply without SubExp
            # must not leave an earlier "subscriber" lifting the daily cap.
            await self._storage.update_qrz_state(
                last_login_ms=now,
                backoff_level=0,
                backoff_until_ms=None,
                subscription=result.sub_exp,
            )
            await self._observe_login(result, now)
        elif result.outcome is Outcome.AUTH_FAILED:
            await self._storage.update_qrz_state(
                auth_failed=1, last_error=result.error or "login rejected", last_error_ms=now
            )
            logger.warning("QRZ login rejected: %s — stopped until new credentials", result.error)
        else:
            await self._handle_failure(result, now)

    async def _lookup(self, callsign: str, now: int, used_before: int, cap: int | None) -> None:
        gen = self._cred_gen
        await self._storage.record_qrz_lookup(now, callsign)
        await self._storage.update_qrz_state(last_request_ms=now)
        if cap is not None and used_before + 1 >= cap:
            await self._suspend(now, _CAP_REASON, SUSPEND_CAP)
        result = await self._send(
            {"s": self._session_key or "", "callsign": callsign}, is_login=False
        )
        if not isinstance(result, QrzResponse):
            await self._storage.set_qrz_lookup_outcome(now, callsign, "transport_error")
            logger.info(
                "QRZ lookup %s -> transport_error count=none ledger=%d/%s",
                callsign,
                used_before + 1,
                _fmt(cap),
            )
            return
        await self._storage.set_qrz_lookup_outcome(now, callsign, result.outcome.value)
        logger.info(
            "QRZ lookup %s -> %s count=%s ledger=%d/%s",
            callsign,
            result.outcome.value,
            _fmt(result.count),
            used_before + 1,
            _fmt(cap),
        )
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
        if result.outcome in {Outcome.REFUSED, Outcome.RATE_LIMITED}:
            await self._refusal_backoff(result, now)
        else:
            await self._backoff(now, result.error or result.outcome.value, 0)

    async def _refusal_backoff(self, result: QrzResponse, now: int) -> None:
        """QRZ said no: a fixed `REFUSAL_BACKOFF_MS`, then a fresh login. The
        spec's "at least 24 hours" for a refusal was never observed to hold, and
        a 24 h suspension is what wedged the service on 2026-10-05; the raw text
        goes into the log so the real duration can be learned from the journal."""
        self._session_key = None
        kind = "refused" if result.outcome is Outcome.REFUSED else "rate limited"
        reason = result.error or ("Connection refused" if kind == "refused" else "rate limited")
        await self._storage.update_qrz_state(
            backoff_until_ms=now + REFUSAL_BACKOFF_MS, last_error=reason, last_error_ms=now
        )
        state = await self._storage.get_qrz_state()
        used = await self._storage.count_qrz_lookups_since(now - WINDOW_MS)
        logger.warning(
            "QRZ %s: error=%r http=%s count=%s ledger=%d/%s tier=%s; backing off %d s",
            kind,
            result.error,
            _fmt(self._last_http_status),
            _fmt(result.count),
            used,
            _fmt(_daily_cap(state["subscription"])),
            _fmt(_account_tier(state["subscription"])),
            REFUSAL_BACKOFF_MS // 1000,
        )

    async def _record_session_info(self, result: QrzResponse) -> None:
        fields: dict[str, Any] = {}
        if result.count is not None:
            fields["server_count"] = result.count
        if result.sub_exp:
            fields["subscription"] = result.sub_exp
        if fields:
            await self._storage.update_qrz_state(**fields)

    async def _observe_login(self, result: QrzResponse, now: int) -> None:
        """Take QRZ's Count at a login as the baseline of its own 24 h tally.

        Only on a free account and only when it can be a tally: QRZ reports
        thousands on some accounts (DM3KS, a subscriber; a free one too since
        2026-10-05), and on 2026-10-05 a Count of exactly the cap met a ledger
        showing 0/50, suspending lookups for 24 h with nothing that ever
        refreshed it. The Count is read here and nowhere else."""
        plausible = _count_plausible(result.count)
        self._login_seen = True
        self._login_count = result.count
        self._server_baseline = (
            result.count if plausible and _is_free_tier(result.sub_exp) else None
        )
        used = await self._storage.count_qrz_lookups_since(now - WINDOW_MS)
        cap = _daily_cap(result.sub_exp)
        remaining = [
            left
            for left in (
                None if cap is None else cap - used,
                self._server_remaining(0),
            )
            if left is not None
        ]
        logger.info(
            "QRZ login ok sub=%s count=%s plausible=%s ledger=%d/%s budget_today=%s",
            result.sub_exp or "none",
            _fmt(result.count),
            "yes" if plausible else "no",
            used,
            _fmt(cap),
            _fmt(max(0, min(remaining)) if remaining else None),
        )

    def _server_remaining(self, since_login: int) -> int | None:
        """Lookups QRZ would still allow today, or None without a usable baseline."""
        if self._server_baseline is None:
            return None
        return SERVER_FREE_LIMIT - (self._server_baseline + since_login)

    def _baseline_expired(self, state: dict[str, Any], now: int) -> bool:
        """The baseline describes QRZ's 24 h tally AT the login; the ledger rows
        counted on top of it never age out, so a session living for days would
        reach baseline + 100 on lookups QRZ itself no longer counts. Past 24 h a
        fresh login re-reads Count (it costs no lookup and obeys the 30 s gate).
        Only where a baseline exists: a subscriber, or an ignored Count, has
        nothing to refresh and keeps its session exactly as before."""
        last_login = state["last_login_ms"]
        return (
            self._server_baseline is not None
            and last_login is not None
            and now - int(last_login) >= WINDOW_MS
        )

    async def _lookups_since_login(self, state: dict[str, Any]) -> int:
        # Ledger rows, not later responses' Count: those may cross the limit.
        if self._server_baseline is None or state["last_login_ms"] is None:
            return 0
        return await self._storage.count_qrz_lookups_since(int(state["last_login_ms"]))

    async def _stop_if_server_budget_used(
        self, state: dict[str, Any], now: int, used: int, cap: int | None
    ) -> bool:
        """Back off, never suspend, when baseline + lookups since login reach QRZ's limit.

        The session is dropped so the next attempt logs in and re-reads Count."""
        since_login = await self._lookups_since_login(state)
        remaining = self._server_remaining(since_login)
        if remaining is None or remaining > 0:
            return False
        reported = state["server_count"]
        self._session_key = None
        await self._storage.update_qrz_state(
            backoff_until_ms=now + REFUSAL_BACKOFF_MS,
            last_error=f"QRZ budget used up (QRZ reports {_fmt(reported)})",
            last_error_ms=now,
        )
        logger.warning(
            "QRZ server budget used up: baseline=%s since_login=%d count=%s ledger=%d/%s;"
            " backing off %d s, then a new login re-reads Count",
            _fmt(self._server_baseline),
            since_login,
            _fmt(reported),
            used,
            _fmt(cap),
            REFUSAL_BACKOFF_MS // 1000,
        )
        return True

    async def _lift_obsolete_suspension(self, state: dict[str, Any], now: int) -> None:
        if not self._suspension_obsolete(state, now):
            return
        count_based = _is_count_suspension(state)
        await self._storage.update_qrz_state(
            suspended_until_ms=None, suspend_reason=None, last_error=None, last_error_ms=None
        )
        state["suspended_until_ms"] = None
        if count_based:
            logger.info("QRZ stale Count suspension lifted: QRZ's Count no longer suspends")
        else:
            logger.info("QRZ budget suspension lifted: subscriber account has no daily limit")

    def _suspension_obsolete(self, state: dict[str, Any], now: int) -> bool:
        """A running suspension that no rule sets any more.

        - set by the retired Count gate, on any account: QRZ's Count suspends
          nothing now (v2.1.6; it wedged a free account for good on 2026-10-05);
        - our ledger cap on a subscriber, who has no daily limit. Written by
          v2.1.1-v2.1.3, which applied both gates to subscribers too."""
        until = state["suspended_until_ms"]
        if not until or int(until) <= now:
            return False
        if _is_count_suspension(state):
            return True
        if _is_free_tier(state["subscription"]):
            return False
        reason = state["suspend_reason"]
        if reason is not None:
            return bool(reason == SUSPEND_CAP)
        # Written before `suspend_reason` existed (v2.1.1/v2.1.2): infer it.
        # A replaced password clears `last_error` but not the suspension —
        # DM3KS's exact state — and then only the budget gates or a refusal
        # can have set it. A refusal hidden that way costs one login that QRZ
        # refuses again, never a lookup.
        error = state["last_error"]
        if error is None:
            return True
        return str(error).startswith(_CAP_REASON)

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
