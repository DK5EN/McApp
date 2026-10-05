# Node Admin (RM1 / HMAC remote admin): UI concept and implementation plan

Date: 2026-10-05, revised after `/fable-review` the same day (rev 2). Status: PLAN, nothing coded. Builds on the
concept paper `~/Desktop/mcapp-hmac-remote-admin.md` (2026-10-04; firmware side: `concept-open-issues-20261004.md` §6,
upstream issue #1189). Repos: backend `~/WebDev/MCProxy`, frontend `~/WebDev/webapp` (separate git repo, commit
independently). Firmware ground truth: `MeshCom-Firmware-DEV-Main`, branch `fork-dev` (db9c4495), files
`src/remote_cmd.cpp`, `src/rm_runtime.cpp`, `src/remote_cmd.h`, `docs/adr-remote-hmac.md`,
`tools/tests/remote_cmd_vectors.json`.

## 0. What the review changed (read this first)

Six independent finders produced 120+ candidates; five Opus verifiers tried to refute each one against the real code.
No candidate was refuted outright, but many sub-claims were, and several severities dropped. Everything below that is
marked `[R]` is a verified review result. The ones that change the design:

1. **The node is slower and stricter than the paper assumes.** One accepted command or sync per 10 s per node, one
   high-water mark per node shared by all senders, a 2-frame queue, and a lockout (3 counted rejects in 90 s lock RM for
   5 min, silencing even a valid sync). McApp must enforce one in-flight command per target and 10 s spacing itself.
2. **The counter rule is wrong in the paper.** The firmware's own sender uses `max(last+1, unix_time, hwm+1)`. A
   sequential small McApp counter dies the moment any other sender (a second SysOp, the firmware web UI) has used the
   node. McApp adopts the same rule.
3. **Replies reach McApp with a `{NNN` suffix on the Extern-UDP path** (BLE copy is stripped). The hook must strip it, or
   every reply fails on a UDP-only box while the BLE bench passes green.
4. **`UPDATE ... RETURNING` through the storage helpers silently rolls back.** The counter must be allocated in one
   `db_write` transaction in `to_thread`.
5. **The `setout` grammar, the Re-ask semantics, the "bad tag" meaning and the reply-tag orientation in the paper are
   wrong** (details in §2).
6. **Security: the API has no authentication at all** (pre-existing). Node Admin ships behind a config flag, default
   off, plus a Host/Origin guard that makes it LAN-only even in the public-TLS modes.

## 1. Decisions

| Id  | Decision                                                                                                                                                                                              |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| U1  | Confirmation via `useConfirm()` (the promise wrapper over `BaseConfirmModal`, danger variant) for risky commands only: `reboot`, `gateway`, `mesh`, `txpower`, `setout`. [R] names `useConfirm`.      |
| U2  | New `/nodeadmin` view plus a `NodeAdminSecretsCard` in Settings (General). Not a Bluetooth-view tab.                                                                                                  |
| U3  | Auto-sync before the first command to a target after a McApp restart (user decision). Revised mechanics in §3.4: sync is a freshness nicety, no longer a correctness gate (see D2).                   |
| U4  | RM1 replies also appear in the normal DM conversation; the admin view adds the verified badge.                                                                                                        |
| U5  | Deliverable is this markdown file, no HTML mockup.                                                                                                                                                    |
| U6  | Pushing the password to a node is out of v1.                                                                                                                                                          |
| D1  | `node_admin.enabled` in `config.json`, default `false`. Off: router not mounted, `GET /api/node-admin/targets` answers 503 (the webapp treats it as absent), nav item and card hidden.                |
| D2  | Counter rule `ctr = max(stored + 1, last_hwm + 1, unix_seconds)`, `last_hwm` only ever rises. Falls back to `max(stored+1, last_hwm+1)` when the Pi clock is before 2024 (firmware `clockUnix` rule). |
| D3  | One command in flight per target (HTTP 409 on a second), and no frame (command or sync) to a target earlier than 10 s after the previous reply (or after the previous hand-off if none came).         |
| D4  | `tx_verdict` is dropped from v1. The service gets the transport result directly from an injected `transmit()` callable and stores `send_error`.                                                       |
| D5  | Per-target optional `tx_max` (default 15 dBm, the lowest board maximum) caps the `txpower` input. Over-limit values are a silent counted reject on the node, so the UI must not offer them.           |

## 2. Corrections to the concept paper (verified)

| Paper                                                                    | Reality [R]                                                                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `setout <n> <0\|1>` (§3, W3)                                             | `setout <a0..a7\|b0..b7> <on\|off>` (`remote_cmd.cpp:149-154`); anything else is `blocked`, a counted reject. Runtime answers `err not output` for a pin not set up as output.                                                                                                                    |
| `txpower <n>`                                                            | `0..board max`, no negatives, no leading zero. Board max is 22 default, 20 T-Beam/LoRa32 v2.1, 15 Heltec V2. Out of range is silent and counted, never an `err` reply.                                                                                                                            |
| Counter "per (own call, node)", sequential, jump to `hwm+1` on `ctr<hwm` | One mark per node for all senders. Use D2. The firmware sender seeds with unix time (~1.79e9).                                                                                                                                                                                                    |
| Tag `dst`/`src` "as in the frame"; reply tag unspecified                 | Both tags use the COMMAND's orientation: `dst` = managed node, `src` = commanding node (`msg_source_call`, upper case, SSID as stored on the node). Reply tag is `RM1R\|<managed>\|<commander>\|ctr\|result`, `result` includes the `ok `/`err ` prefix. Never use the reply frame's own src/dst. |
| `CALL_SIGN` is bare                                                      | On a BLE-attached box McApp adopts the node's call with SSID from the `I` register (`main.py:2611-2633`), and `apply_callsign` can change it at runtime. Persist `src_call` per log row.                                                                                                          |
| "Resend is a new counter" (MA-D4) / plan rev 1 "Re-ask is safe"          | The node caches ONE reply (the latest accepted command, RAM only, 10 min, cleared by reboot). Re-ask is safe only for the newest row of that target inside 10 min; otherwise it is a counted silent replay reject, or, if the original never arrived, a first execution.                          |
| `bad tag` = "wrong password on the node"                                 | A wrong password gives NO reply at all (silent tag reject). `bad tag` means a reply arrived that McApp could not verify: McApp bug, password changed since, or spoofed.                                                                                                                           |
| "The tag reveals nothing about the key" (§4.5)                           | False for a human password: `K = SHA-256(password)`, no salt, and the tag confirms a guess offline from ONE captured frame (accepted in the firmware ADR RM-D1). Use a random 14-character password (Generate button, §3.3).                                                                      |
| `RM1` commands can reach a gateway target via the server                 | They cannot: a server-delivered copy is shown as plain text and the later RF copy is dropped by DM dedup (`rmTryQueue` refuses `msg_server`). The target must hear the command over LoRa.                                                                                                         |
| `sync` reply semantic                                                    | Sync always has `ctr 0`, its reply carries no nonce and verifies forever. Use it only as `max`-merge into `last_hwm`. Sync also stamps the 10 s limiter.                                                                                                                                          |
| Status reply example                                                     | Real: `ok v=<ver> up=<minutes> bat=<n> heap=<n> gw=<0\|1> mesh=<0\|1>`; toggles `ok <name>=on\|off`; `ok txpower=<n>`; `ok sent`; `ok rebooting`; `ok a2=on`; errors only `failed`, `not output`, `storage`.                                                                                      |
| Vectors at `test/test_remote_cmd/vectors.json`                           | `tools/tests/remote_cmd_vectors.json` (firmware repo is canonical). Copy lives at `src/mcapp/remote_cmd_vectors.json` (dev tarball ships `*.json`; prod does not, so runtime must never read it).                                                                                                 |
| `requiresAdminBackend` hides Node Admin on mc-chat                       | That flag means mc-chat-only. Add `requiresMcAppBackend`.                                                                                                                                                                                                                                         |
| `tests/vectors/` location                                                | Would not travel in the dev tarball.                                                                                                                                                                                                                                                              |

Prerequisites on the target (bench and field): `--remotemgmt on` AND a non-empty `--passwd` (check `--info`: `RM: on`).
Otherwise the RM1 DM is ordinary acked text. Password rules: 1..14 bytes, ASCII, no leading space, not `none`.
Target callsign at most 9 characters (a longer `{CALL}` goes out as a broadcast).

## 3. UI concept

### 3.1 Principles

- One screen for one job: pick a node, pick a command, see the exact frame that went out, see the verified answer.
- State is explicit and slow: every history row walks `queued -> on air -> waiting -> verified | no reply | bad tag`,
  derived server-side from timestamps. A reply takes 20-45 s on air; never imply instant execution.
- A wrong password, a rate-limit drop, a lockout, RM switched off and an internet-only path all look identical on air:
  silence. The UI says so instead of guessing ("no reply" help text, §3.4).
- The password is write-only everywhere. No endpoint returns a tag for a counter that has not been sent.
- Reuse the existing vocabulary (`SettingsCard`, `BaseBadge`, `useConfirm`, `useToast`, CSS tokens). Reply text is
  rendered as plain text only (interpolation, never `v-html`; the monitor's `v-html` is not a pattern to copy).

### 3.2 Navigation

New `NAV_ITEMS` entry "Node Admin" (`/nodeadmin`, `group: 'utility'`, `surfaces: 'both'`, `requiresMcAppBackend`). On
mobile it appears in the More sheet, which grows from 4 to 5 rows on McApp [R]. No router guard: the view renders three
states from `adminStatus.backendIsMcChat` (null: `BaseLoadingState`, true: `BaseEmptyState` "only available on McApp",
false: the UI). A guard would bounce every reload because the value is `null` at first paint [R].

### 3.3 Settings card: "Node admin keys"

Placed in the General section beside `QrzLookupCard`, rendered only when `backendIsMcChat === false` and the backend
reports node admin enabled.

```
+- Node admin keys --------------------------------------------+
| One password per managed node. Stored encrypted on this Pi,  |
| never shown again. It is also that node's net-console and    |
| KISS password, and anyone who captures one RM1 frame on air  |
| can guess a short password offline. Use a random one.        |
|                                                              |
|  DK5EN-90   [key set]   max TX 20 dBm   updated 2026-10-04   |
|                                      [Replace] [Remove]      |
|  DK5EN-91   [key unreadable - re-enter]      [Set key]       |
|                                                              |
|  + Add node                                                  |
|  Callsign [ DK5EN-92 ]   Password [ ********** ] [Generate]  |
|  Max TX power [ 15 ] dBm (lowest board limit; raise per node)|
|  [Save]                                                      |
|                                                              |
|  Generate: 14 random [A-Za-z0-9] characters (~83 bits),      |
|  shown ONCE with "set this on the node: --passwd ..." and a  |
|  copy button. Warn (never block) under 12 chars, all digits, |
|  or all lower case.                                          |
|  Remove: inline "Remove key? The counter is kept."           |
+--------------------------------------------------------------+
```

Validation inline: callsign is upper-cased and trimmed, at most 9 characters, must not equal this McApp's attached
node call; password 1..14 ASCII bytes, no leading space, not `none`. The password field is cleared after success and
after failure. A target whose ciphertext cannot be decrypted (board moved, `secret.key` lost) shows `key unreadable`
with a re-enter action, never a 500.

### 3.4 Main view `/nodeadmin` (desktop)

```
+- Node Admin ---------------------------------------------------------------+
| Target [ DK5EN-90  v ]   key set   counter 1791234567   last sync 12 min ago |
| Via:   [BLE connected]   (no automatic UDP fallback; UDP only when BLE is    |
|         not configured)                                                      |
|                                                                              |
| +- Compose ---------------------------+ +- Answer --------------------------+|
| | Command [ display      v ]          | | #...567  display off              ||
| | Value   (o) off  ( ) on             | |   waiting for reply... 00:27 / 2:00||
| |                                     | |   handed to node via BLE 14:02:11 ||
| | Will send:                          | |                                   ||
| |  RM1 <next> display off <tag>       | | #...566  status                   ||
| |                                     | |   [verified] ok v=4.35u up=4320.. ||
| | [ Send ]            [ Sync ]        | |   14:01:30 -> 14:01:58            ||
| +-------------------------------------+ +-----------------------------------+|
|                                                                              |
| History                                          [filter: this node v]       |
| ctr         time      command       via   state            reply             |
| ...567      14:02:11  display off   BLE   waiting 0:27     -                 |
| ...566      14:01:30  status        BLE   verified         ok v=4.35u up=..  |
| ...565      13:58:02  txpower 15    UDP   no reply [Re-ask]-                 |
| ...564      13:50:44  reboot        BLE   bad tag          (unverified text) |
+------------------------------------------------------------------------------+
```

Behaviour:

- **Target select** lists every node with a stored key; an empty list links to Settings > Node admin keys.
- **Command select** is driven by one static allowlist table (`utils/nodeAdminCommands.ts`) that mirrors the firmware
  exactly: `status`, `sendpos`, `sendtrack`, `reboot`, `sync`; toggles `gps|track|display|gateway|mesh` with an
  on/off segmented control; `txpower` as an integer input bounded by the target's `tx_max`; `setout` as bank `a|b`,
  pin `0-7` and an on/off switch (frame `setout a2 on`). No free-text commands.
- **"Will send" preview shows a placeholder, never a tag.** The counter and tag exist only at hand-off; the real frame
  appears in the row after sending. No endpoint ever returns a tag for an unsent counter [R].
- **Risky commands** (U1) go through `useConfirm()`: "Reboot DK5EN-90? It will be unreachable for about a minute."
- **Send is non-blocking.** `POST /send` allocates the counter, writes the row and returns; the transmission runs as a
  server background task (BLE remote calls can take 30 s, the client aborts at 10 s and a retry would execute twice
  [R]). The client never auto-retries; on any error it refetches history. Send and Sync are disabled for a target
  while one of its rows is `queued`, `syncing` or `on air`, and for 10 s after its last reply (D3).
- **Auto-sync (U3):** the first send to a target after a McApp restart goes out as a sync first; the command is handed
  off no earlier than 10 s after the sync reply (the node's rate limit). The row shows `syncing`. If no sync reply
  arrives within 90 s the command is sent anyway, with a warning toast: the unix-time counter floor (D2) means a
  stale-state rejection is unlikely, and the old "do not send, offer Send anyway" gate is gone because it only fed the
  lockout counter.
- **State derivation:** `no reply` is a server state computed from `sent_at` (120 s), not a browser timer; the
  countdown in the UI is derived from `sent_at`. A late reply still flips the row.
- **Re-ask** is offered only on the newest row of that target, within 10 min of its hand-off, at most once per 10 s,
  never for `reboot`, and goes through `useConfirm()` for risky commands. It resends the byte-identical stored frame
  with no new counter. Otherwise only **Run again** (new counter, new execution) exists. After 2 silent outcomes to
  one target inside 90 s the UI stops offering sends for 5 min and says "node may be locked out (5 min) or
  unreachable", never "wrong password".
- **"no reply" help text** lists the real causes: RM off or no password on the node, wrong stored password, rate limit
  (10 s), lockout (5 min), the node did not hear McApp's node over LoRa (a gateway/internet-only path is never
  executed), or the reply is still on air. A chat bubble reading "Delivered" for an RM1 command means only "the frame
  reached the node", never "executed".
- **Verified badge** means the reply tag checked out. `bad tag` keeps the raw text as evidence and never closes the
  row (§5, an unverified frame cannot mask the real reply). `verified` is terminal and wins over any later frame.
- **Recovery:** the store refetches `GET /api/node-admin/history?target=` on view mount, on every SSE (re)connect
  (`system:connected`) and on `visibilitychange` to visible. The SSE has no replay and drops slow clients [R].
- **Unavailable states**, one sentence each: no keys stored; backend is mc-chat; node admin disabled in config; no
  route to the attached node ("No BLE connection and no UDP target").

### 3.5 Mobile

Single column: Target, Compose, Answer, History. History collapses to cards (command, state badge, time, reply on a
second line); history requests carry `limit`. Send and Sync are full-width at the bottom of the Compose card. The
preview wraps and never scrolls horizontally. Reduced-motion: the countdown is text, no spinner animation is required
to convey state (`BaseSpinner` has no reduced-motion rule today [R]).

### 3.6 In the normal chat

The sent command and the target's reply remain ordinary DM bubbles (U4). Known limits [R]: a blocked-text pattern such
as `ok` hides the reply bubble, and the classifier may tag it; the admin view (SSE plus history) is the authoritative
surface. The reply also triggers a Web Push with the raw `RM1 ...` text (deferred, §8).

## 4. Data model (schema v35)

```
node_admin_keys   (target_call TEXT PK, password_enc TEXT, created_at, updated_at)
node_admin_state  (target_call TEXT PK, ctr INTEGER NOT NULL DEFAULT 0, last_hwm INTEGER NOT NULL DEFAULT 0,
                   last_sync_at, tx_max INTEGER NOT NULL DEFAULT 15)
node_admin_log    (id INTEGER PK, target_call, src_call, ctr, cmd, args, text, sent_at, handed_off_at,
                   transport, send_error, reply_text, reply_at, verified INTEGER, result TEXT)
-- partial unique index on (target_call, ctr) WHERE ctr > 0   (sync rows all have ctr 0)
```

- `target_call = target.strip().upper()` at the API boundary; the same value keys the state row, the AAD
  (`node_admin.password:<TARGET>`), the tag input and the transmit `dst`. SSID-normalise as the firmware does.
- `password_enc` is the `SecretBox` token (`v1:...`, a string). One shared `SecretBox` instance with QRZ. Decrypt via
  `asyncio.to_thread` (first use reads or creates the key file) and optionally cache the derived HMAC key per target,
  invalidated on PUT and DELETE.
- `src_call` is frozen per row at hand-off. Verification uses the row's `src_call`, never the current `my_callsign`.
- `text` is the exact frame sent (needed for a byte-identical Re-ask). The state row is KEPT when a key is removed or
  replaced; a re-added key continues from the stored counter (T15: restarting at 1 means silent replay rejects and a
  lockout).
- **Counter allocation is one `db_write` transaction in `to_thread`:** `INSERT OR IGNORE` the state row, then
  `UPDATE ... SET ctr = MAX(ctr + 1, last_hwm + 1, <unix floor>) ... RETURNING ctr`, `fetchone()` inside the `with`,
  then the log INSERT. Never `_mutate` (returns rowcount only) and never `_query` (read connection, never commits, rolls
  back on close) [R]. Precedent: `storage/uptime.py:244-258`, `storage/classifier_api.py:60-80`. Refuse at
  `4294967295`. `last_hwm = MAX(last_hwm, reported)` only after tag verification; a forged sync reply carrying
  `ctr=4294967295` must not move it.
- `LATEST_SCHEMA_VERSION` 34 to 35 in `storage/constants.py` and the `current_version < 35` block in the same commit.
  `UPDATE ... RETURNING` needs SQLite >= 3.35 (met by the Pi's system library; assert in the W1 test).
- Startup reconciliation marks rows still `queued`/`syncing`/`waiting` from a previous process as `abandoned`/`no
reply`. The log table is capped (e.g. 1000 rows per target) by the writer.

## 5. Backend and API

Service `node_admin_service.py`, pure core `remote_cmd.py`, router `sse_routes/node_admin.py`, as in the paper §4.2-4.3,
with these rules (all [R]):

**Pure core (`remote_cmd.py`)**

- `rm_tag`, `rm_reply_tag`, the exact firmware grammar and allowlist (`rmParse` rules: lower-case, single spaces,
  `cmd <= 15`, `args <= 23`, forbidden `;` `{` `%` `--`), `parse_reply(text)` which strips the suffix with the existing
  `util.strip_ack_suffix` (strict `\{([0-9]+)$`; MCProxy's own helper is fine, the "do not reuse" warning applies to
  mc-chat/webapp), then splits the LAST token off as the tag and everything between `ok|err` and the tag as `result`.
  Tag compare with `hmac.compare_digest`. The runtime never reads the vectors JSON and `main.py` never imports
  `remote_cmd_tests` at module level (release tarball rules).

**Send path**

- `POST /api/node-admin/send` returns after the row insert; the transmit is a background task. Per-target
  `asyncio.Lock` covers allocation and hand-off; a second send gets 409 (D3).
- Counter, tag, `src_call` and `text` are produced at hand-off time, after the sync gate opens, never at click time.
- Transport: the orchestrator injects `transmit(transport, dst, msg) -> str | None` built in `main.py` over the same
  helpers `_handle_outbound` uses (it returns the failure reason). Payload on both topics mirrors linkcheck:
  `{"dst", "msg", "type": "msg", "src_type": "node_admin"}`; transport choice copies `_linkcheck_send_topic`
  (`commands/linkcheck.py:476-486`) and uses `get_protocol("ble_client")` (a `None` client is possible;
  `_ble_connected(client)` takes the client). There is no automatic UDP fallback; UDP success only means the datagram
  left. `send_error` records a refusal such as "BLE not connected"; no counter is consumed when there is no route.
- `src` for the tag is the attached node's call exactly as the node reports it (`I.CALL`); on a UDP-only box where it
  is unknown, refuse to send until it is configured. The command DM must differ from McApp's own callsign.
- No RM1 text in `send_failed` broadcasts or monitor payloads beyond `RM1 <ctr> <cmd>` (truncate the tag); the INFO log
  line already cuts at 40 chars.

**Reply hook** (`storage/ingest.py`, seam only)

- `reply_hook: Callable[[dict[str, Any]], Awaitable[None]] | None`, `None` by default, called in `store_message` before
  `_should_filter_message`, next to the gateway-uptime hook (about lines 1826-1834). It only observes; the reply still
  lands as a DM (U4).
- The seam is `try: ... except Exception: logger.exception(...)` and the hook body is a synchronous prefilter
  (`msg.startswith("RM1 ")`, length bound, an open row for the target in memory) plus a `create_task` or `to_thread`
  for DB and SecretBox work. A raising hook must not lose the stored row.
- Correlate only when `parsing.strip_relay_path(src) == row.target_call` AND the frame `dst` resolves to
  `row.src_call`; anything else is ignored silently. This also rejects overheard replies addressed to another SysOp.
- BLE and UDP copies of one reply arrive in two independent tasks about 100 ms apart. Exactly-once is a synchronous
  in-memory claim taken before the first await, or `UPDATE ... WHERE id=? AND verified IS NOT 1` acting (SSE, gate
  release, `last_hwm`) only when `rowcount == 1`.
- A frame that fails verification never changes row state (appended as capped evidence, counted as
  `rejected_replies`); `verified` is terminal. A sync reply matches only the newest OPEN sync row of that target with
  `reply_at > sent_at`.
- Limitation to document: `_storage_handler` returns before `store_message` on a blocklist verdict, so a blocked target
  never reaches the hook.

**Events**

- The service emits the bare SSE event `node_admin:reply` (full log row) through `broadcast_event`, precedent
  `monitor:console` (`node_console.py:335`), on every state transition, so a second browser sees a send. The webapp
  keeps it out of `ENVELOPE_UNWRAP` and upserts by row id. There is no replay (§3.4 recovery).

**Guard and security** (D1)

- `node_admin.enabled` (default false) and optional `node_admin.allowed_origins` under the `config.json` loader
  (`StallsConfig` pattern). When disabled the router is not mounted.
- A guard on `/api/node-admin/*` only: reject a `Host` that is not in the LAN allowlist (`hostname -s`,
  `mcapp.local`, the names in `Caddyfile.mcapp:117`, private/loopback IP literals; NOT `TLS_HOSTNAME`), and reject a
  present `Origin` whose host differs from `Host`. This closes CSRF from any LAN browser page and DNS rebinding, and
  makes the feature LAN-only in every public-TLS mode with no new secret. A custom header would NOT help:
  `allow_headers=["*"]` approves any header. Same-origin GETs send no Origin and pass.
- Rejected for v1 (verified as disproportionate): PIN or cookie session, PBKDF2 or a separate RM key (both change the
  firmware contract the ADR decided against), a sync nonce, "replace needs the old password", a watermark file outside
  the DB.
- Redaction: `stall_middleware.py` withheld-body check changes from exact match to a prefix rule
  (`path == "/api/node-admin/keys" or path.startswith("/api/node-admin/keys/")`, QRZ exact entry kept); the webapp
  `stallReporter.ts` `BODY_DROP_PATH_PREFIXES` gains `/api/node-admin/keys` (prefix match already covers
  `/keys/{target}`). A normal JSON body is already masked by `redact()`, so the real exposure is a truncated or non-JSON
  body; the suite case must use one. A 15-character password also produces a pydantic 422 whose `input` echoes the
  plaintext even with `SecretStr`: strip `input` from the response (custom handler) and add a case.
- Out of scope, tracked separately: in the public-TLS modes the whole unauthenticated API (including the `--` console
  passthrough and `/api/update/*`) is already internet-reachable. Fix with basic-auth or Cloudflare Access in the
  public Caddyfile templates; it does not belong in this feature.

**Routes**

```
PUT    /api/node-admin/keys/{target}   {"password": "...", "tx_max": 15}   -> 204 (write-only)
DELETE /api/node-admin/keys/{target}                                        -> 204 (state kept)
GET    /api/node-admin/targets          -> [{target, has_key, key_unreadable, ctr, last_hwm, last_sync_at, tx_max}]
POST   /api/node-admin/send             {"target","cmd","args","transport":"auto|ble|udp"} -> {log_id, ctr, text}
POST   /api/node-admin/reask/{log_id}   -> {log_id}      (409 unless newest row, <10 min, >=10 s since last)
POST   /api/node-admin/sync/{target}    -> {log_id}
GET    /api/node-admin/history?target=&limit=   -> [log rows, with computed state]
SSE    node_admin:reply                 full log row, bare event
```

## 6. Wave plan (`/orchestrate-waves`)

Writers are `implementer` (Sonnet). The orchestrator owns every integration file and all git. Each behavioural wave ends
with the full gate and the `fable-review` advisor pass before its commit. Every brief states: exclusive file set, no
git, no whole-repo formatter, no contact with mcapp.local, BLE, a node or any radio, regression-test-first, scoped
verification, terse report. Backend and webapp commits are separate per wave.

Wave status log (update after every wave):

- W0: pending
- W1: pending
- W2: pending
- W3: pending
- W4: pending

### Shared resources

No shared build tree or port: each suite runs on ephemeral state. Writers run only their own suite; the whole
`scripts/run_startup_tests.py` run belongs to the gate. The one exclusive resource is the bench (node, BLE,
mcapp.local): W4 only, orchestrator, serialized.

### W0: contract pins (orchestrator, no writers)

Q1-Q3 are answered by the firmware source (§2, §7). Remaining W0 work: pin the Python interface the W2 writers code
against, so they stay disjoint:

- `NodeAdminService` Protocol (list_targets, set_key, delete_key, send, reask, sync, history, on_reply) in a small
  stub module, `SSEManager.node_admin_service: NodeAdminService | None = None` in `sse_handler.py`,
  `reply_hook` type, the `transmit` callable signature, the config keys.

### W1: pure core and storage (backend, 2 writers)

| Owner | Exclusive files                                                                                                                                                                                                     | Verification                                        |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| A1    | `src/mcapp/remote_cmd.py` (new), `src/mcapp/remote_cmd_tests.py` (new), `src/mcapp/remote_cmd_vectors.json` (new, copied from the firmware repo, sha256 pinned, copy and hash in one commit)                        | own suite; `uvx ruff check`; `uv run mypy`; sha pin |
| A2    | `storage/migrations.py` (v35 block), `storage/constants.py`, `storage/node_admin.py` (new mixin), `sqlite_storage.py` (mixin list), `storage/migration_chain_tests.py`, `storage/node_admin_storage_tests.py` (new) | migration chain suite; own suite; mypy              |

Orchestrator gate hotspots: `scripts/run_startup_tests.py` registers `remote_cmd_tests` and
`node_admin_storage_tests`.

### W2: service, routes, hook (backend, 3 writers, after the W1 gate)

| Owner | Exclusive files                                                                                                                                    | Verification                                                                           |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| B1    | `node_admin_service.py` (new), `node_admin_service_tests.py` (new)                                                                                 | own suite (service cases in §6.1)                                                      |
| B2    | `sse_routes/node_admin.py` (new), `schemas.py`, `stall_middleware.py` (withheld-prefix change only), `node_admin_routes_tests.py` (new, top-level) | own suite (guard, redaction, 422)                                                      |
| B3    | `storage/ingest.py` (the `reply_hook` seam only), `storage/node_admin_hook_tests.py` (new)                                                         | hook suite (placement, exactly-once, raising hook); diff of the existing ingest suites |

Orchestrator after the writers return: `main.py` (construct service with the shared `SecretBox()`, build `transmit`,
assign `reply_hook`, one shared `wire_node_admin()` used by `main.py` and one test), `sse_handler.py` (mount router, the
Host/Origin guard wiring, `node_admin_service` attribute), `config_loader.py` (`node_admin` section), and
`run_startup_tests.py` (register `node_admin_service_tests`, `node_admin_routes_tests`, `node_admin_hook_tests`; five
new suites in total with W1). Gate: `uv run python scripts/run_startup_tests.py`, `uvx ruff check`,
`uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src`. Advisor pass. Commit.

### W3: webapp (3 writers, after the W2 gate)

The orchestrator first writes `types/nodeAdmin.ts` from the W2 contract and applies the hotspot edits itself:
`events/eventTypes.ts` and `composables/useSSEClient.ts` (`SSE_EVENT_KEYS`), `router/index.ts`,
`constants/navigation.ts` (`requiresMcAppBackend`), `App.vue` (`navGate` must honour the new flag; it is the sole
consumer of `requiresAdminBackend`), `stores/adminStatus.ts` (`mcAppAvailable = backendIsMcChat === false`, null =
hidden) and its spec, `main.ts` (`initEventBus` plus the HMR dispose list), `views/SettingsView.vue` (the card slot,
gated on `backendIsMcChat === false`), `services/stallReporter.ts` (+ spec case).

| Owner | Exclusive files (all new)                                                                                                           | Verification                                                         |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| C1    | `components/settings/NodeAdminSecretsCard.vue` + spec                                                                               | own spec; eslint and `vue-tsc` on the files                          |
| C2    | `stores/nodeAdmin.ts`, `views/NodeAdminView.vue`, `components/nodeadmin/NodeAdminComposer.vue`, `utils/nodeAdminCommands.ts`, specs | own specs (store state machine from SSE fixtures; composer risk set) |
| C3    | `components/nodeadmin/NodeAdminHistory.vue`, `components/nodeadmin/NodeAdminAnswer.vue`, specs                                      | own specs; 375 px and 1280 px render assertions                      |

Interface pinned in the briefs: `NodeAdminHistory` takes `rows: NodeAdminLogRow[]`, emits `reask(id)` and `rerun(id)`;
`NodeAdminAnswer` takes `row: NodeAdminLogRow | null`. The planned `sendQueueStore.nodeadmin.spec.ts` is dropped: it
tested a path that is never taken (admin never uses `/api/send`) [R]. The real invariants are in the store spec.

Gate: `npm run lint`, `format:check`, `typecheck`, `test`, `build:strict`, then a built-bundle check that the route and the
`node_admin:reply` key are present. Test over plain `http://mcapp.local` too (no secure-context-only APIs, e.g.
`crypto.randomUUID`). Advisor pass.

### W4: bench and docs (orchestrator, serialized; needs firmware RM on the target)

- Prerequisites on DK5EN-90: `--remotemgmt on`, non-empty `--passwd`, `--info` shows `RM: on`. Target is a non-gateway
  node (a gateway target can receive the command via the server and ignore it).
- Frame budget stated up front (about 4 keyings per DM on official firmware). Transmit only between the owned bench
  nodes (DK5EN-90 target, DK5EN-14 attached).
- Steps: `status`, `display off|on`, `sync`, one reboot. **One run with BLE disabled (UDP only)**: the only real-air
  proof that the `{NNN` suffix is handled. Capture both real reply shapes (BLE-stripped, UDP with `{NNN`) as fixtures.
- Counted rejects (wrong key, replay, unknown command, stale Re-ask) are at least 60 s apart (window is anchored at the
  first reject; 0/45/90 s locks, 0/46/92 s does not). If the lockout is tested on purpose, run it as its own step,
  wait 5 min and prove recovery with a `sync`. McApp itself must refuse a non-newest Re-ask and a sync-then-command at
  less than 10 s.
- Docs: `doc/architecture-reference.md`, `doc/dataflow.md` (send and reply flow), `doc/database-reference.md` (v35),
  `doc/operations-reference.md` (new endpoints, the secret, the "silent on stale counter / lockout / RM off" runbook),
  a CLAUDE.md section for the non-obvious rules (hook before the filter, reply `{NNN` suffix, unix-time counter,
  one-in-flight and 10 s spacing, lockout arithmetic, flag inversion, vectors corpus with the firmware repo as
  canonical), the corpus entry in "Key Gotchas", and webapp `docs/`. Mark paper §4.4/§5 superseded by this document.
  Run `npx --yes prettier@3 --write` on each markdown file, then `uvx ruff format --check .`.
- Release is a separate step (`/dev-release`).

### 6.1 Minimal required test cases (each catches a bug that would otherwise ship green)

Mutation checks are named in the briefs: drop `dst` or swap `dst/src` in the tag input; replace
`strip_ack_suffix`; use `hwm+1` only or read-then-write allocation; let a forged `ctr=4294967295` move `last_hwm`;
move the hook after the filter, after the claim, after the INSERT; drop the tag check, the ctr correlation, the src
check; make the withheld list exact-match again. Each must fail at least one named test.

- **A1 (pure):** all 17 command and 4 reply vectors plus the sha256 pin; every reply vector with `{087` appended parses
  and verifies identically; validator rows (`setout a2 on` accepts, `setout 2 1` rejects, txpower above the bound
  rejects, ctr 0 non-sync rejects, a blocked command rejects).
- **A2 (storage):** allocation (10,50)->51, (50,50)->51, (60,50)->61 and refusal at 4294967295, read back from a fresh
  connection; `last_hwm` max-merge (41 then 5 stays 41); key delete then re-add does not restart the counter.
- **B1 (service):** wiring replay of all vectors through the real service with config `CALL_SIGN` bare and a different
  injected `attached_call()` (published `{dst, msg}` must equal the vector); wrong tag gives `bad tag` and leaves
  `last_hwm` unmoved, including the forged sync; verified then forged bad-tag stays verified with one SSE event, and
  bad tag then genuine ends verified; fake clock (119 999 ms waiting, 120 000 ms no reply, late reply flips, startup
  sweep); auto-sync then command spacing >= 10 s and a second send held 10 s after the previous reply; Re-ask refused
  unless newest, byte-identical, no counter consumed; lower-case PUT then upper-case send works and an unreadable key
  publishes nothing and consumes no counter; a password sentinel absent from DEBUG logs, the monitor ring and the log
  table, including after a forced decrypt failure.
- **B2 (routes):** a clone of `stall_http_tests` case 9 for `PUT /api/node-admin/keys/DK5EN-90` through the real
  `StallMiddleware` with an oversized or non-JSON body (the redaction masks a small JSON body, so only those cases
  discriminate the prefix rule); no response contains the password including the 422; foreign `Origin` 403, foreign
  `Host` 403, same-origin OK, GET without Origin OK; disabled config 503.
- **B3 (hook):** real `store_message` with the UDP-shaped reply carrying `{NNN`, then the `ble_remote` copy 100 ms later:
  one `messages` row, hook invoked for both, row verified once, one SSE event; a raising hook still stores the row.
- **Orchestrator:** a published `ble_message` through the real `_handle_outbound` keeps `msg` byte-identical with
  verdict `sent`.
- **W3:** exact method, URL and body per call with the target URI-encoded; `stallReporter` drops the body for
  `/api/node-admin/keys/DK5EN-90`; store keeps `verified` against a later `bad tag` event and does not retry a failed
  POST on reconnect; composer risky set equals U1, a risky Send calls `post` zero times before confirm, `setout` emits
  `a2 on`; the card clears the password after success and failure; the view mounts and sends with
  `crypto.randomUUID` undefined.

## 7. Questions

Answered by firmware source (no longer open):

1. `src` in the tag is the attached node's `node_call` byte-exact; `dst` is the target's call upper-cased and
   SSID-normalised; both tags use the command orientation.
2. PN retries are caught by the target's dedup and never reach RM; a Re-ask carries a fresh `{NNN` and does, with the
   single-slot-cache semantics in §2.
3. A second command waits for the reply plus 10 s; sync rows are not unique by counter.

Still open:

- Whether the MeshCom server routes a DM to a gateway target at all (outside the repo); only the "not executed via the
  server" half is firmware-certain.
- Whether the firmware wants a freshness binding for sync later (rejected for v1 because the unix-time floor makes it
  unnecessary).
- The per-target `tx_max` default of 15 is a recommendation; the operator can raise it per node.

## 8. Deferred, not forgotten

- Web Push noise for RM1 replies (push contract v8 with mc-chat).
- Chat-bubble link to the admin row.
- Per-node custom allowlists, fleet status sweep.
- Authentication for the whole API in the public-TLS modes (pre-existing exposure, separate work item).
- A `held`/store-and-forward note: RM1 frames are never held or stored by the firmware, so no handling is needed.

## 9. Review log (refuted and downgraded claims, do not re-investigate)

- "Admin password is captured into `stall_events`": downgraded. A normal JSON body is masked by `redact()`
  (`stalls.py:148,376`); only a truncated or non-JSON body leaks. The webapp already redacts `*password*` and
  already prefix-matches its drop list.
- "QRZ never tested the stall side": refuted, `stall_http_tests.py` case 9 does.
- "Two writers on one `node_admin_tests.py`": there were three similarly named modules; not a silent shadow because
  ruff `F811` fails the gate. Renamed anyway.
- "Cached sync reply returns an old hwm": refuted, sync is never cached; the real issue is that an OLD sync reply
  replays.
- "Cached Re-ask reply reuses the msg_id": refuted, `sendReply` goes through `sendMessage` and mints a new one.
- "A hook that raises means the message is not published to SSE": refuted, `_broadcast_handler` is a separate
  subscriber; the stored row is what is lost.
- "Custom `X-McApp-Admin` header protects the routes": refuted, `allow_headers=["*"]`.
- "HTTPS attacker pages are blocked by mixed content": refuted, `https://mcapp.local` exists via Caddy's internal CA and
  public mode is HTTPS.
- "Free-text args allow command injection": refuted, the firmware validates args exactly and rejects `--`, `;`, `{`, `%`.
- "`BaseConfirmModal` has no consumers / double tap reboots twice": refuted, 12 consumers and `useConfirm` settles once.
- "Add the card to the SettingsView spec stubs": refuted, the spec mounts `shallow: true`.
- "Dev shape validator needs an entry": refuted, the map is deliberately partial.
- "`{087}` strict-pin test": padding, the firmware never emits `{NNN}`.
- "Command-to-reply tag reflection": impossible, `RM1|` vs `RM1R|`.
- "Hook position before the filter sees every copy; command handler cannot run an RM1 reply (requires `!`)": verified
  non-issues.
- The pre-existing High (unauthenticated API reachable from the internet in public-TLS mode) is real but pre-dates
  and is independent of this feature.
