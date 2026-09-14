# Store-and-forward DM status (`failed` / `held`) — MCProxy implementation plan

Status: approved 2026-09-14, implemented in waves (see §7).
Source spec: `MeshCom-Firmware-DEV-Main/docs/client-integration-store-forward.md`
(firmware fork-main `150b0a4a`, stages 0, 2.1, 3, 4 of `docs/dm-transport-impl-plan-20260913.md`).

## 1. BLUF

Five new facts must reach the webapp: status `0x03 failed`, `0x04 held`, the holder callsign, a
durable per-message status that survives a reload, and precedence so a late `acked` always wins.
That needs schema v31, a rank-based write in `_handle_ack`, two new `ack_kind` values, and one
push-contract bump in mc-chat.

Store-node **configuration** (spec §5) and `held` synthesis from the legacy `:sto` text are
deliberately out of scope — see §6.

## 2. Surface, by file

| Area                                             | Change                                                                        |
| ------------------------------------------------ | ----------------------------------------------------------------------------- |
| `ble_protocol.py`                                | `ACK_KIND_BY_TYPE` / `ACK_TEXT_BY_TYPE` += `0x03 failed`, `0x04 held`         |
| `storage/migrations.py`, `storage/constants.py`  | v31: `messages.delivery_status TEXT`, `messages.holder TEXT`; bump to 31      |
| `storage/ingest.py`                              | `_handle_ack`: failed/held branches, precedence write, ledger rows, SSE shape |
| `storage/constants.py` `_MSG_SELECT`, `query.py` | surface the two columns in history                                            |
| `udp_handler.py`                                 | extUDP ack datagram accepts status 3/4 (free — gate reads `ACK_KIND_BY_TYPE`) |
| mc-chat `contract/push_contract.json` → subtree  | v10: noise clause widens `:ack` → `:ack` / `:rej` / `:sto`                    |
| `push_delivery.py`                               | that widened predicate                                                        |

Frame decoding needs **no** change. `parse_ack_appendix` already walks the frame by its length
byte, `_ACK_APPENDIX_MAX_LEN = 10` is a superset of the spec's `n <= 9`, and an unrecognised
status byte already falls through to `unknown(...)`. The spec's §6 byte vectors are therefore
pinned as tests, not implemented as new code.

## 3. Two defects the current code has the moment `0x03` arrives

1. **`_handle_ack` sets `send_success = 1` unconditionally, for every ack type.** A `failed` frame
   would mark the message transport-confirmed — the exact inversion of what it means. `failed`
   must leave `send_success` alone. `held` may set it: a store node demonstrably took the frame
   off the air.
2. **The non-`0x02` branch publishes `sent: True`.** For `failed` that is a lie on a field the
   webapp already renders. The `failed` event carries no `sent` key at all.

## 4. Precedence: one integer rank, applied on write

The spec's five prose rules in §2 collapse to a monotone rank, which is the only form that
survives out-of-order frames:

```
sent / node / gateway = 1     held = 2     failed = 3     acked = 4
```

`delivery_status` / `holder` are written only when `new_rank > rank(current)`. That yields every
rule in spec §2 and every sequence in spec §6 — `held → acked` ends acked, `acked → held` stays
acked, `failed → acked` ends acked — with no special case.

`messages.acked` and `messages.send_success` stay the single flags the bubble renders;
`delivery_status` is the detail behind them, exactly as `message_acks` is the detail behind
`send_success`.

`message_acks` also gains `kind='failed'` / `kind='held'` rows (`from_call` = destination /
holder). Its `(msg_id, kind, from_call)` primary key collapses the firmware's hourly `held` repeat
per holder for free, while two _different_ holders stay two rows — which spec §2 asks for.

## 5. `msg_status` SSE payloads

Cases 1-6 of `storage/ack_status_tests.py` stay byte-identical; these are new shapes only.

```
failed:  {msg_id, acked: false, failed: true, ack_kind: "failed", from: <destination>}
held:    {msg_id, sent: true, ack_kind: "held", from: <holder>, holder: <holder>}
```

`holder` is a deliberate alias of `from` on the `held` event. The spec names it, and the webapp
reads it without having to know this repo's attribution convention. The cost is one duplicated
key on one event.

**`acked: false` on the `failed` event is load-bearing, not decoration.** The webapp's
`msg:status` handler (`src/stores/messages.ts`, the branch after the `data.sent !== undefined`
one) treats _any_ event that carries no `sent` key and does not carry `acked === false` as a peer
acknowledgement, and patches the bubble to `msg_ack: true` — ✓✓ Delivered. A `failed` event
without `acked: false` would therefore render the one thing it is meant to deny, on every webapp
build that predates the frontend change. `acked: false` is already documented there as an explicit
no-op, so the key makes today's frontend ignore the event instead of inverting it. Do not drop it
once the webapp learns `ack_kind: "failed"` — it stays the compatibility floor.

The `held` event needs no such guard: `sent: true` sends it down the transport branch, which marks
the bubble sent and appends the ack without claiming delivery. That is already the honest rendering
of `held` on an un-updated webapp.

**No reuse of the existing `send_failed` event.** `MessageRouter._publish_send_failed`
(`main.py`) emits `{send_failed: true, dst, msg, ...}` for a LOCAL send failure — before the node
has assigned a msg_id, which is why the webapp matches it by `dst` + `msg` content. The firmware's
`0x03` is a different fact (the frame went out, the mesh gave up) and it _has_ a msg_id, so it
must not be squeezed into a content-matched event. The webapp change will set the same
`send_failed` / `send_fail_reason` display fields from the new msg_id-keyed branch; the wire
events stay distinct.

## 6. Decisions (approved 2026-09-14)

### 6.1 Status-driven push notifications are NOT in this plan

Spec §4 wants a push on `failed` ("not delivered to X") and on `acked`-after-`held` ("delivered to
X after being held by Y"). Today `push_delivery.py` subscribes to inbound **mesh messages**;
pushing on `msg_status` is a second, differently-shaped source with its own eligibility, dedup and
payload questions, and `acked`-after-`held` additionally needs a history lookup at push time to
know there was a holder.

That is its own plan. This one ships the honest status everywhere it is _displayed_; push stays
silent about delivery status. The only push change here is the noise-filter widening in §2, which
is about suppressing `:rej` / `:sto` chatter, not about announcing status.

### 6.2 No `held` synthesis from the legacy `:sto` text

Spec §3 recommends treating a `^\S{1,9}\s*:sto\d{3}( \S+)?$` DM as informational — but only "from
a node that never sends `0x04`", a condition we cannot cheaply establish. Synthesising `held` from
the text unconditionally would double-count on fork firmware, which consumes the text and emits
`0x04` instead.

So the text stays an ordinary, visible DM. `query.py`'s history exclusion is
`msg NOT GLOB '*:ack[0-9]*'`, which does not match `:sto` — history therefore already behaves as
spec §3 demands ("never filter it silently") with no change. Contract v10 makes it push-silent.
If an old node stays in the fleet and the operator wants the state rendered, synthesising `held`
from the text is a one-line follow-up on top of §4's rank write.

### 6.3 Store-node configuration (spec §5) is out of scope

`--store` / `--storecall` / `--storetime` / `--storeslots` / `--storenotice`, the `--mbox` dump,
the `[STORE];…` console lines and the `/?page=mailbox` GUI are a separate feature: configuring and
inspecting a store node, not showing DM status. Spec §5 says so itself.

The spec's open question — a `STORE` / `STON` field in the `SN` node-settings JSON frame — is
answered in `MeshCom-Firmware-DEV-Main/docs/client-integration-store-forward.md` §5.1 rather than
here, because it is a firmware decision this repo only consumes.

### 6.4 The webapp is a separate repo and a separate change

This plan changes the `msg_status` wire shape and adds two history fields. Nothing renders `held`
or `failed` until `/Users/martinwerner/WebDev/webapp` follows. Order of work: backend (this plan),
then webapp, then mc-chat, then one dev release covering all three.

## 7. Waves

Dispatched with `/orchestrate-waves`; disjoint file sets per wave, full gate after each wave,
orchestrator-only commits.

**Wave 1** (parallel)

- 1A: `ble_protocol.py`, `ble_protocol_tests.py` — status maps; spec §6's three byte vectors
  pinned as decode tests.
- 1B: `storage/migrations.py`, `storage/constants.py` (`LATEST_SCHEMA_VERSION`),
  `storage/migration_chain_tests.py` — migration 31, constant bumped in the same commit.

**Wave 2** (parallel)

- 2A: `storage/ingest.py`, `storage/ack_status_tests.py` — the two branches, the rank write, the
  ledger rows, the payloads of §5; new cases covering spec §6's three precedence sequences.
- 2B: `udp_handler.py`, `udp_parsing_tests.py` — extUDP status 3/4 vectors (behaviour arrives free
  from 1A; the tests pin it).

**Wave 3** (parallel)

- 3A: `storage/constants.py` (`_MSG_SELECT`), `storage/query.py`, `storage/query_tests.py` —
  `held` / `failed` survive a reload.
- 3B (orchestrator, two repos): push contract **v10** in mc-chat, `git subtree split` +
  `git subtree pull` here, widened noise predicate in `push_delivery.py`, new vectors, re-captured
  sha256 in `push_tests.py` — pull and hash in one commit.

**Wave 4** — `CLAUDE.md` section, this document's final state, prettier then
`ruff format --check .`, full gate.

Gate after every wave: `uvx ruff check`, `uvx ruff format --check .`,
`uv run mypy src/mcapp ble_service/src`, `uv run python scripts/run_startup_tests.py`.

## 8. Follow-up, not in this plan

- Webapp rendering of `held` / `failed` (§6.4).
- mc-chat: `_decode_aprs_struct()` treats `0x41` as an APRS packet (spec §4); its data model needs
  `status` + `holder`.
- Status-driven push notifications (§6.1).
- Store-node configuration surfaces (§6.3).
