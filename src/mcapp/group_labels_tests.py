"""Offline regression suite for group_labels.py and the refresh-script parser.

Never touches the network: the parser is fed an inline fixture page. Exit-code
friendly: `run_group_labels_tests()` returns True iff every case passes.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .group_labels import build_label_payload, load_snapshot, upstream_for

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "refresh_group_labels.py"


def _load_script() -> ModuleType | None:
    if not _SCRIPT.exists():  # prod tarball ships no scripts/
        return None
    spec = importlib.util.spec_from_file_location("refresh_group_labels", _SCRIPT)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["refresh_group_labels"] = mod
    spec.loader.exec_module(mod)
    return mod


def _snap() -> dict[str, Any]:
    return {
        "entries": [
            {"group": "232", "prefix": "OE", "hint": "Oesterreich", "remark": None},
            {"group": "262", "prefix": "DL", "hint": "Deutschland", "remark": None},
            {
                "lo": 2321,
                "hi": 2329,
                "prefix": "OE1-OE9",
                "prefix_base": "OE",
                "first_suffix": 1,
                "width": 1,
                "hint": "OE-Bundeslaender",
                "remark": None,
            },
            {
                "lo": 26200,
                "hi": 26299,
                "prefix": "DL00-DL99",
                "prefix_base": "DL",
                "first_suffix": 0,
                "width": 2,
                "hint": "DL regional",
                "remark": "de",
            },
            {"group": "26206", "prefix": "DL06", "hint": "exact wins", "remark": None},
            {
                "lo": 700,
                "hi": 702,
                "prefix": "weird",
                "prefix_base": None,
                "first_suffix": None,
                "width": None,
                "hint": "odd",
                "remark": None,
            },
            {"lo": 1, "hi": 5000, "prefix": "X", "hint": None, "remark": None},
        ]
    }


# The two range rows exactly as icssw.org serves them (note the spaces in the
# first one): the parser's range branch is only exercised through these.
_REAL_RANGE_ROWS = (
    '["2321 - 2329","OE1-OE9","OE-Bundesl\\u00e4nder",null],'
    '["26200-26299","DL00-DL99","Deutschland regionale Gruppen","Deutschsprachige Meldungen"]'
)


def _fixture_html(rows: int, extra: str = "") -> str:
    body = ",".join(f'["{i + 1}","P{i}","H{i}",null]' for i in range(rows))
    if extra:
        body += "," + extra
    head = '["Group","Pr\\u00e4fix","Hinweis","Reichweite"]'
    return "<x>[[" + head[1:-1] + "]," + body + '],[{"row":0}]</x>'


def run_group_labels_tests() -> bool:
    """Run the group-label regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []
    payload = build_label_payload(_snap(), {})
    labels = payload["labels"]

    results.append(("2325 -> OE5 range", labels["2325"]["prefix"] == "OE5"))
    results.append(
        (
            "2325 source range, range hint",
            labels["2325"]["source"] == "range" and labels["2325"]["hint"] == "OE-Bundeslaender",
        )
    )
    results.append(("26244 -> DL44", labels["26244"]["prefix"] == "DL44"))
    results.append(
        (
            "exact beats range",
            labels["26206"]["hint"] == "exact wins" and labels["26206"]["source"] == "icssw",
        )
    )
    results.append(("unparseable range keeps generic prefix", labels["701"]["prefix"] == "weird"))
    results.append(("range wider than 1000 not expanded", "2500" not in labels))
    results.append(
        (
            "countries has 232/262, not 2325",
            "232" in payload["countries"]
            and "262" in payload["countries"]
            and "2325" not in payload["countries"],
        )
    )

    ov = build_label_payload(_snap(), {"262": {"hint": "BRD", "prefix": None}})["labels"]["262"]
    results.append(
        (
            "override per-field inherit",
            ov["prefix"] == "DL" and ov["hint"] == "BRD" and ov["source"] == "mixed",
        )
    )
    own = build_label_payload(_snap(), {"#OE-SOTA": {"prefix": "SOTA"}})["labels"]["#OE-SOTA"]
    results.append(
        (
            "override-only key is own",
            own["source"] == "own" and own["prefix"] == "SOTA" and own["hint"] is None,
        )
    )
    empty = build_label_payload(_snap(), {"262": {"prefix": None}, "999": {}})
    results.append(
        (
            "all-None override ignored",
            empty["labels"]["262"]["source"] == "icssw" and "999" not in empty["labels"],
        )
    )
    new_c = build_label_payload(_snap(), {"999": {"hint": "x"}})
    results.append(("override-created 3-digit key lands in countries", "999" in new_c["countries"]))
    results.append(
        ("upstream_for ignores overrides", upstream_for(_snap())["262"]["source"] == "icssw")
    )

    real = load_snapshot()
    real_labels = build_label_payload(real, {})["labels"]
    results.append(("shipped snapshot has >= 50 entries", len(real["entries"]) >= 50))
    results.append(
        ("shipped snapshot 2325 -> OE5", real_labels.get("2325", {}).get("prefix") == "OE5")
    )

    script = _load_script()
    if script is None:
        results.append(("refresh script parser (skipped: no scripts/)", True))
    else:
        entries = script.build_entries(script.extract_rows(_fixture_html(60)))
        results.append(("parser: 60 fixture rows parse", len(entries) == 60))
        ranged = script.build_entries(script.extract_rows(_fixture_html(60, _REAL_RANGE_ROWS)))
        by_lo = {e["lo"]: e for e in ranged if "lo" in e}
        oe, dl = by_lo.get(2321, {}), by_lo.get(26200, {})
        results.append(
            (
                "parser: real range rows ('2321 - 2329', '26200-26299') parse as ranges",
                (oe.get("hi"), oe.get("prefix_base"), oe.get("first_suffix"), oe.get("width"))
                == (2329, "OE", 1, 1)
                and oe.get("remark") is None
                and (dl.get("hi"), dl.get("prefix_base"), dl.get("first_suffix"), dl.get("width"))
                == (26299, "DL", 0, 2)
                and dl.get("remark") == "Deutschsprachige Meldungen",
            )
        )
        try:
            script.build_entries(script.extract_rows(_fixture_html(10)))
            raised = False
        except script.ParseError:
            raised = True
        results.append(("parser: <50 rows raises", raised))
        pr = script.parse_prefix_range("DL00-DL99")
        results.append(
            (
                "parser: DL00-DL99",
                (pr["prefix_base"], pr["first_suffix"], pr["width"]) == ("DL", 0, 2),
            )
        )
        results.append(
            (
                "parser: unparseable prefix -> None",
                script.parse_prefix_range("DL 99")["prefix_base"] is None,
            )
        )

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")
    all_ok = all(ok for _, ok in results)
    print(f"    group_labels: {'PASS' if all_ok else 'FAIL'}")
    return all_ok
