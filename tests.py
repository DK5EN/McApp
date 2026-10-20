"""Startup regression suite for the classifier package (CLS-01).

Ephemeral tempfile-SQLite, mirroring the storage/sse startup-test pattern
already used elsewhere (never touches a live/persisted DB). Exercises the
package's public entry points end-to-end: Layer 1 (contract engine conformance), Layer 2
(template fingerprint/exemption/auto-beacon threshold), Layer 3 (score),
and the Classifier orchestrator (live classify + reclassify paths).

This module is synced via git subtree into other host repos (see the
top-level package docstring). It deliberately avoids importing a concrete
Storage class directly — mc-chat's `meshcom_mock.storage.Storage` and
MCProxy's `mcapp.sqlite_storage.SQLiteStorage` live one level above wherever
this subtree currently sits, under different import paths — so
`_make_storage()` below tries both, exactly the way `..classifier.types`'s
docstring already documents the subtree/host-repo boundary.

Host repos wire this into their own startup-test driver:
    - mc-chat: not required (pytest already covers this logic thoroughly);
      available for parity/quick smoke checks.
    - MCProxy: `scripts/run_startup_tests.py` imports
      `mcapp.classifier.tests.run_all_tests` alongside the other suites.
"""

from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path
from typing import Any, cast

from .classify import Classifier
from .engine import Engine, conformance_failures, load_contract
from .score import compute as score_compute
from .template import check_only, fingerprint, is_exempt, update_and_check
from .types import (
    AUTO_BEACON_TAG,
    CATEGORIES,
    DIRECTED_DST_RE,
    StorageProtocol,
    derive_category,
)

# Host layout: <pkg>/classifier/tests.py next to <pkg>/contract/ (MCProxy:
# src/mcapp/contract). The subtree stays path-agnostic otherwise.
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract" / "classifier_contract.json"


def _load_contract() -> tuple[Engine, dict[str, Any], str]:
    data: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    engine = load_contract(data)
    return engine, data, f"{engine.taxonomy_version}:{engine.source_commit}"


async def _make_storage(db_path: str) -> StorageProtocol:
    """Construct + initialize a real (tempfile) SQLite-backed storage,
    trying MCProxy's layout first, then mc-chat's."""
    import importlib

    try:
        _sqlite_storage = importlib.import_module("mcapp.sqlite_storage")  # MCProxy layout
        return cast(StorageProtocol, await _sqlite_storage.create_sqlite_storage(db_path))
    except ImportError:
        # mc-chat layout. Imported dynamically (not `from ..storage import Storage`) because
        # this module is a git subtree synced into MCProxy, where `..storage` resolves to
        # `mcapp.storage` (a namespace package with no `Storage`) — a static relative import
        # would make mypy fail here even though this branch never actually executes in
        # MCProxy (the try above always succeeds).
        _mock_storage = importlib.import_module("meshcom_mock.storage")
        mock_storage = _mock_storage.Storage(db_path)
        await mock_storage.initialize()
        return cast(StorageProtocol, mock_storage)


async def _store_unclassified(storage: StorageProtocol, msg: dict[str, Any]) -> None:
    """Insert a row via the host's real ingestion path, portably across the two
    ``store_message`` signatures: MCProxy's requires a ``raw`` positional arg,
    mc-chat's does not. Introspect and adapt so this suite runs in both repos."""
    params = inspect.signature(storage.store_message).parameters  # type: ignore[attr-defined]
    if "raw" in params:
        await storage.store_message(msg, json.dumps(msg))  # type: ignore[attr-defined]
    else:
        await storage.store_message(msg)  # type: ignore[attr-defined]


def _msg(
    src: str = "OE1XYZ-1",
    dst: str = "20",
    text: str = "hello world",
    msg_id: str = "AAAA0001",
) -> dict[str, Any]:
    return {"msg_id": msg_id, "src": src, "dst": dst, "msg": text}


class _OccurrenceStorage:
    """Test shim wrapping a real storage.

    Answers ``count_recent_messages_by_template_src`` from an injected list of
    ``(template_hash, src, ts_ms)`` occurrences so the auto-beacon 24 h/72 h
    *window* branches of ``update_and_check`` can be exercised at controlled
    timestamps without populating the messages table. Every other call
    (upsert/get/set on the template row) delegates to the wrapped, real
    tempfile-backed storage. This is the only way to isolate the 24 h branch as
    the deciding rule: in a live burst the 3-in-72 h rule (evaluated later but
    more permissive) always promotes first, so the 24 h branch is otherwise
    unreachable.
    """

    def __init__(self, inner: Any, occurrences: list[tuple[str, str, int]]) -> None:
        self._inner = inner
        self._occurrences = occurrences

    async def count_recent_messages_by_template_src(
        self, template_hash: str, src: str, since_ms: int
    ) -> int:
        return sum(
            1
            for h, s, ts in self._occurrences
            if h == template_hash and s == src and ts >= since_ms
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _UserActionStorage:
    """Test shim: forces ``upsert_beacon_template`` to report a given
    ``user_action`` ('promote'/'demote') so ``update_and_check``'s override
    branches can be exercised without persisting the override. All other calls
    delegate to the wrapped real storage.
    """

    def __init__(self, inner: Any, action: str) -> None:
        self._inner = inner
        self._action = action

    async def upsert_beacon_template(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        row = dict(await self._inner.upsert_beacon_template(*args, **kwargs))
        row["user_action"] = self._action
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def run_all_tests() -> bool:
    """Run the classifier startup regression suite. Returns True iff all pass."""
    results: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "classifier_test.db")
        storage = await _make_storage(db_path)
        try:
            await _run_suite(storage, results)
        finally:
            # aiosqlite (mc-chat's Storage) keeps a background connection
            # thread alive until closed -- skipping this hangs the process
            # at event-loop teardown when run standalone (not a classifier
            # bug; caught during CLS-01 authoring).
            await storage.close()  # type: ignore[attr-defined]

    for label, ok in results:
        print(f"    {'✅ PASS' if ok else '❌ FAIL'} | {label}")

    return all(ok for _, ok in results)


async def _run_suite(storage: StorageProtocol, results: list[tuple[str, bool]]) -> None:
    """The actual test body, run inside run_all_tests()'s try/finally.

    Split into per-layer coroutines so each stays within the statement-count
    lint budget; they all share the single tempfile-backed ``storage``.
    """
    await _suite_engine(results)
    await _suite_directed_dst(results)
    await _suite_template(storage, results)
    await _suite_score(results)
    await _suite_orchestrator(storage, results)


async def _suite_directed_dst(results: list[tuple[str, bool]]) -> None:
    """Replay directed_dst_vectors.json — the cross-repo DIRECTED contract.

    Storage-free on purpose: it pins the PREDICATE and the engine rule that
    carries it, so it can run identically in every host repo. The webapp
    replays the same vectors against its own isDirectedDst().
    """
    vectors_path = Path(__file__).with_name("directed_dst_vectors.json")
    contract: dict[str, Any] = json.loads(vectors_path.read_text(encoding="utf-8"))
    vectors: list[dict[str, Any]] = contract["vectors"]

    results.append(("directed: vectors file is non-empty", len(vectors) > 0))

    mismatches = [
        v["name"]
        for v in vectors
        if (DIRECTED_DST_RE.search(v["dst"]) is not None) != v["is_directed"]
    ]
    results.append(
        (
            f"directed: DIRECTED_DST_RE replays all {len(vectors)} vectors"
            + (f" (failed: {mismatches})" if mismatches else ""),
            mismatches == [],
        )
    )

    # The engine's own 'directed' rule must agree with the predicate compiled in
    # types.py, or the L1 flag and is_exempt() drift apart.
    engine, _data, _cid = _load_contract()
    engine_mismatches = [
        v["name"]
        for v in vectors
        if v["dst"] and engine.classify("x", "XX1ABC-1", v["dst"]).directed != v["is_directed"]
    ]
    results.append(
        (
            "directed: contract engine agrees with the vectors on every dst"
            + (f" (failed: {engine_mismatches})" if engine_mismatches else ""),
            engine_mismatches == [],
        )
    )

    # template.is_exempt must read the same predicate: a long, non-human
    # message is exempt from auto-beacon promotion iff its dst is directed.
    long_tokens = ["a", "longer", "conversational", "message", "body"]
    exempt_mismatches = [
        v["name"]
        for v in vectors
        if v["dst"] and is_exempt(long_tokens, {"qso"}, v["dst"]) != v["is_directed"]
    ]
    results.append(
        (
            "directed: is_exempt agrees with the vectors on every dst"
            + (f" (failed: {exempt_mismatches})" if exempt_mismatches else ""),
            exempt_mismatches == [],
        )
    )


async def _suite_engine(results: list[tuple[str, bool]]) -> None:
    """Layer 1: the contract engine replays the exported conformance vectors."""
    engine, data, cid = _load_contract()
    failures = conformance_failures(engine, data)
    results.append(
        (
            f"engine: conformance over {len(data.get('vectors', []))} vectors"
            + (f" (first failure: {failures[0]})" if failures else ""),
            failures == [],
        )
    )
    results.append(("engine: contract id is <taxonomy_version>:<commit>", ":" in cid))
    results.append(("engine: 'qso' is a taxonomy tag", "qso" in engine.tag_ids))
    results.append(
        (
            "engine: plain greeting is greeting + qso",
            engine.classify("Hallo zusammen", "XX1ABC-1", "20").tags >= {"greeting", "qso"},
        )
    )
    derived = {
        "wx": derive_category({"weather"}, False),
        "dm": derive_category({"greeting", "qso"}, True),
        "plain": derive_category(set(), False),
        "known": all(c in CATEGORIES for c in ("timestamp_beacon", "qso", "other")),
    }
    results.append(
        (
            "category: derive_category follows the fixed table",
            derived["wx"] == "wx_beacon"
            and derived["dm"] == "directed"
            and derived["plain"] == "other"
            and bool(derived["known"]),
        )
    )


async def _suite_template(storage: StorageProtocol, results: list[tuple[str, bool]]) -> None:
    """Layer 2 fingerprint/exemption, auto-beacon thresholds, and overrides."""
    # ── Layer 2: fingerprint + tokenize + exemption ────────────────────────
    fp1 = fingerprint("Hello World! 123")
    fp2 = fingerprint("hello   world!  456")  # different digits, same shape
    results.append(("template: fingerprint is a 12-hex-char string", len(fp1) == 12))
    results.append(
        (
            "template: fingerprint normalizes digits/whitespace/case",
            fp1 == fp2,
        )
    )

    results.append(
        (
            "template: is_exempt — short token list is exempt",
            is_exempt(["hi"], {"qso"}, "20"),
        )
    )
    results.append(
        (
            "template: is_exempt — alert tag is exempt, greeting tag is not (issue #17)",
            is_exempt(["a", "longer", "message", "body"], {"alert"}, "20")
            and not is_exempt(["a", "longer", "message", "body"], {"greeting"}, "20"),
        )
    )
    results.append(
        (
            "template: is_exempt — directed dst (callsign-SSID) is exempt",
            is_exempt(["a", "longer", "message", "body"], {"qso"}, "OE5HWN-12"),
        )
    )
    results.append(
        (
            "template: is_exempt — long, non-human, non-directed is NOT exempt",
            not is_exempt(["a", "longer", "conversational", "message"], {"qso"}, "20"),
        )
    )
    results.append(
        (
            "template: check_only matches is_exempt on the same inputs",
            check_only(_msg(text="a longer conversational message", dst="20"), {"qso"})
            == is_exempt(["a", "longer", "conversational", "message"], {"qso"}, "20"),
        )
    )

    # ── Layer 2: auto-beacon threshold escalation ──────────────────────────
    # AUTO_BEACON_RULES' cheapest rule is "count >= 8, lifetime". Feed the
    # same non-exempt template 8 times and confirm it transitions once.
    beacon_text = "regular status update from the node right now"
    transitioned_at: int | None = None
    now_ms = 1_770_000_000_000
    for i in range(8):
        result = await update_and_check(
            storage, _msg(text=beacon_text, msg_id=f"BEACON{i:03d}"), now_ms + i, tags={"qso"}
        )
        if result.transitioned:
            transitioned_at = i
    results.append(
        (
            "template: auto-beacon transitions on the 8th lifetime occurrence",
            transitioned_at == 7,
        )
    )

    final_check = await update_and_check(
        storage, _msg(text=beacon_text, msg_id="BEACON999"), now_ms + 100, tags={"qso"}
    )
    results.append(
        ("template: template stays flagged as beacon after transitioning", final_check.is_beacon)
    )

    # ── Layer 2: windowed auto-beacon thresholds (5-in-24h, 3-in-72h) ───────
    # These use _OccurrenceStorage to inject the windowed message counts at
    # controlled timestamps (see the shim's docstring for why isolating the
    # 24 h branch requires a shim). The template row itself is real: each
    # update_and_check upserts it once (lifetime count == 1 << 8), so the
    # lifetime rule provably cannot fire — only the window branch under test.

    # 5-in-24h: four prior occurrences within 24 h + the current message == 5.
    now_24h = 1_770_100_000_000
    text_24h = "fast beacon window status cycle marker alpha"
    fp_24h = fingerprint(text_24h)
    occ_24h = [(fp_24h, "OE24H-1", now_24h - h * 3600 * 1000) for h in (1, 2, 3, 4)]
    res_24h = await update_and_check(
        _OccurrenceStorage(storage, occ_24h),
        _msg(src="OE24H-1", dst="20", text=text_24h, msg_id="W24H0001"),
        now_24h,
        tags={"qso"},
    )
    results.append(
        (
            "template: auto-beacon transitions on the 5-in-24h threshold",
            res_24h.transitioned and res_24h.is_beacon,
        )
    )

    # 3-in-72h: two prior occurrences 48 h old (inside 72 h, OUTSIDE 24 h, so
    # the 24 h rule cannot fire) + the current message == 3.
    now_72h = 1_770_200_000_000
    text_72h = "slow beacon window status cycle marker bravo"
    fp_72h = fingerprint(text_72h)
    occ_72h = [(fp_72h, "OE72H-1", now_72h - 48 * 3600 * 1000) for _ in range(2)]
    res_72h = await update_and_check(
        _OccurrenceStorage(storage, occ_72h),
        _msg(src="OE72H-1", dst="20", text=text_72h, msg_id="W72H0001"),
        now_72h,
        tags={"qso"},
    )
    results.append(
        (
            "template: auto-beacon transitions on the 3-in-72h threshold (24h rule idle)",
            res_72h.transitioned and res_72h.is_beacon,
        )
    )

    # Below every threshold: a single prior occurrence must NOT promote.
    text_below = "lonely beacon window marker charlie ping"
    fp_below = fingerprint(text_below)
    res_below = await update_and_check(
        _OccurrenceStorage(storage, [(fp_below, "OEBLW-1", now_72h - 10 * 3600 * 1000)]),
        _msg(src="OEBLW-1", dst="20", text=text_below, msg_id="WBLW0001"),
        now_72h,
        tags={"qso"},
    )
    results.append(
        (
            "template: no auto-beacon below all thresholds",
            not res_below.transitioned and not res_below.is_beacon,
        )
    )

    # ── Layer 2: user_action promote/demote overrides ───────────────────────
    # promote forces beacon despite a fresh (count == 1) template.
    promo_text = "promoted template body marker delta echo"
    res_promote = await update_and_check(
        _UserActionStorage(storage, "promote"),
        _msg(src="OEPRM-1", dst="20", text=promo_text, msg_id="PRM00001"),
        now_24h,
        tags={"qso"},
    )
    results.append(
        (
            "template: user_action='promote' is a beacon regardless of count",
            res_promote.is_beacon and not res_promote.transitioned,
        )
    )

    # demote suppresses even a template that is already auto_beacon=1.
    demo_text = "demoted template body marker foxtrot golf"
    demo_hash = fingerprint(demo_text)
    await storage.upsert_beacon_template(demo_hash, demo_text, "OEDEM-1", now_24h)
    await storage.set_template_auto_beacon(demo_hash, True)
    res_demote = await update_and_check(
        _UserActionStorage(storage, "demote"),
        _msg(src="OEDEM-1", dst="20", text=demo_text, msg_id="DEM00001"),
        now_24h,
        tags={"qso"},
    )
    results.append(
        (
            "template: user_action='demote' suppresses beacon despite auto_beacon flag",
            not res_demote.is_beacon and not res_demote.transitioned,
        )
    )


async def _suite_score(results: list[tuple[str, bool]]) -> None:
    """Layer 3 info-score value range, low-content clamp, and ordering."""
    # ── Layer 3: score ──────────────────────────────────────────────────────
    score = score_compute(_msg(text="a normal conversational reply here"), {"qso"}, 0)
    results.append(("score: compute() returns a value in [0, 1]", 0.0 <= score <= 1.0))

    bot_score = score_compute(_msg(text="!wx"), {"bot"}, 0, automated_tags={"bot"})
    results.append(("score: bot tag is clamped low", bot_score <= 0.25))

    # Ordering property: an informative multi-token sentence must outscore a
    # low-information emoji-only or URL-only body.
    informative_score = score_compute(
        _msg(text="the repeater on the hill is back online after maintenance today"),
        {"qso"},
        0,
    )
    emoji_only_score = score_compute(_msg(text="😀😀😀😀😀😀"), {"qso"}, 0)
    url_only_score = score_compute(_msg(text="https://example.com/a/very/long/path"), {"qso"}, 0)
    results.append(
        (
            "score: informative sentence outscores an emoji-only body",
            informative_score > emoji_only_score,
        )
    )
    results.append(
        (
            "score: informative sentence outscores a URL-only body",
            informative_score > url_only_score,
        )
    )


async def _suite_orchestrator(storage: StorageProtocol, results: list[tuple[str, bool]]) -> None:
    """Orchestrator classify() live/reclassify paths and reclassify jobs."""
    # ── Orchestrator: Classifier.classify() ──────────────────────────────────
    engine, _data, cid = _load_contract()
    classifier = Classifier(storage, engine, cid)
    await classifier.load()

    cls_live = await classifier.classify(_msg(text="hello there", msg_id="LIVE0001"))
    results.append(("classify: live path tags a greeting", cls_live.category == "greeting"))
    results.append(
        (
            "classify: a human greeting is not auto_beacon and keeps qso",
            AUTO_BEACON_TAG not in cls_live.tags and "qso" in cls_live.tags,
        )
    )
    results.append(
        ("classify: live path returns a 12-char template_hash", len(cls_live.template_hash) == 12)
    )

    cls_reclassify = await classifier.classify(
        _msg(text="hello there", msg_id="LIVE0001"), update_stats=False
    )
    results.append(
        (
            "classify: reclassify path (update_stats=False) matches the live-path hash",
            cls_reclassify.template_hash == cls_live.template_hash,
        )
    )

    # ── Orchestrator: reclassify() background job ────────────────────────────
    # No rows in `messages` were ever inserted (classify() doesn't write there
    # -- that's the host app's job), so this exercises an empty-batch run:
    # count_messages_to_classify()==0, the loop's first get_messages_to_classify()
    # returns [], and the job completes immediately.
    await storage.bump_classifier_version()
    await classifier.load()
    job = await classifier.reclassify()
    if job._task is not None:
        await job._task
    results.append(("reclassify: job completes", job.done))
    results.append(("reclassify: job reports no fatal error", job.error is None))

    # ── Orchestrator: reclassify() over a NON-empty batch ────────────────────
    # Persist a few unclassified rows via the real ingestion path (no classifier
    # attached to storage, so they land with classifier_ver=NULL / category=NULL),
    # then force a reclassify and assert every row is re-annotated.
    base_ts = 1_770_300_000_000
    for i in range(3):
        await _store_unclassified(
            storage,
            {
                "msg_id": f"RCL{i:05d}",
                "src": "OE7RCL-1",
                "dst": "20",
                "msg": f"conversational reclassify sample message number {i}",
                "timestamp": base_ts + i,
            },
        )

    reclassify_job = await classifier.reclassify(force=True)
    if reclassify_job._task is not None:
        await reclassify_job._task
    results.append(
        (
            "reclassify: non-empty batch processes every row without error",
            reclassify_job.done
            and reclassify_job.error is None
            and reclassify_job.total >= 3
            and reclassify_job.processed == reclassify_job.total,
        )
    )
    remaining = await storage.count_messages_to_classify(classifier_ver_below=classifier.version)
    results.append(("reclassify: no rows remain below the current version", remaining == 0))

    annotated = await storage.get_messages_to_classify(limit=100)
    rcl_rows = [r for r in annotated if str(r.get("msg_id", "")).startswith("RCL")]
    results.append(
        (
            "reclassify: inserted rows are re-annotated (category + version populated)",
            len(rcl_rows) == 3
            and all(
                r.get("category") is not None and r.get("classifier_ver") == classifier.version
                for r in rcl_rows
            ),
        )
    )
