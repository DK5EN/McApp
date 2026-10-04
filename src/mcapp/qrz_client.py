"""QRZ.com XML API: response parsing and classification, no I/O.

Kept pure so every branch of the protocol's error rules is testable without a
network. The rules follow the spec's "The Session Node" and "Error Conditions"
sections (XML spec 1.36); the mapping to our reactions is in
doc/2026-10-04_0848-qrz-callsign-lookup-plan.md §3.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import StrEnum

QRZ_XML_URL = "https://xmldata.qrz.com/xml/current/"
QRZ_AGENT = "mcapp"


# "Connection refused" is the spec's one named special case: "successful login
# will not be possible for at least 24 hours".
_REFUSED_RE = re.compile(r"connection refused", re.IGNORECASE)
_RATE_LIMIT_RE = re.compile(r"limit|exceeded|too many|quota", re.IGNORECASE)
_NOT_FOUND_RE = re.compile(r"^not found", re.IGNORECASE)

# Leading postal code, with or without a country prefix: "A-4060 Leonding",
# "D-85354 Freising", "85354 Freising", "CH-8000 Zürich". Trailing: "Leonding 4060".
_LEADING_POSTCODE_RE = re.compile(r"^(?:[A-Z]{1,3}-)?\d[\d-]*\s+")
_TRAILING_POSTCODE_RE = re.compile(r"\s+\d[\d-]*$")

# Base callsign shape: optional 1-2 char prefix part, one digit, 1-4 letters,
# e.g. DK5EN, OE5HWN, 9A1AA, W1AW, 2E0ABC. Aliases like WLNK have no digit.
_CALLSIGN_RE = re.compile(r"^(?:[A-Z]{1,2}|[0-9][A-Z]|[A-Z][0-9])[0-9][A-Z]{1,4}$")


class Outcome(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    SESSION_INVALID = "session_invalid"
    REFUSED = "refused"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    LOGGED_IN = "logged_in"
    MALFORMED = "malformed"


@dataclass(slots=True)
class QrzResponse:
    """One parsed reply. `record` holds the `<Callsign>` children by tag."""

    outcome: Outcome
    key: str | None = None
    count: int | None = None
    sub_exp: str | None = None
    error: str | None = None
    message: str | None = None
    record: dict[str, str] = field(default_factory=dict)


def base_callsign(callsign: str) -> str:
    return callsign.strip().upper().split("-", maxsplit=1)[0]


def is_lookup_candidate(base: str) -> bool:
    """Does this base look like an amateur callsign QRZ could know?"""
    return bool(_CALLSIGN_RE.match(base))


def first_name(fname: str | None) -> str | None:
    """'Martin Stefan' -> 'Martin'."""
    if not fname:
        return None
    parts = fname.split()
    return parts[0] if parts else None


def qth_from_addr2(addr2: str | None) -> str | None:
    """'A-4060 Leonding' -> 'Leonding'; values without a postal code pass."""
    if not addr2:
        return None
    text = " ".join(addr2.split())
    text = _LEADING_POSTCODE_RE.sub("", text)
    text = _TRAILING_POSTCODE_RE.sub("", text)
    return text or None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _local(tag: str) -> str:
    """Element name without its namespace: the live server sends
    `xmlns="http://xmldata.qrz.com"`, the spec's own examples do not."""
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in node if _local(c.tag) == name), None)


def parse_response(body: str, *, is_login: bool) -> QrzResponse:
    """Classify one XML reply per the spec's session and error rules.

    An `<Error>` WITH a `<Key>` is a data error (the session is still valid);
    an `<Error>` WITHOUT one means no valid session exists. On a login, a
    missing key with any error other than refusal or a rate limit means the
    credentials were rejected.
    """
    try:
        root = ET.fromstring(body)  # noqa: S314 - fixed upstream host over TLS, no DTD/entity use in its schema
    except ET.ParseError:
        return QrzResponse(outcome=Outcome.MALFORMED, error="unparseable XML response")

    session = _child(root, "Session")
    if session is None:
        return QrzResponse(outcome=Outcome.MALFORMED, error="response without Session node")

    def text(name: str) -> str | None:
        node = _child(session, name)
        value = node.text if node is not None else None
        return value.strip() if value and value.strip() else None

    resp = QrzResponse(
        outcome=Outcome.MALFORMED,
        key=text("Key"),
        count=_int_or_none(text("Count")),
        sub_exp=text("SubExp"),
        error=text("Error"),
        message=text("Message"),
    )

    callsign = _child(root, "Callsign")
    if callsign is not None and not is_login:
        resp.record = {
            _local(child.tag): child.text.strip()
            for child in callsign
            if child.text and child.text.strip()
        }

    error = resp.error or ""
    if _REFUSED_RE.search(error):
        resp.outcome = Outcome.REFUSED
    elif error and _RATE_LIMIT_RE.search(error):
        resp.outcome = Outcome.RATE_LIMITED
    elif resp.key is None:
        resp.outcome = Outcome.AUTH_FAILED if is_login else Outcome.SESSION_INVALID
    elif is_login:
        resp.outcome = Outcome.LOGGED_IN
    elif resp.record:
        resp.outcome = Outcome.FOUND
    elif _NOT_FOUND_RE.search(error):
        resp.outcome = Outcome.NOT_FOUND
    elif error:
        # Any other data error with a live key: nothing to show for this call.
        resp.outcome = Outcome.NOT_FOUND
    return resp
