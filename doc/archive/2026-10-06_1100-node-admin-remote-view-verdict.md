# Node Admin remote view - Fable verdict

Date 2026-10-06. Subject: `2026-10-06_1100-node-admin-remote-view-concept.md` (draft 1). Method: seven finders (firmware
facts, backend, webapp reuse, protocol/lockout, state model, test audit, requirements/UX), then three adversarial
verifiers on the session model. The protocol verifier ran the real `NodeAdminService` and SQLite storage against a temp
DB with a fake clock and transmit (nothing on air). All outcomes are folded into draft 2 of the concept.

## Findings in shipped v1 code (concept W0)

| #   | Sev    | Finding                                                                                                                                                                                                                              | Evidence                                 |
| --- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- |
| V1  | High   | Re-ask never consults the lockout guard, and `mark_node_admin_handed_off` restarts the 120 s / 10 min windows, so the same row can be re-asked every 61 s, even 862 s after the original send (past the node's cache: replay strike) | `node_admin_service.py:570-608`          |
| V2  | High   | A reply with a result over 63 characters is dropped without a log; the row becomes `no_reply`; two such rows trip McApp's own 5 min guard although the node answered                                                                 | `remote_cmd.py:325`, reproduced          |
| V3  | Medium | A ctr-0 reply binds to the newest unanswered sync row of any age; the second transport copy of a fresh sync reply verifies an older failed sync row and broadcasts twice (reproduced)                                                | `storage/node_admin.py:316-322`          |
| V4  | Medium | `txpower` bounded by the key's `tx_max` (0..30) only; the node's `allowed()` bounds by `TX_POWER_MAX`, so a higher value is a counted reject                                                                                         | `remote_cmd.py:79`, `remote_cmd.cpp:146` |
| V5  | Low    | `set_key` does not clear `_synced`; `_broadcast_row` searches only the newest 100 rows                                                                                                                                               | `node_admin_service.py`                  |

## Findings that changed the concept

| #   | Sev    | Finding                                                                                                                                                 | Outcome in draft 2                  |
| --- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| C1  | High   | The node's DM retry ladder resends McApp's `RM1 ` DMs (BLE and UDP, `loop_functions.cpp:4359-4421`); draft 2 §5.4 covers only the node's own web sender | 180 s cool-down; firmware ask D4    |
| C2  | High   | Fold by `reply_at` lets a late status override a newer ack; send order (id/ctr) is the only key matching node state                                     | §4.3                                |
| C3  | High   | "Guard is the hard floor" is false (re-ask bypass, other senders, late keyings, oversize replies)                                                       | §4.2 backstop + invariants          |
| C4  | High   | No spec mounts any BLE page component; "existing specs green" could not fail                                                                            | Characterisation specs first (§6)   |
| C5  | Medium | `rm=2` placement undecided in draft 2; "newest of sync/status" downgrades when only one carries it                                                      | Sticky per carrier; firmware ask D3 |
| C6  | Medium | `gps off` also clears Track; LED is RAM-only; status must be an atomic snapshot; `err` replies need an effect table                                     | §4.3 effect table                   |
| C7  | Medium | A `no_reply` toggle may have executed                                                                                                                   | Uncertain tile state (§3.3)         |
| C8  | Medium | Auto-sync sends the held command after a sync timeout (firmware drops it)                                                                               | Connect precondition, drop (D2)     |
| C9  | Medium | Read all stopping on any `err` ends at `sens` on most nodes; tagged errors count nothing                                                                | Stop only on silent outcomes        |
| C10 | Medium | Other senders share hwm, rate and reject count; McApp only discards foreign replies matching its own ctr                                                | Foreign-traffic pause               |
| C11 | Medium | Prune by id can evict the newest row of a rarely read command; `history()` sees 100 rows                                                                | Dedicated query, prune exemption    |
| C12 | Medium | `.toggle-btn` CSS is shared by three BLE sections; a toggle-grid extraction does not reuse it                                                           | `ToggleTile.vue`                    |
| C13 | Medium | Store `tick()` comes only from the view's clock; no wait-for-terminal                                                                                   | Store timer (W2c)                   |
| C14 | Medium | Firmware page order and labels (Light, Track) changed silently; Track warning omitted                                                                   | Restored; warning on Track on       |
| C15 | Medium | McApp-owned hand-written grammar corpus would pin McApp's guess, not the firmware                                                                       | Firmware strings; firmware ask D5   |
| C16 | Low    | `busy` is sender-side only; `bat=100` can also mean no battery; reboot "30 s" unsourced; `pos` args 26 > 23; wrong error citation                       | §2, §5, §7 corrected                |
| C17 | Low    | `/state` needs stale-response guard, `now_ms`, refetch on reconnect/visible; verified-but-unparseable status                                            | §4.3, §4.1                          |

## Refuted claims (do not re-investigate)

- "Fold must order by `reply_at`" (B5): a verified reply for row N describes the state after N however late it arrives;
  the node executes only above its shared high-water mark.
- "Forbidden characters `; { % --` in a write cost a counted reject" (P10): refused locally in `_check_text` and
  re-validated in `build_command_text`.
- "A 409 from a second tab carries no detail" (W-10): the routes map `NodeAdminBusyError` to 409 with `detail`.
- "A successful Connect is followed by a second auto-sync" (B7): a verified sync sets `_synced`; only a failed one
  leaves it unset.
- "`EditableField` cannot do a confirm-gated write" (W-06): the `NodeRadioCard` country field already does it.
- "`_send_pending` skips syntax validation" (B2): it re-validates via `build_text`; it skips in-flight, rate, lockout
  and any gate placed only in `send()`.
- "Commit-before-event race on `/state` refetch" (S10): `_broadcast_row` runs after `apply_node_admin_reply` commits.
- "lucide is pinned to 1.41.0" (recon): the webapp resolves 1.52.0; no icon blocker.

## Declined suggestions

- **Defer Phase B entirely** (UX finder): declined; the operator asked for the extended-command set. Adopted the split
  instead: design in full now, per-command code with the firmware.
- **Server-side sweep sequencer** (UX finder): declined. A client-side sequencer stops when the tab closes, which is the
  safe failure; the backend already serialises a target (one in flight, 409 for a second tab). A server-side job adds a
  lifecycle, a cancel route and restart recovery for a sequence of at most 11 frames.
- **Extract `RegisterBadgeBar`**: declined; BLE needs 2 states on spans, remote needs 6 plus click.
- **Compute ages server-side** (S12): partially adopted: `/state` returns `now_ms`, the client computes from it.
