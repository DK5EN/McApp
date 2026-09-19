"""Regression suite for the message-suppression predicate
(`storage/suppression.py`).

Backend half of doc/2026-09-19_2140-unread-suppression-plan.md §D1 — the
predicate itself, built and pinned in isolation from the summary-query wiring
a later wave adds. Pure functions plus one ephemeral-SQLite-backed loader —
no TTY, no network, no `/etc/mcapp`. Follows the house pattern used by
`storage.read_cursor_tests.run_read_cursor_tests()`: a `(label, ok)` results
list, a PASS/FAIL line per case, and a bool return.

Coverage:
  1. `suppression_vectors.json` sha256 drift tripwire (pattern from
     `commands.hashtag_dst_tests`) plus every vector in the corpus replayed
     through the real `is_suppressed`.
  2. `load_policy` against a from-scratch ephemeral SQLite DB (schema copied
     from `storage/migrations.py`'s `filter_prefs`/`blocked_texts`/
     `beacon_templates` DDL): no `filter_prefs` row, a malformed JSON blob, a
     missing table entirely, and a populated policy including a
     `user_action IS NULL` row that must land in neither the promoted nor
     the demoted set.
  3. `view_from_row` against a `tags` column that is a JSON array string, an
     already-decoded list, `NULL`, and malformed JSON — plus a row missing
     every key outright.
  4. `policy_is_noop` true for an empty/default configuration and false as
     soon as any clause could actually suppress something, including the
     asymmetric cases (blocked text alone; a demoted-only policy staying a
     no-op because demoting can only prevent suppression, never cause it).

All entry points here are synchronous; `run_suppression_predicate_tests` is
declared `async` only to match the calling convention of the other suites in
this package (there is no I/O to await).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .suppression import (
    NOOP_POLICY,
    MessageView,
    SuppressionPolicy,
    is_suppressed,
    load_policy,
    policy_from_parts,
    policy_is_noop,
    view_from_row,
)

_VECTORS_PATH = Path(__file__).parent / "suppression_vectors.json"

# Captured sha256 of the raw vectors file bytes at the time this suite was
# written. A change here means the corpus changed -- either update this
# constant deliberately (and re-check the hand-written cases below still
# agree with the new corpus) or the change was unintentional and this
# tripwire just did its job. Mirrors commands/hashtag_dst_tests.py.
_EXPECTED_SHA256 = "4c9f5dea8c7011e632a9f6e0b59287de26bb291fa009c5d25218b0cb328675d1"

# DDL copied verbatim from storage/migrations.py (migrations v11, v16, v17).
# Kept as a local copy rather than importing anything from migrations.py: a
# suite that mirrors the wire schema, not one that depends on the migration
# runner ever having executed.
_FILTER_PREFS_DDL = """
    CREATE TABLE filter_prefs (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        prefs TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
"""

_BLOCKED_TEXTS_DDL = """
    CREATE TABLE blocked_texts (
        text TEXT PRIMARY KEY,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
"""

_BEACON_TEMPLATES_DDL = """
    CREATE TABLE beacon_templates (
        template_hash TEXT PRIMARY KEY,
        example_msg   TEXT NOT NULL,
        example_src   TEXT NOT NULL,
        srcs          TEXT NOT NULL,
        count         INTEGER NOT NULL DEFAULT 0,
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL,
        auto_beacon   INTEGER NOT NULL DEFAULT 0,
        user_action   TEXT
    )
"""


def _load_corpus() -> tuple[dict[str, Any], bool]:
    """Load the vendored suppression-predicate corpus. Canonical copy — the
    webapp's mirror must stay parse-equal to this one. Returns
    (contract, sha_ok) where sha_ok reports whether the raw file bytes still
    match the captured drift-tripwire hash."""
    raw = _VECTORS_PATH.read_bytes()
    sha_ok = hashlib.sha256(raw).hexdigest() == _EXPECTED_SHA256
    contract: dict[str, Any] = json.loads(raw)
    return contract, sha_ok


def _policy_from_spec(spec: dict[str, Any]) -> SuppressionPolicy:
    """Translate one corpus `policies[...]` entry (webapp wire shape) into a
    `SuppressionPolicy` via the real `policy_from_parts` — never a
    reimplementation of its parsing."""
    template_actions: dict[str, str | None] = {}
    for template_hash in spec.get("promotedHashes", []):
        template_actions[template_hash] = "promote"
    for template_hash in spec.get("demotedHashes", []):
        template_actions[template_hash] = "demote"

    filter_prefs = {
        "enabled": spec.get("enabled"),
        "hiddenCategories": spec.get("hiddenCategories"),
        "minInfoScore": spec.get("minInfoScore"),
        "hideAutoBeacons": spec.get("hideAutoBeacons"),
    }
    return policy_from_parts(filter_prefs, spec.get("blockedTexts", []), template_actions)


def _test_corpus_vectors(results: list[tuple[str, bool]]) -> None:
    """Replay every vector in suppression_vectors.json through the real
    is_suppressed, after pinning the corpus itself with the sha256
    tripwire."""
    contract, sha_ok = _load_corpus()
    results.append(
        ("suppression_vectors.json sha256 matches captured hash (drift tripwire)", sha_ok)
    )

    version = contract.get("version")
    results.append((f"suppression_vectors.json version == 1 (got {version!r})", version == 1))

    policies = {name: _policy_from_spec(spec) for name, spec in contract["policies"].items()}

    for vector in contract["vectors"]:
        policy = policies[vector["policy"]]
        view = view_from_row(vector["message"])
        actual = is_suppressed(view, policy)
        expected = vector["suppressed"]
        results.append((f"vector: {vector['name']}", actual == expected))


def _make_conn(*, with_filter_prefs_table: bool = True) -> sqlite3.Connection:
    """Build an in-memory SQLite connection with the three tables
    `load_policy` reads, mirroring migrations.py's DDL exactly."""
    conn = sqlite3.connect(":memory:")
    if with_filter_prefs_table:
        conn.execute(_FILTER_PREFS_DDL)
    conn.execute(_BLOCKED_TEXTS_DDL)
    conn.execute(_BEACON_TEMPLATES_DDL)
    conn.commit()
    return conn


def _insert_template(
    conn: sqlite3.Connection,
    template_hash: str,
    user_action: str | None,
) -> None:
    conn.execute(
        "INSERT INTO beacon_templates"
        " (template_hash, example_msg, example_src, srcs, count,"
        "  first_seen, last_seen, auto_beacon, user_action)"
        " VALUES (?, 'example', 'DK5EN-1', '[]', 1, 't0', 't0', 0, ?)",
        (template_hash, user_action),
    )


def _test_load_policy(results: list[tuple[str, bool]]) -> None:
    """load_policy against ephemeral SQLite DBs it never migrated itself."""
    # No filter_prefs row at all -> falls back to a no-op policy.
    conn = _make_conn()
    try:
        policy = load_policy(conn)
        results.append(("load_policy: no filter_prefs row -> NOOP_POLICY", policy == NOOP_POLICY))
    finally:
        conn.close()

    # Malformed JSON in the prefs blob -> falls back to a no-op policy rather
    # than raising.
    conn = _make_conn()
    try:
        conn.execute("INSERT INTO filter_prefs (id, prefs) VALUES (1, ?)", ("not-json-{",))
        conn.commit()
        policy = load_policy(conn)
        results.append(
            ("load_policy: malformed JSON prefs -> NOOP_POLICY, no raise", policy == NOOP_POLICY)
        )
    finally:
        conn.close()

    # filter_prefs table missing entirely -> falls back to a no-op policy.
    conn = _make_conn(with_filter_prefs_table=False)
    try:
        policy = load_policy(conn)
        results.append(
            (
                "load_policy: filter_prefs table missing -> NOOP_POLICY, no raise",
                policy == NOOP_POLICY,
            )
        )
    finally:
        conn.close()

    # A fully populated policy: one promoted hash, one demoted hash, a
    # user_action IS NULL row that must land in neither set, real prefs and
    # blocked texts.
    conn = _make_conn()
    try:
        prefs = json.dumps(
            {
                "enabled": True,
                "hiddenCategories": ["node_advert"],
                "minInfoScore": 0.5,
                "hideAutoBeacons": True,
            }
        )
        conn.execute("INSERT INTO filter_prefs (id, prefs) VALUES (1, ?)", (prefs,))
        conn.execute("INSERT INTO blocked_texts (text) VALUES (?)", ("badword",))
        _insert_template(conn, "promo123abc1", "promote")
        _insert_template(conn, "demo456def12", "demote")
        _insert_template(conn, "untouched0001", None)
        conn.commit()

        policy = load_policy(conn)
        results.append(("load_policy: enabled reflects stored prefs", policy.enabled is True))
        results.append(
            (
                "load_policy: hiddenCategories reflects stored prefs",
                policy.hidden_categories == frozenset({"node_advert"}),
            )
        )
        results.append(
            ("load_policy: minInfoScore reflects stored prefs", policy.min_info_score == 0.5)
        )
        results.append(
            (
                "load_policy: hideAutoBeacons reflects stored prefs",
                policy.hide_auto_beacons is True,
            )
        )
        results.append(
            (
                "load_policy: blocked_texts reflects stored rows",
                policy.blocked_texts == ("badword",),
            )
        )
        results.append(
            (
                "load_policy: promoted template lands in promoted_hashes",
                "promo123abc1" in policy.promoted_hashes,
            )
        )
        results.append(
            (
                "load_policy: demoted template lands in demoted_hashes",
                "demo456def12" in policy.demoted_hashes,
            )
        )
        results.append(
            (
                "load_policy: user_action IS NULL template lands in NEITHER set",
                "untouched0001" not in policy.promoted_hashes
                and "untouched0001" not in policy.demoted_hashes,
            )
        )
    finally:
        conn.close()


def _test_view_from_row(results: list[tuple[str, bool]]) -> None:
    """view_from_row against every shape `messages.tags` can arrive in."""
    view = view_from_row(
        {"msg": "hi", "category": "other", "tags": '["a", "b"]', "info_score": 0.4}
    )
    results.append(
        ("view_from_row: tags as JSON array string decodes", view.tags == frozenset({"a", "b"}))
    )

    view = view_from_row({"msg": "hi", "tags": ["x", "y"]})
    results.append(
        ("view_from_row: tags already a list is kept as-is", view.tags == frozenset({"x", "y"}))
    )

    view = view_from_row({"msg": "hi", "tags": None})
    results.append(
        ("view_from_row: tags NULL -> empty frozenset, no raise", view.tags == frozenset())
    )

    view = view_from_row({"msg": "hi", "tags": "not-json"})
    results.append(
        (
            "view_from_row: malformed JSON tags -> empty frozenset, no raise",
            view.tags == frozenset(),
        )
    )

    view = view_from_row({})
    expected_default = MessageView(
        msg="", category="", tags=frozenset(), info_score=None, template_hash=""
    )
    results.append(
        (
            "view_from_row: row missing every key -> safe defaults, no raise",
            view == expected_default,
        )
    )


def _test_policy_is_noop(results: list[tuple[str, bool]]) -> None:
    """policy_is_noop: true for a trivial configuration, false as soon as
    any clause could actually bite. Includes the two asymmetric cases: a
    disabled policy with a blocked text is NOT a no-op (blocklist is
    unconditional), and a demoted-only policy IS a no-op (demoting can only
    prevent suppression, never cause it)."""
    results.append(("policy_is_noop: NOOP_POLICY itself is a no-op", policy_is_noop(NOOP_POLICY)))

    empty = policy_from_parts({}, [], {})
    results.append(("policy_is_noop: {} prefs, no blocked texts -> no-op", policy_is_noop(empty)))

    hidden = policy_from_parts({"enabled": True, "hiddenCategories": ["x"]}, [], {})
    results.append(
        ("policy_is_noop: enabled + a hidden category -> NOT a no-op", not policy_is_noop(hidden))
    )

    blocked_but_disabled = policy_from_parts({"enabled": False}, ["bad"], {})
    results.append(
        (
            "policy_is_noop: disabled but a blocked text present -> NOT a no-op (unconditional)",
            not policy_is_noop(blocked_but_disabled),
        )
    )

    promoted = policy_from_parts({"enabled": True}, [], {"h1": "promote"})
    results.append(
        ("policy_is_noop: enabled + a promoted hash -> NOT a no-op", not policy_is_noop(promoted))
    )

    demoted_only = policy_from_parts({"enabled": True}, [], {"h1": "demote"})
    demoted_only_label = (
        "policy_is_noop: enabled + ONLY a demoted hash -> still a no-op"
        " (demoting can only prevent suppression)"
    )
    results.append((demoted_only_label, policy_is_noop(demoted_only)))


async def run_suppression_predicate_tests() -> bool:
    """Run the suppression-predicate regression suite. Returns True iff
    every case passes."""
    results: list[tuple[str, bool]] = []

    _test_corpus_vectors(results)
    _test_load_policy(results)
    _test_view_from_row(results)
    _test_policy_is_noop(results)

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    passed = all(ok for _, ok in results)
    print(f"  suppression_predicate: {'PASS' if passed else 'FAIL'}")
    return passed


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(0 if asyncio.run(run_suppression_predicate_tests()) else 1)
