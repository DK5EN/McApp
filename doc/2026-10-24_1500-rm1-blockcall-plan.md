# RM1 `blockcall` in the Remote view (campaign BLK-RM)

Firmware BLK-01 (fork-dev `1635ba56`, release v4.40a.10.23) gave the node a call blocklist and the
RM1 command `blockcall`: no argument reads, `none` clears, a comma list REPLACES the whole list.
This campaign makes it readable and writable from McApp's Remote nodes view.

Firmware references: `src/blocked_calls.cpp` (normalise, parse, match), `src/rm_format.h`
`rmFmtBlock` (reply), `src/rm_exec_write.cpp` `writeBlockcall`, `docs/campaign-blockcall.md`.

## Decisions (operator, 2026-10-24)

| #   | Topic      | Decision                                                                                                                                                                                                |
| --- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | Self-block | Refuse entries that block any own call (attached node, logged `src_call`s) or the target, by the firmware match rule: a bare entry covers every SSID, so bare `DK5EN` is refused for own `DK5EN-98`.    |
| D2  | Read-back  | The write ack `ok b=<csv>` is the read-back (firmware re-reads the stored list). McApp compares it with the requested list. No extra frame.                                                             |
| D3  | Long list  | A known list that is cut (`,+N`) or longer than 39 chars is read-only remotely; only `none` (clear all, confirm in the UI) is allowed. Server-enforced, not UI only. A write needs a known list (read). |
| D4  | Refresh    | Read on demand from the card's own button; not part of refresh-all.                                                                                                                                     |

## Contract

`src/mcapp/rm_blocklist_vectors.json` (v1) is canonical here, sha256-pinned in
`rm_blocklist_tests.py`, hand-copied to the webapp (`src/utils/__tests__/rm_blocklist_vectors.json`)
with its own pin. Sections: `normalize`, `parse`, `match` (firmware), `reply` (`b=` body), `build`
and `guard` (McApp sender rules, see the corpus `about`).

Python (`src/mcapp/rm_blocklist.py`, pure, imports nothing from `remote_cmd`):

```python
ARGS_MAX: Final = 39
LIST_MAX: Final = 16


def normalize(s: str) -> str | None: ...
def parse_list(text: str) -> tuple[str | None, str | None]: ...  # (csv, None) | (None, bad)
def matches(csv: str, call: str) -> bool: ...


@dataclass(frozen=True)
class BlockReply:
    calls: tuple[str, ...]
    omitted: int


def parse_reply_body(body: str) -> BlockReply | None: ...


class BlocklistError(ValueError):
    code: str  # invalid | self | target | too_long
    entry: str | None
    length: int | None


def build_args(calls: Sequence[str], own: Iterable[str], target: str) -> str: ...
def write_refusal(known: BlockReply | None, args: str) -> str | None: ...  # None = allowed
```

Webapp (`src/utils/nodeAdminBlocklist.ts`): `normalizeBlockCall`, `parseBlockList`,
`blockListMatches`, `parseBlockReply`, `buildBlockcallArgs` (`{ok: true, args}` or
`{ok: false, error, entry?, length?}`), `blockcallWriteRefusal`.

State (W2b): `fields.blockcall.value = {calls, omitted, requested, matches}` from every verified
`ok b=` reply; `requested`/`matches` only on a write ack (`null` on a read).

## Waves

| Wave | Owner              | Files                                                                                                                                       | State                                  |
| ---- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| W0   | orchestrator       | `rm_blocklist_vectors.json`, this doc                                                                                                       | done                                   |
| W1a  | implementer        | `src/mcapp/rm_blocklist.py`, `src/mcapp/rm_blocklist_tests.py`                                                                              | done ba6b496                           |
| W1b  | implementer        | webapp `src/utils/nodeAdminBlocklist.ts`, `src/utils/__tests__/nodeAdminBlocklist.spec.ts`, `src/utils/__tests__/rm_blocklist_vectors.json` | done webapp c67c9a1                    |
| W1 G | orchestrator       | `scripts/run_startup_tests.py` registration                                                                                                 | done, advisor APPROVED                 |
| W2a  | implementer        | `remote_cmd.py` (allowlist), `node_admin_service.py` (D1 + D3 gates) + their tests                                                          | done, advisor rework applied, APPROVED |
| W2b  | implementer        | `node_admin_state.py` + `node_admin_state_tests.py`                                                                                         | done                                   |
| W2c  | implementer        | webapp `RemoteBlocklistCard.vue`, `NodeAdminView.vue`, `nodeAdminCommands.ts`, `nodeAdminRemoteState.ts`, vocabulary, types + specs         | done (+ no Run again for writes)       |
| W3   | orchestrator       | CLAUDE.md, `doc/operations-reference.md`                                                                                                    | done                                   |
| W4   | operator OK needed | on-air bench (DK5EN-1 / DK5EN-92) after a dev release                                                                                       | open                                   |

## Known residuals

- A relay or neighbour on the RM path can be blocked; its frames (our replies) then vanish silently.
  Not detectable; the card says so.
- A list changed locally after our last read is overwritten by the next remote write.
- Blocking another own node (`DK5EN-14` from `DK5EN-98`) is allowed: D1 matches by the firmware
  rule, and an SSID entry blocks only that SSID.
- `err failed` on a write: firmware `writeBlockcall` sets the RAM list before the save fails, so the node runs the
  requested list while McApp still shows the old one (until the next Read or reboot). Nothing unseen is lost by the
  next write.
- The card's self check uses the BLE `I` register call plus the loaded history's `src_call`s; the server uses the
  configured attached call plus every target's logged `src_call`. They can differ (UDP-only box before the first
  sync); the UI can then offer a Save the server refuses with a clear 422, never the other way.
- History never offers Run again for a `blockcall` write (`isBlocklistWrite`): an old full-list write would drop
  entries added since, and an old `none` would clear without a confirm. Re-ask stays (cached reply, not a re-run).
