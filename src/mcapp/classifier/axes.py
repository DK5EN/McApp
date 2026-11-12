"""Message axes (format, timing, echo) executing the mcmap-exported axes contract.

Mirrors mcmap's ``proxy/src/classifier/{live,structure,cadence,timingHmm,echo}.ts``.  The
contract carries the data (token patterns, both HMMs, thresholds, vectors); this module carries
the control flow.  Shared subtree: import only stdlib, ``regex`` and siblings here, and keep it
free of anything host-specific.

Float semantics follow the TypeScript reference operation by operation (log space, strict ``>``
over ascending index, probability floor before the log), because the vectors pin them.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import regex

SUPPORTED_SCHEMA = 1
_MEMO_CAP = 100_000

# Signal names structure emits; the contract's ``structure.signals`` must list exactly these.
_SIGNALS = (
    "locator",
    "unit-values",
    "key-value-list",
    "pipe-fields",
    "url",
    "command-led",
    "emoji-led",
    "callsign-led",
    "clock",
    "no-words",
    "mostly-prose",
    "mostly-data",
)
# State names the share-based signals refer to (the model's stateNames carry them).
_PROSE_STATE = "PROSE"
_DATA_STATES = ("MEASUREMENT", "LABELLED_LIST", "NUMERIC_REPORT")
_LEADING_SIGNAL = {"CMD": "command-led", "EMOJI": "emoji-led", "CALL": "callsign-led"}

CADENCE_PERIODIC = "periodic"
CADENCE_JITTERED = "jittered"
CADENCE_SINGLE = "single"

# The timing vocabulary; the contract's ``timing_values`` must equal it (checked on load).
TIMING_VALUES: tuple[str, ...] = ("one-shot", "scheduled", "burst", "jittered")


@dataclass(frozen=True)
class MessageFormat:
    dominant: str | None
    signals: tuple[str, ...]


@dataclass(frozen=True)
class EchoMessage:
    src: str
    text: str
    time_unix: float


@dataclass(frozen=True)
class EchoCandidate:
    """An earlier message offered to ``live_echo``.

    ``key`` is opaque and compared as a *string* for time ties (mcmap: ``msg_key``).  A host
    that keys by numeric row id must pass a zero-padded string so string order equals numeric
    order.
    """

    key: str
    src: str
    text: str
    time_unix: float


def _lg(x: float, floor: float) -> float:
    return math.log(max(x, floor))


def _viterbi(
    log_pi: Sequence[float],
    log_a: Sequence[Sequence[float]],
    emit: Sequence[Sequence[float]],
) -> list[int]:
    """Viterbi over precomputed log tables; ``emit[t][i]`` = log emission of state i at step t.

    Ties go to the lowest index (strict ``>`` over ascending index), as in the reference.
    """
    steps = len(emit)
    if steps == 0:
        return []
    k = len(log_pi)
    delta = [log_pi[i] + emit[0][i] for i in range(k)]
    psi: list[list[int]] = []
    for t in range(1, steps):
        nxt = [0.0] * k
        back = [0] * k
        for j in range(k):
            best = -math.inf
            arg = 0
            for i in range(k):
                v = delta[i] + log_a[i][j]
                if v > best:
                    best = v
                    arg = i
            nxt[j] = best + emit[t][j]
            back[j] = arg
        psi.append(back)
        delta = nxt
    last = 0
    for i in range(1, k):
        if delta[i] > delta[last]:
            last = i
    path = [0] * steps
    path[steps - 1] = last
    for t in range(steps - 2, -1, -1):
        path[t] = psi[t][path[t + 1]]
    return path


def _compile(pattern: str, flags: str, label: str) -> regex.Pattern[str]:
    if flags != "":
        raise ValueError(f"{label}: unsupported flags {flags!r}")
    try:
        return regex.compile(pattern, regex.V0)
    except regex.error as exc:
        raise ValueError(f"{label}: pattern does not compile: {exc}") from exc


class AxesEngine:
    # Class attribute so a test (or a tighter host) can shrink it.
    MEMO_CAP = _MEMO_CAP

    def __init__(self, data: Mapping[str, Any]) -> None:
        schema = data.get("schema")
        if schema != SUPPORTED_SCHEMA:
            raise ValueError(f"unsupported axes contract schema {schema!r}")
        self._source_commit = str(data.get("source", {}).get("commit", ""))
        self._load_constants(data["constants"])
        self._timing_values = tuple(str(v) for v in data["timing_values"])
        if self._timing_values != TIMING_VALUES:
            raise ValueError("axes contract: timing_values differ from the engine's vocabulary")

        self._load_structure(data["structure"])
        self._load_timing(data["timing_model"])
        self._memo: OrderedDict[str, MessageFormat] = OrderedDict()

    def _load_constants(self, c: Mapping[str, Any]) -> None:
        self._cadence_window = int(c["CADENCE_WINDOW"])
        self._min_intervals = int(c["MIN_INTERVALS"])
        self._tolerance = float(c["TOLERANCE"])
        self._min_share = float(c["MIN_SHARE"])
        self._max_multiple = int(c["MAX_MULTIPLE"])
        self._min_median = float(c["MIN_MEDIAN_SEC"])
        self._live_gaps = int(c["LIVE_TIMING_GAPS"])
        self._echo_window = float(c["ECHO_WINDOW_SEC"])
        self._echo_min_tokens = int(c["ECHO_MIN_TOKENS"])
        self._echo_containment = float(c["ECHO_CONTAINMENT"])
        self._echo_min_units = int(c["ECHO_MIN_WORD_UTF16_UNITS"])
        self._gap_floor = float(c["LOG_GAP_FLOOR_SEC"])
        self._prob_floor = float(c["VITERBI_PROB_FLOOR"])
        self._unit_min = int(c["STRUCTURE_UNIT_VALUES_MIN"])
        self._kv_min = int(c["STRUCTURE_KEY_VALUE_LIST_MIN"])
        self._pipe_min = int(c["STRUCTURE_PIPE_FIELDS_MIN"])
        self._prose_share = float(c["STRUCTURE_MOSTLY_PROSE_SHARE"])
        self._data_share = float(c["STRUCTURE_MOSTLY_DATA_SHARE"])

    def _load_structure(self, s: Mapping[str, Any]) -> None:
        if tuple(s["signals"]) != _SIGNALS:
            raise ValueError("axes contract: structure.signals differ from the engine's signals")
        self._signals = tuple(str(x) for x in s["signals"])
        classes = [str(x) for x in s["token_classes"]]
        pats = s["token_patterns"]
        if [str(p["cls"]) for p in pats] != classes:
            raise ValueError("axes contract: token_patterns do not follow token_classes order")
        self._classes = tuple(classes)
        alternation = "|".join(
            f"(?P<{p['cls']}>{_compile(str(p['pattern']), str(p['flags']), p['cls']).pattern})"
            for p in pats
        )
        # One alternation, first match wins, like the sticky named-group RegExp in the reference.
        self._token_re = _compile(alternation, "", "token alternation")
        ws = str(s["whitespace_class"])
        self._ws = _compile(f"[{ws}]", "", "whitespace class")
        split = s["echo_token_split"]
        self._echo_split = _compile(str(split["pattern"]), str(split["flags"]), "echo split")

        m = s["model"]
        self._state_names = tuple(str(x) for x in m["stateNames"])
        model_classes = [str(x) for x in m["classes"]]
        self._obs_index = {c: model_classes.index(c) for c in model_classes}
        pf = self._prob_floor
        self._s_log_pi = [_lg(x, pf) for x in m["pi"]]
        self._s_log_a = [[_lg(x, pf) for x in row] for row in m["A"]]
        self._s_log_b = [[_lg(x, pf) for x in row] for row in m["B"]]
        if not (len(self._s_log_pi) == len(self._state_names) == int(m["states"])):
            raise ValueError("axes contract: structure model state count mismatch")

    def _load_timing(self, t: Mapping[str, Any]) -> None:
        pf = self._prob_floor
        self._t_mu = [float(x) for x in t["mu"]]
        self._t_s2 = [float(x) for x in t["sigma2"]]
        self._t_log_pi = [_lg(x, pf) for x in t["pi"]]
        self._t_log_a = [[_lg(x, pf) for x in row] for row in t["A"]]
        self._t_class_of = tuple(str(x) for x in t["classOf"])

    # -- metadata -------------------------------------------------------------------------

    @property
    def source_commit(self) -> str:
        return self._source_commit

    @property
    def timing_values(self) -> tuple[str, ...]:
        return self._timing_values

    @property
    def format_values(self) -> frozenset[str]:
        return frozenset(self._state_names) | frozenset(self._signals)

    # -- format ---------------------------------------------------------------------------

    def tokenize(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        n = len(text)
        pos = 0
        is_ws = self._ws.match
        match = self._token_re.match
        while pos < n:
            while pos < n and is_ws(text, pos) is not None:
                pos += 1
            if pos >= n:
                break
            m = match(text, pos)
            if m is None or m.end() == pos:
                pos += 1
                continue
            cls = m.lastgroup
            out.append((cls if cls is not None else "PUNCT", m.group()))
            pos = m.end()
        return out

    def _state_path(self, tokens: Sequence[tuple[str, str]]) -> list[int]:
        obs = [self._obs_index[cls] for cls, _ in tokens]
        emit = [[row[o] for row in self._s_log_b] for o in obs]
        return _viterbi(self._s_log_pi, self._s_log_a, emit)

    def _signals_of(self, tokens: Sequence[tuple[str, str]], names: Sequence[str]) -> list[str]:
        counts: dict[str, int] = {}
        for cls, _ in tokens:
            counts[cls] = counts.get(cls, 0) + 1

        def count(c: str) -> int:
            return counts.get(c, 0)

        sig: list[str] = []
        for cls, minimum, name in (
            ("LOC", 1, "locator"),
            ("UNIT", self._unit_min, "unit-values"),
            ("KV", self._kv_min, "key-value-list"),
            ("PIPE", self._pipe_min, "pipe-fields"),
            ("URL", 1, "url"),
        ):
            if count(cls) >= minimum:
                sig.append(name)
        led = _LEADING_SIGNAL.get(tokens[0][0]) if tokens else None
        if led is not None:
            sig.append(led)
        if count("TIME") > 0 or count("DATE") > 0:
            sig.append("clock")
        if tokens and count("WORD") == 0 and count("CALL") == 0 and count("LOC") == 0:
            sig.append("no-words")
        denom = max(1, len(names))
        if names.count(_PROSE_STATE) / denom >= self._prose_share:
            sig.append("mostly-prose")
        # three shares, each divided separately like the reference (float-identical)
        if sum(names.count(st) / denom for st in _DATA_STATES) >= self._data_share:
            sig.append("mostly-data")
        return sig

    def _format_of(self, text: str) -> MessageFormat:
        tokens = self.tokenize(text)
        names = [self._state_names[s] for s in self._state_path(tokens)]
        dominant: str | None = None
        if tokens:
            best = 0
            for name in self._state_names:
                n = names.count(name)
                if n > best:
                    best = n
                    dominant = name
        return MessageFormat(dominant, tuple(self._signals_of(tokens, names)))

    def states_of(self, text: str) -> list[str]:
        """Hidden-state names per token (diagnostics and vectors)."""
        return [self._state_names[s] for s in self._state_path(self.tokenize(text))]

    def message_format(self, text: str) -> MessageFormat:
        """Format of `text`, LRU-memoised. Call from one event loop only: the memo is not
        thread-safe (a hit racing an eviction raises KeyError)."""
        hit = self._memo.get(text)
        if hit is not None:
            self._memo.move_to_end(text)
            return hit
        out = self._format_of(text)
        self._memo[text] = out
        if len(self._memo) > self.MEMO_CAP:
            self._memo.popitem(last=False)
        return out

    # -- echo -----------------------------------------------------------------------------

    def echo_tokens(self, text: str) -> frozenset[str]:
        # str.lower() follows the interpreter's Unicode tables (CPython 3.11: 14.0) while
        # Node follows a newer version; a few dozen recently assigned capitals lowercase
        # differently. Accepted: real traffic does not use them.
        units = self._echo_min_units
        return frozenset(
            w
            for w in self._echo_split.split(text.lower())
            if len(w.encode("utf-16-le")) // 2 >= units
        )

    def live_echo(self, cur: EchoMessage, window: Iterable[EchoCandidate]) -> str | None:
        mine = self.echo_tokens(cur.text)
        if len(mine) < self._echo_min_tokens:
            return None
        lo = cur.time_unix - self._echo_window
        cands = sorted(
            (c for c in window if c.src != cur.src and lo <= c.time_unix <= cur.time_unix),
            # JS `<` on msg keys compares UTF-16 code units, not code points.
            key=lambda c: (c.time_unix, c.key.encode("utf-16-be", "surrogatepass")),
        )
        for c in cands:
            theirs = self.echo_tokens(c.text)
            if len(theirs) < self._echo_min_tokens:
                continue
            common = sum(1 for w in mine if w in theirs)
            if common / len(mine) >= self._echo_containment:
                return c.key
        return None

    # -- timing ---------------------------------------------------------------------------

    def _is_on_clock(self, gap: float, period: float) -> bool:
        for k in range(1, self._max_multiple + 1):
            target = k * period
            if abs(gap - target) <= self._tolerance * target:
                return True
        return False

    def cadence(self, times: Sequence[float]) -> str:
        t = sorted(x for x in times if math.isfinite(x))[-self._cadence_window :]
        gaps = [t[i] - t[i - 1] for i in range(1, len(t))]
        if len(gaps) < self._min_intervals:
            return CADENCE_SINGLE
        s = sorted(gaps)
        mid = len(s) // 2
        med = s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2
        on_clock = sum(1 for g in gaps if self._is_on_clock(g, med)) / len(gaps) if med > 0 else 0
        periodic = med >= self._min_median and on_clock >= self._min_share
        return CADENCE_PERIODIC if periodic else CADENCE_JITTERED

    def hmm_classes_of_gaps(self, gaps: Sequence[float]) -> list[str]:
        if not gaps:
            return []
        floor = self._gap_floor
        pf = self._prob_floor
        emit: list[list[float]] = []
        for g in gaps:
            x = math.log(max(g, floor))
            row: list[float] = []
            for mu, s2 in zip(self._t_mu, self._t_s2, strict=True):
                dens = math.exp(-((x - mu) ** 2) / (2 * s2)) / math.sqrt(2 * math.pi * s2)
                row.append(math.log(max(dens, pf)))
            emit.append(row)
        path = _viterbi(self._t_log_pi, self._t_log_a, emit)
        return [self._t_class_of[s] for s in path]

    def live_timing(self, prior_times: Iterable[float], t: float) -> str:
        earlier = sorted({p for p in prior_times if math.isfinite(p) and p < t})
        if not earlier:
            return "one-shot"
        earlier.append(t)
        if self.cadence(earlier[-self._cadence_window :]) == CADENCE_PERIODIC:
            return "scheduled"
        recent = earlier[-(self._live_gaps + 1) :]
        gaps = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        cls = self.hmm_classes_of_gaps(gaps)
        return cls[-1] if cls else "jittered"


def load_axes_contract(data: Mapping[str, Any]) -> AxesEngine:
    return AxesEngine(data)


def load_axes_contract_file(path: Path) -> AxesEngine:
    return AxesEngine(json.loads(path.read_text(encoding="utf-8")))


def _chk_tokenize(e: AxesEngine, x: Mapping[str, Any]) -> str | None:
    got = [c for c, _ in e.tokenize(x["text"])]
    return None if got == x["classes"] else f"{x['text']!r}: got {got}; want {x['classes']}"


def _chk_echo_tokens(e: AxesEngine, x: Mapping[str, Any]) -> str | None:
    got = e.echo_tokens(x["text"])
    if got == frozenset(x["tokens"]):
        return None
    return f"{x['text']!r}: got {sorted(got)}; want {sorted(x['tokens'])}"


def _chk_format(e: AxesEngine, x: Mapping[str, Any]) -> str | None:
    f = e.message_format(x["text"])
    states = e.states_of(x["text"])
    if f.dominant == x["dominant"] and list(f.signals) == x["signals"] and states == x["states"]:
        return None
    return (
        f"{x['text']!r}: got {f.dominant} {list(f.signals)} {states}; "
        f"want {x['dominant']} {x['signals']} {x['states']}"
    )


def _chk_cadence(e: AxesEngine, x: Mapping[str, Any]) -> str | None:
    got = e.cadence(x["times"])
    return None if got == x["cadence"] else f"{x['times']}: got {got}; want {x['cadence']}"


def _chk_timing_gaps(e: AxesEngine, x: Mapping[str, Any]) -> str | None:
    got = e.hmm_classes_of_gaps(x["gaps"])
    return None if got == x["classes"] else f"{x['gaps']}: got {got}; want {x['classes']}"


def _chk_live_timing(e: AxesEngine, x: Mapping[str, Any]) -> str | None:
    got = e.live_timing(x["prior_times"], x["t"])
    if got == x["timing"]:
        return None
    return f"{x['prior_times']} t={x['t']}: got {got}; want {x['timing']}"


def _chk_live_echo(e: AxesEngine, x: Mapping[str, Any]) -> str | None:
    cur = EchoMessage(x["cur"]["from"], x["cur"]["text"], x["cur"]["time_unix"])
    win = [EchoCandidate(w["msg_key"], w["from"], w["text"], w["time_unix"]) for w in x["window"]]
    got = e.live_echo(cur, win)
    return None if got == x["echo_of"] else f"{cur.text!r}: got {got}; want {x['echo_of']}"


_CHECKS = {
    "tokenize": _chk_tokenize,
    "echo_tokens": _chk_echo_tokens,
    "format": _chk_format,
    "cadence": _chk_cadence,
    "timing_gaps": _chk_timing_gaps,
    "live_timing": _chk_live_timing,
    "live_echo": _chk_live_echo,
}


def axes_conformance_failures(engine: AxesEngine, data: Mapping[str, Any]) -> list[str]:
    """Run every vector kind; return human-readable mismatches (empty = conformant)."""
    failures: list[str] = []
    vectors = data.get("vectors", {})
    for kind, check in _CHECKS.items():
        for i, x in enumerate(vectors.get(kind, [])):
            problem = check(engine, x)
            if problem is not None:
                failures.append(f"{kind} {i} {problem}")
    return failures
