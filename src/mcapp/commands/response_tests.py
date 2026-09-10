"""Built-in test suite for ``ResponseMixin._chunk_response`` (commands/response.py).

Table-driven, synchronous, no pytest — mirrors the house pattern in
``parsing_tests.py``: a ``results: list[tuple[str, bool]]``, a
``_record`` helper printing "✅ PASS | label" / "❌ FAIL | label" lines, and a
final summary line plus ``return all(...)``.

TX-02 residual (doc/2026-09-10_1900-ble-protocol-parity-audit.md):
``_chunk_response`` caps chunks at ``MAX_RESPONSE_LENGTH`` (140) UTF-8 BYTES,
but its two fallback paths used to slice by CHARACTERS — a multi-byte
character (umlauts, emoji) could push a single chunk well over the firmware's
160-byte `{dst}msg` frame limit. These cases pin the byte-safe split.

Run headless:
    uv run python -c "import sys; from mcapp.commands.response_tests import \
run_response_tests; sys.exit(0 if run_response_tests() else 1)"
"""

from __future__ import annotations

from .constants import MAX_CHUNKS, MAX_RESPONSE_LENGTH
from .response import ResponseMixin


class _ResponseHarness(ResponseMixin):
    """Minimal concrete ResponseMixin instance: _chunk_response uses no other state."""


def _record(results: list[tuple[str, bool]], label: str, ok: bool) -> None:
    icon = "✅ PASS" if ok else "❌ FAIL"
    print(f"{icon} | {label}")
    results.append((label, ok))


def _chunk_byte_lengths(chunks: list[str]) -> list[int]:
    return [len(chunk.encode("utf-8")) for chunk in chunks]


def _test_multibyte_fallback_split(results: list[tuple[str, bool]]) -> None:
    """150 x 'ö': no ', ' or ' | ' separator, so this hits the fallback split.

    Each 'ö' is 2 UTF-8 bytes, so the input is 300 bytes -> fits in
    MAX_CHUNKS=3 chunks of <=140 bytes each once the split is byte-aware.
    Against the unmodified character-wise fallback, chunk 1 was
    response[0:140] = 140 CHARACTERS = 280 bytes, well over the cap.
    """
    stub = _ResponseHarness()  # type: ignore[abstract] # partial test double for CommandHandler mixins
    response = "ö" * 150

    chunks = stub._chunk_response(response)
    byte_lens = _chunk_byte_lengths(chunks)

    _record(
        results,
        f"150x'ö': every chunk <= {MAX_RESPONSE_LENGTH} bytes (got {byte_lens})",
        all(n <= MAX_RESPONSE_LENGTH for n in byte_lens),
    )
    _record(results, "150x'ö': chunks joined == input", "".join(chunks) == response)
    _record(
        results,
        f"150x'ö': at most MAX_CHUNKS={MAX_CHUNKS} chunks (got {len(chunks)})",
        len(chunks) <= MAX_CHUNKS,
    )


def _test_realistic_weather_sentence(results: list[tuple[str, bool]]) -> None:
    """A realistic weather reply with umlauts and an emoji, no ' | ' separator
    and no clean two-part ', ' split (many commas) -> also hits the fallback.
    """
    stub = _ResponseHarness()  # type: ignore[abstract] # partial test double for CommandHandler mixins
    response = (
        "\U0001f324️ 21.3°C, 68%, 1004 hPa, Wind 12 km/h aus SW, "
        "Böen 30 km/h, leicht bewölkt, Sicht gut, Tendenz fallend, "
        "Regen 0.2 mm/h, Taupunkt 15°C, UV 3, Sonne bis 19:40 Uhr"
    )

    chunks = stub._chunk_response(response)
    byte_lens = _chunk_byte_lengths(chunks)

    _record(
        results,
        f"weather sentence: every chunk <= {MAX_RESPONSE_LENGTH} bytes (got {byte_lens})",
        all(n <= MAX_RESPONSE_LENGTH for n in byte_lens),
    )
    _record(
        results,
        "weather sentence: chunks join round-trips (up to MAX_CHUNKS truncation)",
        "".join(chunks) == response[: len("".join(chunks))],
    )


def _test_oversized_pipe_part_split(results: list[tuple[str, bool]]) -> None:
    """A ' | '-separated response where one part alone exceeds max_bytes.

    80 x 'ä' is 160 bytes -- over the 140-byte cap on its own -- so the ' | '
    branch must split that single part further instead of appending it whole.
    """
    stub = _ResponseHarness()  # type: ignore[abstract] # partial test double for CommandHandler mixins
    oversized_part = "ä" * 80
    response = f"OE1ABC {oversized_part} | OE2XYZ ok"

    chunks = stub._chunk_response(response)
    byte_lens = _chunk_byte_lengths(chunks)

    _record(
        results,
        f"oversized ' | ' part: every chunk <= {MAX_RESPONSE_LENGTH} bytes (got {byte_lens})",
        all(n <= MAX_RESPONSE_LENGTH for n in byte_lens),
    )


def _test_ascii_fallback_unchanged(results: list[tuple[str, bool]]) -> None:
    """Pure-ASCII 300-char response: pins that ASCII chunk sizes did not move
    (140/140/20 bytes == chars, since ASCII is 1 byte/char either way).
    """
    stub = _ResponseHarness()  # type: ignore[abstract] # partial test double for CommandHandler mixins
    response = "a" * 300

    chunks = stub._chunk_response(response)
    byte_lens = _chunk_byte_lengths(chunks)

    _record(
        results,
        f"ASCII 300 chars: chunk byte sizes unchanged (got {byte_lens})",
        byte_lens == [140, 140, 20],
    )
    _record(results, "ASCII 300 chars: chunks joined == input", "".join(chunks) == response)


def _test_short_response_passthrough(results: list[tuple[str, bool]]) -> None:
    """A response already within budget returns [response] unchanged."""
    stub = _ResponseHarness()  # type: ignore[abstract] # partial test double for CommandHandler mixins
    response = "OK"

    chunks = stub._chunk_response(response)

    _record(results, "short response: returned unchanged as single chunk", chunks == [response])


def run_response_tests() -> bool:
    """Run the ResponseMixin chunking test suite. Return True iff every case passed."""
    print("Testing ResponseMixin._chunk_response:")
    print("=" * 50)

    results: list[tuple[str, bool]] = []
    _test_multibyte_fallback_split(results)
    _test_realistic_weather_sentence(results)
    _test_oversized_pipe_part_split(results)
    _test_ascii_fallback_unchanged(results)
    _test_short_response_passthrough(results)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print("=" * 50)
    print(f"response: {'PASS' if passed == total else 'FAIL'} ({passed}/{total})")
    return all(ok for _, ok in results)


if __name__ == "__main__":
    import sys

    sys.exit(0 if run_response_tests() else 1)
