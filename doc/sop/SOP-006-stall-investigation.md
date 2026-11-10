# SOP-006: Investigate a reported stall

| Field | Value |
|---|---|
| ID | SOP-006 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` (production), the development Mac for the code change |
| Applies to (variants) | `debian-13-trixie` |
| Sources | ADR-0007; `doc/archive/2026-09-15_1530-stall-tracking-plan.md`; `doc/archive/2026-09-18_2200-stall-followup-plan.md`; `CLAUDE.md` "Stall Tracking"; `scripts/replay_stall.py` |

## 1. Purpose and scope

Turns "the UI felt slow" into a recorded row, a reproduction and a cause. The stall store is the evidence.

- Applies when: a user reports slowness, a stall shows in the webapp, or `/api/stalls/summary` shows a path well above its baseline.
- Does **not** apply when: the box is down. Use RB-001 first.

## 2. Variant notes

- n/a. Only the Pi records server-side stalls. The development Mac has no `stall_events` of its own.

## 3. Inputs

- Access: `curl` to `http://mcapp.local` (LAN). No SSH needed for the read.
- Tools: `scripts/replay_stall.py` from the repo (`uv run`).
- Preconditions: the report has a time window or a path. Without one, start from the summary.

## 4. Steps

1. **Baseline first.** `curl -s 'http://mcapp.local/api/stalls/summary' | python3 -m json.tool`. Note p95 and max per path.
2. **Pull the critical rows** for the window: `curl -s 'http://mcapp.local/api/stalls?severity=critical&limit=50'`.
   Note `kind`, `path`, `duration_ms`, `request_id`, and the `context` (pool queue, loop lag, RSS).
3. **Classify the kind.** `loop_lag` and `pool_wait` point at the infrastructure. `handler` points at a publish subscriber.
   `http` and `client_http` point at the route. `sse_answer_missing` points at the page request path.
4. **Reproduce.** `uv run python scripts/replay_stall.py --base http://mcapp.local --id <row> --repeat 10`. Compare the replay's
   p95 with the recorded duration.
5. **Look at writes before queries.** Closing the last connection to a WAL database costs a checkpoint and an fsync. In the
   past, per-call connections were the cause, not the query plans (`CLAUDE.md`, "Per-call write connections"). Count fsyncs first.
6. **Record the finding** in `../review-log.md` (date, row id, cause, action), and in the backlog if work follows.

## 5. Definition of success

- A cause is named with evidence: the recorded row, and a replay that matches it.
- After a fix, the same replay is below the threshold (0.5 s stall, 2 s critical; 100 ms for `loop_lag`).
- Known non-issues are not reopened: `/api/weather` at 0.5 to 1.5 s is closed (decision 2026-09-18), and an
  `sse_heartbeat` from a hidden tab is a sample, not an incident.

## 6. Troubleshooting

| Symptom (exact message) | Cause | Action |
|---|---|---|
| `stall_events` is empty for the window | the writer thread was starved or the queue dropped rows | check `dropped` in the context, then RB-001 |
| replay returns 404 | the row was pruned (`max_rows`, default 5000) | use the summary and a newer row |
| `loop_lag` rows with a stack | a blocking call on the loop | the stack in `detail.stack` names the frame |

## 7. Feedback and changes

- Thresholds live in `config.json` under `stalls`. Changing them is a config change, not a code change.

## 8. HITL review (owner)

- [ ] Confirmed: this matches how stalls were actually investigated
- [ ] Refuted: see comments
- Comments:
- Reviewer / date:
