"""Web Push delivery internals (Wave 5, PWA campaign): the pure matcher and
payload builder, the per-subscription coalescing state machine, VAPID
keypair persistence, and the background dispatcher that performs the actual
pywebpush delivery in isolation from the mesh-message ingest path.

See `src/mcapp/contract/push_contract.json` (byte-verbatim copy of the wire
contract also implemented by the mc-chat sibling) for match/coalesce/prune
semantics; `push_tests.py` runs every vector in it against this module.

Execution isolation (contract `execution_isolation`): `PushDispatcher.handle_mesh_message`
is the coroutine subscribed to `MessageRouter`'s "mesh_message" topic (wired in
`sse_routes/push.py`). It performs ONE local SQLite read (fast, not the network
call this isolation protects against) plus pure in-memory matching/coalescing,
and never awaits push delivery itself — it only enqueues. `_drain_loop` (a
background task) is the sole place that calls the injectable `webpush_fn`, via
`asyncio.to_thread` with explicit connect/read timeouts, so a no-internet Pi
Zero 2W cannot stall the event loop or SSE heartbeats on an unreachable push
service.

Testability seams (contract): `now()` is an injectable clock (never real
wall-clock in tests) and `webpush_fn` is an injectable callable (never real
pywebpush in tests). `generate_vapid_keypair` is the only function that
performs real EC keygen; tests inject a fake generator into
`load_or_create_vapid` instead of calling it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import os
import re
import stat
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pywebpush import WebPushException
from pywebpush import webpush as _real_webpush

from .linkcheck import is_link_check_payload
from .logging_setup import get_logger
from .storage.constants import DEDUP_WINDOW_MS
from .util import strip_ack_suffix

logger = get_logger(__name__)

COALESCE_WINDOW_SECONDS = 5.0
# Reuse the storage layer's own dedup window (contract `dedup`: "this is why
# storage dedups on a ~60-min window" — push must fire at most once per
# logical message over the SAME horizon).
DEDUP_WINDOW_SECONDS = DEDUP_WINDOW_MS / 1000
PUSH_CONNECT_TIMEOUT_S = 3.0
PUSH_READ_TIMEOUT_S = 5.0
PRUNE_STATUS_CODES = frozenset({401, 403, 404, 410})
MAX_TEXT_LEN = 120
SWEEP_INTERVAL_S = 0.5
QUEUE_MAXSIZE = 1000
DEFAULT_VAPID_SUBJECT = "mailto:admin@example.com"

VAPID_PATH = Path("/var/lib/mcapp/vapid.json")


def vapid_path() -> Path:
    """Where the VAPID keypair is persisted.

    `MESHCOM_VAPID_PATH` overrides everything — the escape hatch for a packaging
    layout that puts state somewhere else. Otherwise production writes to
    `/var/lib/mcapp` (the systemd StateDirectory) and a dev machine, which has no
    business creating a root-owned directory, writes under the user's state dir.
    """
    override = os.getenv("MESHCOM_VAPID_PATH")
    if override:
        return Path(override)
    if os.getenv("MCAPP_ENV") == "dev":
        return user_state_dir() / "vapid.json"
    return VAPID_PATH


def user_state_dir() -> Path:
    """Per-user state directory (XDG_STATE_HOME, else ~/.local/state/mcapp)."""
    xdg = os.getenv("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "mcapp"


# ── dst/src resolution (contract `dst_resolution` / `src_resolution`) ──────
# On a LoRa mesh, relay-hopped frames are the COMMON case: dst is
# 'VIA[,VIA2],TARGET' and src is 'SRC,VIA[,VIA2]' (opposite order). Mirrors
# storage/constants.py:compute_conversation_key's target extraction
# (`dst.rsplit(",", maxsplit=1)[-1].strip()`) rather than calling that
# function directly — compute_conversation_key returns a sorted DM-pair key
# (SSIDs stripped), which is the wrong shape for push matching; push needs
# the raw resolved target/source, SSID intact, to compare against
# `own`/`filter.groups`.


def _resolve_target(dst: str) -> str:
    """Resolve a possibly via-routed dst to its real target: the LAST
    comma-separated component, trimmed. A dst with no comma is its own
    target."""
    return (dst or "").rsplit(",", maxsplit=1)[-1].strip()


def _resolve_source(src: str) -> str:
    """Resolve a possibly via-routed src to its real sender: the FIRST
    comma-separated component, trimmed. SSID is kept (contract
    `src_resolution`)."""
    return (src or "").split(",", maxsplit=1)[0].strip()


# ── Pure payload builder + eligibility + matcher ────────────────────────────

# contract `payload_ack_suffix_semantics`: strip the firmware's trailing
# ack-request suffix (`{NNN`, no closing brace) and trim, via the shared
# definition in util. STRICT by contract: a trailing `{NNN}` is ordinary chat
# text, and mc-chat's looser `strip_ack_request` must NOT be substituted here --
# it would reduce a `{pong}{451010884}` link-check frame to `{pong}`, which
# eligibility clause (d) does not recognise, reopening the v5 bug.


def _payload_fields(raw_message: dict[str, Any], text: str) -> dict[str, Any]:
    """Shared field assembly for `build_push_payload` and `_build_gate_view`:
    defaulted type, raw (unresolved) src/dst, the given `text` truncated to
    MAX_TEXT_LEN, msg_id, and timestamp converted from epoch ms to epoch s
    when numeric. The only difference between the two callers is whether
    `text` was run through `strip_ack_suffix` first — factored out so that
    difference can never accidentally drift into a second one.
    """
    ts = raw_message.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        ts = ts / 1000
    return {
        "type": raw_message.get("type", "msg"),
        "src": raw_message.get("src", ""),
        "dst": raw_message.get("dst", ""),
        "text": text[:MAX_TEXT_LEN],
        "msg_id": raw_message.get("msg_id"),
        "ts": ts,
    }


def build_push_payload(raw_message: dict[str, Any]) -> dict[str, Any]:
    """Translate a raw mesh-ingest message (src/dst/msg-or-text/type/msg_id/
    timestamp) into the contract's push payload shape (type/src/dst/text/
    msg_id/ts) — contract `payload_schema`.

    `src`/`dst` are carried through RAW (not resolved) per payload_schema —
    resolution happens at eligibility/match time, not in the stored payload.
    `text` is `str(msg.get('msg') or msg.get('text') or '')`, with the
    firmware's ack-request suffix stripped (`strip_ack_suffix`, contract
    `payload_ack_suffix_semantics`) BEFORE truncation to MAX_TEXT_LEN chars —
    stripping first means the cap always carries 120 chars of real text and
    truncation can never split the suffix into a bare '{'. `ts` is converted
    from epoch milliseconds to epoch SECONDS when `timestamp` is numeric; a
    non-numeric value passes through unchanged.

    This builds the DELIVERED payload only. `PushDispatcher.handle_mesh_message`
    must gate (eligibility/blocklist/dedup) on the UNSTRIPPED text first — see
    `_build_gate_view` — and call this function only after every gate passes.
    """
    text = str(raw_message.get("msg") or raw_message.get("text") or "")
    return _payload_fields(raw_message, strip_ack_suffix(text))


def _build_gate_view(raw_message: dict[str, Any]) -> dict[str, Any]:
    """Gate-only payload view for eligibility/blocklist/dedup (contract
    `payload_ack_suffix_semantics`, rule 2): reproduces `build_push_payload`'s
    extraction/defaulting/ts-conversion/truncation exactly, via the same
    shared `_payload_fields`, but does NOT strip the firmware ack-request
    suffix — the gates must see the message as it arrived on the wire.

    Three reasons this view is separate from the delivered payload, in order
    of how quietly each one bites: a stripped '{ping}{087' collapses to
    '{ping}', which eligibility clause (d) would then only catch because ping
    recognition happens to be a prefix check rather than equality — an
    implementation-detail dependency, not a design one. Stripping before the
    blocklist gate changes nothing (the gate keys on `src`, never `text`) but
    is kept unstripped anyway for uniformity with the other two gates.
    Stripping before dedup would widen its msg_id-less (resolved-src,
    resolved-dst, text) fallback key, silently collapsing two distinct
    messages that differed only in their ack counter into one.

    Used ONLY by `PushDispatcher.handle_mesh_message` to build the
    eligibility/blocklist/dedup gate view; the delivered payload is built
    separately, strictly AFTER every gate has passed, via `build_push_payload`
    (which DOES strip).
    """
    text = str(raw_message.get("msg") or raw_message.get("text") or "")
    return _payload_fields(raw_message, text)


def _push_text(payload: dict[str, Any]) -> str:
    """The msg-or-text fallback extraction, coerced exactly as
    `build_push_payload` does it. Shared by eligibility clauses (a) and (c) so
    both judge the same string.

    In production the caller has always run `build_push_payload` first, so
    `text` is already populated and `msg` is absent — the fallback matters only
    for the contract fixture's own vectors, which spell some frames with a `msg`
    key. Reading both keeps this predicate's verdict identical to mc-chat's on
    every vector instead of merely coincidentally equal (a `msg`-only ACK used
    to be rejected here by clause (a) for having no `text`, and by clause (c)
    upstream — same answer, different reason, one refactor away from diverging).
    """
    return str(payload.get("text") or payload.get("msg") or "")


# Contract v8: the firmware answers every `--` command with an ordinary text
# frame from this literal pseudo-callsign (lower-case, no SSID, never
# via-routed), dst "*". `storage/ingest._should_filter_message` refuses to
# persist it with the same exact comparison; the push dispatcher subscribes to
# the raw router topics, not to storage, so without this rule every
# broadcast:true subscriber got a notification reading "--ackinfo on" after
# each BLE reconnect (RX-03, doc/2026-09-10_1900-ble-protocol-parity-audit.md).
# Compared against the RAW src, exact and case-sensitive: "RESPONSE-1" is a
# valid amateur callsign and stays eligible.
_COMMAND_REPLY_PSEUDO_CALL = "response"

# Contract `eligibility_noise_semantics`: the node-local reply texts, all in the
# firmware's padded-callsign layout (`%-9.9s:ack%03i` and its siblings).
# `":ack"` since contract v4; `":rej"` and `":sto"` since v10 — the
# store-and-forward replies a firmware WITHOUT the 0x41 status frame emits as
# ordinary DMs (`"DK5EN-93 :sto017 DK5EN-14"`). Substring tests, deliberately
# broader than the strict `:ack[0-9]` history predicate; the contract is the
# authority on why.
_NOISE_MARKERS = (":ack", ":rej", ":sto")


def _is_node_local_noise(payload: dict[str, Any]) -> bool:
    """Contract `eligibility` (c) / `eligibility_noise_semantics`: True iff the
    message is a node-local reply text (`:ack`, and since contract v10 `:rej` /
    `:sto`), a `{CET}` time broadcast, or (contract v8) a firmware command reply
    from the `response` pseudo-callsign.

    All of them arrive as ordinary `type:"msg"` text frames, so clause (a)
    passes them.
    Without this, the mesh's `"<CALL>  :ackNNN"` reply to every outbound message
    meant the operator got one notification per message they SENT (default
    `filter.dm = true`), and every `broadcast: true` subscriber was notified for
    each `{CET}` time broadcast — which `storage._should_filter_message` refuses
    to even persist.

    The `":ack"` substring test is push_contract-governed and deliberately
    broad — broader than the strict ack marker every other predicate now uses
    (case-sensitive ':ack' + ASCII digit; ack_predicate_vectors.json v2, and
    `storage/query.py`'s `msg NOT GLOB '*:ack[0-9]*'` exclusion). Residual,
    deliberate width difference: a bare ':ack' with no digit is push-silent
    here but IS history-visible as an ordinary message. A push must never
    announce a message that no conversation view will show — the converse
    (this one visible-but-silent case) is the accepted cost. See the
    contract's `eligibility_noise_semantics` for the accepted false positive.

    `":sto"` (contract v10) is history-visible ON PURPOSE and must stay that
    way: behind a node without the 0x41 status frame it is the only signal the
    operator gets that a store node is holding the DM, and the firmware's
    client guide forbids filtering it silently. `storage/query.py`'s exclusion
    matches `:ack[0-9]` only, so nothing here needs to change to keep it
    visible — but it does make `:sto` another deliberate push-silent yet
    view-visible case, which is the accepted direction, never the forbidden one
    (a push for a message no view will show).
    """
    if payload.get("src") == _COMMAND_REPLY_PSEUDO_CALL:
        return True
    text = _push_text(payload)
    return any(marker in text for marker in _NOISE_MARKERS) or text.startswith("{CET}")


def is_eligible(payload: dict[str, Any], own_callsign: str) -> bool:
    """Pure predicate: contract `eligibility`. Checked ONCE per message,
    before dedup and before per-subscription matching.

    (a) must be a chat text message — type == "msg" AND non-empty text
        (excludes telemetry ('tele'), position beacons ('pos'), any other
        non-chat type, and any no-text frame — a type:"msg" with empty text
        must not push a blank notification).
    (b) resolved source (first comma-component of src) must NOT be the
        node's own callsign — never push our own outbound sends or
        mesh-echoes of them.
    (c) must not be node-local noise — a text ACK or a `{CET}` time
        broadcast. This lived at the router wiring seam until contract v4
        made the exclusion universal; see `_is_node_local_noise`.
    (d) must not be a `{ping}`/`{pong}` link-check protocol frame
        (contract v5, `eligibility_linkcheck_semantics`). The SAME
        predicate the storage guard in front of `_insert_message_row`
        uses (`linkcheck.is_link_check_payload`), so push and message
        history agree by construction — the push dispatcher subscribes to
        the router topics, not to storage, so without this clause it
        announced raw `{pong}{451010884}` frames that no conversation
        view will ever show.
    (e) must not carry the firmware's `app_offline` flag (contract v9,
        `eligibility_app_offline_semantics`). Byte 5 of a BLE data frame
        is a bitfield; the firmware sets 0x20 on every frame it replays
        from its catch-up ring after a reconnect, on command replies and
        on back-pressure notices, and the official app suppresses the
        notification for it. `ble_protocol.decode_binary_message` emits it
        as the boolean `app_offline`; a UDP datagram has no such byte and
        never sets the key. Boolean True only, never merely truthy (RX-04,
        doc/2026-09-10_1900-ble-protocol-parity-audit.md).
    """
    if payload.get("type") != "msg" or not _push_text(payload):
        return False
    if _is_node_local_noise(payload):
        return False
    if is_link_check_payload(_push_text(payload)):
        return False
    if payload.get("app_offline") is True:
        return False
    resolved_src = _resolve_source(str(payload.get("src") or ""))
    return resolved_src != own_callsign


def is_sender_blocked(payload: dict[str, Any], blocked_callsigns: set[str]) -> bool:
    """Pure predicate: contract `blocklist`. Checked ONCE per message,
    together with eligibility and BEFORE dedup and per-subscription matching.

    Returns True (suppress: zero pushes, no coalesce window opened or fed)
    iff the message's resolved source (src_resolution — first comma-component,
    upper-cased) is an element of the node's GLOBAL blocked_callsigns set,
    compared case-insensitively. A message merely RELAYED THROUGH a blocked
    node (the blocked callsign is a via-hop, not the first comma-component) is
    NOT suppressed — only messages *originated* by a blocked callsign are.
    """
    resolved_src = _resolve_source(str(payload.get("src") or ""))
    return resolved_src.upper() in {c.upper() for c in blocked_callsigns}


# ── @-mention matching (contract v11 `mention_semantics`) ───────────────────
#
# (^|[^A-Za-z0-9])@BASE(-[0-9]{1,2})?([^A-Za-z0-9]|$), matched CASE-SENSITIVELY
# against the UPPERCASED text with an UPPERCASED BASE (own_callsign with any
# trailing '-SSID' removed). Deliberately LOOKAROUND-FREE — do not "simplify"
# this to a lookbehind or to `\b`. Two reasons, both load-bearing: (1) the
# lookbehind form (`(?<![A-Za-z0-9])`) is unsupported before Safari 16.4, and
# the webapp's filter module compiles its regex at load time, so a lookbehind
# here would throw SyntaxError and take the whole module down for an older
# iOS PWA, not just this clause. (2) matching case-INsensitively instead of
# uppercasing first would make the ASCII boundary classes stop being ASCII —
# Python's re.IGNORECASE also folds U+017F/U+212A into [A-Za-z0-9], and
# JavaScript's /i without /u disagrees again — so two independent
# implementations would silently diverge on exotic Unicode input.
# Uppercase-then-compare-case-sensitively is exactly reproducible everywhere.
#
# The leading boundary class keeps an e-mail address ('sam@dk5en.de') from
# mentioning whoever the local part spells; the trailing one keeps a longer,
# different callsign ('@DK5ENX', '@DK5EN2') from mentioning `own`. The
# optional '-SSID' is matched but never compared — the operator is the
# station, not the SSID. Two shapes fall out of the pattern's mechanics
# rather than any stated rule: '@DK5EN-123' matches (the SSID group's {1,2}
# cannot consume all three digits, so it backtracks to matching nothing, and
# the literal '-' itself then satisfies the trailing boundary class);
# '@@DK5EN' matches (the first '@' satisfies the leading boundary class, the
# second '@' is the literal preceding BASE).
_MENTION_PATTERN_TEMPLATE = r"(^|[^A-Za-z0-9])@{base}(-[0-9]{{1,2}})?([^A-Za-z0-9]|$)"


@functools.lru_cache(maxsize=32)
def _compiled_mention_pattern(base_callsign_upper: str) -> re.Pattern[str]:
    """Compiled once per distinct BASE (own_callsign is fixed for the
    lifetime of a running node, so this is effectively compiled once in
    production, never per call). Case-SENSITIVE by construction — callers
    uppercase both the text and BASE before matching; see `mentions_own`."""
    return re.compile(_MENTION_PATTERN_TEMPLATE.format(base=re.escape(base_callsign_upper)))


def _base_callsign(callsign: str) -> str:
    """`callsign` with any trailing '-SSID' suffix removed."""
    return callsign.split("-", 1)[0]


def mentions_own(text: str, own_callsign: str) -> bool:
    """Pure predicate: contract v11 `mention_semantics`. True iff `text`
    mentions `own_callsign` — an '@' followed by own_callsign's BASE (SSID
    stripped), optionally followed by '-SSID', bounded on both sides so
    neither an e-mail's local part nor a longer/different callsign counts.
    See `_MENTION_PATTERN_TEMPLATE` above for the pattern and why it is
    lookaround-free.

    An absent/empty `text` never matches. If BASE is empty after stripping
    the SSID (a malformed `own_callsign`), nothing ever matches.
    """
    if not text:
        return False
    base = _base_callsign(own_callsign)
    if not base:
        return False
    return _compiled_mention_pattern(base.upper()).search(text.upper()) is not None


def matches(payload: dict[str, Any], own_callsign: str, filt: dict[str, Any], text: str) -> bool:
    """Pure predicate: contract `match_semantics` (mentions: contract v11).

    First resolve `dst` to `target` per dst_resolution, then push IFF
    ( target == own AND filter.dm ) OR ( target == '*' AND filter.broadcast )
    OR ( target is an element of filter.groups, compared as trimmed strings )
    OR ( filter.mentions AND `text` mentions `own` per `mentions_own` ).
    The mentions clause is an INDEPENDENT disjunct, never a narrowing of the
    other three — a message can push on a mention alone regardless of
    dm/broadcast/groups.

    `text` MUST be the FULL, untruncated message text (contract
    `mention_semantics`) — never `payload["text"]`, which `_payload_fields`
    truncates to `MAX_TEXT_LEN` (120) chars. Callers pass the same raw
    msg-or-text extraction the gates already use
    (`raw_message.get("msg") or raw_message.get("text")`), taken BEFORE
    truncation — see `PushDispatcher.handle_mesh_message`, which computes it
    once per message and passes it through to every subscription's match
    check.
    """
    target = _resolve_target(str(payload.get("dst") or ""))
    if target == own_callsign and filt.get("dm", True):
        return True
    if target == "*" and filt.get("broadcast", False):
        return True
    groups = filt.get("groups") or []
    trimmed_groups = {str(g).strip() for g in groups}
    if target in trimmed_groups:
        return True
    return bool(filt.get("mentions", False)) and mentions_own(text, own_callsign)


# ── Coalescing state machine (contract `coalesce`) ──────────────────────────


class PushCoalescer:
    """Per-subscription-endpoint coalescing state machine.

    Pure and synchronous; driven entirely by an injectable `now()` clock so
    tests never touch real wall-clock time.

    `submit()` is called inline from the (non-blocking) mesh-message handler
    for every matching message. `pop_expired()` is called by a periodic
    background sweep in production (`PushDispatcher._sweep_loop`, real
    wall-clock polling) or explicitly by tests after advancing the fake clock.
    """

    def __init__(self, window_seconds: float, now: Callable[[], float]) -> None:
        self._window_seconds = window_seconds
        self._now = now
        self._windows: dict[str, dict[str, Any]] = {}

    def submit(
        self, endpoint: str, sub: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Record a matching message for `endpoint`.

        Returns the payload to push immediately if no window was open for
        this endpoint (and opens a fresh window); returns None if a window
        was already open (the message is buffered instead).
        """
        window = self._windows.get(endpoint)
        if window is None:
            self._windows[endpoint] = {
                "closes_at": self._now() + self._window_seconds,
                "buffer": [],
                "sub": sub,
            }
            return payload
        window["buffer"].append(payload)
        window["sub"] = sub
        return None

    def pop_expired(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Close every window whose `closes_at` has passed; return
        `(sub, summary)` for each closed window with a non-empty buffer
        (contract: exactly one summary push per closed window, carrying
        `{count, latest}`). Windows with an empty buffer are dropped silently.
        """
        now = self._now()
        due_endpoints = [ep for ep, win in self._windows.items() if now >= win["closes_at"]]
        results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for endpoint in due_endpoints:
            window = self._windows.pop(endpoint)
            if window["buffer"]:
                summary = {"count": len(window["buffer"]), "latest": window["buffer"][-1]}
                results.append((window["sub"], summary))
        return results


# ── Dedup guard (contract `dedup`) ──────────────────────────────────────────


class PushDedup:
    """Bounded, time-pruned dedup guard for the push path.

    A logical message may reach the ingest handler more than once (multiple
    gateways hearing the same LoRa frame, UDP retransmits) — the same reason
    storage itself dedups on a ~60-min window (`storage/constants.py:DEDUP_WINDOW_MS`).
    Checked ONCE per message, before matching/coalescing: a duplicate must
    neither produce a second immediate push nor increment a coalesce summary
    count for ANY subscription, so this guard is global (per-dispatcher), not
    per-subscription like `PushCoalescer`.

    Dedup key: `msg_id` within the window when truthy, else the tuple
    (resolved-src, resolved-dst, text). Driven by the same injectable `now()`
    clock as the coalescer — never real wall-clock in tests.
    """

    def __init__(self, window_seconds: float, now: Callable[[], float]) -> None:
        self._window_seconds = window_seconds
        self._now = now
        # OrderedDict, and a hit never refreshes its timestamp, so insertion order IS
        # expiry order — _prune() can stop at the first live entry instead of walking
        # the whole map. The window is an hour (DEDUP_WINDOW_MS), so the old O(n)
        # comprehension rebuilt a throwaway list over an hour of traffic on EVERY
        # inbound chat frame, even with zero push subscriptions.
        self._seen: OrderedDict[Any, float] = OrderedDict()

    def _prune(self) -> None:
        cutoff = self._now() - self._window_seconds
        while self._seen:
            key, seen_at = next(iter(self._seen.items()))
            if seen_at >= cutoff:
                break
            del self._seen[key]

    def is_duplicate(self, payload: dict[str, Any]) -> bool:
        """Return True if this message's dedup key was already seen within
        the window (and do NOT re-record it); otherwise record it as newly
        seen and return False."""
        self._prune()
        msg_id = payload.get("msg_id")
        key: Any
        if msg_id:
            key = ("id", msg_id)
        else:
            resolved_src = _resolve_source(str(payload.get("src") or ""))
            resolved_dst = _resolve_target(str(payload.get("dst") or ""))
            key = ("triple", resolved_src, resolved_dst, payload.get("text"))
        if key in self._seen:
            return True
        self._seen[key] = self._now()
        return False


# ── VAPID keypair generation + persistence ──────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_vapid_keypair(subject: str = DEFAULT_VAPID_SUBJECT) -> dict[str, str]:
    """Generate a fresh VAPID (P-256) keypair via py_vapid.

    REAL crypto — only ever called at first real use via `load_or_create_vapid`'s
    default `generator`. Tests inject a fake generator instead so this function
    is never exercised in the suite.

    Persists the private key as the raw base64url-encoded 32-byte scalar (not
    PEM) — this is exactly the string form `pywebpush.webpush`'s own
    `Vapid.from_string` round-trips via `Vapid.from_raw`, so delivery can pass
    it straight through without any extra reconstruction step.
    """
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415 - ditto
    from py_vapid import Vapid  # noqa: PLC0415 - real crypto, only touched at first real use

    vapid = Vapid()
    vapid.generate_keys()
    raw_private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    raw_public = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "private_key": _b64url(raw_private),
        "public_key": _b64url(raw_public),
        "subject": subject,
    }


def _with_subject_override(keypair: dict[str, str]) -> dict[str, str]:
    """Apply `MESHCOM_VAPID_SUB` to an already-built keypair.

    Applied on LOAD, not only on generation, and that is the point: the symptom
    this override exists for is Apple returning 403 `BadJwtToken` for a `sub`
    with no TLD. That is discovered on an install whose keypair already exists,
    and regenerating one to change a claim would invalidate every stored
    subscription. The `sub` is a JWT claim, not key material.
    """
    override = os.getenv("MESHCOM_VAPID_SUB")
    if override and keypair.get("subject") != override:
        return {**keypair, "subject": override}
    return keypair


def load_or_create_vapid(
    path: Path | None = None,
    generator: Callable[[], dict[str, str]] = generate_vapid_keypair,
) -> dict[str, str]:
    """Load the persisted VAPID keypair, generating + persisting one on first
    use so it survives slot swaps (contract `vapid`: generated once per
    install, never committed).

    `path` defaults to `vapid_path()`, resolved per CALL rather than baked in at
    import so `MESHCOM_VAPID_PATH` / `MCAPP_ENV` are actually honoured.

    `generator` is an injectable seam — tests pass a fake so real EC crypto
    never runs in the suite.

    NEVER raises. This runs inside `build_app()` (via `_create_app` →
    `build_push_router`) with no try/except anywhere on the path, so any exception
    here took down the ENTIRE proxy — UDP ingest, BLE, SSE — not just Web Push. A
    truncated `vapid.json` (power loss mid-write on a Pi with no fsync) or an
    unwritable state directory used to mean `mcapp.service` never started again,
    with nothing in the log pointing at push. Push degrades to "no delivery" instead.
    """
    if path is None:
        path = vapid_path()

    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("VAPID keyfile at %s is unreadable/corrupt; regenerating", path)
        else:
            if isinstance(loaded, dict) and loaded.get("private_key") and loaded.get("public_key"):
                _tighten_vapid_mode(path)
                return _with_subject_override(cast("dict[str, str]", loaded))
            logger.warning("VAPID keyfile at %s has an unexpected shape; regenerating", path)

    try:
        keypair = generator()
    except Exception:
        logger.exception("VAPID keypair generation failed; Web Push disabled for this run")
        return {"private_key": "", "public_key": "", "subject": DEFAULT_VAPID_SUBJECT}

    keypair = _with_subject_override(keypair)

    if _persist_vapid(path, keypair):
        return keypair

    # The preferred location is unwritable. Before giving up, try the per-user
    # state dir: an EPHEMERAL key is the worst outcome, because it changes on
    # every restart and silently invalidates every stored push subscription.
    # A stable key in a second-choice location keeps push working; the warning
    # above is what tells an operator the primary path needs fixing.
    fallback = user_state_dir() / path.name
    if fallback != path and _persist_vapid(fallback, keypair):
        logger.warning("Persisted VAPID keypair to %s instead", fallback)
        return keypair

    logger.warning(
        "VAPID keypair is EPHEMERAL — it changes on restart and every existing "
        "push subscription will stop delivering. Fix write access to %s.",
        path.parent,
    )
    return keypair


def _tighten_vapid_mode(path: Path) -> None:
    """Narrow an existing keyfile to 0600 if it is wider.

    The chmod on the write path only ever applied to files this code CREATED,
    so a keyfile written before that chmod existed kept its 0644 for good —
    which is precisely the exposure the write path guards against: a raw P-256
    private scalar readable by any local account, enough to forge VAPID JWTs
    authenticating as this node. Checked on every load so an already-deployed
    install repairs itself rather than waiting for a key regeneration that
    would invalidate every stored subscription.
    """
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & ~(stat.S_IRUSR | stat.S_IWUSR):
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            logger.warning("Tightened VAPID keyfile %s from %o to 0600", path, current)
    except OSError as exc:
        logger.warning("Could not tighten permissions on VAPID keyfile %s: %s", path, exc)


def _persist_vapid(path: Path, keypair: dict[str, str]) -> bool:
    """Write the keypair to `path` (0600). True on success.

    Logs a message, NOT a traceback: an unwritable state directory is an
    environment condition with a self-explanatory errno, and the stack frames
    only bury the one line an operator needs.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(keypair), encoding="utf-8")
        # 0600: this is a raw P-256 private scalar. At the default 0644 any local
        # account could read it and forge VAPID JWTs authenticating as this node.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        logger.warning("Could not persist VAPID keypair to %s: %s", path, exc)
        return False
    return True


# ── Background dispatcher: the ONLY place that calls webpush_fn ────────────


class PushDispatcher:
    """Background delivery engine: drains an in-process queue via
    `asyncio.to_thread` webpush calls, isolated from the mesh-message ingest
    path (contract `execution_isolation`).

    `storage` must expose the push_subscriptions CRUD added to
    `SQLiteStorage`: `list_push_subscriptions`, `delete_push_subscription`.
    """

    def __init__(  # noqa: PLR0913 - every field is an independent testability seam (contract)
        self,
        *,
        storage: Any,
        vapid: dict[str, str],
        webpush_fn: Callable[..., Any] = _real_webpush,
        now: Callable[[], float] = time.monotonic,
        connect_timeout: float = PUSH_CONNECT_TIMEOUT_S,
        read_timeout: float = PUSH_READ_TIMEOUT_S,
        coalesce_window_seconds: float = COALESCE_WINDOW_SECONDS,
        dedup_window_seconds: float = DEDUP_WINDOW_SECONDS,
        sweep_interval_seconds: float = SWEEP_INTERVAL_S,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self._storage = storage
        self._vapid = vapid
        self._webpush_fn = webpush_fn
        self._now = now
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sweep_interval = sweep_interval_seconds
        self.coalescer = PushCoalescer(coalesce_window_seconds, now)
        self.dedup = PushDedup(dedup_window_seconds, now)
        self._queue: asyncio.Queue[tuple[dict[str, Any], dict[str, Any]]] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._drain_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the background drain + sweep tasks. Idempotent."""
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain_loop())
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        """Cancel the background tasks.

        Called from `SSEManager.stop_server()` (which the shutdown ladder reaches at
        step 4) as well as by tests. Previously nothing in production called this, so
        both perpetual tasks were still pending at process exit — a drain task could be
        mid-`asyncio.to_thread(webpush)` when the loop was torn down.
        """
        for task in (self._drain_task, self._sweep_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._drain_task = None
        self._sweep_task = None

    async def handle_mesh_message(
        self,
        raw_message: dict[str, Any],
        own_callsign: str,
        blocked_callsigns: set[str] | None = None,
    ) -> None:
        """Subscriber for `MessageRouter`'s "mesh_message" topic.

        Fast and non-blocking: one local SQLite read (`list_push_subscriptions`,
        not the network call execution-isolation protects against) plus
        in-memory eligibility/blocklist/dedup/matching/coalescing. NEVER awaits
        push delivery itself — a matching message only reaches the network via
        `_enqueue` + `_drain_loop`, decoupled by `self._queue`.

        `blocked_callsigns` is the node's GLOBAL blocklist (admin kickban +
        curated sperrliste); the caller (`sse_routes/push.py`) sources it live
        from the commands protocol. Omitted / None => the gate is inert.

        Order (contract): eligibility + blocklist once (together), then dedup
        once, then per-subscription match+coalesce — an ineligible, blocked,
        or duplicate message never opens/feeds ANY subscription's coalesce
        window.

        contract `payload_ack_suffix_semantics` (rule 2): the gates run on
        `_build_gate_view` (UNSTRIPPED text) and `build_push_payload` (the
        STRIPPED, delivered payload) is only constructed after every gate has
        passed — do not reorder this: stripping first would widen dedup's
        msg_id-less fallback key. See `_build_gate_view`'s docstring.

        contract v11 `mention_semantics`: the mention test needs the FULL,
        untruncated message text — `_payload_fields` (used by both the gate
        view and the delivered payload) truncates to `MAX_TEXT_LEN` (120)
        chars, and a mention starting past that cap would otherwise silently
        never fire. `full_text` is the same raw msg-or-text extraction as
        `build_push_payload`, computed here BEFORE `strip_ack_suffix` and
        BEFORE truncation, and passed to every subscription's `matches()`
        check below.
        """
        if not own_callsign:
            return
        gate_view = _build_gate_view(raw_message)
        full_text = str(raw_message.get("msg") or raw_message.get("text") or "")
        if not is_eligible(gate_view, own_callsign):
            return
        # contract `blocklist`: gate on the node's GLOBAL blocked_callsigns set
        # together with eligibility and BEFORE dedup/matching — a blocked
        # sender's message produces zero pushes, consumes no dedup slot, and
        # opens no coalesce window (return before touching either).
        if is_sender_blocked(gate_view, blocked_callsigns or set()):
            return
        if self.dedup.is_duplicate(gate_view):
            return
        # Gates passed: NOW build the delivered (ack-suffix-stripped) payload.
        payload = build_push_payload(raw_message)
        subs = await self._storage.list_push_subscriptions()
        for sub in subs:
            if not matches(payload, own_callsign, sub["filter"], full_text):
                continue
            immediate = self.coalescer.submit(sub["endpoint"], sub, payload)
            if immediate is not None:
                self._enqueue(sub, immediate)

    def _enqueue(self, sub: dict[str, Any], item: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((sub, item))
        except asyncio.QueueFull:
            logger.warning(
                "push queue full (maxsize=%d), dropping delivery for endpoint=%s",
                self._queue.maxsize,
                sub.get("endpoint"),
            )

    async def _sweep_loop(self) -> None:
        """Real-time-driven window closer. Polls at SWEEP_INTERVAL_S — tests
        never exercise this loop, they drive `coalescer.pop_expired()` directly
        against a fake clock instead (see module docstring)."""
        while True:
            await asyncio.sleep(self._sweep_interval)
            # Guarded like _drain_loop: an unhandled exception here killed the task
            # permanently and SILENTLY, so every coalesced summary (the 2nd..Nth message
            # inside a window) was buffered and never delivered again until restart while
            # immediate pushes kept working — the symptom looked like "coalescing broke".
            try:
                for sub, summary in self.coalescer.pop_expired():
                    self._enqueue(sub, summary)
            except Exception:
                logger.exception("push coalesce sweep failed; continuing")

    async def _drain_loop(self) -> None:
        while True:
            sub, item = await self._queue.get()
            try:
                await self._deliver_one(sub, item)
            except Exception:
                logger.exception("push delivery failed for endpoint=%s", sub.get("endpoint"))

    async def _deliver_one(self, sub: dict[str, Any], item: dict[str, Any]) -> None:
        """The ONLY place that calls the (possibly network-blocking)
        `webpush_fn` — via `asyncio.to_thread` with explicit connect/read
        timeouts, so the event loop is never stalled by a slow/unreachable
        push service (contract `execution_isolation`).
        """
        claims = {"sub": self._vapid.get("subject", DEFAULT_VAPID_SUBJECT)}
        try:
            await asyncio.to_thread(
                self._webpush_fn,
                subscription_info=sub["subscription"],
                data=json.dumps(item),
                vapid_private_key=self._vapid["private_key"],
                vapid_claims=claims,
                timeout=(self._connect_timeout, self._read_timeout),
            )
        except WebPushException as exc:
            status = _status_code(exc)
            if status in PRUNE_STATUS_CODES:
                logger.info(
                    "pruning push subscription endpoint=%s after status=%s",
                    sub.get("endpoint"),
                    status,
                )
                await self._storage.delete_push_subscription(sub["endpoint"])
            else:
                logger.warning("push delivery error (status=%s): %s", status, exc)


def _status_code(exc: WebPushException) -> int | None:
    """Extract the HTTP status code from a WebPushException's response,
    whichever way pywebpush (a real `requests.Response`) or a test double
    (any object exposing `.status_code`) attached it."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None
