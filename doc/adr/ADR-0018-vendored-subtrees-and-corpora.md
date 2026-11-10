# ADR-0018: Vendored subtrees and hand-copied corpora

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-05-08 (classifier subtree squashed, commit `618d1eb`); contract subtree 2026-07-26 |
| Deciders | Martin Werner |
| Affects | `src/mcapp/classifier/`, `src/mcapp/contract/` (git subtrees from mc-chat); corpora `*_vectors.json`, `contract/*.json`, `suppression_vectors.json` |
| Supersedes | a local copy of the classifier (removed 2026-05-08, commit `5b0b3c4`) |
| Superseded by | - |

## Context

Two repositories implement the same contracts: MCProxy (this repo, the production backend) and mc-chat
(the mock and the upstream of the shared code). Divergent copies drift silently. A parity test in one
repo can pass against a corpus that was quietly edited in the other.

## Decision

- **Shared code:** `classifier/` and `contract/` are git subtrees, pulled from mc-chat with `--squash`.
  mc-chat is upstream. Never edit these directories in MCProxy.
- **Shared corpora:** the JSON vectors that are not subtrees are hand-copied between repos. Each copy
  is pinned by a sha256 in every suite that reads it. The corpus is canonical in one repo, named in its
  suite, and copied with its hash update in the same commit.
- **A contract edit starts in mc-chat**, then reaches MCProxy by split and pull. The pull and the
  re-captured hash belong in one commit.

## Consequences

- A local edit to a vendored file is caught by the hash, not by review.
- Syncing takes two repositories. `../sop/SOP-005-wire-contract-change.md` is the step list.
- The vendored `contract/` directory includes `push_contract.json` and `command_contract.json`. Its
  prefix was changed on 2026-07-26 after a subtree pull deleted a copy outside it.

## Alternatives considered

- A shared package: rejected, two repos with different runtimes and deploy paths.
- Copy-and-forget: rejected, the drift this ADR exists to stop.

## Evidence

- `CLAUDE.md`, sections "Vendored Subtrees" and "Key Gotchas" (six vector corpora)
- `../policy/POL-002-vendored-and-corpora.md`
