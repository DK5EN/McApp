#!/usr/bin/env python3
"""Read-only classifier soak check against a live McApp database (RB-012).

Runs ON the box with the system python3 and the stdlib only — no McApp import,
no venv. Opens the database read-only and never writes. Usage:

    python3 soak_classifier_check.py --since-ms <restart epoch ms> [--db PATH]

`--since-ms` is the service's ActiveEnterTimestamp in milliseconds:
    date -d "$(systemctl show mcapp -p ActiveEnterTimestamp --value)" +%s%3N

The human categories and the alert-tag rule in SQL_CONTRADICTIONS mirror
`_HUMAN_CATEGORIES` (classifier/template.py) and `_history_exempt_sql`
(storage/classifier_api.py). Re-check both before trusting a verdict from this
script after a classifier sync.
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime

SAMPLE = 15
LEGACY_HIDDEN = frozenset({"bot", "node", "software", "time"})

# All SQL is static text; every value is a bound parameter.
SQL_TAG_COUNTS = """
SELECT j.value AS tag, COUNT(*) AS n
FROM messages m, json_each(CASE WHEN json_valid(m.tags) THEN m.tags ELSE '[]' END) j
WHERE m.timestamp >= ? GROUP BY j.value ORDER BY n DESC
"""
SQL_OBSOLETE = """
SELECT COUNT(*) FROM messages m,
  json_each(CASE WHEN json_valid(m.tags) THEN m.tags ELSE '[]' END) j
WHERE m.timestamp >= ? AND j.value IN ('bot', 'node', 'software', 'time')
"""
SQL_AUTO_SAMPLE = """
SELECT m.src, m.dst, m.msg, m.template_hash FROM messages m
WHERE m.timestamp >= ? AND EXISTS (
  SELECT 1 FROM json_each(CASE WHEN json_valid(m.tags) THEN m.tags ELSE '[]' END) j
  WHERE j.value = 'auto_beacon')
ORDER BY m.timestamp DESC LIMIT ?
"""
SQL_HUMAN_SAMPLE = """
SELECT m.src, m.dst, m.msg, m.tags FROM messages m
WHERE m.timestamp >= ? AND m.type = 'msg' AND NOT EXISTS (
  SELECT 1 FROM json_each(CASE WHEN json_valid(m.tags) THEN m.tags ELSE '[]' END) j
  WHERE j.value = 'auto_beacon')
ORDER BY m.timestamp DESC LIMIT ?
"""
SQL_CONTRADICTIONS = """
SELECT bt.template_hash, bt.example_msg, bt.srcs, bt.count, bt.user_action
FROM beacon_templates bt
WHERE bt.auto_beacon = 1 AND EXISTS (
  SELECT 1 FROM messages m WHERE m.template_hash = bt.template_hash
  AND (m.category IN ('directed', 'alert') OR EXISTS (
    SELECT 1 FROM json_each(CASE WHEN json_valid(m.tags) THEN m.tags ELSE '[]' END) j
    WHERE j.value = 'alert')))
"""


def _zulu(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(text: object, n: int = 90) -> str:
    s = "" if text is None else str(text).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _pairs(rows: list[sqlite3.Row]) -> dict[object, object]:
    return {r[0]: r[1] for r in rows}


def check_new_rows(conn: sqlite3.Connection, since: int) -> None:
    """Steps 2a-2c: tags, classifier_ver and obsolete tags on rows since the restart."""
    q = conn.execute
    total = q("SELECT COUNT(*) FROM messages WHERE timestamp >= ?", (since,)).fetchone()[0]
    by_type = q(
        "SELECT type, COUNT(*) FROM messages WHERE timestamp >= ? GROUP BY type", (since,)
    ).fetchall()
    print(f"\n## 2a new rows: {total}  by type: {_pairs(by_type)}")
    for r in q(SQL_TAG_COUNTS, (since,)):
        print(f"  {r['tag']:<14} {r['n']}")
    empty = q(
        "SELECT COUNT(*) FROM messages WHERE timestamp >= ? AND tags = '[]'", (since,)
    ).fetchone()[0]
    nulls = q(
        "SELECT type, COUNT(*) FROM messages WHERE timestamp >= ? AND tags IS NULL GROUP BY type",
        (since,),
    ).fetchall()
    print(f"  tags='[]'      {empty}")
    print(f"  tags NULL      {_pairs(nulls)}")

    print("\n## 2b classifier_ver on new rows (ver, type, n):")
    for r in q(
        "SELECT classifier_ver, type, COUNT(*) FROM messages WHERE timestamp >= ? "
        "GROUP BY classifier_ver, type",
        (since,),
    ):
        print(f"  {r[0]!s:<6} {r[1]!s:<6} {r[2]}")
    all_vers = q(
        "SELECT classifier_ver, COUNT(*) FROM messages WHERE type = 'msg' GROUP BY classifier_ver"
    ).fetchall()
    print(f"  all msg rows by ver: {_pairs(all_vers)}")

    obs_new = q(SQL_OBSOLETE, (since,)).fetchone()[0]
    obs_all = q(SQL_OBSOLETE, (0,)).fetchone()[0]
    print(f"\n## 2c obsolete tags: new rows {obs_new}, all rows {obs_all}")


def print_samples(conn: sqlite3.Connection, since: int) -> None:
    """Step 2d: rows to read by eye."""
    print(f"\n## 2d newest {SAMPLE} auto_beacon rows (src > dst | template | text):")
    for r in conn.execute(SQL_AUTO_SAMPLE, (since, SAMPLE)):
        print(f"  {r['src']} > {r['dst']} | {r['template_hash']} | {_short(r['msg'])}")
    print(f"\n## 2d newest {SAMPLE} msg rows WITHOUT auto_beacon (src > dst | tags | text):")
    for r in conn.execute(SQL_HUMAN_SAMPLE, (since, SAMPLE)):
        print(f"  {r['src']} > {r['dst']} | {r['tags']} | {_short(r['msg'])}")


def check_templates(conn: sqlite3.Connection, since: int) -> None:
    """Step 3: promotion guard, counts, operator overrides, recent promotions."""
    q = conn.execute
    contra = q(SQL_CONTRADICTIONS).fetchall()
    print(f"\n## 3a contradictions (auto_beacon=1 with human history): {len(contra)}")
    for r in contra:
        print(
            f"  {r['template_hash']} n={r['count']} action={r['user_action']} "
            f"srcs={_short(r['srcs'], 40)} | {_short(r['example_msg'])}"
        )

    total, auto, ua = q(
        "SELECT COUNT(*), SUM(auto_beacon = 1), SUM(user_action IS NOT NULL) FROM beacon_templates"
    ).fetchone()
    print(f"\n## 3b beacon_templates total={total} auto_beacon=1: {auto} user_action set: {ua}")
    for r in q(
        "SELECT template_hash, user_action, auto_beacon, example_msg FROM beacon_templates "
        "WHERE user_action IS NOT NULL"
    ):
        agrees = (r["user_action"] == "promote") == bool(r["auto_beacon"])
        note = "flag agrees" if agrees else "flag differs; suppression reads user_action"
        print(
            f"  {r['template_hash']} {r['user_action']:<8} auto={r['auto_beacon']} ({note})"
            f" | {_short(r['example_msg'], 60)}"
        )

    rows = q(
        "SELECT template_hash, example_msg, srcs, count, first_seen FROM beacon_templates "
        "WHERE auto_beacon = 1 AND last_seen >= ? ORDER BY last_seen DESC",
        (_zulu(since),),
    ).fetchall()
    print(f"\n## 3c auto_beacon templates with last_seen since restart: {len(rows)}")
    for r in rows:
        print(
            f"  {r['template_hash']} n={r['count']} first={r['first_seen']} "
            f"srcs={_short(r['srcs'], 50)} | {_short(r['example_msg'])}"
        )


def check_prefs(conn: sqlite3.Connection) -> None:
    """Step 4: stored filter preferences and legacy tag names."""
    try:
        row = conn.execute("SELECT prefs FROM filter_prefs WHERE id = 1").fetchone()
    except sqlite3.OperationalError as exc:
        print(f"\n## 4 filter_prefs unreadable: {exc}")
        return
    prefs = json.loads(row[0]) if row and row[0] else {}
    hidden = prefs.get("hiddenTags")
    legacy = sorted(set(hidden or []) & LEGACY_HIDDEN)
    print(
        f"\n## 4 filter_prefs: enabled={prefs.get('enabled')} hiddenTags={hidden} "
        f"hideAutoBeacons={prefs.get('hideAutoBeacons')} minInfoScore={prefs.get('minInfoScore')}"
        f" legacyNames={legacy}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-ms", type=int, required=True)
    ap.add_argument("--db", default="/var/lib/mcapp/messages.db")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(f"# since {_zulu(args.since_ms)} ({args.since_ms} ms)")
        check_new_rows(conn, args.since_ms)
        print_samples(conn, args.since_ms)
        check_templates(conn, args.since_ms)
        check_prefs(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
