"""Regression suite for the group label / callsign override routes (`labels.py`).

Plan: doc/2026-10-25_0900-conversation-nav-group-labels-plan.md §3, D4-D7, D11.
Fully offline: a real `SSEManager` + real SQLite storage in a temp dir, driven
through httpx's ASGI transport. Broadcasts are read from in-memory `SSEClient`
queues. Runner: `run_labels_routes_tests()` is ASYNC.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from ..commands.constants import has_console
from ..qrz_service import QrzLookupService
from ..secret_box import SecretBox
from ..sqlite_storage import create_sqlite_storage
from ..sse_handler import SSEClient, SSEManager
from .labels import build_labels_router

_BASE = "http://localhost"
_BAD = "ZZ-NOT-A-KEY-SECRET"
_LONG = "x" * 200


class _Router:
    def __init__(self, storage: Any) -> None:
        self.storage_handler = storage
        self.my_callsign = "DK5EN"
        self.filter_history_row = None

    def subscribe(self, _topic: str, _handler: Any) -> None:
        return

    def get_protocol(self, _name: str) -> Any:
        return None


def _events(client: SSEClient) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    while not client.queue.empty():
        lines = client.queue.get_nowait().strip("\n").split("\n")
        name = next((x[7:] for x in lines if x.startswith("event: ")), "")
        data = next((x[6:] for x in lines if x.startswith("data: ")), "null")
        out.append((name, json.loads(data)))
    return out


def _named(events: list[tuple[str, Any]], name: str) -> list[Any]:
    return [d for n, d in events if n == name]


async def _run(record: Any) -> None:  # noqa: PLR0915 - one linear scenario
    with tempfile.TemporaryDirectory() as tmp:
        storage = await create_sqlite_storage(Path(tmp) / "labels_test.db")
        try:
            mgr = SSEManager(host="127.0.0.1", port=0, message_router=_Router(storage))
            listener = SSEClient("l1")
            mgr.clients["l1"] = listener
            app = FastAPI()
            app.include_router(build_labels_router(mgr))
            http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE)

            r = await http.get("/api/group_labels")
            body = r.json()
            record(
                "L1. GET shape: labels, upstream, snapshot{fetched_at, source_url}; no countries",
                r.status_code == 200
                and set(body) == {"labels", "upstream", "snapshot"}
                and set(body["snapshot"]) == {"fetched_at", "source_url"}
                and "262" in body["labels"]
                and body["labels"]["262"]["source"] in {"icssw", "range"},
            )
            up262 = body["upstream"]["262"]

            r = await http.put("/api/group_labels/262", json={"hint": "Mein Hinweis"})
            lab = r.json().get("label", {})
            record(
                "L2. PUT per field: hint own, other fields inherited, source mixed",
                r.status_code == 200
                and lab.get("hint") == "Mein Hinweis"
                and lab.get("prefix") == up262["prefix"]
                and lab.get("source") == "mixed",
            )
            ev = _events(listener)
            pushed = _named(ev, "proxy:group_labels")
            record(
                "L3. PUT re-broadcasts bare proxy:group_labels {labels, countries}",
                len(pushed) == 1
                and set(pushed[0]) == {"labels", "countries"}
                and pushed[0]["labels"]["262"]["hint"] == "Mein Hinweis"
                and "262" in pushed[0]["countries"],
            )

            r = await http.put("/api/group_labels/99999", json={"prefix": "XY"})
            record(
                "L4. override on a key without upstream is source own",
                r.json()["label"]["source"] == "own" and r.json()["label"]["prefix"] == "XY",
            )
            _events(listener)

            r = await http.delete("/api/group_labels/262")
            after = (await http.get("/api/group_labels")).json()["labels"]["262"]
            record(
                "L5. DELETE reverts to upstream, 204 empty body, re-broadcasts",
                r.status_code == 204
                and r.content == b""
                and after["source"] == up262["source"]
                and len(_named(_events(listener), "proxy:group_labels")) == 1,
            )
            await http.delete("/api/group_labels/99999")
            _events(listener)

            ok_keys = True
            for key in ("*", "20", "%23OE-SOTA"):
                ok_keys = (
                    ok_keys
                    and (
                        await http.put(f"/api/group_labels/{key}", json={"remark": "r"})
                    ).status_code
                    == 200
                )
                await http.delete(f"/api/group_labels/{key}")
            record("L6. valid keys accepted: '*', numeric group, hashtag", ok_keys)
            _events(listener)

            bad: list[httpx.Response] = []
            bad.append(await http.put(f"/api/group_labels/{_BAD}", json={"hint": "h"}))
            bad.append(await http.delete(f"/api/group_labels/{_BAD}"))
            bad.append(await http.put("/api/group_labels/262", json={"hint": _LONG + _BAD}))
            bad.append(await http.put("/api/group_labels/262", json={"prefix": 5}))
            bad.append(await http.put("/api/group_labels/262", json={"nope": _BAD}))
            bad.append(await http.put("/api/group_labels/262", content=b"[" + _BAD.encode()))
            bad.append(await http.put(f"/api/callsign_info/{_BAD}/override", json={"qth": "q"}))
            bad.append(
                await http.put("/api/callsign_info/DK5EN/override", json={"first_name": _LONG})
            )
            record(
                "L7. invalid key / overlong / wrong type / unknown field / junk body -> 422",
                all(x.status_code == 422 for x in bad),
            )
            record(
                "L8. no 422 body echoes the submitted input",
                all(_BAD not in x.text and "xxxx" not in x.text for x in bad),
            )
            record(
                "L9. rejected writes broadcast nothing",
                _events(listener) == [],
            )

            r = await http.put(
                "/api/callsign_info/dk5en-12/override", json={"first_name": "Marty", "qth": "Ort"}
            )
            ev = _events(listener)
            info = _named(ev, "proxy:callsign_info")
            record(
                "L10. callsign override PUT: base call, manual flag, broadcast merged",
                r.status_code == 200
                and r.json()["info"]["first_name"] == "Marty"
                and len(info) == 1
                and info[0]["DK5EN"]["first_name"] == "Marty"
                and info[0]["DK5EN"]["qth"] == "Ort"
                and info[0]["DK5EN"].get("manual") is True,
            )

            r = await http.delete("/api/callsign_info/DK5EN/override")
            info = _named(_events(listener), "proxy:callsign_info")
            record(
                "L11. DELETE of an override-only callsign broadcasts an all-null entry",
                r.status_code == 204
                and info
                == [
                    {
                        "DK5EN": {
                            "first_name": None,
                            "qth": None,
                            "country": None,
                            "lat": None,
                            "lon": None,
                            "locator": None,
                        }
                    }
                ],
            )

            await storage.upsert_callsign_info(
                "OE5HWN", "found", 1, first_name="Hans", qth="Linz", country="Austria"
            )
            await http.put("/api/callsign_info/OE5HWN/override", json={"first_name": "Johann"})
            _events(listener)
            await http.delete("/api/callsign_info/OE5HWN/override")
            info = _named(_events(listener), "proxy:callsign_info")
            record(
                "L12. DELETE with a QRZ row broadcasts the restored QRZ entry (no manual)",
                len(info) == 1
                and info[0]["OE5HWN"]["first_name"] == "Hans"
                and "manual" not in info[0]["OE5HWN"],
            )

            burst = "".join([c async for c in mgr.initial_events("burst")])
            pos = burst.find("event: proxy:group_labels")
            data_line = burst[pos:].split("\n", 2)[1] if pos >= 0 else ""
            payload = json.loads(data_line[6:]) if data_line.startswith("data: ") else {}
            record(
                "L13. connect burst carries bare proxy:group_labels {labels, countries}",
                pos >= 0 and set(payload) == {"labels", "countries"} and "262" in payload["labels"],
            )

            # _publish merges an override; legacy payload without one.
            got: list[dict[str, Any]] = []

            async def on_info(delta: dict[str, Any]) -> None:
                got.append(delta)

            svc = QrzLookupService(
                storage,
                SecretBox(key_path=Path(tmp) / "secret.key", binding=b"board:TEST"),
                on_info=on_info,
            )
            qrz_info = {
                "first_name": "Hans",
                "qth": "Linz",
                "country": "Austria",
                "lat": None,
                "lon": None,
                "locator": None,
            }
            await svc._publish("OE5HWN", qrz_info)
            plain = got[-1]
            await http.put("/api/callsign_info/OE5HWN/override", json={"first_name": "Johann"})
            await svc._publish("OE5HWN", qrz_info)
            record(
                "L14. QRZ _publish: unchanged without override, merged (manual) with one",
                plain == {"OE5HWN": qrz_info}
                and got[-1]["OE5HWN"]["first_name"] == "Johann"
                and got[-1]["OE5HWN"]["qth"] == "Linz"
                and got[-1]["OE5HWN"].get("manual") is True,
            )
            await http.aclose()
        finally:
            await storage.close()


async def run_labels_routes_tests() -> bool:
    """Return True iff every label/override route case passes (async)."""
    if has_console:
        print("\n🧪 Testing group label routes:")
        print("=" * 55)
    results: list[tuple[str, bool]] = []

    def record(label: str, ok: Any) -> None:
        results.append((label, bool(ok)))
        if has_console:
            print(f"{'✅ PASS' if ok else '❌ FAIL'} | {label}")

    await _run(record)
    passed = sum(1 for _, ok in results if ok)
    if has_console or passed != len(results):
        for label, ok in results:
            if not ok:
                print(f"❌ FAIL | {label}")
        print(f"\n🧪 Label Routes Summary: {passed}/{len(results)} tests passed")
    return passed == len(results)


if __name__ == "__main__":
    _ok = asyncio.run(run_labels_routes_tests())
    print(f"labels_routes: {'PASS' if _ok else 'FAIL'}")
    raise SystemExit(0 if _ok else 1)
