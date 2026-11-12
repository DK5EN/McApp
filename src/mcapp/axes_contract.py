"""Host-side loader and streaming helpers for the shared message-axes contract.

Lives outside ``mcapp/classifier/`` on purpose (that package is a subtree vendored
from mc-chat). The contract arrives through the ``src/mcapp/contract/`` subtree and
is shipped by the production release (scripts/release.sh) next to
``classifier_contract.json``.

``AxesRuntime`` bundles the engine with the one constant it does not expose
(``ECHO_MIN_TOKENS``, read from the same JSON, so a refit contract changes both in
lockstep).  ``AxesBackfillState`` is the O(senders x 32
+ rows in the last ECHO_WINDOW_SEC) streaming mirror of what ingest computes row by
row, so the backfill reproduces ingest on sorted arrival.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .classifier.axes import AxesEngine, EchoCandidate, EchoMessage, load_axes_contract_file

DEFAULT_AXES_CONTRACT_PATH: Path = (
    Path(__file__).resolve().parent / "contract" / "axes_contract.json"
)

# The engine memoises message_format per text (LRU, default 100k entries). On the Pi
# Zero a few thousand recent texts are plenty (history pages re-render the same rows).
MEMO_CAP = 5000

# Width of an echo key: the row id zero-padded so string order == numeric order.
KEY_WIDTH = 12


def echo_key(row_id: int) -> str:
    return f"{row_id:0{KEY_WIDTH}d}"


@dataclass(frozen=True)
class AxesRuntime:
    """The engine plus the one constant it has no public accessor for.

    ``prior_limit`` / ``echo_window_sec`` come from the engine's public properties;
    ``echo_min_tokens`` is read from the contract JSON by `load_axes`.  ``format_lock``
    serialises the engine's memoised ``message_format``: it is called from the event
    loop (live annotation) AND from ``asyncio.to_thread`` workers (history pages), and
    the memo is an unsynchronised LRU (a hit racing an eviction raises KeyError).
    """

    engine: AxesEngine
    echo_min_tokens: int
    format_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    @property
    def source_commit(self) -> str:
        return self.engine.source_commit

    @property
    def prior_limit(self) -> int:
        """How many distinct earlier times `live_timing` can consume."""
        return self.engine.prior_limit

    @property
    def echo_window_sec(self) -> float:
        return self.engine.echo_window_sec


def load_axes(path: Path = DEFAULT_AXES_CONTRACT_PATH, *, memo_cap: int = MEMO_CAP) -> AxesRuntime:
    engine = load_axes_contract_file(path, memo_cap=memo_cap)
    constants = json.loads(path.read_text(encoding="utf-8"))["constants"]
    return AxesRuntime(engine=engine, echo_min_tokens=int(constants["ECHO_MIN_TOKENS"]))


def format_payload(runtime: AxesRuntime, text: str) -> dict[str, Any]:
    """Wire shape of the (read-time, never stored) format axis. Thread-safe."""
    with runtime.format_lock:
        fmt = runtime.engine.message_format(text)
    return {"dominant": fmt.dominant, "signals": list(fmt.signals)}


class AxesBackfillState:
    """Streaming state for the backfill.

    Rows are fed per second-group in id order (see `ingest.backfill_message_axes`), so
    "stored before this row" (= lower row id) holds for everything already absorbed within
    the second; rows of earlier seconds are filtered by id explicitly, which makes the
    result independent of arrival order.
    """

    def __init__(self, runtime: AxesRuntime) -> None:
        self._rt = runtime
        # +1: a same-second earlier row may sit at the tail and is filtered by `< t`.
        self._maxlen = runtime.prior_limit + 1
        # src -> deque of (time, smallest id seen at that time), distinct times ascending.
        self._times: dict[str, deque[tuple[int, int]]] = {}
        # src -> True once a time was evicted from its deque (history deeper than we hold).
        self._truncated: set[str] = set()
        # (time, id, src, msg_id, text, tokens) of rows within the echo window; the token
        # set is computed once per row instead of once per (row, later row) pair.
        self._window: deque[tuple[int, int, str, str | None, str, frozenset[str]]] = deque()

    def prior_for(self, src: str, t: int, row_id: int) -> list[int] | None:
        """Distinct earlier times of `src` with time < t from rows with id < row_id.

        None = the held history cannot answer exactly (out-of-order row on a sender with
        more history than we keep); the caller asks the database instead.
        """
        need = self._rt.prior_limit
        dq = self._times.get(src)
        eligible = [x for x, mid in dq if x < t and mid < row_id] if dq else []
        if len(eligible) >= need or src not in self._truncated:
            return eligible[-need:]
        return None

    def compute(
        self, row_id: int, src: str, text: str, t: int, prior: list[int]
    ) -> tuple[str, int, str | None]:
        """Return ``(timing, echo, echo_of)`` for the row; does not absorb it."""
        rt = self._rt
        timing = rt.engine.live_timing(prior, t)

        lo = t - rt.echo_window_sec
        while self._window and self._window[0][0] < lo:
            self._window.popleft()
        echo, echo_of = 0, None
        mine = rt.engine.echo_tokens(text)
        # Prefilter: a match needs containment > 0 of OUR tokens, so a window row sharing no
        # token can never be the original; skipping it only saves the engine re-tokenising.
        cands = [
            EchoCandidate(key=echo_key(i), src=s, text=x, time_unix=ts)
            for ts, i, s, _m, x, toks in self._window
            if ts <= t and i < row_id and not mine.isdisjoint(toks)
        ]
        hit = rt.engine.live_echo(EchoMessage(src=src, text=text, time_unix=t), cands)
        if hit is not None:
            echo = 1
            echo_of = next((m for _ts, i, _s, m, _x, _k in self._window if i == int(hit)), None)
        return timing, echo, echo_of

    def absorb(self, row_id: int, src: str, msg_id: str | None, text: str, t: int) -> None:
        dq = self._times.get(src)
        if dq is None:
            dq = self._times[src] = deque(maxlen=self._maxlen)
        if not dq or dq[-1][0] != t:
            if len(dq) == dq.maxlen:
                self._truncated.add(src)
            dq.append((t, row_id))
        elif dq[-1][0] == t and row_id < dq[-1][1]:
            dq[-1] = (t, row_id)
        self._window.append((t, row_id, src, msg_id, text, self._rt.engine.echo_tokens(text)))
