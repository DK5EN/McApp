"""Observed sessions: a decoded, read-only view of RM1 remote-admin traffic.

Pure functions over stored message rows (plan:
doc/2026-10-11_1400-rm1-observed-sessions-plan.md). Nothing here transmits, reads
the DB or touches the clock: ``now_ms`` and the keys are passed in.

A command ``RM1 <ctr> <cmd>[ <args>] <tag>`` and its reply ``RM1 <ctr> ok|err
<result> <tag>`` are paired by (target, commander, ctr); both tags use the
COMMAND's orientation (dst = managed node, src = commander), so a reply frame
travels target -> commander. One session per (commander, target).

``verified`` is POSITIVE ONLY (owner decision 2026-10-11): present and true when
a key for the target was given and the tag checks out, otherwise the key is
absent, never false. The tag itself is never part of the output.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from . import remote_cmd
from .commands.parsing import resolve_dst_target, strip_relay_path
from .util import strip_ack_suffix

ACTIVE_MS: Final = 120_000  # a session is active while its last frame is younger than this
NO_REPLY_MS: Final = 120_000  # a command unanswered this long is "no reply"
DEDUP_MS: Final = 5_000  # identical frame again within this = a transport copy
UNIX_MIN_S: Final = 1_704_067_200  # 2024-01-01: below this a ctr is a plain counter
UNIX_SLACK_S: Final = 86_400  # a ctr up to a day ahead of the clock still reads as unix seconds


@dataclass
class _Exchange:
    ctr: int
    command: remote_cmd.ParsedCommand | None = None
    command_ms: int | None = None
    reply: remote_cmd.ParsedReply | None = None
    reply_ms: int | None = None
    command_ok: bool = False  # the slotted command verified against the target's key
    reply_ok: bool = False  # the slotted reply verified against the target's key


def _call(raw: object, *, dst: bool) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return remote_cmd.normalize_call(resolve_dst_target(raw) if dst else strip_relay_path(raw))
    except ValueError:
        return None


def _norm(call: str) -> str | None:
    try:
        return remote_cmd.normalize_call(call)
    except ValueError:
        return None


def split_fields(body: str) -> list[list[str]]:
    """``"t=4 p=2/2/22 a=-"`` -> ``[["t","4"],["p","2/2/22"],["a","-"]]``.

    Only ``key=value`` tokens; free text (an ``err`` reason) stays in ``result``.
    """
    out: list[list[str]] = []
    for token in body.split(" "):
        key, sep, value = token.partition("=")
        if sep and key:
            out.append([key, value])
    return out


def _sent_ms(ex: _Exchange, now_ms: int) -> int | None:
    if UNIX_MIN_S <= ex.ctr <= now_ms // 1000 + UNIX_SLACK_S:
        return ex.ctr * 1000
    return ex.command_ms if ex.command_ms is not None else ex.reply_ms


def _frame_ok(key: bytes | None, target: str, commander: str, text: str, *, command: bool) -> bool:
    """Whether this one frame authenticates under ``key`` (False without a key)."""
    if key is None:
        return False
    try:
        verify = remote_cmd.verify_command if command else remote_cmd.verify_reply
        return verify(key, target, commander, text) is not None
    except remote_cmd.RmError:
        return False


def _verified(ex: _Exchange) -> bool:
    """Every PRESENT part authenticated; a part that fails (or is forged) vetoes it."""
    if ex.command is None and ex.reply is None:
        return False
    return (ex.command is None or ex.command_ok) and (ex.reply is None or ex.reply_ok)


def _exchange_dict(
    ex: _Exchange, now_ms: int, *, keyed: bool, origin: str | None = None
) -> dict[str, Any]:
    reply: dict[str, Any] | None = None
    if ex.reply is not None:
        latency = (
            ex.reply_ms - ex.command_ms
            if ex.reply_ms is not None
            and ex.command_ms is not None
            and ex.reply_ms >= ex.command_ms
            else None
        )
        reply = {
            "status": ex.reply.status,
            "result": ex.reply.body,
            "fields": split_fields(ex.reply.body),
            "reply_ms": ex.reply_ms,
            "latency_ms": latency,
        }
    out: dict[str, Any] = {
        "ctr": ex.ctr,
        "sent_ms": _sent_ms(ex, now_ms),
        "cmd": ex.command.cmd if ex.command else None,
        "args": ex.command.args if ex.command else None,
        "command_ms": ex.command_ms,
        "reply": reply,
        "no_reply": (
            ex.reply is None and ex.command_ms is not None and now_ms - ex.command_ms > NO_REPLY_MS
        ),
    }
    if keyed and _verified(ex):
        out["verified"] = True
    if origin is not None:
        out["origin"] = origin
    return out


def _decode(
    row: Mapping[str, Any],
) -> (
    tuple[remote_cmd.ParsedCommand | None, remote_cmd.ParsedReply | None, str, str, str, int] | None
):
    """(command, reply, text, commander, target, timestamp) of an RM1 row, else None."""
    msg = row.get("msg")
    ts = row.get("timestamp")
    if not isinstance(msg, str) or not isinstance(ts, int):
        return None
    text = strip_ack_suffix(msg)
    cmd = remote_cmd.parse_command(text)
    rep = None if cmd else remote_cmd.parse_reply(text)
    if cmd is None and rep is None:
        return None
    src = _call(row.get("src"), dst=False)
    dst = _call(row.get("dst"), dst=True)
    if src is None or dst is None or src == dst:
        return None
    commander, target = (src, dst) if cmd is not None else (dst, src)
    return cmd, rep, text, commander, target, ts


def _origin(ctr: int, ours: Collection[int] | None) -> str | None:
    """``mcapp`` / ``other`` for an exchange of an own session, None when it proves nothing.

    ctr 0 is the sync: McApp's log always holds one, so another sender's sync would read
    ``mcapp``. It carries no origin at all.
    """
    if ours is None or ctr == 0:
        return None
    return "mcapp" if ctr in ours else "other"


def _slot(slots: list[_Exchange], ctr: int, *, is_command: bool, ok: bool) -> _Exchange:
    """The exchange a frame belongs to; opens one when none fits.

    ctr 0 (sync) repeats: a command opens a new exchange, a reply binds to the
    newest one still unanswered, else (a verifying reply only) to the newest one
    holding an unverified reply, which it then replaces.
    """
    found: _Exchange | None = None
    if ctr != 0:
        found = slots[0] if slots else None
    elif not is_command:
        found = next((e for e in reversed(slots) if e.reply is None), None)
        if found is None and ok:
            found = next((e for e in reversed(slots) if not e.reply_ok), None)
    if found is None:
        found = _Exchange(ctr=ctr)
        slots.append(found)
    return found


def _order(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    def ts(r: Mapping[str, Any]) -> int:
        v = r.get("timestamp")
        return v if isinstance(v, int) else 0

    return sorted(rows, key=ts)


def pair_rows(rows: Iterable[Mapping[str, Any]], pair: tuple[str, str]) -> list[Mapping[str, Any]]:
    """The RM1 rows of one (commander, target) pair, in either direction."""
    out: list[Mapping[str, Any]] = []
    for row in rows:
        frame = _decode(row)
        if frame is not None and (frame[3], frame[4]) == pair:
            out.append(row)
    return out


def build_sessions(
    rows: Iterable[Mapping[str, Any]],
    now_ms: int,
    keys: Mapping[str, bytes],
    own_calls: Collection[str] = (),
    mcapp_ctrs: Mapping[str, Collection[int]] | None = None,
) -> list[dict[str, Any]]:
    """Sessions newest first, each with its exchanges newest first.

    ``rows``: stored message rows (``src``, ``dst``, ``msg``, ``timestamp`` ms).
    ``keys``: target call -> derived key, only targets this box stores.
    ``own_calls``: calls that make a session "this McApp" (commander or target).
    ``mcapp_ctrs``: target call -> counters in McApp's own log. Exchanges of a session whose
    commander is one of ``own_calls`` get ``origin`` ``"mcapp"`` (ctr in that set) or
    ``"other"`` (another sender signing with our call); other sessions carry no ``origin``, and
    neither do ctr-0 (sync) exchanges: McApp's log always holds ctr 0, so it proves nothing.
    """
    own = {c for c in (_norm(o) for o in own_calls) if c}
    norm_keys = {n: k for t, k in keys.items() if (n := _norm(t))}
    # (commander, target) -> ctr -> exchanges (several only for ctr 0 = sync)
    book: dict[tuple[str, str], dict[int, list[_Exchange]]] = {}
    last_seen: dict[tuple[str, str], int] = {}
    seen_copy: dict[tuple[bool, str, str, str], int] = {}

    for row in _order(rows):
        frame = _decode(row)
        if frame is None:
            continue
        cmd, rep, text, commander, target, ts = frame
        ctr = cmd.ctr if cmd is not None else rep.ctr if rep is not None else 0
        pair = (commander, target)
        last_seen[pair] = max(last_seen.get(pair, 0), ts)

        copy_key = (cmd is not None, commander, target, text)
        prev = seen_copy.get(copy_key)
        seen_copy[copy_key] = ts
        if prev is not None and ts - prev <= DEDUP_MS:
            continue

        # With a key each frame is verified as it is slotted: a verifying frame replaces
        # an unverified occupant of its slot, never the other way round. Without a key
        # the first frame wins and nothing is verified.
        key = norm_keys.get(target)
        ok = _frame_ok(key, target, commander, text, command=cmd is not None)
        slots = book.setdefault(pair, {}).setdefault(ctr, [])
        ex = _slot(slots, ctr, is_command=cmd is not None, ok=ok)
        if cmd is not None and (ex.command is None or (ok and not ex.command_ok)):
            ex.command, ex.command_ms, ex.command_ok = cmd, ts, ok
        elif rep is not None and (ex.reply is None or (ok and not ex.reply_ok)):
            ex.reply, ex.reply_ms, ex.reply_ok = rep, ts, ok

    sessions: list[dict[str, Any]] = []
    for (commander, target), by_ctr in book.items():
        keyed = target in norm_keys
        ours: Collection[int] | None = None
        if commander in own:
            by_target = {n: c for t, c in (mcapp_ctrs or {}).items() if (n := _norm(t))}
            ours = by_target.get(target, ())
        exchanges = [
            _exchange_dict(
                ex,
                now_ms,
                keyed=keyed,
                origin=_origin(ex.ctr, ours),
            )
            for slots in by_ctr.values()
            for ex in slots
        ]
        exchanges.sort(
            key=lambda e: (e["sent_ms"] or 0, e["command_ms"] or e["reply"]["reply_ms"] or 0),
            reverse=True,
        )
        last_ms = last_seen[(commander, target)]
        sessions.append(
            {
                "commander": commander,
                "target": target,
                "own": commander in own or target in own,
                "last_ms": last_ms,
                "active": now_ms - last_ms < ACTIVE_MS,
                "exchanges": exchanges,
            }
        )
    sessions.sort(key=lambda s: s["last_ms"], reverse=True)
    return sessions
