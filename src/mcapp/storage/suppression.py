"""Server-side mirror of the webapp's message-suppression predicate.

Why this exists: the sidebar unread badge counts every stored message newer
than the read cursor, but the webapp never renders a message its own spam
filter or blocklist hides — and the read cursor only advances over rendered
bubbles. A conversation whose *newest* message is one the client hides can
therefore never have its badge cleared by any client action (see
doc/2026-09-19_2140-unread-suppression-plan.md, root cause). The fix is to
teach the server the same "will the client show this?" predicate so `unread`
can be computed over only the rows the client would actually display.

This module is the predicate ONLY — it is deliberately free of any
`get_conversation_summary` wiring (that is a separate wave's job) and free of
any I/O beyond `load_policy`'s three read-only SELECTs.

Mirrors, verbatim in decision order, two independent webapp functions
(webapp `src/stores/messages/predicates.ts`):

  * `isSpamByClassifier` (lines ~93-117) — gated entirely behind
    `prefs.enabled`; template-hash overrides beat category/tag/score rules;
    an explicit "not enabled" or "no match" is NOT-spam, never an error.
  * the `isTextBlocked` call inside `passesBaseGuards` (~line 143) — this is
    UNCONDITIONAL, evaluated independently of the spam prefs. A disabled spam
    filter must never disable the blocklist; that asymmetry is deliberate and
    pinned by the corpus (`enabled: false` + a matching blocked text is still
    suppressed).

`is_suppressed` combines the two exactly as the webapp's two call sites do:
a message is hidden if EITHER the text is blocked OR the classifier calls it
spam.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SuppressionPolicy:
    """The operator's suppression configuration, already normalised out of
    whatever the three source tables happen to contain. Every collection is
    a frozen, hashable type so a `SuppressionPolicy` is safe to compare for
    equality (used by `policy_is_noop`'s trivial-policy check and by tests)
    and to pass across an `asyncio.to_thread` boundary without aliasing."""

    enabled: bool
    hidden_categories: frozenset[str]
    min_info_score: float | None
    hide_auto_beacons: bool
    promoted_hashes: frozenset[str]
    demoted_hashes: frozenset[str]
    # Lower-cased at construction (policy_from_parts) so is_text_blocked
    # never has to re-lower every pattern on every call.
    blocked_texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MessageView:
    """The slice of a `messages` row the predicate needs, already coerced to
    the exact types `is_suppressed` expects — callers build this once per row
    via `view_from_row` rather than passing a raw DB row (or dict) around."""

    msg: str
    category: str
    tags: frozenset[str]
    info_score: float | None
    template_hash: str


# A policy that suppresses nothing, for any input. Returned by
# `policy_from_parts`/`load_policy` whenever the stored configuration cannot
# suppress anything (or could not be parsed at all) — the safe fallback,
# because failing OPEN (suppress nothing) can only ever make a message
# visible that the client would have hidden, never the reverse: an unread
# count that is too high is a stale badge, an unread count that is too low
# hides real traffic.
NOOP_POLICY = SuppressionPolicy(
    enabled=False,
    hidden_categories=frozenset(),
    min_info_score=None,
    hide_auto_beacons=False,
    promoted_hashes=frozenset(),
    demoted_hashes=frozenset(),
    blocked_texts=(),
)


def policy_is_noop(policy: SuppressionPolicy) -> bool:
    """True iff no possible message could ever be suppressed by `policy` —
    lets a caller skip the whole per-row pass (the common case: an install
    that never touched the spam-filter settings or blocklist).

    Blocked texts are unconditional, so a non-empty list always disqualifies
    a policy from being a no-op regardless of `enabled` (mirrors the
    asymmetry `is_text_blocked`/`is_spam_by_classifier` themselves encode).
    When `enabled` is False, `is_spam_by_classifier`'s first clause makes
    every other field irrelevant — including a non-empty `promoted_hashes` —
    so only `enabled=True` combined with at least one field that clause 2-6
    can actually act on makes the classifier half non-trivial.
    `demoted_hashes` never appears here: demoting can only PREVENT
    suppression, never cause it, so its contents can never turn a
    would-be-noop policy into one that suppresses something.
    """
    if policy.blocked_texts:
        return False
    if not policy.enabled:
        return True
    return not (
        policy.promoted_hashes
        or policy.hidden_categories
        or policy.hide_auto_beacons
        or policy.min_info_score is not None
    )


def _as_bool(value: object, *, default: bool) -> bool:
    """Coerce a JSON-decoded value to bool, tolerating any wrong type
    (a stored prefs blob is operator/client-controlled JSON, never trusted
    to have the right shape)."""
    return value if isinstance(value, bool) else default


def _as_optional_float(value: object) -> float | None:
    """Coerce a JSON-decoded value to float, or None if it is missing or the
    wrong type. `bool` is deliberately excluded even though it is a `int`
    subclass — a stray `true`/`false` must not be read as 1.0/0.0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_str_frozenset(value: object) -> frozenset[str]:
    """Coerce a JSON-decoded value to a frozenset of non-empty strings,
    dropping any non-string element and the empty string (an empty entry
    must never match a message field that is itself empty — see
    `is_spam_by_classifier`'s truthiness guards)."""
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item)


def policy_from_parts(
    filter_prefs: Mapping[str, Any],
    blocked_texts: Sequence[str],
    template_actions: Mapping[str, str | None],
) -> SuppressionPolicy:
    """Build a `SuppressionPolicy` from the three independent sources that
    together mirror the webapp's spam-filter state.

    `filter_prefs` is the camelCase JSON blob `POST /api/filter_prefs`
    stores verbatim and `get_filter_prefs()` reads back — `{}` when no row
    exists yet, so every key is optional. It is operator/client input, so
    every value is validated and a wrong type falls back to "this clause
    does nothing" rather than raising.

    `template_actions` maps `template_hash` -> `'promote' | 'demote' | None`
    (typically `beacon_templates.user_action`, pre-filtered to non-NULL by
    the caller — see `load_policy`). A hash mapped to anything other than
    the literal string `'promote'`/`'demote'` (including `None`) lands in
    neither set.
    """
    enabled = _as_bool(filter_prefs.get("enabled"), default=False)
    hidden_categories = _as_str_frozenset(filter_prefs.get("hiddenCategories"))
    min_info_score = _as_optional_float(filter_prefs.get("minInfoScore"))
    hide_auto_beacons = _as_bool(filter_prefs.get("hideAutoBeacons"), default=False)

    promoted = frozenset(
        template_hash
        for template_hash, action in template_actions.items()
        if action == "promote" and template_hash
    )
    demoted = frozenset(
        template_hash
        for template_hash, action in template_actions.items()
        if action == "demote" and template_hash
    )

    texts = tuple(text.lower() for text in blocked_texts if isinstance(text, str) and text)

    return SuppressionPolicy(
        enabled=enabled,
        hidden_categories=hidden_categories,
        min_info_score=min_info_score,
        hide_auto_beacons=hide_auto_beacons,
        promoted_hashes=promoted,
        demoted_hashes=demoted,
        blocked_texts=texts,
    )


def load_policy(conn: sqlite3.Connection) -> SuppressionPolicy:
    """Load the current suppression policy from an EXISTING connection.

    Synchronous and read-only by contract: the caller holds the connection
    under `db_read(...)` with `PRAGMA query_only=ON` already set (see
    `storage/query.py`'s existing `_run()` pattern), so this function issues
    no writes and does its own `conn.row_factory` assignment defensively
    rather than assuming the caller already set one.

    Three independent SELECTs, matched to the schema in
    `storage/migrations.py`:

      * `filter_prefs` — at most one row (`id = 1`); its `prefs` column is a
        JSON blob written verbatim by `POST /api/filter_prefs`.
      * `blocked_texts` — one row per pattern.
      * `beacon_templates WHERE user_action IS NOT NULL` — the WHERE clause
        is load-bearing, not an optimisation: on mcapp.local this table
        holds 14858 rows of which 0 have a user_action, so loading the whole
        table would be a needless full scan on every call for a set that is
        tiny by construction.

    Any table missing, any row malformed (bad JSON, wrong column types) or
    any other `sqlite3.Error` falls back to `NOOP_POLICY` rather than
    raising — a suppression predicate must never be the reason `unread`
    computation fails outright.
    """
    try:
        conn.row_factory = sqlite3.Row

        prefs_row = conn.execute("SELECT prefs FROM filter_prefs WHERE id = 1").fetchone()
        filter_prefs: dict[str, Any] = {}
        if prefs_row is not None:
            try:
                parsed_prefs = json.loads(prefs_row["prefs"])
            except (json.JSONDecodeError, TypeError):
                parsed_prefs = None
            if isinstance(parsed_prefs, dict):
                filter_prefs = parsed_prefs

        text_rows = conn.execute("SELECT text FROM blocked_texts").fetchall()
        blocked_texts = [row["text"] for row in text_rows if isinstance(row["text"], str)]

        action_rows = conn.execute(
            "SELECT template_hash, user_action FROM beacon_templates WHERE user_action IS NOT NULL"
        ).fetchall()
        template_actions: dict[str, str | None] = {
            row["template_hash"]: row["user_action"]
            for row in action_rows
            if isinstance(row["template_hash"], str)
        }

        return policy_from_parts(filter_prefs, blocked_texts, template_actions)
    except sqlite3.Error:
        return NOOP_POLICY


def _parse_tags(value: object) -> frozenset[str]:
    """`messages.tags` is stored as a JSON array string, but callers may
    also hand this a value already decoded to a list (or `NULL`/malformed
    JSON) — tolerate all three shapes, never raise."""
    if isinstance(value, list):
        return frozenset(item for item in value if isinstance(item, str))
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return frozenset()
        if isinstance(decoded, list):
            return frozenset(item for item in decoded if isinstance(item, str))
    return frozenset()


def view_from_row(row: Mapping[str, Any]) -> MessageView:
    """Build a `MessageView` from a `messages` row (a plain `dict`/mapping —
    callers holding a `sqlite3.Row` convert with `dict(row)` first, matching
    the existing `build_msg(dict(row))` pattern in `storage/query.py`).

    Every field tolerates `NULL`/missing/wrong-typed input: a classifier
    column can be `NULL` for a pre-classifier-era row, and this function
    must never be the reason a summary query raises.
    """
    msg = row.get("msg")
    category = row.get("category")
    template_hash = row.get("template_hash")

    return MessageView(
        msg=msg if isinstance(msg, str) else "",
        category=category if isinstance(category, str) else "",
        tags=_parse_tags(row.get("tags")),
        info_score=_as_optional_float(row.get("info_score")),
        template_hash=template_hash if isinstance(template_hash, str) else "",
    )


def is_text_blocked(msg: str, policy: SuppressionPolicy) -> bool:
    """Mirrors the webapp's `isTextBlocked`: case-insensitive substring
    match against every blocked pattern. Unconditional — never gated on
    `policy.enabled`, which governs the classifier half only."""
    if not policy.blocked_texts:
        return False
    lower_msg = msg.lower()
    return any(pattern in lower_msg for pattern in policy.blocked_texts)


def is_spam_by_classifier(view: MessageView, policy: SuppressionPolicy) -> bool:
    """Mirrors the webapp's `isSpamByClassifier` in its exact decision
    order — do not reorder these clauses, the order itself is the contract:

      1. disabled                        -> False (stop)
      2. template_hash in promoted       -> True  (stop)
      3. template_hash in demoted        -> False (stop)
      4. category in hidden_categories   -> True  (stop)
      5. hide_auto_beacons + auto_beacon -> True  (stop)
      6. info_score below min_info_score -> True
      7. otherwise                       -> False
    """
    if not policy.enabled:
        return False

    template_hash = view.template_hash
    if template_hash and template_hash in policy.promoted_hashes:
        return True
    if template_hash and template_hash in policy.demoted_hashes:
        return False

    if view.category and view.category in policy.hidden_categories:
        return True
    if policy.hide_auto_beacons and "auto_beacon" in view.tags:
        return True

    return (
        policy.min_info_score is not None
        and view.info_score is not None
        and view.info_score < policy.min_info_score
    )


def is_suppressed(view: MessageView, policy: SuppressionPolicy) -> bool:
    """A message is hidden from the client if EITHER half says so — the same
    combination the webapp applies at each of its filter call sites
    (`passesBaseGuards`'s blocklist check plus a separate `isSpamByClassifier`
    check alongside it, never one without the other)."""
    return is_text_blocked(view.msg, policy) or is_spam_by_classifier(view, policy)
