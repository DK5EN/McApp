# RM1 frames: hidden in chat, shown as Observed sessions

Date: 2026-10-11 · Target: v2.1.10-dev.2 · Repos: MCProxy, webapp

## Why

RM1 remote-admin frames (`RM1 <ctr> <cmd> [args] <tag>` and `RM1 <ctr> ok|err <result> <tag>`) are
DMs on air. Any McApp whose node overhears them (or sends them) showed them as chat bubbles:
automated, unreadable, and noisy. They belong on Node Admin > Remote Management, decoded.

## Decisions (owner, 2026-10-11)

| Topic                         | Decision                                                                                                                        |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Which frames are hidden       | All RM1 frames, our own and foreign, in chat, unread, push and the foreground beep                                              |
| RF Monitor                    | Keeps showing them, dimmed, verdict `dropped · rm1`                                                                             |
| Observed sessions content     | All sessions, our own included (marked "this McApp")                                                                            |
| History window                | 24 h by default; rows stay stored under normal retention                                                                        |
| Trust display                 | Positive only: "verified" when the tag checks out against a key this box stores; otherwise no indicator at all, never a warning |
| Positions control (feed card) | Hidden until wave B instead of disabled radios                                                                                  |

## Design

- One predicate for the RM1 shape (`rm1_shape.is_rm1_frame`, `{NNN` suffix tolerated), used by the
  hidden-message rules in `storage/query.py` (history, snapshot, unread), `broadcast_verdict`
  (live + monitor reason), and a local push gate in `sse_routes/push.py`. Rows stay stored.
- Observed sessions are derived from the stored RM1 rows: commands and replies paired by
  (target, commander, ctr); one session per commander → target; active while the last frame is
  < 120 s old; counter = unix seconds = send time; reply split into key/value fields; tag never
  shown. `GET /api/node-admin/observed?hours=24`, live SSE `node_admin:observed`. Nothing transmits.
- Webapp mirrors the predicate at the ingest chokepoint and the offline-cache hydrate path, and in
  the foreground beep.

## Waves

| Wave | Scope                                                                                                                                                                               | State                                                             |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| 1    | Backend: 1A suppression (rm1_shape, query.py, push.py) + 1B observed (remote_cmd parse_command, node_admin_observed, storage query, route, SSE)                                     | Done 2026-10-11, advisor APPROVED after one rework round          |
| 2    | Webapp: 2A hide (rm1.ts, messageProcessor, messages hydrate, foreground beep) + 2B panel (store, ObservedSessions.vue) + gate: SSE wiring, NodeAdminView mount, feed card positions | Done 2026-10-11 (webapp), advisor APPROVED after one rework round |
| 3    | Release v2.1.10-dev.2, deploy, verify; live RM1 check needs the owner's go (transmits)                                                                                              | Pending                                                           |

## Wave 1 as built (backend)

- `rm1_shape.is_rm1_frame` + `RM1_HIDDEN_GLOBS` (GLOB is a superset; it anchors the 16-hex tag at
  the end). Hidden through `_HIDDEN_GLOBS` and also the DM-page predicate (`_NOT_STORE_NOTICE_SQL`),
  which had its own list. Live: `broadcast_verdict` -> `("dropped", "rm1")`. Push: node-local gate in
  `sse_routes/push.py` (no contract change).
- `node_admin_observed.build_sessions` (pure) runs in `asyncio.to_thread`; the live event rebuilds
  only the frame's pair (`pair_rows`). With a stored key each frame is verified as it is slotted:
  a verifying frame replaces an unverified occupant, and `verified: true` needs every present part
  to verify (advisor rework: a forged command with the predictable ctr was shown as verified).
- Known limit: our OWN outbound commands appear only through the node's BLE echo; a UDP-only box
  sees its own replies but not its own commands (send path untouched on purpose).

## Wave 2 as built (webapp)

- `utils/rm1.ts` mirrors `rm1_shape` (advisor differential check: 8 320 cases, no divergence).
  Ingest skip, hydrate skip, boot purge of exactly the skipped ids (no second IndexedDB read),
  no foreground beep, monitor label "Remote admin (RM1), hidden".
- `ObservedSessions.vue` on Remote Management, also under "No node keys stored"; hidden on mc-chat,
  while undetermined/not loaded, and when node admin reports disabled. Client recomputes `active`,
  `no_reply` and the 24 h window on a 10 s tick; upsert ignores older sessions.
- `s=` (switch letters) is shown raw: no decoder exists yet.
- Open, unrelated: `ReleaseNotesCard.spec.ts` V-SW3 flakes under full-suite load (1 of 7 runs;
  10/10 alone) — async render timing, pre-existing.
