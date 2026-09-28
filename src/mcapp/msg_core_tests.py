"""Startup regression suite for `util.msg_core()` / `util.msg_id_retry_variants()`.

Pins the firmware PN-retry XOR layout (firmware `src/pn_retry.h`,
`docs/pn-retry-mcapp.md` §5 case 1): retry k of a direct message XORs bits
10-11 of the msg_id with k and leaves every other bit alone. Pure functions,
no I/O.
"""

from __future__ import annotations

from .util import MSG_ID_CORE_MASK, msg_core, msg_id_retry_variants

_Results = list[tuple[str, bool]]

# Vector from the firmware note: E1E05457 has bits 10-11 = 01, retry 1 XORs
# them to 00.
_ORIGINAL = "E1E05457"
_RETRY_1 = "E1E05057"


def _xor_retry(msg_id: str, k: int) -> str:
    return f"{int(msg_id, 16) ^ (k << 10):08X}"


def _test_mask_value() -> _Results:
    return [("mask clears exactly bits 10-11", MSG_ID_CORE_MASK == 0xFFFFFFFF & ~(0b11 << 10))]


def _test_core_equal_across_retries() -> _Results:
    variants = [_xor_retry(_ORIGINAL, k) for k in range(4)]
    return [
        ("firmware vector: retry 1 is E1E05057", _xor_retry(_ORIGINAL, 1) == _RETRY_1),
        (
            "core(original) == core(retry 1) == E1E05057",
            msg_core(_ORIGINAL) == msg_core(_RETRY_1) == "E1E05057",
        ),
        ("all four retries share one core", len({msg_core(v) for v in variants}) == 1),
        ("core is a no-op when bits 10-11 are already 0", msg_core(_RETRY_1) == _RETRY_1),
    ]


def _test_core_keeps_other_bits() -> _Results:
    # Counter (bits 0-9) and node id above bit 11 must survive.
    other = f"{int(_ORIGINAL, 16) ^ 0x1:08X}"
    high = f"{int(_ORIGINAL, 16) ^ 0x1000:08X}"
    return [
        ("different counter -> different core", msg_core(other) != msg_core(_ORIGINAL)),
        ("different bit 12 -> different core", msg_core(high) != msg_core(_ORIGINAL)),
    ]


def _test_core_edge_inputs() -> _Results:
    return [
        ("None -> None", msg_core(None) is None),
        ("empty -> None", msg_core("") is None),
        ("lower case normalised", msg_core("e1e05457") == "E1E05057"),
        ("non-hex returned unchanged (upper)", msg_core("zz12") == "ZZ12"),
        ("wrong length returned unchanged", msg_core("1234") == "1234"),
        # Extern-UDP admits any JSON scalar as msg_id; must not raise.
        ("int input stringified, no raise", msg_core(12345) == "12345"),
        # Exactly 8 ASCII hex digits: int(s, 16) alone would accept these.
        ("signed id returned verbatim", msg_core("-1A2B3C4") == "-1A2B3C4"),
        ("underscored id returned verbatim", msg_core("1A2B_3C4") == "1A2B_3C4"),
    ]


def _test_retry_variants() -> _Results:
    variants = msg_id_retry_variants(_ORIGINAL)
    expected = {_xor_retry(_ORIGINAL, k) for k in range(4)}
    return [
        ("four variants", len(variants) == 4),
        ("input first", variants[0] == _ORIGINAL),
        ("variants are exactly the four XOR retries", set(variants) == expected),
        ("non-hex yields itself only", msg_id_retry_variants("abc") == ("ABC",)),
    ]


def run_msg_core_tests() -> bool:
    """Run the msg_core suite; return True iff all cases passed."""
    results: _Results = []
    results.extend(_test_mask_value())
    results.extend(_test_core_equal_across_retries())
    results.extend(_test_core_keeps_other_bits())
    results.extend(_test_core_edge_inputs())
    results.extend(_test_retry_variants())

    for label, passed in results:
        print(f"    {'✅ PASS' if passed else '❌ FAIL'} | {label}")

    all_passed = all(passed for _, passed in results)
    print(f"msg_core: {'PASS' if all_passed else 'FAIL'}")
    return all_passed
