# ADR-0021: `#TAG` destinations are channels, not callsigns

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-08-20 (commit `ea15511`) |
| Deciders | Martin Werner |
| Affects | `commands/parsing.py` (`is_hashtag`, `dst_kind`, `resolve_dst_target`), `storage/conversation_key` logic, `commands/hashtag_dst_vectors.json` |
| Supersedes | - |
| Superseded by | - |

## Context

The MeshCom firmware 4.36 puts a hashtag such as `#OE-SOTA` in the destination field. All three
repositories classified it as a personal DM. The conversation-key code then split the tag on its
first hyphen, so `#OE-SOTA` became `#OE<>DK5EN`. Distinct tags collapsed, and one tag fragmented per sender.

## Decision

- Add **sibling** predicates beside `is_group()`: `is_hashtag()`, `dst_kind()`, `resolve_dst_target()`.
  `is_group()` is not widened, because a corpus mirrored in mc-chat and the webapp pins it.
- Classification is **case-insensitive** and **not length-bounded**. A tag that fails either falls into the
  DM branch, which is the original defect.
- `dst_kind` returns `"unknown"` (never `"direct"`) for a `#`-prefixed value that fails the tag charset.
  Such a value addresses nobody.
- The firmware's 9-character cap is send-side grammar, enforced at the API boundary, not in classification.
- **No prefix or subscription matching.** The firmware's stated rule contradicts its own examples, so
  implementing either would encode a guess.

## Consequences

- Hashtag channels get their own conversation key and stop fragmenting.
- The corpus `hashtag_dst_vectors.json` (32 vectors, sha256-pinned) guards the rule in this repo.

## Alternatives considered

- Widen `is_group()`: rejected, would change a pinned, shared corpus.
- Split on the first hyphen only for known formats: rejected, the split was the bug.

## Evidence

- `CLAUDE.md`, section "Key Gotchas" (first bullet)
- `MeshCom-Hashtag-prep.md` (background, in the firmware repo)
