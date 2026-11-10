# POL-002: Vendored subtrees and shared corpora

| Field | Value |
|---|---|
| ID | POL-002 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to | `src/mcapp/classifier/`, `src/mcapp/contract/`, every `*_vectors.json`, `contract/*.json`, `suppression_vectors.json` |
| Sources | `CLAUDE.md` ("Vendored Subtrees", "Key Gotchas": "Six vector corpora are hand-copied"); ADR-0018 |

## 1. Rule

1. **Never edit a vendored subtree in place.** `src/mcapp/classifier/` and `src/mcapp/contract/` come from
   mc-chat. Changes start in mc-chat, then reach MCProxy by split and pull (`../sop/SOP-005-wire-contract-change.md`).
2. **Each shared corpus has one canonical copy.** Its suite pins a sha256 of the local copy. Changing a corpus
   means changing every copy and the pin in the same commit.
3. **Pull and hash belong in one commit.** A subtree pull without the re-captured hash is incomplete.
4. **Prefix matters.** A file outside a subtree's prefix is deleted by the next pull. Keep shared files
   inside the prefix.
5. **Canonical location of each corpus** is named in its suite. The current list is in `CLAUDE.md`
   ("Six vector corpora", plus `remote_cmd_vectors.json` in the firmware repo and `rm_blocklist_vectors.json`).

## 2. Why

Two repos implement one contract. A silent edit in one repo makes that repo's parity test pass against a
corpus the other repo does not have. The pin turns a silent edit into a failing test.

## 3. Verify

- Each suite that reads a corpus checks its sha256 at startup. A mismatch fails the gate.
- `git subtree` history: `git log -- src/mcapp/contract` shows only squashed pulls, never hand edits.

## 4. Exceptions

- `ack_match_vectors.json` and `blocklist_decision_vectors.json` are in the webapp only. Their sha256 is pinned there too.
- A corpus with no sibling copy (for example `hashtag_dst_vectors.json`) still needs its own pin.

## 5. Related

- ADR-0018: why this is done this way
- `../sop/SOP-005-wire-contract-change.md`: the steps
- POL-007: what the corpora test
