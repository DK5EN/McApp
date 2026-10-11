# Internet feed for McApp: groups, broadcast and positions from meshmap

Date: 2026-10-11 · Status: concept, nothing built · Repos: MCProxy (backend), webapp (frontend)

Server side: `mcmap/docs/proposals/data-mcapp-box-feed.md` (the API contract this paper consumes).

## 0. BLUF

The Pi, not the browser, connects to meshmap. When the operator enables **Internet** in Settings,
the backend registers the box with meshmap (TOFU token), opens **one** SSE connection, and receives a
batch every 30 s with:

- **group and broadcast messages** for up to **10 groups** plus `*`, chosen in the GUI and
  independent of the node's 6 firmware group slots (`node_gcb[0..5]`);
- **node positions** within a GUI-set radius around the box, or the whole world.

Messages go through the normal ingest as `src_type: "www"`: they are stored with RF retention, count
in unread badges, trigger Web Push, are blocklisted and classified, and carry a visible "via
internet" marker. They are kept out of every path that would make the Pi **act** on them: commands,
ACK matching, gateway uptime, node admin, signal tables. Positions stay in RAM on the Pi and are
relayed to the webapp over SSE; nothing about them touches the SD card.

The browser never talks to meshmap. The existing guard spec `noInternetFeed.spec.ts` stays and keeps
that true.

## 1. Context

- On 2026-10-10 the browser-direct feed (`wss://mcmap.oevsv.at/ws`) was removed because upstream shut
  it down (`webapp/docs/2026-10-10_1145-internet-feed-removal-plan.md`, v2.1.7). Users asked for it
  back the next day.
- We run meshmap (`mcmap`), so the replacement is ours to design. The server contract is in the
  mcmap proposal. This paper covers what McApp needs.
- Volume (meshmap, measured 2026-10-11): about 18 group/broadcast messages per hour network-wide,
  roughly 150 KB/day stored at most. That is negligible for the SD card, now that writes go through
  the persistent connection with `synchronous=NORMAL`. Positions (~1 600 nodes worldwide) are many
  small updates and are therefore never stored.

## 2. Data flow

```text
 meshmap ──SSE batch/30 s──▶ InternetFeedService (new, backend)
                               │
                ┌──────────────┴───────────────┐
                ▼                              ▼
     messages: router.publish        positions: InternetPositionCache (RAM)
     topic "www_message"                       │
                │                              ▼
   ┌────────────┼──────────────┐     SSE "internet:positions" (+ replay on connect)
   ▼            ▼              ▼
 _storage_   SSE broadcast    Push
 handler     (mesh:message)   dispatcher
   │
   ▼
 store_message (www guards, §4.3)

 NOT subscribed: CommandHandler, wire_monitor
```

## 3. Decisions taken (owner, 2026-10-11)

| Topic                        | Decision                                                                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Transport                    | Pi relays; one SSE connection per box; 30 s batches                                                                              |
| Identity                     | Call+SSID, node id, BLE MAC, firmware, build, hardware; without BLE: call, Pi WiFi MAC, `Pi Zero 2W`                             |
| Credential                   | TOFU box token issued by meshmap                                                                                                 |
| Registry visibility          | Database only, readable via meshmap's authenticated MCP; never on the public map or Admin view                                   |
| MAC storage on meshmap       | Salted hash                                                                                                                      |
| When identity is sent        | Only while the feed is enabled                                                                                                   |
| Positions                    | Radius (GUI-set) or whole world; RAM only                                                                                        |
| Internet messages            | Count as unread, trigger push, same retention as RF, visible origin marker                                                       |
| Push                         | Offered regardless of origin: RF and internet messages pass the same push filter, no extra coupling                              |
| Defaults on first enable     | Node groups (`GCB0..GCB5`) imported, broadcast **on unless the node has "No All Msg" set** (`SN.NOALL`), positions within 300 km |
| Identity security on meshmap | Learning phase: observe and log, never refuse (mcmap proposal §3.3)                                                              |
| Groups                       | Up to 10 (plus `*`), beyond the node's 6 firmware slots                                                                          |
| Own DMs                      | Not consumed from meshmap for now: pulling private messages off the internet would draw unwanted attention                       |
| Settings                     | GUI only, no `config.json` key                                                                                                   |

## 4. Backend (MCProxy)

### 4.1 Settings and state

New migration **36** (bump `LATEST_SCHEMA_VERSION` in the same commit). One table, single row, the
`qrz_state` pattern:

```text
internet_feed_state
  id INTEGER PRIMARY KEY CHECK (id = 1)
  enabled            INTEGER NOT NULL DEFAULT 0
  groups             TEXT NOT NULL DEFAULT '[]'    -- JSON, ≤ 10, "1".."99999" or "#TAG"
  broadcast          INTEGER NOT NULL DEFAULT 1
  pos_mode           TEXT NOT NULL DEFAULT 'radius' -- off | radius | world
  radius_km          INTEGER NOT NULL DEFAULT 300
  box_id             TEXT
  token_enc          TEXT                           -- secret_box, AD "internet_feed.token:<box_id>"
  identity_json      TEXT                           -- last identity sent (to detect changes)
  cursor             TEXT                           -- last message cursor from meshmap
  last_connected_ms  INTEGER
  last_batch_ms      INTEGER
  last_error         TEXT
```

- The token is encrypted with the existing `SecretBox` instance. It never leaves the backend: not in
  any API response, not in logs.
- Routes (new `sse_routes/internet_feed.py`), behind the same LAN-only Host/Origin guard as Node
  Admin, because enabling the feed sends this box's identity to an external server:
  - `GET /api/internet-feed` → settings plus status (state, since, last batch, last error, counts).
  - `GET` also returns `suggested` while the feed has never been configured: `groups` from the node's
    `GCB0..GCB5` (non-zero slots) and `broadcast = !SN.NOALL`, both read from
    `cached_ble_registers`. Without BLE: no groups, broadcast on, because the node's preference is
    unknown. The webapp prefills the form with it on first enable; once saved, the suggestion is
    never applied again, so a later node change does not silently rewrite the feed settings.
  - `PUT /api/internet-feed` → settings. Validation: at most 10 groups, each `is_group()` or
    `is_hashtag()`, deduplicated, `radius_km` 10..2000. A change reconnects the feed with the new
    subscription.
  - `POST /api/internet-feed/forget` → drops `box_id` and token (fresh registration on the next
    connect).
- `/api/status` `features` gains `"internet_feed"` (always present, like `node_admin`).
- Stall redaction: add `token` to `stalls._is_sensitive_key`, and add `/api/internet-feed` to the
  withheld-body prefixes in `stall_middleware.py`.

### 4.2 InternetFeedService

New module `internet_feed.py`, structured like `BLEClientRemote._sse_loop` and the QRZ `run(stop_event)`
loop, started in `_start_background_tasks`, stopped in `_shutdown_services`. It is inert while
`enabled = 0`.

- **HTTP:** one `httpx.AsyncClient`, SSL context built once via
  `await asyncio.to_thread(httpx.create_ssl_context)` (the 800 ms certifi cost on the Pi Zero must not
  run on the loop). Read timeout 60 s (meshmap pings every 15 s). No new dependency.
- **Register / check-in** before every connect: `POST /api/v2/box/register` with the identity, with
  the bearer token if one is stored. On 401, drop the token and register fresh. meshmap never
  refuses an identity during its learning phase (a token-less register of a known identity gets a
  new token), so there is no "claimed" state to handle. Keeping the token across reconnects matters:
  an IP rotation or reboot must reconnect **with** the token, so meshmap's logs show a reconnect and
  not a re-claim.
- **Identity** (built fresh each time, never the cached register wholesale, because `I` contains the
  BLE PIN `BPIN`):
  - BLE connected: `CALL`, `ID`, `FWVER`, `IS1.BDATE`, `HWID` from `cached_ble_registers`; MAC from
    `ble_client.status.device_address`.
  - Otherwise: `my_callsign`, `/sys/class/net/wlan0/address`, hardware from
    `/proc/device-tree/model` (falls back to `Pi Zero 2W`).
  - A change (BLE connects or disconnects, firmware update) triggers a check-in and a reconnect.
- **Connect:** `GET /api/v2/box/feed?groups=…&bcast=…&pos=…&lat=…&lon=…&radiusKm=…&since=<cursor>`.
  `lat/lon` are the box's configured `LAT`/`LONG`. Without a position, `radius` mode is reported as
  "needs a station position" and positions stay off.
- **Backoff:** 5 s doubling to 300 s with ±20 % jitter. `superseded` and `revoked` stop the loop and
  are shown in the status, not retried blindly.
- **Feed version:** an unknown major `feedVersion` in `hello` stops the loop with a clear status.
- **Status** goes to the webapp as SSE `internet:status`
  (`{state: off|connecting|connected|error|revoked, since_ms, last_batch_ms, error}`) on every
  transition, and is replayed in `initial_events`.

### 4.3 Messages: mapping and guards

Mapping from the feed `Message` to the router dict:

| Router field | Source            | Note                                               |
| ------------ | ----------------- | -------------------------------------------------- |
| `src`        | `from`            | plus the `via` path in the usual `CALL,hop,…` form |
| `dst`        | `to`              | already normalised by meshmap                      |
| `msg`        | `text`            | through `text_decode` rules (already str)          |
| `msg_id`     | `msgId`           | 8-digit upper-case hex: dedups against the RF copy |
| `timestamp`  | `timeUnix * 1000` | meshmap arrival time; ms like every DB timestamp   |
| `type`       | `"msg"`           |                                                    |
| `src_type`   | `"www"`           | the origin marker; no schema change needed         |
| `rssi`/`snr` | absent            | never the box's own reception                      |

Published on a **new topic `www_message`**, not `mesh_message`. Subscribed are `_storage_handler`,
`SSEManager._broadcast_handler` and the push dispatcher, in that order (storage first, so the live
SSE payload carries the classifier fields, the same ordering rule as today). **Not** subscribed:
`CommandHandler._message_handler` (otherwise a `!wx` sent from Vienna would make our node answer on
air) and `wire_monitor` (the RF Monitor shows radio traffic only).

Guards inside `store_message` for `src_type == "www"`, each with its own regression test that fails
when the guard is removed:

| Path                                            | Why it must not see a www frame                                                                                 |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Gateway uptime hook (`is_uplink_time_beacon`)   | a www `{CET}` with no `via` counts as our uplink beacon today (`uptime.py`: `src_type != "lora"` and zero hops) |
| Node admin RM1 reply hook                       | must only bind replies our node actually received                                                               |
| Inline `:ack` / `:sto` matching                 | would mark our DMs delivered/held from internet copies                                                          |
| `_handle_ack`, telemetry, `pos` branches        | a www frame is always `type: "msg"`; anything else is rejected                                                  |
| `_ingest_signal` / mHeard / `station_positions` | already gated on `src_type`; pinned by a test anyway                                                            |

The guards form **one predicate** checked at the top of each branch, not five ad-hoc string tests.
meshmap already filters DMs, `:ack` lines and `{CET}` on its side (contract §4.3). The guards are
defence in depth and do not rely on that.

Applies unchanged: blocklist (`blocklist_decision` in storage and broadcast), classifier, unread
(`get_conversation_summary` has no `src_type` filter), push (push dedup keys on sender + msg core,
so RF + www produce one notification), retention (`prune_messages` is by `type`).

### 4.4 RF copy and internet copy of the same message

- The usual order is RF first, internet later (gateway upload plus up to 30 s batch delay). Then the
  www copy is a duplicate by `(sender, msg_id)` within `DEDUP_WINDOW_MS` and is dropped. Nothing
  changes.
- If the www copy wins (the box heard the message late over many hops), the later RF copy already
  fills `rssi`/`snr` and runs `_ingest_signal` (`ingest.py:2244`). **New:** `_enrich_duplicate_row`
  also rewrites `src_type` from `www` to the RF value in that case, so the bubble loses its "via
  internet" marker once the box heard the message itself. RF never downgrades to www.
- Own messages: our own group message comes back from the internet with our callsign and msg_id and
  is a duplicate of the stored TX row. Push already excludes own src.

### 4.5 Positions in RAM

`InternetPositionCache`: `dict[call, Position]`, bounded at 5 000 entries (oldest `lastSeenUnix`
evicted), entries older than 3 h dropped on each batch (meshmap's own node TTL). Roughly 1 MB in the
`world` case.

- A batch updates the cache, then broadcasts `internet:positions {upserts[], removed[]}` once.
- A `positions {reset: true}` from meshmap (each connect) replaces the cache and is forwarded as
  `internet:positions {reset: true, upserts[...]}`.
- `initial_events` replays the full cache as `reset: true` to each new webapp client.
- Blocklist: positions of blocked callsigns are dropped before they enter the cache.
- Disabling the feed clears the cache and sends `reset: true` with no rows.
- Fan-out stays inside the 256-event SSE client queue: one event per batch, never one per station.
- `station_positions` is never written from this path. `storage/query.py:614`'s dead `'www'` branch
  stays dead.

### 4.6 Memory and load on the Pi Zero 2W

One long-lived httpx stream, one cache of at most ~1 MB, one batch every 30 s. No protobuf, no new
library. The stall recorder's `rss_kb` trend is the instrument for checking it after deployment.

## 5. Frontend (webapp)

### 5.1 Settings > Internet

A new tab, gated by `features.includes('internet_feed')` exactly like Remote nodes (`SettingsView.vue`
`sections`, `v-if` on the panel). One card, `InternetFeedCard.vue`:

- **Enable toggle**, with a one-paragraph notice of what is sent to meshmap.oevsv.at: callsign, node
  id, MAC (stored there only as a salted hash), firmware, hardware, McApp version.
- **Status line** from `internet:status`: connected since, last batch, last error. Revoked gets
  its own text and the "forget registration" action.
- **Groups:** chip editor, up to 10 entries, each `1..99999` or `#TAG`; the counter shows `n/10`.
  "Import node groups" fills from `bleStore` `GCB0..GCB5` while BLE is connected. The point of the
  feature is groups **beyond** those six, so the import is a convenience, not a constraint.
- **Broadcast `*`** toggle. Prefilled off when the node has "No All Msg" set, so the internet feed
  does not bring back what the operator switched off on the node. When the two differ later, a short
  hint next to the toggle says so; the toggle stays the operator's choice.
- **Positions:** Off / Radius (km, default 300, centred on the station position) / World.
- Errors through `useProxyAPI` and `ProxyAPIError.detail`, as `NodeAdminSecretsCard.vue` does.

New user-settings key names. `internetEnabled` is stripped on load and must not be reused. The
settings live on the backend anyway, so the webapp keeps no copy beyond the form state.

### 5.2 Status bar

Bring back the `Globe` chip (`TheStatus.vue`, before the BLE button; mobile sheet row). It now shows
the **backend's** feed state and opens Settings > Internet. It does not toggle anything, and it
appears only when the feature flag is present.

### 5.3 Messages

- `ChatBubble.vue`: a `Globe` icon in the meta area for `src_type === "www"`, and an "Origin:
  Internet (meshmap)" row in the details popover.
- **History fallback:** `srcTypeFallback: 'www'` (`stores/messages.ts:950`) would mark every
  history row without `src_type` as internet. It becomes a neutral value, and the marker requires an
  explicit `"www"` from the backend.
- `SIDEBAR_SRC_TYPES` already contains `'www'`, so unread and sidebar need no change.
- `isDevicePathTimeBeacon` (`stores/messages.ts`) excludes `src_type === "www"`. Otherwise a www
  `{CET}` would re-arm the device-time watchdog, the 2026-07-27 bug again. meshmap filters `{CET}` but
  the client should not depend on that.
- The local-echo set (`LOCAL_ECHO_SRC_TYPES`, `messageProcessor.ts:40`) and the monitor fallback tap
  must ignore `www`.
- Replying in a group that is only received via the internet works as usual: the reply goes out
  through the node over RF. McApp has no internet uplink for sending, and the paper does not add one.

### 5.4 Map

- New SSE event `internet:positions` (add to `EventMap`, the doc table and `SSE_EVENT_KEYS`). The
  positions store applies upserts with `src_type`/`fixSource` `'www'`; `reset` replaces all www rows.
- The existing merge rules already do the right thing: `RF_FIX_SOURCES` excludes `'www'`, so an RF
  fix wins, `hasRfSourcedFix` keeps www stations out of link lines, and `classifyStationReach` reports
  `unknown`.
- **Boot purge:** the unconditional `purgeInternetPositions()` at boot stays, because the server
  replays the current set on connect. www positions are **not** written to IndexedDB, so a stale
  internet position never outlives its feed.
- Marker class `symbol-icon--www` (e.g. dashed outline, reduced saturation) in `buildMarkerIcon` and
  the in-place toggle, plus a MapLegend entry "Internet (meshmap)". The popup shows "Source:
  Internet".

### 5.5 Docs

The webapp `CLAUDE.md` line "foreground push sound source-gated to `local`" is already stale and is
corrected with this work.

## 6. Tests

| Area          | Test                                                                                                                |
| ------------- | ------------------------------------------------------------------------------------------------------------------- |
| Contract      | `box_feed_vectors.json` copied from mcmap, sha256-pinned; mapping and filter vectors replayed                       |
| www guards    | one case per guarded path in §4.3, each mutation-checked (guard removed → test fails)                               |
| Router wiring | `www_message` subscribers are exactly storage, broadcast, push, in that order; commands and wire monitor absent     |
| Dedup         | RF first then www → one row, RF `src_type`; www first then RF → one row, upgraded `src_type`, signal ingested       |
| Positions     | never reach `store_message`; cache bound, TTL, reset, blocklist                                                     |
| Service       | fake SSE server: register, 401 re-register, token kept across reconnects, superseded, revoked, backoff, feedVersion |
| Redaction     | token never in stall rows or logs                                                                                   |
| Webapp        | settings card, status chip, `internet:positions` store handling, history fallback, watchdog exclusion               |

All new suites are registered in `scripts/run_startup_tests.py`. Webapp specs follow the existing
conventions.

## 7. Delivery order

1. **mcmap**: register + feed + Apache rule live on meshmap.oevsv.at (see the mcmap proposal §10).
2. **Backend wave A**: migration 36, settings routes, `InternetFeedService` with messages only, www
   guards, dedup upgrade, tests.
3. **Webapp wave A**: Settings > Internet, status chip, bubble marker, fallback and watchdog fixes.
4. **Dev release, soak** on mcapp.local: unread, push and dedup against real RF traffic.
5. **Wave B**: positions (cache, `internet:positions`, map styling).
6. Production release.

Messages first: they are what the users miss most, and they carry the risky integration (ingest
guards). Positions are self-contained and ride on the proven connection.

## 8. Open questions

Decided 2026-10-11: defaults (node groups, broadcast following `SN.NOALL`, 300 km), push regardless
of origin with no extra push shortcut for feed groups, no DMs from meshmap for now. See §3.

Still open:

1. **Radius after soak:** 300 km is a guess for "my region"; revisit once real map density is seen.

## 9. Campaign state

Resume point for the coding sessions (`/orchestrate-waves` on this doc).

| Wave | Scope                                                                                  | State                                                                  |
| ---- | -------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| 1    | Migration 36 + `InternetFeedStateMixin`; www ingest guards + www→RF `src_type` upgrade | Done 2026-10-11 (`81c028a`)                                            |
| 2    | `internet_feed.py` service + `sse_routes/internet_feed.py` + main/sse wiring           | Done 2026-10-11 (this commit), advisor APPROVED after one rework round |
| 3    | Webapp: Settings > Internet, Globe chip, bubble marker, history fallback, watchdog     | **Next**, fresh session from this section                              |
| B    | Positions (RAM cache, `internet:positions`, map styling)                               | After the dev soak                                                     |

Wave 1 as built (deviations from §4.1 and §4.3):

- `internet_feed_state` has an extra `configured` column (0 until the first settings save; drives
  `suggested`), and `pos_mode` defaults to `off` until wave B.
- One predicate, `storage/www.py` `is_www_frame`. A www frame of any `type` other than `msg` is
  dropped unstored, which covers the ack, telemetry and pos branches in one place. rssi/snr on a
  www frame are blanked. `www_ingest_tests.py` W1-W9 pin every guard (each mutation-checked; the
  `_ingest_signal` skip is redundant by construction and pinned by outcome only).
- The upgrade runs at the top of `_enrich_duplicate_row`, before its all-None early return, so a
  signal-free UDP copy also clears the www marker (W8b); the restart path through the DB backstop
  is W8c.

Carry-forward for wave 2, from the implemented mcmap contract (mcmap `4579dbc`):

- The cursor is the SSE `id:` line (`timeUnix:MSGID`), resent as `since=` or `Last-Event-ID`.
- `batch` carries `positions[]`/`removed[]` too; wave A connects with `pos=off`.
- mcmap re-sends messages across ticks (15-minute lookback); storage dedups them, so the mapper
  must emit `msg_id` as upper-case hex and `src` with its SSID exactly as received.
- `via` equals `from` for direct copies; the mapper sets `via` only when it differs.
- Register body needs `idKind` (`ble`|`pi`) and `mac`; 429/503 carry `retryAfterS`, which the
  backoff honours. Base URL: constant `https://meshmap.oevsv.at`, `MCAPP_INTERNET_FEED_URL` env
  override for testing only.
- After wave 3: push `development` in MCProxy and webapp (owner decision), no release.

meshmap box feed verified live 2026-10-11 (mcmap v0.1.67): a 75 s feed held through Apache with
the server's `MCMAP-1` probe token delivered `hello`, a 2 h backfill batch with an `id:` cursor and
five `: ping` lines 15 s apart (no buffering). Real traffic carries `"via": "NET"`; the owner decided
it is dropped (the mapper appends `via` only when it differs from `from` and is callsign-shaped).

Wave 2 as built (deviations from §4.2-§4.4):

- **Push skips www messages older than 15 min** (`storage/www.py` `WWW_PUSH_MAX_AGE_MS`). §3 says
  push regardless of origin; that still holds for live traffic, but the 2 h first-connect backfill
  and the reconnect lookback after a restart (in-memory push dedup empty) would otherwise notify
  hours-old messages. They are stored and count as unread.
- **Replays are not re-broadcast or re-pushed.** `store_message` flags a www duplicate in place
  (`_www_duplicate`; storage is the first `www_message` subscriber); `SSEManager._www_broadcast_handler`
  and the push subscriber skip flagged frames, and the broadcast handler keeps a 1 h
  `(sender, msg_id)` seen-set for replays storage never reached (blocked / filtered).
- **Token keyring, not one token.** `token_enc` holds one SecretBox JSON `{call|IDKIND|MAC: {box_id,
token}}` (AD `internet_feed.tokens`, LRU 4), so pi↔ble switching (startup before BLE hydration,
  every BLE disconnect) reuses each identity's own token instead of a reclaim at meshmap. Before the
  first register the service waits up to 30 s for the BLE identity while BLE is configured.
  An identity change is the KEY changing (`call`, `idKind`, `mac`); firmware/build refresh on the
  next check-in and never re-register.
- **Each publish is shielded** and drained before a reconnect: a cancel between `store_message`'s
  in-memory claim and its INSERT would otherwise lose the message for good (its replay reads as a
  duplicate).
- Backoff ends early only on a settings change / forget / stop; BLE identity hints never shorten a
  Retry-After. `superseded` / `revoked` halt until the operator saves settings or forgets.
- Settings validation (groups `1..99999` or `#TAG`, ≤ 10 after case-insensitive dedup, radius,
  wholesale PUT) lives in `sse_routes/internet_feed.py`; `suggested` is computed there from the BLE
  register cache (`I.GCB0..5`, `SN.NOALL`).
- Suites: `internet_feed_tests.py` (service, fake server, 69 cases), `internet_feed_routes_tests.py`
  (guard, validation, suggested, body withheld), `internet_feed_wiring_tests.py` I1-I5 (subscriber
  order, replay/backfill gating), `www_ingest_tests.py` W10/W11.
- Not done here, needs the box: a live connect from mcapp.local (needs a dev release and the feed
  enabled, which happens through the wave 3 GUI or a `PUT /api/internet-feed`).
