"""Bool-return test harness for the classifier, invoked from main.py when
has_console() is true. Uses an ephemeral SQLite storage file so live
production data is never touched.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any

from ..logging_setup import get_logger, has_console
from . import rules as rules_mod
from . import score as score_mod
from . import template as template_mod
from .classify import Classifier, _fallback_classification
from .seed import seed_builtin_rules

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


async def _fresh_storage():
    """Create an ephemeral SQLite storage with the full migrated schema."""
    from ..sqlite_storage import create_sqlite_storage

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    storage = await create_sqlite_storage(tmp.name)
    return storage, tmp.name


async def _cleanup(storage: Any, path: str) -> None:
    if getattr(storage, "_read_conn", None) is not None:
        try:
            storage._read_conn.close()
        except Exception:
            pass
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Purity tests — no DB required
# ---------------------------------------------------------------------------


def _test_fingerprint_invariants() -> tuple[str, bool, str]:
    name = "test_fingerprint_invariants"
    fp = template_mod.fingerprint
    try:
        assert fp("hello") == fp(" hello "), "whitespace not trimmed"
        assert fp("Hello") == fp("hello"), "not lowercased"
        assert fp("Temp: 23C") == fp("Temp: 42C"), "numbers not normalized"
        assert fp("Temp: 23.5C") == fp("Temp: 100,25C"), "decimal/comma-decimal not normalized"
        assert fp("visit https://a.b/c") == fp(
            "visit https://other.example/foo/bar"
        ), "URLs not normalized"
        assert fp("\U0001f321 23C") == fp("\U0001f321 99C"), "emoji+number not normalized"
        assert fp("hello") != fp("bye"), "different templates should differ"
        assert len(fp("hello")) == 12, "hash length not 12"
    except AssertionError as exc:
        return name, False, str(exc)
    return name, True, ""


def _test_score_neutral_baseline() -> tuple[str, bool, str]:
    """Short empty message with high tpl_count → score near 0.5.

    With tpl_count=100, freshness = 1/(1+100) ≈ 0.0099, word_count=0 → len_factor=0.
    raw ≈ 0.15 * 0 + 0.15 * 0.0099 - ... ≈ 0.0015, result ≈ 0.5015.
    Using tpl_count=100 (not 0) because tpl_count=0 yields freshness=1.0 → score~0.65.
    """
    name = "test_score_neutral_baseline"
    score = score_mod.compute({"msg": ""}, "other", [], 100)
    if not (0.4 <= score <= 0.6):
        return name, False, f"expected score in [0.4, 0.6], got {score:.4f}"
    return name, True, ""


def _test_score_long_directed() -> tuple[str, bool, str]:
    """15+ word directed message (no beacon tags) should score > 0.6."""
    name = "test_score_long_directed"
    long_msg = "Hello friend this is a very long directed message with many words today"
    score = score_mod.compute({"msg": long_msg}, "directed", [], 0)
    if score <= 0.6:
        return name, False, f"expected score > 0.6, got {score:.4f}"
    return name, True, ""


def _test_score_beacon_template_low() -> tuple[str, bool, str]:
    """Short message with beacon tag and high tpl_count should score < 0.3."""
    name = "test_score_beacon_template_low"
    score = score_mod.compute({"msg": "hi"}, "other", {"beacon"}, 100)
    if score >= 0.3:
        return name, False, f"expected score < 0.3, got {score:.4f}"
    return name, True, ""


def _test_score_clamped() -> tuple[str, bool, str]:
    """Score output must always be in [0.0, 1.0]."""
    name = "test_score_clamped"
    cases = [
        ({"msg": ""}, "other", [], 0),
        ({"msg": "hello world"}, "directed", {"beacon"}, 0),
        ({"msg": "x" * 500}, "qso", [], 9999),
        ({"msg": "\U0001f321" * 20}, "other", {"beacon", "auto_beacon"}, 0),
    ]
    for msg, cat, tags, cnt in cases:
        s = score_mod.compute(msg, cat, tags, cnt)
        if not (0.0 <= s <= 1.0):
            return name, False, f"score {s} out of [0,1] for {msg!r}"
    return name, True, ""


# ---------------------------------------------------------------------------
# DB-dependent tests
# ---------------------------------------------------------------------------


async def _test_rules_load_and_seed() -> tuple[str, bool, str]:
    name = "test_rules_load_and_seed"
    storage, path = await _fresh_storage()
    try:
        inserted = await seed_builtin_rules(storage)
        if inserted == 0:
            return name, False, "seed_builtin_rules returned 0"
        rules = await rules_mod.load_rules(storage)
        if not rules:
            return name, False, "load_rules returned empty list"
        if rules[0].priority != 10:
            return name, False, f"first rule priority should be 10, got {rules[0].priority}"
        if rules[0].name != "CET timestamp":
            return name, False, f"first rule name unexpected: {rules[0].name!r}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_rules_match_timestamp_beacon() -> tuple[str, bool, str]:
    name = "test_rules_match_timestamp_beacon"
    storage, path = await _fresh_storage()
    try:
        await seed_builtin_rules(storage)
        rules = await rules_mod.load_rules(storage)
        msg = {"msg": "{CET}2026-04-18 12:34:56 hello", "src": "OE1ABC", "dst": "*"}
        category, tags = rules_mod.match(msg, rules)
        if category != "timestamp_beacon":
            return name, False, f"expected timestamp_beacon, got {category!r}"
        if "beacon" not in tags:
            return name, False, f"expected 'beacon' in tags, got {tags!r}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_rules_match_miss() -> tuple[str, bool, str]:
    name = "test_rules_match_miss"
    storage, path = await _fresh_storage()
    try:
        await seed_builtin_rules(storage)
        rules = await rules_mod.load_rules(storage)
        msg = {"msg": "xyzzy", "src": "OE1ABC", "dst": "*"}
        category, tags = rules_mod.match(msg, rules)
        if category != "other":
            return name, False, f"expected 'other', got {category!r}"
        if tags:
            return name, False, f"expected empty tags, got {tags!r}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_rules_match_url() -> tuple[str, bool, str]:
    name = "test_rules_match_url"
    storage, path = await _fresh_storage()
    try:
        await seed_builtin_rules(storage)
        rules = await rules_mod.load_rules(storage)
        msg = {"msg": "see https://example.com", "src": "OE1ABC", "dst": "*"}
        category, tags = rules_mod.match(msg, rules)
        if category != "node_advert":
            return name, False, f"expected node_advert, got {category!r}"
        if "has_url" not in tags:
            return name, False, f"expected 'has_url' in tags, got {tags!r}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_template_stats_insert() -> tuple[str, bool, str]:
    name = "test_template_stats_insert"
    storage, path = await _fresh_storage()
    try:
        now_ms = int(time.time() * 1000)
        tpl_hash = template_mod.fingerprint("dummy message for stats")
        msg1 = {"msg": "dummy message for stats", "src": "OE1ABC"}
        row = await template_mod.update_stats(storage, tpl_hash, msg1, now_ms)
        if row["count"] != 1:
            return name, False, f"expected count=1 after insert, got {row['count']}"
        if row["srcs"] != ["OE1ABC"]:
            return name, False, f"expected srcs=['OE1ABC'], got {row['srcs']!r}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_template_stats_update_new_src() -> tuple[str, bool, str]:
    name = "test_template_stats_update_new_src"
    storage, path = await _fresh_storage()
    try:
        now_ms = int(time.time() * 1000)
        tpl_hash = template_mod.fingerprint("multi-src test")
        await template_mod.update_stats(storage, tpl_hash, {"msg": "multi-src test",
                                                             "src": "OE1ABC"}, now_ms)
        row = await template_mod.update_stats(storage, tpl_hash, {"msg": "multi-src test",
                                                                   "src": "OE1XYZ"}, now_ms)
        if row["count"] != 2:
            return name, False, f"expected count=2, got {row['count']}"
        if "OE1ABC" not in row["srcs"] or "OE1XYZ" not in row["srcs"]:
            return name, False, f"expected both srcs present, got {row['srcs']!r}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_template_stats_dedup_src() -> tuple[str, bool, str]:
    name = "test_template_stats_dedup_src"
    storage, path = await _fresh_storage()
    try:
        now_ms = int(time.time() * 1000)
        tpl_hash = template_mod.fingerprint("dedup-src test")
        await template_mod.update_stats(storage, tpl_hash, {"msg": "dedup-src test",
                                                             "src": "OE1ABC"}, now_ms)
        await template_mod.update_stats(storage, tpl_hash, {"msg": "dedup-src test",
                                                             "src": "OE1XYZ"}, now_ms)
        # Same src again — should be deduped
        row = await template_mod.update_stats(storage, tpl_hash, {"msg": "dedup-src test",
                                                                   "src": "OE1ABC"}, now_ms)
        if len(row["srcs"]) != 2:
            return name, False, f"expected 2 unique srcs, got {row['srcs']!r}"
        # OE1ABC moved to end (newest last)
        if row["srcs"][-1] != "OE1ABC":
            return name, False, f"expected OE1ABC newest (last), got {row['srcs']!r}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_template_stats_srcs_cap() -> tuple[str, bool, str]:
    name = "test_template_stats_srcs_cap"
    storage, path = await _fresh_storage()
    try:
        now_ms = int(time.time() * 1000)
        tpl_hash = template_mod.fingerprint("cap-test")
        for i in range(25):
            src = f"OE{i:04d}ABC"
            await template_mod.update_stats(storage, tpl_hash, {"msg": "cap-test",
                                                                  "src": src}, now_ms)
        # Fetch the final row from DB
        rows = await storage._execute(
            "SELECT srcs FROM beacon_templates WHERE template_hash = ?", (tpl_hash,)
        )
        import json

        srcs = json.loads(rows[0]["srcs"]) if rows else []
        if len(srcs) != template_mod.SRCS_CAP:
            return name, False, f"expected {template_mod.SRCS_CAP} srcs, got {len(srcs)}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_auto_beacon_threshold() -> tuple[str, bool, str]:
    """Insert 4 prior messages, then call status once → effective=5 → (True, True).
    Second call → was_auto=True → (True, False).
    """
    name = "test_auto_beacon_threshold"
    storage, path = await _fresh_storage()
    try:
        now_ms = int(time.time() * 1000)
        tpl_hash = template_mod.fingerprint("beacon-threshold-test")
        src = "OE1ABC"
        # Need beacon_templates row first (update_stats creates it)
        msg = {"msg": "beacon-threshold-test", "src": src}
        # Do not count via update_stats to keep count=0 on the template row.
        # Insert 4 messages directly into messages table.
        await storage._execute(
            "INSERT INTO beacon_templates "
            "(template_hash, example_msg, example_src, srcs, count, first_seen, "
            " last_seen, auto_beacon, user_action) "
            "VALUES (?, ?, ?, ?, 0, datetime('now'), datetime('now'), 0, NULL)",
            (tpl_hash, "beacon-threshold-test", src, "[]"),
            fetch=False,
        )
        for i in range(4):
            await storage._execute(
                "INSERT INTO messages (src, dst, msg, type, timestamp, template_hash) "
                "VALUES (?, '*', ?, 'msg', ?, ?)",
                (src, msg["msg"], now_ms - i * 1000, tpl_hash),
                fetch=False,
            )
        # First call: effective = 4+1 = 5 >= threshold (5) → (True, True)
        is_auto, just_crossed = await template_mod.auto_beacon_status(
            storage, tpl_hash, src, now_ms, None
        )
        if not is_auto or not just_crossed:
            return name, False, f"expected (True, True), got ({is_auto}, {just_crossed})"
        # Second call: auto_beacon already=1 in DB → (True, False)
        is_auto2, just_crossed2 = await template_mod.auto_beacon_status(
            storage, tpl_hash, src, now_ms, None
        )
        if not is_auto2 or just_crossed2:
            return (
                name,
                False,
                f"expected (True, False) on 2nd call, got ({is_auto2}, {just_crossed2})",
            )
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_auto_beacon_demote() -> tuple[str, bool, str]:
    name = "test_auto_beacon_demote"
    storage, path = await _fresh_storage()
    try:
        now_ms = int(time.time() * 1000)
        tpl_hash = template_mod.fingerprint("demote-test")
        src = "OE1ABC"
        result = await template_mod.auto_beacon_status(storage, tpl_hash, src, now_ms, "demote")
        if result != (False, False):
            return name, False, f"expected (False, False), got {result}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_auto_beacon_promote() -> tuple[str, bool, str]:
    name = "test_auto_beacon_promote"
    storage, path = await _fresh_storage()
    try:
        now_ms = int(time.time() * 1000)
        tpl_hash = template_mod.fingerprint("promote-test")
        src = "OE1ABC"
        result = await template_mod.auto_beacon_status(storage, tpl_hash, src, now_ms, "promote")
        if result != (True, False):
            return name, False, f"expected (True, False), got {result}"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


# ---------------------------------------------------------------------------
# Integration (end-to-end) test
# ---------------------------------------------------------------------------


async def _test_integration_classify() -> tuple[str, bool, str]:
    name = "test_integration_classify"
    storage, path = await _fresh_storage()
    try:
        await seed_builtin_rules(storage)
        classifier = Classifier(storage)
        await classifier.load()
        now_ms = int(time.time() * 1000)
        msg = {
            "msg": "{CET}2026-04-18 12:00:00 test",
            "src": "OE1XYZ",
            "dst": "*",
            "type": "msg",
            "timestamp": now_ms,
        }
        cls = await classifier.classify(msg)
        if cls.category != "timestamp_beacon":
            return name, False, f"expected timestamp_beacon, got {cls.category!r}"
        if "beacon" not in cls.tags:
            return name, False, f"expected 'beacon' in tags, got {cls.tags!r}"
        if cls.classifier_version < 1:
            return name, False, f"expected classifier_version >= 1, got {cls.classifier_version}"
        if len(cls.template_hash) != 12:
            return name, False, f"template_hash length {len(cls.template_hash)} != 12"
    except Exception as exc:
        return name, False, str(exc)
    finally:
        await _cleanup(storage, path)
    return name, True, ""


async def _test_integration_fallback() -> tuple[str, bool, str]:
    name = "test_integration_fallback"
    try:
        cls = _fallback_classification("some msg text", version=1)
        if cls.category != "other":
            return name, False, f"expected category 'other', got {cls.category!r}"
        if cls.info_score != 0.5:
            return name, False, f"expected info_score=0.5, got {cls.info_score}"
        if cls.tags != ():
            return name, False, f"expected empty tags tuple, got {cls.tags!r}"
        if len(cls.template_hash) != 12:
            return name, False, f"template_hash length {len(cls.template_hash)} != 12"
    except Exception as exc:
        return name, False, str(exc)
    return name, True, ""


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------


async def run_all_tests(storage: Any = None) -> bool:
    if has_console():
        print("\n========== CLASSIFIER TESTS ==========")

    results: list[tuple[str, bool, str]] = []

    # Purity tests (synchronous, no DB)
    results.append(_test_fingerprint_invariants())
    results.append(_test_score_neutral_baseline())
    results.append(_test_score_long_directed())
    results.append(_test_score_beacon_template_low())
    results.append(_test_score_clamped())

    # DB-dependent tests (each gets its own fresh storage)
    results.append(await _test_rules_load_and_seed())
    results.append(await _test_rules_match_timestamp_beacon())
    results.append(await _test_rules_match_miss())
    results.append(await _test_rules_match_url())
    results.append(await _test_template_stats_insert())
    results.append(await _test_template_stats_update_new_src())
    results.append(await _test_template_stats_dedup_src())
    results.append(await _test_template_stats_srcs_cap())
    results.append(await _test_auto_beacon_threshold())
    results.append(await _test_auto_beacon_demote())
    results.append(await _test_auto_beacon_promote())

    # Integration
    results.append(await _test_integration_classify())
    results.append(await _test_integration_fallback())

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)

    if has_console():
        for name, ok, detail in results:
            marker = "[PASS]" if ok else "[FAIL]"
            suffix = f"  ({detail})" if detail else ""
            print(f"  {marker} {name}{suffix}")
        verdict = "PASSED" if passed == total else "FAILED"
        print(f"========== RESULT: {passed}/{total} {verdict} ==========")

    return passed == total
