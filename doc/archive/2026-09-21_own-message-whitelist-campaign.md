# Own-Message Whitelist — campaign state

Status: COMPLETE 2026-09-21. Wave 1 + Wave 2 landed, advisor APPROVED, both repos committed
(MCProxy `4ccabf1`, webapp `e2b84b5`). Pending: dev release + deploy.

## Problem

A message the operator sends that trips the webapp's spam filter is never rendered — a silent
send failure. Fix: messages whose `src` base callsign equals the operator's own base callsign
are exempt from the display-side filters.

## Decisions (user-approved 2026-09-21)

- Exemption covers the classifier spam filter, the local `blocked_texts` list AND the callsign
  blocklist verdict (`blocklistVerdict`).
- `isSpamByClassifier`, `isTextBlocked` and `blocklistVerdict` themselves are NOT modified —
  all three are cross-repo corpus-pinned (`suppression_vectors.json`,
  `blocklist_decision_vectors.json` v2). The exemption lives at the call sites.
- Protocol guards (`duplicate`, `{ping}/{pong}`, `:ackNNN`, bare APRS ack) are never exempted.
- Backend needs no code change: `unread` already excludes own traffic by base callsign, and
  `blocklist_decision` keys on `src` only. MCProxy gets documentation only.
- Finding during recon: the `blocklistVerdict` half can only ever fire for own traffic if the
  operator's own callsign is on the sperrliste/kickban list. It is implemented as a documented
  defensive guard, not as a fix for an observed failure.

## Wave 1 (parallel, disjoint)

| Agent                        | Files                                                                                          | Status                              |
| ---------------------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------- |
| 1a webapp display predicates | `src/stores/messages/predicates.ts`, `sidebar.ts` + their specs                                | done, diff reviewed by orchestrator |
| 1b webapp blocklist path     | `src/services/messageProcessor/blocklist.ts`, `messageProcessor.ts`, `offlineCache.ts` + specs | done, diff reviewed by orchestrator |
| 1c MCProxy docs              | `CLAUDE.md`, `src/mcapp/storage/suppression.py` (docstrings only)                              | done, diff reviewed by orchestrator |

Hotspot, orchestrator-owned, APPLIED: `src/stores/messages.ts`
— `getFilteredMessagesFor` ownCall fallback, `setOwnCallsign` wiring, `isOwnSrc` guards at the
hydration (`source === 'hydrate'`) and `purgeBlockedCallsigns` blocklist call sites.

## Gate

webapp: `npm run lint`, `npm run typecheck`, `npm run test`, `npm run format:check`, `npx vite build`.
MCProxy: `uvx ruff check`, `uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src`,
`uv run python scripts/run_startup_tests.py`.
Gate result 2026-09-21: webapp lint/typecheck/format:check clean, `vitest run` 217 files /
3496 tests passed, `vite build` clean. MCProxy `ruff check` + `ruff format --check .` clean,
mypy "no issues found in 111 source files", `run_startup_tests.py` exit 0.
Advisor pass (fable, on the full two-repo diff) dispatched. Then commit per repo, then
`/dev-release`.

## Advisor pass (fable, 2026-09-21) — APPROVED with rework

All five acceptance criteria confirmed against the code, plus the backend claim (`blocklist_decision`
keys on `src` only; `unread` excludes own traffic before `is_suppressed` ever runs). 13 mutations
run; the ones that SURVIVED became Wave 2.

## Wave 2 (parallel, disjoint) — closing the mutation gaps

| Agent | Files                                                                                          | Finding                                                                                                                                                                |
| ----- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2a    | `src/stores/messages/__tests__/predicates.spec.ts`                                             | group/hashtag and pair-key branches unpinned — the group branch IS the reported symptom; one existing test is mislabelled "DM/pair" but exercises the personal-dst arm |
| 2b    | `src/stores/__tests__/messages.blockedCallsigns.spec.ts`, `src/stores/positions.ts` + its spec | purge/hydrate own-guards unpinned; `purgeBlockedPositions` has no own-guard although live ingest now admits an own position                                            |

Accepted untested: the `getFilteredMessagesFor` ownCall fallback (reachable only from a caller that
omits ownCall; ChatContainer always passes one).

Wave 2 result: group, hashtag and pair-key branches pinned; `purgeBlockedCallsigns`, the hydrate
path and `purgeBlockedPositions` pinned; `purgeBlockedPositions` gained the missing own-guard.
Every new test proved by deleting the guard it pins and watching it fail. Final gate: webapp
217 files / 3507 tests, lint + typecheck + format:check + build clean; MCProxy ruff/mypy clean,
`run_startup_tests.py` exit 0. The group-branch mutation was re-run independently by the
orchestrator before commit.
