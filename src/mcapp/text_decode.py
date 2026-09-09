"""Charset handling for inbound MeshCom payload bytes — ONE definition, both transports.

Two steps, deliberately separable but almost always used together via
`decode_and_filter`:

1. `decode_text` — bytes to str. Nominally UTF-8, with a per-byte CP1252
   fallback (see below).
2. `filter_unsafe` — drop the codepoints that are unsafe to carry downstream.

This module exists because the two ingest routes used to disagree. The
Extern-UDP path ran an aggressive *whitelist* of permitted characters over the
whole datagram; the BLE path ran no character filter at all. The same message
arrives on both (one datagram plus the BLE copy, ~100 ms apart), so a message
containing anything the whitelist did not know about existed in two different
texts — and since the ingest dedup race fix, only the first copy to arrive is
stored, which made the stored text a function of which transport won. The
policy therefore lives here, once, and both callers use it.

The whitelist is gone with it. It had to be extended by hand for every
legitimate character somebody had not thought of, and it lost that race
repeatedly: joined emoji sequences (2026-08-30), `Ç` and `Ñ` while `ç` and `ñ`
were listed, the whole Nordic/Icelandic set, and every decomposed accent
(`u` + U+0308) because combining marks are not symbols or punctuation. What is
left is a narrow rejection of what genuinely breaks something downstream.
"""

from __future__ import annotations

import codecs
import unicodedata

from .logging_setup import get_logger

logger = get_logger(__name__)


# --- Step 1: decode -------------------------------------------------------
#
# Payload text is nominally UTF-8, but several clients (PinPoint among them)
# put umlauts on the wire as single CP1252/Latin-1 bytes ("über" -> b"\xfc").
# Since the firmware's CHR-03 change (MeshCom-Firmware fork-main 16670de9 +
# 094636b2) those bytes are relayed unchanged instead of being dropped as
# invalid UTF-8, so the payload is no longer guaranteed to be valid UTF-8 and
# the deciding is ours. `errors="ignore"` — what both paths used before —
# deletes such a byte silently, losing the character for good.
#
# The handler below re-reads every byte UTF-8 rejects as CP1252 instead. Valid
# UTF-8 sequences are untouched, so a mixed payload keeps both halves; only the
# five bytes undefined in CP1252 (0x81, 0x8D, 0x8F, 0x90, 0x9D) become U+FFFD.
# Kept byte-compatible with mc-chat's `meshcom_mock.decoder.decode_text`, which
# is a separate repo and is deliberately not imported.

_CP1252_ERROR_HANDLER = "meshcom.cp1252-fallback"


def _cp1252_fallback(error: UnicodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeDecodeError):
        raise error
    raw = error.object[error.start : error.end]
    return raw.decode("cp1252", errors="replace"), error.end


codecs.register_error(_CP1252_ERROR_HANDLER, _cp1252_fallback)


def decode_text(raw: bytes) -> str:
    """Decode payload bytes as UTF-8, falling back to CP1252 per rejected byte."""
    return raw.decode("utf-8", errors=_CP1252_ERROR_HANDLER)


# --- Step 2: filter -------------------------------------------------------

# Format characters (Cf) are rejected as a class — a zero-width character is
# invisible, and this filter also runs over callsign fields on the UDP path,
# where an invisible codepoint is a lookalike-station vector. These two are the
# exception: they carry no glyph of their own but bind their neighbours into
# ONE grapheme, so dropping one does not remove a character, it SPLITS a
# sequence the sender composed (`🙋‍♂` renders as two glyphs without the
# joiner — observed 2026-08-30). The variation selectors (Mn) and the enclosing
# keycap (Me) need no exception here: marks are not a rejected category.
_ZERO_WIDTH_JOINER = 0x200D
_TAG_RANGE_START = 0xE0020  # subdivision-flag tag sequences, e.g. 🏴󠁧󠁢󠁳󠁣󠁴󠁿
_TAG_RANGE_END = 0xE007F

# Cc  control characters — never legitimate in a MeshCom payload, and a raw
#     control byte inside the JSON text would break the datagram parse.
# Cf  format/zero-width — see above.
# Cs  surrogates — unpaired ones break the SQLite and JSON round-trip.
# Co  private use — no agreed meaning; renders as tofu on every client.
# Cn  unassigned, which is also where the noncharacters (U+FFFE/U+FFFF in every
#     plane) live. Note this is judged against the *running* Python's Unicode
#     tables: a codepoint assigned after that release reads as Cn and is
#     dropped. That is the one way this filter can still be wrong about a
#     legitimate character, and it self-heals on a Python upgrade.
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def is_safe_char(ch: str) -> bool:
    """Reject only what breaks something downstream. Everything else passes.

    Deliberately a blacklist: the predicate it replaced was a whitelist and
    every character nobody had enumerated was silently deleted from real
    traffic.
    """
    codepoint = ord(ch)

    # Fast path: printable ASCII is the overwhelming majority of every payload
    # and of the JSON structure this runs over on the UDP path.
    if 0x20 <= codepoint < 0x7F:  # noqa: PLR2004 - ASCII printable range
        return True

    if codepoint == _ZERO_WIDTH_JOINER or _TAG_RANGE_START <= codepoint <= _TAG_RANGE_END:
        return True

    if unicodedata.category(ch) in _REJECTED_CATEGORIES:
        # Routine noise from a lossy RF link, not a real error — DEBUG only.
        logger.debug(
            "Dropping unsafe character: %r (U+%04X, %s)",
            ch,
            codepoint,
            unicodedata.name(ch, "UNKNOWN"),
        )
        return False

    return True


def filter_unsafe(text: str) -> str:
    """Drop every codepoint `is_safe_char` rejects."""
    return "".join(ch for ch in text if is_safe_char(ch))


def decode_and_filter(data: bytes) -> str:
    """The full inbound charset policy: CP1252-tolerant decode, then filter."""
    return filter_unsafe(decode_text(data))
