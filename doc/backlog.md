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

## B8 — group labels and callsign name overrides in mc-chat

Operator decision 2026-10-25: parity deferred. mc-chat needs the same `proxy:group_labels` event,
`/api/group_labels` and `/api/callsign_info/{call}/override` routes (wire contract in
`doc/2026-10-25_0900-conversation-nav-group-labels-plan.md` §3) and the shipped ICSSW snapshot.
Until then the webapp on an mc-chat backend shows bare group numbers and the editor's writes fail.

## B9 — serialise label writes before their broadcast

`sse_routes/labels.py` `broadcast_labels()` reads the payload and then broadcasts it. Two
overlapping PUTs can broadcast the older payload last, leaving live clients on a stale label map
until the next edit or reconnect. Needs concurrent operator writes, so low risk; an `asyncio.Lock`
around write + read + broadcast closes it.

## B10 — an override on a country-fallback group should inherit the country entry

Editing one field of a number that only resolves through the country fallback (e.g. `2628` →
`262`'s `DL · Deutschland`) creates an `own` label for `2628`; the exact key then beats the
fallback, so the other fields go blank although the editor showed the country values as
placeholders. Fix in `build_label_payload` (`group_labels.py`): treat the 3-digit country entry as
the upstream of an override key that has no exact or range entry, and include it in the GET
`upstream` map so the editor's `mixed` rules apply.

## B11 — allow desktop images on boards with enough RAM

The bootstrap refuses every desktop image (`check_desktop_image`, `bootstrap/lib/detect.sh`),
which is right for a 512 MB Pi Zero 2W and wrong for a Pi 4/5 with 2-8 GB. Decided 2026-10-31:
decide by `MemTotal` (< 1 GB refuse, 1-2 GB warn + confirm, >= 2 GB proceed), and skip the
headless-only tuning (`gpu_mem=16`, `cma-64`, `audio=off`, service disables) on a desktop box.
Concept: `doc/2026-10-31_0830-desktop-image-gate-concept.md`.

## B12 — McApp as a container image (Alpine, GHCR, Traefik)

A single Alpine-based container for NAS owners and advanced admins on any Linux box in the LAN:
uvicorn serves the SPA and the API, Traefik does TLS through labels, UDP 1799 is published
directly, the database lives in a volume, and updates are new image tags from
`ghcr.io/dk5en/mcapp`. Kubernetes is not a supported target (k3s recipe as an appendix).
Concept: `doc/2026-10-31_0830-container-deployment-concept.md`.

Follow-ups from the container test campaign (`doc/2026-10-31_1200-container-test-campaign.md`):

- **Gateway check is wrong as written.** Detect a rewritten source by "source equals the container's
  default gateway (`/proc/net/route`)", not `x.x.x.1` in `172.16/12`: CT4c learned and persisted the
  traefik-net gateway `192.168.97.1` and every UDP frame to the node was lost.
- The code changes listed in the concept (learning switch, `/api/timezone` 501, secret-box binding,
  Node Admin host allow-list, version metadata) are all still open.
- U4 (amd64 image) is unverified: in the OrbStack amd64 VM (QEMU/Rosetta) runc hung on every
  container start, so this is a host limit, not an image finding. Retest on a real x86 host or a NAS.
- iPhone PWA behind Authelia (campaign CT3a-W5, the only open campaign wave) is untested: the phone
  needs `mcapp-test.air-maxx.net` and `auth-test.air-maxx.net` resolved to the test host
  (`192.168.68.58`, the Mac). No router DNS is available; a public A record pointing at the private
  LAN IP would work unless the router filters private answers. Everything else is ready: the Caddy
  `@mcappct` block and the two `/etc/hosts` lines were kept on purpose; start the stack with
  `cd scripts/container-tests/proxy && docker compose --profile authelia up -d` and run the
  checklist in the concept's "Behind your own reverse proxy" section.
- Unexplained: one datagram from `172.17.0.1` during U3 (rootful Docker in a VM); the anti-flap guard
  ignored it.

## B13 — standalone BLE service and LAN auto-discovery

`ble_service` as a native service for any Linux box with a BLE adapter, plus mDNS/DNS-SD so a
McApp server finds BLE services and other McApp instances in the LAN and pairs with them from
the web GUI. Prerequisite for BLE with the container (B12), useful on bare metal too.
Decided 2026-10-31: `mcapp-ble` ships as the first Debian package (signed APT repo); McApp itself
stays on `mcapp.sh` until B12 has shipped. `avahi-daemon` and `libnss-mdns` become bootstrap
dependencies.
Concept: `doc/2026-10-31_0830-ble-service-and-lan-discovery-concept.md`.

Follow-ups from the container test campaign:

- **Bind guard gap.** `run-macos.sh` refuses a non-loopback bind without an API key; the systemd unit
  starts the uvicorn CLI directly, so nothing guards it there. Needs the wave-1 `main()` entry point.
- Shared adapter-contract suite for the bluez and bleak backends (macOS design §7.2), deferred.
- The dev-group `bleak` has no platform marker, so Linux CI installs bleak + dbus-fast. Harmless; fix
  the comment wording.

## B14 — RM1 update channel switch DEV/MAIN in the Remote view

The firmware gained `upd chan prod|dev` (fork-dev `636e681b`, benched over LoRa on 2026-10-31). McApp
needs the backend shape check, the vectors, the reply handling, and a MAIN/DEV switch on the update
card of the webapp. Work order with contract, file pointers and acceptance criteria:
`doc/2026-10-31_1000-rm1-updchan-requirements.md`.

## B15 — serve the webapp from Caddy and drop lighttpd

lighttpd only serves static files on `127.0.0.1:8082` behind Caddy, which already has
`file_server`. Moving the SPA to Caddy (`try_files` + `file_server`) removes one process, one
package and one config from the Zero 2W. Needs a `SYSTEM_EPOCH` bump and a PSS measurement on
mcapp.local first. Decided 2026-10-31 alongside it: no Ansible rewrite of the bootstrap, no nginx.
Evaluation: `doc/2026-10-31_1100-ansible-and-web-server-evaluation.md`.

## B16 — webapp follow-ups from the container test campaign

Webapp repo. Found while running McApp behind Traefik/Authelia in a container
(`doc/2026-10-31_1200-container-test-campaign.md`).

- **Load the Browser AI WASM on demand.** 107 MB of the 112 MB bundle are the `litertlm_*.wasm`
  variants, which put the image at 228 MB instead of the 60-80 MB target. Operator decision 2026-10-31.
- **GPS card stops retrying `/api/timezone` after a failure** (501 in the image) and the stall
  reporter skips that path.
- **Answer a 401 with a full-page navigation.** Behind Authelia an expired session turns every XHR
  into a 401; only a top-level navigation reaches the login portal.

## B17 — small follow-ups from the container test campaign

- **`scripts/container-tests/node/node.py info`** reports the node IP as `null` unless the node was
  reset: the IP appears only in the WiFi boot line, not in `--info`.
- **Test flake:** `node_admin_service_tests.case_e_dropped` failed once in the full runner with
  `OSError: Directory not empty` in `TemporaryDirectory` cleanup (a DB write outliving `svc.stop()` /
  `storage.close()`); 0/10 failures alone.
