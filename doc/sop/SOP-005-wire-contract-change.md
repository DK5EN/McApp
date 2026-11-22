# SOP-005: Change a wire contract or a shared corpus

| Field | Value |
|---|---|
| ID | SOP-005 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | the development Mac (both repos); `mcapp` only after the release (SOP-001) |
| Applies to (variants) | n/a (repository work) |
| Sources | `CLAUDE.md` ("Vendored Subtrees", "Key Gotchas", "Command contract"); ADR-0018; POL-002; POL-007 |

## 1. Purpose and scope

Changes a shared contract (`contract/` subtree from mc-chat: push, command, classifier) or a hand-copied
corpus (`*_vectors.json`, `suppression_vectors.json`, `ack_match_vectors.json`, and the others in POL-002).
The two repos (MCProxy and mc-chat, plus the webapp for some corpora) must agree afterwards.

- Applies when: a clause of the push, command or classifier contract changes, or a corpus gains a case.
- Does **not** apply when: the change is inside MCProxy and no shared file moves.

## 2. Variant notes

- n/a (repository work). The firmware side is a separate repo and is not touched by this SOP.

## 3. Inputs

- Access: both repos checked out (`~/WebDev/MCProxy`, `~/WebDev/mc-chat`), branch `development`.
- Tools: `git subtree`, `uv`, `./scripts/gate.sh`.
- Preconditions: the contract change is reviewed. Contract edits start in mc-chat, never in MCProxy.

## 4. Steps

1. **Edit upstream.** For a subtree contract (`push_contract.json`, `command_contract.json`, classifier), change it in
   **mc-chat**, run mc-chat's suite, and commit there.
2. **Split and pull (subtree).** From MCProxy:

   ```bash
   cd ~/WebDev/mc-chat && git subtree split --prefix=contract -b contract-subtree
   cd ~/WebDev/MCProxy && git subtree pull --prefix=src/mcapp/contract mc-chat contract-subtree --squash
   ```

   Use the split branch and prefix from the table in `CLAUDE.md` for the classifier (`meshcom_mock/classifier`,
   branch `classifier`, prefix `src/mcapp/classifier`).
3. **Re-pin the hash.** Update the sha256 in each suite that reads the file (`push_tests.py`,
   `dedup_contract_tests.py`, `contract_parity_tests.py` for the contract files). The pin change belongs in the same commit as the pull.
4. **Hand-copied corpora.** Copy the changed JSON to every repo that carries it (POL-002 lists them). Update the
   sha256 in the receiving suite. For the webapp, bump its `EXPECTED_SHA256`.
5. **Run the gate** in both repos: `./scripts/gate.sh` here, and mc-chat's own suite. Parity failure in either is a stop.
6. **Commit** the pull and the hash update together, with `[feat]` or `[fix]` and a note of the contract version.

## 5. Definition of success

- MCProxy's startup suite and mc-chat's parity test pass against the same corpus.
- `sha256sum` of the local file equals the pin in each suite.
- `git log -- src/mcapp/contract` shows the squashed pull, and no hand edit.

## 6. Troubleshooting

| Symptom (exact message) | Cause | Action |
|---|---|---|
| `sha256 mismatch` in a suite | the file was edited locally, or the pin was not updated | restore from upstream or update the pin in the same commit |
| `contract/...` file missing after a pull | the file lies outside the subtree prefix | move it inside the prefix upstream (see ADR-0018, consequences) |
| parity fails only in one repo | one repo was not re-synced | repeat step 2 or 4 for that repo |

## 7. Feedback and changes

- A wrong step is fixed here the same day, or logged in `../review-log.md`.

## 8. HITL review (owner)

- [ ] Confirmed: the steps match the last contract change
- [ ] Refuted: see comments
- Comments:
- Reviewer / date:
