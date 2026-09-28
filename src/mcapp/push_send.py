"""Web Push HTTP delivery without `pywebpush` (B4.3 / Wave W3).

`pywebpush.__init__` imports `aiohttp` and `requests` at module top for a
transport layer this project never uses beyond one `POST` per delivery — that
import alone cost +13.4 MB RSS measured on the dev Mac (the Pi Zero 2W figure
in the 2026-09-15 B4 note is +12.5 MB; see `doc/2026-09-28_1035-backlog-
review-verdict-and-plan.md` B4.3). Its crypto dependencies are the actual
value and stay: `http_ece` (RFC 8291 `aes128gcm`) and `py_vapid` (RFC 8292
VAPID JWT) are reused here UNCHANGED — nothing here hand-rolls ECDH, HKDF,
AES-GCM or JWT signing. Only the HTTP POST moves from `requests` to the
already-production `httpx`.

`webpush()` reproduces `pywebpush.webpush()`'s (2.5.0) production request:
same VAPID claim defaults (`aud`, `exp` +12h, `sub` passthrough) and
`Vapid02` `Authorization: vapid t=<jwt>,k=<pubkey>` header, same
`http_ece.encrypt(..., version="aes128gcm")` body (self-describing per RFC
8188, so no `Crypto-Key`/`Encryption` headers), same `content-encoding`/`ttl`
headers, no `Content-Type`. Headers and JWT claims were checked identical
against a real `pywebpush 2.5.0` call; bodies cannot be byte-identical
(ephemeral sender key + random salt per RFC 8291) but were checked
decrypt-equivalent. See `push_tests.py`'s round-trip/header-shape and
RFC 8291 vector tests for what's pinned here.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec
from http_ece import encrypt as _ece_encrypt
from py_vapid import Vapid

# pywebpush: "encryption lives for 12 hours" (its `webpush()` sets this exact
# expiry when the caller's claims don't already carry one).
VAPID_TOKEN_LIFETIME_S = 12 * 60 * 60

# `pywebpush.WebPusher.send`'s own check: a push service may legitimately
# answer 200/201/202 (accepted, possibly for later delivery); anything past
# that is a failure. NOTE a behavior difference from pywebpush: `requests`
# (pywebpush's transport) follows redirects by default, `httpx.post` does
# NOT, so a 3xx now surfaces here as a (non-pruning, since it's not in
# PRUNE_STATUS_CODES) failure instead of being silently followed to its
# final response. Push services don't redirect in practice, so this is
# believed harmless, but it IS a difference worth knowing about.
_MAX_SUCCESS_STATUS_CODE = 202


class WebPushError(Exception):
    """Delivery failure — the `push_delivery._status_code` / prune-on-4xx path
    reads `.response`, so this carries the same shape `pywebpush.WebPushException`
    did: `.response` is whatever produced the failure (an `httpx.Response` with
    a `.status_code`, or a test double exposing the same attribute), or `None`
    when there was no response at all.

    Deliberately NOT raised for a network-level failure (timeout, connection
    refused) — those propagate as the underlying `httpx.HTTPError` unwrapped,
    exactly as a `requests` timeout/connection error propagated THROUGH
    `pywebpush.webpush()` unwrapped (pywebpush only wraps a response it
    actually received and rejected). `_deliver_one` catches `httpx.HTTPError`
    itself (a separate `except` clause from this class) and never prunes it
    either, so the net behavior is unchanged.
    """

    def __init__(self, message: str, response: object | None = None) -> None:
        super().__init__(message)
        self.response = response


def _b64url_decode(data: str) -> bytes:
    """`base64.urlsafe_b64decode`, tolerant of the missing '=' padding every
    Web Push subscription key arrives without."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _subscriber_keys(subscription_info: Mapping[str, Any]) -> tuple[bytes, bytes]:
    """Decode `keys.p256dh` (the subscriber's raw uncompressed EC point) and
    `keys.auth` (their 16-byte auth secret) — the same two fields
    `pywebpush.WebPusher.__init__` reads, decoded the same way."""
    keys = subscription_info["keys"]
    return _b64url_decode(keys["p256dh"]), _b64url_decode(keys["auth"])


def _vapid_headers(
    endpoint: str, vapid_private_key: str, vapid_claims: Mapping[str, Any]
) -> dict[str, str]:
    """Build the `Authorization: vapid t=...,k=...` header via `py_vapid`,
    filling in `aud`/`exp` exactly as `pywebpush.webpush()` does when the
    caller's claims don't already carry them (`_deliver_one` only ever
    supplies `sub`)."""
    claims = dict(vapid_claims)
    if not claims.get("aud"):
        parsed = urlparse(endpoint)
        claims["aud"] = f"{parsed.scheme}://{parsed.netloc}"
    if not claims.get("exp") or int(claims.get("exp") or 0) < int(time.time()):
        claims["exp"] = int(time.time()) + VAPID_TOKEN_LIFETIME_S
    vapid = Vapid.from_string(private_key=vapid_private_key)
    return dict(vapid.sign(claims))


def _encrypt(data: bytes, receiver_key: bytes, auth_secret: bytes) -> bytes:
    """RFC 8291 `aes128gcm`: a fresh ephemeral P-256 keypair per message (never
    reused — reusing the sender key across messages would let a push service
    correlate them), random salt, `http_ece.encrypt` composes the
    self-describing body (salt + record size + sender pubkey + ciphertext)
    exactly as `pywebpush.WebPusher.encode()` does for this encoding."""
    server_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    encrypted: bytes = _ece_encrypt(
        data,
        salt=None,
        private_key=server_key,
        dh=receiver_key,
        auth_secret=auth_secret,
        version="aes128gcm",
    )
    return encrypted


def webpush(  # noqa: PLR0913 - mirrors pywebpush.webpush()'s own keyword shape (contract)
    *,
    subscription_info: Mapping[str, Any],
    data: str,
    vapid_private_key: str,
    vapid_claims: Mapping[str, Any],
    timeout: tuple[float, float] | float | None = None,
    ttl: int = 0,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """Encrypt `data` and POST it to the subscription's endpoint — the
    `pywebpush.webpush()` replacement `PushDispatcher._deliver_one` calls via
    `asyncio.to_thread`. Same keyword shape (`_deliver_one` never passes
    `ttl`/`client`, so their defaults are what production uses).

    `timeout`, when a `(connect, read)` tuple (`_deliver_one`'s shape),
    becomes an `httpx.Timeout` with matching connect/read bounds; a bare float
    or `None` passes straight through to httpx.

    `client` is a testability seam only — production always uses the module-
    level `httpx.post` (a fresh connection per delivery, same as `pywebpush`'s
    default `requests.post` with no session reuse); tests inject an
    `httpx.Client(transport=httpx.MockTransport(...))` so the REAL
    `_deliver_one` path can be driven end to end with no network access.

    Raises `WebPushError` for any response `pywebpush` would also have
    rejected (`status_code > 202` — matches `WebPusher.send`'s check, so a
    202 Accepted push service is not treated as a failure). A network-level
    failure (timeout, connection error) raises the underlying `httpx.HTTPError`
    unwrapped — see `WebPushError`'s docstring for why that's the correct
    parity, not an oversight.
    """
    endpoint = subscription_info["endpoint"]
    receiver_key, auth_secret = _subscriber_keys(subscription_info)
    headers = _vapid_headers(endpoint, vapid_private_key, vapid_claims)
    headers["content-encoding"] = "aes128gcm"
    headers["ttl"] = str(ttl or 0)
    body = _encrypt(data.encode("utf-8"), receiver_key, auth_secret)

    httpx_timeout: httpx.Timeout | float | None
    if isinstance(timeout, tuple):
        connect, read = timeout
        httpx_timeout = httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
    else:
        httpx_timeout = timeout

    poster = client.post if client is not None else httpx.post
    response = poster(endpoint, content=body, headers=headers, timeout=httpx_timeout)

    if response.status_code > _MAX_SUCCESS_STATUS_CODE:
        raise WebPushError(
            f"Push failed: {response.status_code} {response.reason_phrase}\n"
            f"Response body:{response.text}",
            response=response,
        )
    return response
