# Backlog

Deferred tasks with a due date or a data-collection dependency. One section per item; move
resolved items to `doc/archive/` with their outcome. Closed on 2026-09-28:
`doc/archive/2026-09-28-backlog-closed.md` (B3, most of B2, B4 items 1, 2b, 4-8). Review and
closing plan: `doc/2026-09-28_1035-backlog-review-verdict-and-plan.md`.

## B2 — split `msg_www` in the webapp (narrowed 2026-09-28; plan wave W5)

**Problem:** the webapp sets `msg_www` for every own **local** echo (`messageProcessor.ts`) and
again when the oevsv.at internet firehose delivers a duplicate (`stores/messages.ts`). Because the
echo already set it, "seen on the internet" can never contribute anything for an own message —
and on the extUDP path, which has no binary gateway ack, that sighting is the only proof a
message reached the backbone.

**Fix:** a separate `msg_echo` for the local echo; `msg_www` means the firehose only; the ✓ title
names which one fired. Webapp-only — `msg_www` is not on the wire, so no MCProxy or mc-chat change.

Everything else B2 asked for (Node vs Gateway attribution, arrival instant, schema, ✓✓ semantics,
mc-chat parity) is resolved; see the archive.

## B3 — release notes on the Update page

Closed here 2026-09-28: tracked and owned by webapp backlog B5 (plan wave W4).

## B4 — memory footprint on mcapp.local (residue)

Baseline for all remaining measurements, mcapp.local 2026-09-28 with mcapp up 46 min, from
`/proc/<pid>/smaps_rollup`: mcapp Pss 113 MB, swap 0; mcapp-ble 43 MB; caddy 28 MB; `free`
168 MB available; mcapp cgroup `memory.peak` 159 MB.

1. **Drop the unused uvicorn extras (plan wave W2).** Both `pyproject.toml`s pin
   `uvicorn[standard]`; `import uvicorn` pulls `watchfiles` and `Config.load()` pulls `websockets`
   (+3.0 MB per process), and neither service uses reload or WebSockets. `uvloop` and `httptools`
   do real work under uvicorn's `auto` defaults and stay as explicit dependencies.
2. **Replace pywebpush's HTTP layer (plan wave W3).** `pywebpush` imports `aiohttp` and `requests`
   at module top: +13.4 MB on top of fastapi/httpx/cryptography. Its crypto (`http_ece`,
   `py_vapid`, +0.6 MB) stays; only the POST moves to httpx.
3. **Open question — does mcapp grow? (plan wave W6, after W2/W3 are deployed).** The 2026-09-15
   note measured 67 MB at import and 85-95 MB live with 40 MB swapped; the 2026-09-28 figure is
   113 MB Pss with nothing swapped, so the two are not comparable. Read Pss at ~1 h, ~24 h and
   ~72 h uptime: growth under ~5 MB/day that flattens closes this as baseline (page cache +
   arenas); steady linear growth opens a `tracemalloc` investigation behind a dev flag.

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
