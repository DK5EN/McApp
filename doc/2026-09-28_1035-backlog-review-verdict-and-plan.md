# Backlog review 2026-09-28 — Fable verdict and closing plan

Scope: every open item in `doc/backlog.md` (B2, B3, B4 residue + open question, B5), checked
against the current code of MCProxy, webapp and mc-chat and against mcapp.local (read-only). One
finder per item, every load-bearing claim re-verified by hand before it landed here.

## Wave status log

| Wave | Content                                       | Owner           | Status                            |
| ---- | --------------------------------------------- | --------------- | --------------------------------- |
| W1   | Backlog rewrite: close/narrow/split           | agent (docs)    | done 6cf9527                      |
| W2   | Drop unused uvicorn extras (both services)    | agent (MCProxy) | done a81ea8b                      |
| W3   | Replace pywebpush's HTTP layer with httpx     | agent (MCProxy) | done 3d960d9, live push verified  |
| W4   | Webapp: release notes card (webapp B5)        | agent (webapp)  | done ac20373                      |
| W5   | Webapp: split `msg_www` (narrowed B2)         | agent (webapp)  | done 6972088                      |
| W6   | B4 open question: PSS re-measure after deploy | agent (ops)     | 47 min read done; 24 h + 72 h due |
| W7   | B5 Winlink phase 0 on air, then phase 1 check | operator, agent | needs operator                    |

Found and fixed at the advisor gates, beyond the plan:

- W2: `bootstrap/lib/health.sh`'s venv probe imported `websockets` and would have failed every
  deploy and `--converge` once it left the env; now probes `uvicorn, httptools, dbus_next`, pinned
  by the new `health_probe` suite.
- W3: a timeout or connection error is one warning line instead of a traceback per push.
- W5: the firehose branch marked only `src_type == 'node'` (a pre-BLE leftover), so "seen on the
  internet" could never fire on a BLE box; cached pre-split rows are normalised on hydrate so they
  do not claim an internet sighting.
- Left open, recorded as webapp backlog B7: the firehose marking looks a message up by its raw
  `msg_id`, so a copy of a PN-retry XOR variant dedups but marks nothing.

## Verdict per item

| Item                        | Verdict                            | Reason                                                                   |
| --------------------------- | ---------------------------------- | ------------------------------------------------------------------------ |
| B2 delivery status          | NARROW to `msg_www` split (webapp) | (a) and (c)1/2/5 resolved by ACK attribution + store-forward             |
| B3 release notes            | CLOSE here, pointer to webapp B5   | no MCProxy work; release.sh already publishes the notes as release body  |
| B4.1, 4, 6, 7, 8            | CLOSE (shipped, verified live)     | gpu_mem=16 (arm 496M), CmaTotal 64 MB, cgroup_enable=memory live         |
| B4.2a uvicorn extras        | DO (small, ~3 MB per process)      | websockets + watchfiles are imported by `Config.load()` and never used   |
| B4.2b fold/rewrite BLE      | CLOSE                              | payoff < risk; process isolation of BlueZ is the point of the service    |
| B4.3 pywebpush              | DO, narrowed: keep its crypto deps | 13.4 MB import cost is requests + aiohttp; http_ece + py_vapid are 0.6MB |
| B4.5 journalxship tmpfs     | CLOSE here                         | owned by the AIOps units, no repo under ~/WebDev carries it              |
| B4 open question (growth)   | KEEP, time-gated re-measure        | single 46-min snapshot cannot tell leak from baseline                    |
| B5 validate shipped code    | KEEP, reframe: not blocked         | needs ~15 min operator on-air time, then an agent DB check               |
| B5 account-gated phases 2-4 | KEEP as operator decision          | outward-facing identity; close if the operator declines the account      |

### B2 — mostly already done

- **Resolved:** Node vs Gateway ACK is stored per station with its own timestamp in the
  `message_acks` ledger (schema v29, `storage/ingest.py` `_record_message_ack`), served by
  `GET /api/messages/{msg_id}/acks` and rendered in the webapp's ack popover. `delivery_status`
  (v31) carries held/failed/acked. "Can a Gateway ACK upgrade a Node ACK" is moot: they are two
  ledger rows, not one overwritten flag. ✓✓ requires `msg_ack` since webapp `a7a6698`
  (2026-08-19). mc-chat carries the same wire shapes.
- **Still true:** `messageProcessor.ts:348` sets `msg_www` for every own local echo
  (`src_type` node/ble/ble_remote), and `ChatBubble.vue:412` renders the single ✓ from
  `msg_sent || msg_www`. The firehose duplicate path (`stores/messages.ts:1030`) sets
  `msg_www = true` on a message where it is already true, so "seen on the internet" still cannot
  contribute anything for an own message. That matters on the extUDP path, which has no binary
  gateway ack — there the firehose echo is the only proof the frame reached the backbone.
- **Taste, not defect:** four distinct bubble glyphs. The popover already shows the per-station
  detail; not worth a design pass on its own.

### B3 — a duplicate of webapp B5

Confirmed not implemented: `useVersionCheck.ts:82` types the release as `{tag_name, prerelease}`,
no markdown renderer in `package.json`, webapp B5 still "not started". MCProxy owns nothing:
`scripts/release.sh` already publishes `doc/release-history.md`'s section as the release body.

### B4 residue

- Live on mcapp.local 2026-09-28 (mcapp up 46 min): mcapp Pss 113 MB, swap 0; mcapp-ble 43 MB;
  caddy 28 MB; `free` 168 MB available; mcapp cgroup `memory.peak` 159 MB.
- **B4.2a:** both `pyproject.toml`s pin `uvicorn[standard]`. Measured: `uvicorn.Config(app).load()`
  imports `httptools`, `websockets` and `watchfiles` (+3.0 MB). No WebSocket route and no
  `--reload` exists; the `websocket_*` names in `main.py` are internal pub/sub topics. `uvloop` and
  `httptools` do real work under `loop/http="auto"`, so keep them as explicit deps and drop only
  the rest.
- **B4.3:** measured import delta of `pywebpush` on top of fastapi/httpx/cryptography: **+13.4
  MB**; `http_ece` + `py_vapid` alone: **+0.6 MB**. `pywebpush/__init__.py` imports `aiohttp` and
  `requests` at module top. So the swap needs no hand-written crypto at all — only the HTTP
  request pywebpush builds (encrypt via `http_ece`, sign via `py_vapid`, POST via httpx).
- **B4 item 8 text is stale:** the live fix is the Caddy drop-in in `configure_caddy_sudo`
  (`bootstrap/lib/packages.sh`), not the template. Correct when archiving.

### B5

All three code items exist and are gated (`storage/query.py:119` `_APRS_ACK_GLOBS` in the `query`
suite; `push_delivery.py:61` `MAX_TEXT_LEN` in the `push` suite; webapp `callsignUtils.ts:55`
`isValidPairMember`). Only the APRS-ack glob and the push truncation lack real-frame evidence; the
digit-less pair key is already live-proven by the same code path (`APRS2SOTA<>DL8FMA`).

## Refuted claims (do not re-investigate)

- "pywebpush saves only 6.5 MB, and that is disk, not RAM" — refuted: import RSS delta measured at
  13.4 MB, matching the backlog's 12.5 MB Pi measurement.
- "Replacing pywebpush means hand-rolling RFC 8291 aes128gcm" — refuted: `http_ece` does it and
  costs 0.6 MB; only the transport is replaced.
- "uvloop/httptools are loaded for nothing" (backlog wording) — refuted: they are active under
  uvicorn's `auto` defaults. Only `websockets` and `watchfiles` are dead weight.
- "B4.5 tmpfs cap is ours to ship" — refuted: no repo under ~/WebDev references `journalxship`
  outside docs.
- "cgroup memory accounting is still missing" — refuted: `/proc/cmdline` carries
  `cgroup_enable=memory cgroup_memory=1` after the stock `cgroup_disable=memory`, and
  `memory.current` reads real values.

## Implementation plan

Every code wave ends with the full gate (`uvx ruff check`, `uvx ruff format --check .`,
`uv run mypy src/mcapp ble_service/src`, `uv run python scripts/run_startup_tests.py`; webapp:
lint, type-check, vitest) and an independent advisor read of the diff before the commit.

### W1 — backlog rewrite (docs only)

1. Move B3, B4 (items 1, 4-8, 2b, 5) and the resolved parts of B2 to
   `doc/archive/2026-09-28-backlog-closed.md` with the outcome and evidence from this doc; fix the
   item-8 Caddy wording there.
2. `doc/backlog.md` keeps: B2 narrowed (→ W5), B4 open question (→ W6), B4.2a/B4.3 until W2/W3
   land, B5 split into B5a (operator on-air, not blocked) and B5b (account decision).
3. Replace B3 with a one-line pointer to webapp B5.
4. prettier, then `uvx ruff format --check .`.

Exit: backlog lists only items with an owner and a closing criterion.

### W2 — uvicorn extras (MCProxy + ble_service)

1. `uvicorn[standard]` → `uvicorn` plus explicit `uvloop` and `httptools` in both
   `pyproject.toml`s; pass `ws="none"` in `sse_handler.py`'s `uvicorn.Config` and `--ws none` in
   `bootstrap/templates/mcapp-ble.service` so a transitive `websockets` is never imported.
2. `uv lock`; regenerate `ble_service/uv.lock` standalone (outside the workspace, per the
   known lock quirk); `uv sync --all-packages`.
3. Test: a startup-suite check that after `Config.load()` neither `websockets` nor `watchfiles` is
   in `sys.modules` (fails before, passes after).
4. After deploy: Pss of both services at matched uptime vs the numbers above.

Exit: both services run, SSE and BLE REST unaffected, measured delta recorded, B4.2a archived.

### W3 — pywebpush out, its crypto stays (MCProxy)

1. New `push_send.py` (or inside `push_delivery.py`): build the aes128gcm body with
   `http_ece.encrypt(..., version="aes128gcm")`, the VAPID `Authorization: vapid t=..., k=...`
   header via `py_vapid`, plus `TTL`, `Content-Encoding: aes128gcm`; POST with a sync
   `httpx.Client` inside the existing `asyncio.to_thread` call, same timeouts.
2. Map the response to the existing `_status_code` / `PRUNE_STATUS_CODES` path through a small
   exception type replacing `WebPushException`; plug into the existing `webpush_fn` seam so every
   contract vector runs unchanged.
3. Tests: round-trip — encrypt with the new sender, decrypt with `http_ece` using the RFC 8291
   Appendix A keys; header shape; 404/410 → prune, 5xx → no prune; timeout path.
4. Remove `pywebpush` from `pyproject.toml`; confirm `requests`/`aiohttp` leave `uv.lock`;
   update the `ignore_missing_imports` list in `[tool.mypy]`.
5. Live: one push to the iOS PWA and one to desktop Chrome before release; watch for Apple
   `BadJwtToken` (sub must stay a real FQDN).

Exit: push delivered live on iOS and Chrome, import delta re-measured, B4.3 archived.

### W4 — release notes on the Update page (webapp B5)

Per `webapp/docs/change-log-update-disp.md`: capture `body` in `fetchReleases()`; render with
`marked` + `DOMPurify` (never raw `v-html`); a collapsible card on `Update.vue` beside Version
Status, not inside `BaseConfirmModal`; pick the stable or prerelease body per the active update
path; tests in `useVersionCheck.spec.ts` plus a render test with a hostile body. Close webapp B5.

### W5 — `msg_www` split (narrowed B2, webapp)

1. New `msg_echo` for the local node echo (`messageProcessor.ts:348`); `msg_www` is set ONLY by
   the firehose duplicate path (`stores/messages.ts:1030`).
2. `ChatBubble.vue:412` renders ✓ from `msg_sent || msg_echo`, and the check's title (or the
   existing ack badge row) names "seen on the internet" when `msg_www` is set.
3. Offline cache (`offlineCache.ts:298`) and the `_patchMessage` field list learn the new field;
   cached rows with the old meaning are harmless (the ✓ they render stays identical).
4. Tests: local echo sets only `msg_echo`; firehose duplicate sets `msg_www` on an own message
   that already has `msg_echo`.

No MCProxy or mc-chat change: `msg_www` is webapp-internal, not on the wire. Exit: B2 closed.

### W6 — memory growth, answer the open question

After W2/W3 are deployed: read `smaps_rollup` Pss of mcapp at ~1 h, ~24 h and ~72 h uptime.
Growth under ~5 MB/day that flattens → close as baseline (page cache + arenas). Steady linear
growth → a follow-up with `tracemalloc` snapshots behind a dev flag. Either way B4 closes.

### W7 — Winlink (operator-owned)

1. Operator: phase 0 from the campaign doc, ~7 DMs to `WLNK-1` from `DK5EN-98` (~15 min on air).
2. Agent: phase 1 checks on the live DB, SSE and push — does a real `ackNNNN` hit
   `_APRS_ACK_GLOBS`, does the 120-char truncation hold on the help reply.
3. Operator decision on the account. Declined → close B5b with the reason. Accepted → campaign
   phases 2-4 as written.
