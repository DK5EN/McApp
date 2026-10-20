"""Classifier REST surface after the mcmap-contract unification.

Pins: rule CRUD and the dry-run tester are gone (404/405, never a handler),
`/api/classifier/status` reports `rule_count` (from the loaded contract) and
`contract_id`, and the startup order is template promotion backfill BEFORE the
reclassify sweep (reclassify reads the stored `auto_beacon` flag).
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
from fastapi import FastAPI

from .classifier import Classifier
from .classifier_contract import DEFAULT_CONTRACT_PATH, contract_id, load_engine
from .commands.constants import has_console
from .main import run_classifier_startup
from .sse_routes.classifier import build_classifier_router

_BASE = "http://localhost"
_EXPECTED_RULES = 69


class _Storage:
    """Recorder standing in for SQLiteStorage; only the calls under test."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def count_messages_to_classify(self, classifier_ver_below: int | None = None) -> int:
        return 5 if classifier_ver_below is not None else 9

    async def backfill_auto_beacon_templates(self) -> int:
        self.calls.append("promote")
        return 1

    async def get_meta(self, key: str) -> str | None:
        return None

    async def set_meta(self, key: str, value: str) -> None:
        self.calls.append("marker")


class _Manager:
    def __init__(self, classifier: Classifier, storage: _Storage) -> None:
        self._classifier = classifier
        self._storage = storage

    def require_storage(self) -> _Storage:
        return self._storage

    def require_classifier(self) -> Classifier:
        return self._classifier

    async def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        return None


class _Job:
    job_id = "j1"
    total = 5
    processed = 0
    done = False


class _RecordingClassifier:
    version = 3

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def reclassify(self, progress_cb: Any = None) -> _Job:
        self.calls.append("reclassify")
        return _Job()


async def run_classifier_routes_tests() -> bool:
    """Return True iff the classifier route and startup-order cases pass."""
    if has_console:
        print("\n🧪 Testing classifier routes + startup order:")
        print("=" * 55)
    results: list[tuple[str, bool]] = []

    def _record(label: str, ok: Any) -> None:
        results.append((label, bool(ok)))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    calls: list[str] = []
    storage = _Storage(calls)
    engine = load_engine()
    classifier = Classifier(cast(Any, storage), engine, contract_id(engine))
    app = FastAPI()
    app.include_router(build_classifier_router(cast(Any, _Manager(classifier, storage))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE) as c:
        for method, url in (
            ("GET", "/api/classifier/rules"),
            ("POST", "/api/classifier/rules"),
            ("POST", "/api/classifier/rules/test"),
            ("PATCH", "/api/classifier/rules/1"),
            ("DELETE", "/api/classifier/rules/1"),
        ):
            r = await c.request(method, url, json={"name": "x", "pattern": "y", "category": "z"})
            _record(f"{method} {url} is gone", r.status_code in (404, 405))

        r = await c.get("/api/classifier/status")
        body = r.json()
        contract = json.loads(DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"))
        _record("status 200", r.status_code == 200)
        _record(
            f"status rule_count == {_EXPECTED_RULES} (= contract rules)",
            body.get("rule_count") == _EXPECTED_RULES == len(contract["rules"]),
        )
        _record(
            "status contract_id is '<taxonomy>:<commit>'",
            isinstance(body.get("contract_id"), str)
            and body["contract_id"] == contract_id(engine)
            and ":" in body["contract_id"],
        )
        _record("status keeps classifier_version/jobs", {"classifier_version", "jobs"} <= set(body))

    calls.clear()
    rec = _RecordingClassifier(calls)
    await run_classifier_startup(cast(Any, rec), cast(Any, storage), None)
    _record(
        "startup: promotion backfill runs before reclassify",
        calls[:2] == ["promote", "reclassify"],
    )

    passed = sum(1 for _, ok in results if ok)
    if has_console:
        print(f"\n🧪 classifier_routes: {passed}/{len(results)} passed")
    return passed == len(results)
