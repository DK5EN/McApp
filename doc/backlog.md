# Backlog

Deferred tasks with a due date or a data-collection dependency. One section per item; move
resolved items to `doc/archive/` with their outcome. Closed on 2026-09-28:
`doc/archive/2026-09-28-backlog-closed.md` (B2, B3, B4 items 1, 2, 3, 4-8). Review and
closing plan: `doc/2026-09-28_1035-backlog-review-verdict-and-plan.md`.

## B4 — memory footprint on mcapp.local (one open question)

Measured on mcapp.local 2026-09-28 from `/proc/<pid>/smaps_rollup`, before and after v2.0.17-dev.1
(W2 + W3), at matched uptime:

| Build                   | Uptime | mcapp Pss | mcapp cgroup peak | mcapp-ble Pss |
| ----------------------- | ------ | --------- | ----------------- | ------------- |
| v2.0.16 (old)           | 46 min | 113.4 MB  | 158.7 MB          | 43.2 MB       |
| v2.0.16 (old)           | ~3 h   | 113.4 MB  | —                 | 42.6 MB       |
| v2.0.17-dev.1 (W2 + W3) | 3 min  | 85.9 MB   | —                 | 43.4 MB       |
| v2.0.17-dev.1 (W2 + W3) | 47 min | 97.3 MB   | 132.0 MB          | 43.5 MB       |

mcapp is ~16 MB lighter at equal uptime, matching the import-cost estimate (13.4 + ~3). mcapp-ble
shows no measurable change; the extras drop saved nothing visible there. The old build was flat
between 46 min and 3 h, which already argues against a leak.

**Open question — does mcapp grow? (plan wave W6; open, no read of the v2.0.17 process yet).** The 2026-09-15
note measured 67 MB at import and 85-95 MB live with 40 MB swapped; the 2026-09-28 figure is
113 MB Pss with nothing swapped, so the two are not comparable. Read Pss at ~1 h, ~24 h and
~72 h uptime: growth under ~5 MB/day that flattens closes this as baseline (page cache +
arenas); steady linear growth opens a `tracemalloc` investigation behind a dev flag.

Every read must come from ONE process: record its start time with the figure
(`systemctl show mcapp -p ExecMainStartTimestamp`) and compare only reads that share it. The
47 min read above belongs to the v2.0.17-dev.1 process, which the v2.0.17 restart at 12:33:37
replaced, so it is not the ~1 h point of the current series — and dev.1's own warm-up (85.9 to
97.3 MB in ~44 min) is as large as the 5 MB/day threshold. Any deploy restarts the series, and
the release cadence has not yet left a 72 h window; the cheaper source is `stall_events`, whose
server context carries `rss_kb`, `version` and `slot`, so the trend can be read per process
after the fact (RSS, not Pss — a trend source, not a substitute for the three reads).

## B5 — Winlink over MeshCom

**Goal:** find out what of Winlink's APRSLink command set works over MeshCom from `DK5EN-98`, and
what our stack does with the traffic. Runbook and campaign:
`doc/2026-09-19_1300-winlink-account-and-test-campaign.md`; command reference and firmware
findings: `doc/2026-09-19_1100-winlink-wlnk1-command-test-plan.md`.

### B5a — first real traffic for shipped code (not blocked; operator on air, plan wave W7)

Phases 0 and 1 need no account: ~7 DMs to `WLNK-1` from `DK5EN-98`, about 15 minutes on air,
then an agent checks the live DB, SSE and push. Two things shipped in v2.0.11-dev.3 have no
real-frame evidence yet:

- the bare-APRS-ack filter (`_APRS_ACK_GLOBS`, `storage/query.py`), written from firmware source —
  zero such rows exist in the live DB;
- push payload truncation (`MAX_TEXT_LEN = 120`), since APRSLink's help reply is longer and would
  be the first inbound text to hit the cap in a delivered push.

The digit-less pair key (`isValidPairMember`) needs no Winlink traffic: the same code path is
already live-proven by `APRS2SOTA<>DL8FMA`.

### B5b — account and phases 2-4 (operator decision)

`DK5EN` has no Winlink account and there is no web signup — the account is created by connecting
to the CMS without a password, which then mails you one. Outward-facing; needs the operator's
explicit go-ahead. Declined → close B5b with the reason. Recommended client is Pat's
`darwin_amd64` build under Rosetta.

**Do not** re-open the security posture question casually: APRSLink's challenge/response discloses
three password characters by position, in clear, across the mesh and APRS-IS on every login. The
decision taken is to use a password that protects nothing else.

## B6 — keep an older message's ACK records when its msg_id is reused

Left open on 2026-09-28 by choice: only the read side was fixed then (`?since=` on
`GET /api/messages/{msg_id}/acks`, commits 856348b / webapp 4362bbb).

When a msg_id is reused, the older message's own ACK records are still deleted when the newer
message's first ACK arrives (`_prune_stale_message_acks`, `storage/ingest.py`, called from
`_handle_ack`). The older message's popover therefore shows nothing instead of its real ACKs. The
prune exists because the `message_acks` key `(msg_id, kind, from_call)` carries no message
identity, so without it `INSERT OR IGNORE` swallows the newer message's ACKs (CLAUDE.md, ACK
Attribution). Keeping both needs a schema migration that adds message identity to the key (for
example the bound message row id), and the `?since=` read path then filtering on it.

## B7 — convert MH `DATE`/`TIME` through the node's `SN.UTCOF`

`timestamp_from_date_time()` still assumes the host offset (contract §4, plan D6). With the node
now following its own TZ rule the two can differ. Different code path, deliberately deferred from
the node-TZ campaign.
