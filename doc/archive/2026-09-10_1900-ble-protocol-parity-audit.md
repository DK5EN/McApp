# BLE protocol parity audit: MCProxy vs. firmware vs. official app

Date: 2026-09-10. Firmware: `MeshCom-Firmware-DEV-Main` @ `75df1cdb` (fork-main, v4.35t.09.10).
App: `Meshcom-MobileApp` (Ionic/Capacitor). MCProxy: `de7a6ac` on `development`.

## BLUF

MCProxy parses **every** frame kind and every `D{` register the firmware emits over BLE, and its
ACK handling is ahead of the official app. The gaps are elsewhere: five things the firmware
puts on the wire that we throw away or mis-file on receive, and three send-side behaviours where
the firmware silently discards or mangles what we write. None of them crash the proxy. Three
affect stored data or on-air behaviour today.

Ranked by impact:

| ID    | Direction | Finding                                                                                                             | Severity |
| ----- | --------- | ------------------------------------------------------------------------------------------------------------------- | -------- |
| RX-01 | node → us | The node's 4-byte reception timestamp is discarded; backlog after a BLE outage is stamped at reconnect time         | P1       |
| TX-03 | us → node | Two `0xA0` writes in one firmware loop pass: only the last survives                                                 | P1       |
| TX-01 | us → node | Any `--` string from the webapp reaches the node verbatim, including `--cleanflash` / `--btcode`                    | accepted |
| RX-02 | node → us | APRS `T#` telemetry frames are stored as chat in a phantom group 100001 (latent, none observed)                     | P1/P2    |
| RX-03 | node → us | Firmware command replies (`response>*:`) are filtered from storage but still pushed and streamed                    | P2       |
| RX-04 | node → us | The flag nibble (`app_offline`, `msg_server`, `msg_track`) is stored opaque; catch-up frames push like live traffic | P2       |
| TX-04 | us → node | A failed BLE send is invisible to the client; the UDP path reports it                                               | P2       |
| TX-10 | both      | Back-pressure notices and per-message nacks from the node are ignored                                               | P2       |
| TX-02 | us → node | Byte caps exist on both send paths; residual is the chunker's character-wise fallback on multi-byte text            | P3       |
| RX-07 | doc       | `MH.SRC` / `GW` / `PP` were reverted upstream; `CLAUDE.md` and the parser docstrings are stale                      | doc      |

Verified clean (no defect): every binary write layout we build, the `0x50` "off-by-one", command
prefix shadowing for our 15 literals, FCS and `msg_id` endianness, hello frames, `--utcoff`
before `0x20`, `--ackinfo on` timing, the 244-byte salvage, and the one-notification-one-frame
assumption. See section 5.

Method: four read-only inventories (firmware TX, firmware RX, app, MCProxy), then two
comparison passes with every finding re-verified against source in all three repos, then the
orchestrator re-checked each P1 by hand and against the production database on `mcapp.local`.

## 1. Receive side: node → MCProxy

### RX-01 (P1) — the node's reception timestamp is discarded

`addBLEOutBuffer()` appends 4 bytes of `getUnixClock()`, big-endian, to every non-`D` frame
(`loop_functions.cpp:601-609`). MCProxy unpacks the trailer as the last `I` of
`_FOOTER_FORMAT = "<BBBHBBBBI"` (little-endian, so byte-swapped) and then throws it away;
`transform_common_fields` stamps `now_ms()` (`ble_protocol.py:151`, `:239-241`, `:600`).

Why it matters: the text ring is filled whether or not a phone is connected. DMs are queued with
no `isPhoneReady` gate (`lora_functions.cpp:1098`), group traffic only sets the `app_offline`
flag and still queues (`:1170-1180`). Only positions are gated (`:1299`). The ring holds 20
entries and drains only while connected (`phone_commands.cpp:62`). After a BLE outage the whole
backlog arrives in a burst and every row gets the reconnect second. That breaks conversation
ordering, the unread cursor arithmetic (a cursor is a timestamp), and the dedup window against
the UDP copy that arrived on time.

The app reads it: `getUint32(len - 5, false) * 1000`, accepted only in a sane range
(`MessageHandler.ts:206-219`, `:566-570`).

Fix: read `byte_msg[-5:-1]` as `>I`, range-check like the app, use it as `timestamp` with
`now_ms()` as fallback, keep arrival time in a separate field.

### RX-02 (P1 in code, unobserved on air) — telemetry frames mis-filed as chat

Telemetry is a `:` text frame to group `100001` with payload `<call>:T#seq,v1..v5,bits`
(`loop_functions.cpp:5075`, `:5084-5085`, `:5145`). The BLE dispatcher only looks for `T#` on
position frames (`ble_protocol.py:948-951`), and a position frame's `message` always starts
with `!`, so `transform_tele` and `parse_aprs_telemetry` are unreachable on BLE. A telemetry
frame goes through `transform_msg` and is stored as a chat message with `dst="100001"`.

Caveat from the orchestrator: the production database on `mcapp.local` holds **zero** rows with
`dst='100001'` over the last 7 days, and the firmware's own display path also suppresses that
group (`loop_functions.cpp:2384`). Whether received telemetry actually reaches the BLE ring
depends on a branch in `lora_functions.cpp` that was not traced end to end. Treat this as a
verified dead-code defect with an unconfirmed trigger. The app does not parse it either.

Fix: detect `:T#` after the padded originator callsign on `PAYLOAD_TYPE_MSG`, hand it to
`transform_tele`, change `parse_aprs_telemetry` to `re.search`, and add a captured vector to
`ble_protocol_tests.py` (there is no `T#` vector today).

### RX-03 (P2) — command replies leak into Web Push and SSE

Every `--xxx` reply is a text frame with `src = "response"`, `dst = "*"`
(`loop_functions.cpp:683-706`), including `--ackinfo on`, which we send on every connect
(`ble_adapter.py:1670`), and `--wrong command <x>` for any typo the webapp forwards. Storage
filters `src == "response"` (`storage/ingest.py:1823`). Push does not go through storage: it
subscribes to the raw router topics (`sse_routes/push.py:150`) and `is_eligible`
(`push_delivery.py:237-267`) has no `response` rule. Every reconnect therefore pushes
`--ackinfo on` to broadcast subscribers.

Fix: add the `response` exclusion to `_is_node_local_noise` and pin it in the push contract corpus
(mc-chat side first, then subtree pull).

### RX-04 (P2) — the flag nibble is stored opaque

Byte 5 of the APRS frame is a bitfield: `0x0F` max_hop, `0x10` mesh, `0x20` app_offline,
`0x40` track, `0x80` server (`aprs_functions.cpp:1094-1108`). `0x20` is set on every frame
buffered while no phone was attached (`lora_functions.cpp:1172`), on every command reply and on
every back-pressure notice, and its documented meaning is "do not announce". MCProxy stores
`mesh_info = raw >> 4` verbatim (`ble_protocol.py:291`) and nothing tests any bit. The app
suppresses the OS notification on `0x20` (`MessageHandler.ts:296-314`).

Combined with RX-01: a 20-minute BLE outage yields a burst of Web Push notifications for
20-minute-old traffic, all timestamped now.

Fix: emit `app_offline`, `msg_server`, `msg_track`, `mesh` as booleans next to `mesh_info` and
gate push eligibility on `app_offline`.

### RX-05 (P2) — position beacons: neighbour count `/N<n>` dropped

`PositionToAPRS()` appends `/N<count>` with no `=` (`loop_functions.cpp:4415-4420`). Our extras
regex requires `=` (`ble_protocol.py:527`), so the token is discarded. The app parses and
persists it (`MessageHandler.ts:797-802`). `MH.NCNT` is not a substitute: different source,
different population.

### RX-06 (P2, nRF52 only) — frames of 252-254 bytes lose their footer tail

`encodeAPRS()` can produce 254 bytes; `addBLEOutBuffer()` clamps to 251 to make room for the
timestamp (`loop_functions.cpp:585-587`). The last 1-3 footer bytes go missing and our purely
positional footer unpack (`ble_protocol.py:239-241`) then reads `hw_id`, `lora_mod`, `fw` out of
message text and stores them. On ESP32 the same frame hits a `uint8_t` wrap in
`phone_commands.cpp:125-127` and arrives as a 0-1 byte notify we drop with a WARNING.

Fix: validate the two structural footer bytes (`0x00` at offset 0, `0x7E` at offset 8) before
trusting the window; on mismatch emit `None` for the hardware fields.

### RX-07 (doc) — `MH.SRC` / `GW` / `PP` were reverted upstream

The live MH builder emits 13 keys: `TYP CALL DATE TIME PLT HW MOD RSSI SNR DIST PL MESH NCNT`
(`mheard_functions.cpp:400-414`). `SRC`/`GW` came in `c04ed7b0` and were reverted in
`dc7d56d7`; `PP` came in `fff4010e` and was reverted in `17d1796e`. `PLT` stays.

MCProxy is not broken by this: every access is `.get()`, every coercer is `None`-safe, the
`"heard"` upsert is guarded. But `hey_path.py`, the `mh_origin` upsert and every BLE-path write
to `station_positions.gw` are inert against this firmware, and `CLAUDE.md` §"MHeard Register"
plus the docstrings at `ble_protocol.py:822-845` describe the fields as live. Whether the node
on `mcapp.local` runs a build that predates the revert was not verifiable from the logs (no
raw MH frames at INFO). Re-mark the section as "reverted upstream, parser retained, inert".

### RX-08 to RX-14 (P3)

- **RX-08** The 16 `--` config acks the app consumes (`--txpower`, `--utcoff`, `--gateway`, ...)
  are dropped by us. Acceptable: we re-query the registers instead (`main.py:1108-1117`).
- **RX-09** `ble_service`'s notification deque (`maxlen=1000`, `main.py:185-187`) drops the
  oldest entry with no log or counter, and every SSE consumer `popleft`s the same deque.
- **RX-10** `decode_binary_message` has no length guard before the 7-byte header unpack
  (`ble_protocol.py:286`); the `struct.error` is caught one level up. Reachable via the ESP32
  wrap in RX-06.
- **RX-11** `remaining_msg.find(b"\x00") == -1` at `ble_protocol.py:235` is unreachable on
  well-formed frames (the `0x7E` terminator stops the rstrip) but unguarded.
- **RX-12** `/D=` is an 8-char bit string stored as a float in `extras`; `/U=`, `/I=`, `/V=`
  land untyped.
- **RX-13** `/A=` has a 5-digit metre form in the firmware (`loop_functions.cpp:4315-4318`) that
  we would drop; all three call sites currently pass `bFuss = true`, so it is unreachable today.
- **RX-14** `ble_protocol_tests.py:1218` pins an `I.FWDATE` passthrough; the firmware no longer
  emits that key.

## 2. Send side: MCProxy → node

### TX-01 (P1, ACCEPTED 2026-09-10) — unbounded `--` pass-through reaches the node

**Decision:** accepted as-is by the operator on 2026-09-10. The API is LAN-only and
unauthenticated by design; the pass-through stays unbounded. No allow-list will be added.
Recorded so the finding is not re-raised.

`main.py:691-699` forwards any webapp command starting `--` as an `0xA0` frame, no allow-list.
`POST /api/send` has no auth (`sse_routes/stream.py:99-145`), which is by design for the whole
API on a LAN box, so the exposure is the pass-through itself. `commandCheck` is a
case-insensitive prefix match (`command_functions.cpp:157-168`), so `--rebootXYZ` matches too.
Reachable: `--cleanflash` (factory wipe + reboot, `:631-652`), `--dfu`, `--btcode <pin>`
(`:3397`, flash write; from the next hello on the node tears down every connection whose hash is
wrong, and MCProxy keeps sending the open hello), `--setssid none --setpwd none`.

Fix: an explicit allow-list of the operator-settings verbs the webapp exposes in front of
`_handle_device_a0_command`; log and refuse everything else.

### TX-02 (P3, downgraded after review) — outbound byte cap

The original finding claimed no 160-byte cap exists on the outbound path. That is wrong. The
firmware side is as stated: `sendMessage` returns `BP_SEND_INVALID` for `strMsg.length() > 160`
bytes including the `{dst}` prefix (`loop_functions.cpp:3917-3921`) and never routes that
result to a notice. But both MCProxy send paths already enforce it:

- **Webapp / API path.** `SendMessageRequest` (`schemas.py:86-87`, `:150-185`) measures
  `2 + utf8(dst) + utf8(msg)` against 160 for `type == "BLE"` and `3 + dst + msg` against 159
  plus a 150-byte `msg` cap for the UDP-routed types, and rejects with a 422 rather than
  truncating. The webapp's chat input mirrors the same contract client-side
  (`ChatInput.vue:19-26`, `min(150, 156 - utf8Bytes(dst))`, Blob-based byte count).
- **Command replies** (weather, group responses, data commands). `send_response` runs every
  reply through `_chunk_response` (`commands/response.py:197-231`), which splits at
  `MAX_RESPONSE_LENGTH = 140` UTF-8 bytes into at most 3 chunks, leaving room for the
  `(n/m) ` header and `{dst}`.

Residual, verified by running the chunker: the **fallback branch** of `_chunk_response`
(`response.py:227`) slices by characters, not bytes. It fires only when the reply exceeds 140
bytes and has neither exactly one `", "` nor any `" | "` separator. Measured: 150 `ö` yields
a 280-byte first chunk; a realistic 161-character weather sentence yields a 149-byte chunk,
which with the 6-byte header and an 11-byte `{dst}` is 166 bytes, over the firmware limit. The
firmware then drops that chunk silently and the operator sees `(2/2)` without `(1/2)`.

Fix: make the fallback split on encoded length (walk characters, cut when the next one would
exceed the budget) and subtract the header and `{dst}` overhead from `max_bytes`. One regression
test with a multi-byte reply. Everything else in the original TX-02 is closed.

### TX-03 (P1) — two `0xA0` writes in one loop pass: only the last survives

The firmware drains its whole BLE RX queue in a `while` loop into the single global
`textbuff_phone` and acts on it once afterwards (`esp32_main.cpp:3058-3084`,
`nrf52_main.cpp:1642-1670`). On nRF52 the FIFO read can additionally concatenate two writes into
one item (`nrf52_ble.cpp:264-283`). `BLEAdapter.write` serialises behind a lock but adds no
inter-frame gap (`ble_adapter.py:1313-1344`), and each webapp message is an independent POST.
Two quick chat messages, or a chat message overlapping a `--` command, lose the first one with
no ACK, no echo and no log line on either side. The app is not exposed: its senders are a human
and slow rotators.

Fix: a single outbound queue with a minimum gap of about 200 ms between `0xA0` frames.

### TX-04 (P2) — a failed BLE send is invisible

`_send_via_ble` discards the boolean from `send_message` and never calls
`_publish_send_failed` (`main.py:2094-2099`). The UDP sibling does both (`:2023-2062`).
`send_message` returns `False` on any exception and on disconnect
(`ble_client_remote.py:585-597`). The webapp bubble stays on "Sending..." forever. This also
hides TX-03 and the TX-02 residual.

### TX-05 to TX-11 (P2)

- **TX-05** Time sync once per connect (`main.py:1012-1042`); the hourly DST loop resends only on
  an offset change. The app resends `0x20` every 20 s. A BLE-only node drifts on its RC
  oscillator and every `MH` `node_timestamp` drifts with it. Resend on the 300 s keepalive tick.
- **TX-06** `ble_pin` doubles as the BlueZ pairing passkey (`ble_service/main.py:1288`,
  `ble_adapter.py:721-731`). The nRF52 link-layer PIN is hardcoded `000000`
  (`configuration_global.h:370`, `nrf52_ble.cpp:98`); ESP32 enforces none. `--btcode` is only
  the app-layer hello hash. A node with a `--btcode` set fails nRF52 pairing. Split the two.
- **TX-07** `0x50` callsign, `0x55` WiFi and `0x95` symbol are RAM-only in the firmware (no
  `save_settings()`, `phone_commands.cpp:434-473`, `:596-646`, `:544-561`), but
  `/api/ble/config/{callsign,wifi,aprs}` report success and never chain `--save`. The app uses
  the flash-writing `--setcall` / `--setssid` / `--symid` equivalents. MCProxy is the only sender
  of `0x50/0x55/0x70/0x80/0x90/0x95/0xF0` against this firmware.
- **TX-08** An empty `group` is framed as `{}`; the firmware parses that as a DM to an empty
  callsign (`loop_functions.cpp:3925-3946`): ack suffix appended, retransmission armed, four
  keyings over two minutes. The endpoint calls it "broadcast". Normalise to `*` or reject.
- **TX-09** A `#TAG` destination goes out as `{#OE-SOTA}text`. The firmware has no hashtag
  branch: it becomes a DM with a `{NNN` suffix and armed retransmission. Our `dst_kind` is used
  only on the receive side. Decide the send semantics explicitly.
- **TX-10** The node's `QRS`/`QRT`/`QTA` notices and `QRT NOT SENT - ` / `QTA NOT SENT - ` nacks
  (`backpressure.h:102-103`, `:140-156`) are sourced from the node's own callsign on purpose so
  they cannot be filtered as `response`. Neither we nor the app match them. They land as chat
  from ourselves; no throttle, no send-failure correlation.
- **TX-11** `--btcode` through the pass-through is not paired with `PATCH /api/ble/pin`
  (`ble_client_remote.py:649-661`). The app writes both in one handler (`Connect.tsx:491`).

### TX-12 to TX-18 (P3)

- **TX-12** `query_extended_registers` claims `--io`/`--tel` are not in the node's connect burst;
  both are (`esp32_main.cpp:309-310`, `nrf52_main.cpp:275-276`). The 10-command mcapp sweep is
  the burst verbatim. Harmless reconciler, wrong docstring.
- **TX-13** `_BLE_MTU_LIMIT = 247` is enforced only on `0x50`/`0x55`; the `0xA0` builders cap at 255. nRF52 ATT MTU is 250 (`nrf52_ble.cpp:91`). Reachable only by a long `--` command (text is already capped by the schema, see TX-02).
- **TX-14** `--utcoff` is formatted `+.1f`; the firmware takes a float. Quarter-hour zones lose
  3 minutes.
- **TX-15** `/api/ble/config/position` defaults `save=False`, which the firmware answers with an
  immediate position and weather beacon on RF (`phone_commands.cpp:515-542`).
- **TX-16** `0x55` with an empty password is a firmware no-op (`phone_commands.cpp:620`) that we
  report as success.
- **TX-17** Commands the firmware accepts that we never send: `--mheard`/`--mh` (replay the whole
  register as `MH` records), `--sendhey`, `--sendtele`, `--sendpos`, `--sendtrack` (the app sends
  the last two), `--conffin`, `--ackinfo off`, `--via`, `--maxhop`, `--shortpath`.
- **TX-18** Every outbound message costs a flash write on the node (`loop_functions.cpp:4058-4066`).
  `topic_beacon` allows a 1-minute floor; three such beacons are about 1.6 M writes a year.
  Raise the floor.

## 3. Parity table: node → phone

| Frame / TYP                         | Firmware emits | App parses                     | MCProxy parses                      | Note                       |
| ----------------------------------- | -------------- | ------------------------------ | ----------------------------------- | -------------------------- |
| Text `40 3A`                        | Y              | Y                              | Y                                   | RX-01, RX-04               |
| Telemetry `:T#` to `100001`         | Y              | N                              | N (stored as chat)                  | RX-02                      |
| Command reply `response>*:`         | Y              | partial (16 acks)              | partial (storage drops, push leaks) | RX-03, RX-08               |
| BP notice `<own_call>>dst:`         | Y              | N (as chat)                    | N (as chat)                         | TX-10                      |
| Position `40 21`                    | Y              | Y                              | partial                             | RX-05, RX-06, RX-12, RX-13 |
| ACK `40 41` + appendix              | Y              | byte 6 only, no appendix       | Y, full                             | ahead of the app           |
| `I SE S1 SW S2 SN W G SA`           | Y              | partial                        | Y (passthrough)                     | `FWDATE` gone (RX-14)      |
| `IO TM`                             | Y              | N                              | Y                                   |                            |
| `AN`                                | ESP32 only     | N                              | Y                                   | correctly optional         |
| `MH` (13 keys)                      | Y              | partial (ignores `PLT`, `MOD`) | Y                                   | `SRC/GW/PP` inert (RX-07)  |
| `CONFFIN`                           | Y              | Y                              | Y                                   |                            |
| nRF52 `0xF0A1` settings struct      | nRF52 only     | N                              | N                                   | same data as registers     |
| 4-byte BE time trailer              | Y              | partial                        | N                                   | RX-01                      |
| `0x91` legacy MHeard, `0x80` config | no producer    | N                              | N                                   | dead on all sides          |

Unknown `D{` TYPs we drop: none. Unhandled binary prefixes: none. Unknown prefixes are logged at
WARNING and counted (`ble_client_remote.py:1088`).

## 4. Parity table: phone → node

| Write / command                                  | Firmware accepts    | App sends            | MCProxy sends                        | Note                        |
| ------------------------------------------------ | ------------------- | -------------------- | ------------------------------------ | --------------------------- |
| Hello open / hello PIN                           | Y (`>= 35`)         | Y (len 35)           | Y (len 36)                           | both valid                  |
| `0x20` time sync                                 | Y                   | every 20 s           | once per connect                     | TX-05                       |
| `0x50` `0x55` `0x70` `0x80` `0x90` `0x95` `0xF0` | Y                   | N (uses `--set*`)    | Y (config endpoints)                 | TX-07, TX-15                |
| `0xA0` text                                      | Y                   | Y                    | Y                                    | TX-03/08/09, TX-02 residual |
| `0xA0` `--` command                              | Y                   | 57 verbs             | 13 literals + unbounded pass-through | TX-01                       |
| Connect burst (`--info` ... `--tel`)             | node runs it itself | relies on it         | duplicates it at hello+12 s          | TX-12                       |
| `--ackinfo on`                                   | Y, per session      | N                    | Y, hello+1 s                         | correct                     |
| `--utcoff` then `0x20`                           | Y                   | never paired         | paired, correct order                | TX-14                       |
| `--pos` keepalive                                | Y, no flash write   | 5-10 s while visible | every 300 s                          | safe                        |
| `--sendpos` `--sendtrack` `--mheard`             | Y                   | first two            | N                                    | TX-17                       |
| BP notices / nacks (node → phone)                | emitted             | ignored              | ignored                              | TX-10                       |

## 5. Verified clean

- Every binary write layout (`_frame()` length byte, `0x50` inner length, `0x55` two-length
  layout, `0x70/0x80/0x90` save flag at `[6]`) matches `phone_commands.cpp` byte for byte.
- The `0x50` VLA loop writes index `len`, the last valid index. In bounds. The `len == 0` hazard
  is unreachable from our builder.
- No prefix shadowing for any of our 15 command literals across all 306 `commandCheck` sites.
- FCS: firmware writes big-endian, `calc_fcs` swaps, we unpack `<H`. Compensates exactly.
  `msg_id` is little-endian on both ends. The app reads it big-endian, an app bug.
- `--ackinfo on` at hello+1 s cannot be undone: `bAckInfo` is cleared before our hello.
- The 244-byte clamp: the firmware's `bleJsonFrameFailSoft` drops whole members from the end;
  our salvage is a superset. `CALL` is the third `I` member and always survives. The two `MH`
  builders still use the plain framer, and there the salvage is load-bearing.
- One notification = one frame, one trailing pad byte: consistent with `_FOOTER_LEN = 14`.
  Residual: whether the nRF52 BLEUart library chunks a >MTU write is not decidable from the
  firmware repo.
- `/A=` six-digit feet form, `0.0/0.0` position rejection, `split_path` originator order.

## 6. Leads that did not survive

Dropped after verification, recorded so they are not re-found: the original TX-02 "no byte
cap" claim (the schema and the chunker both enforce one; see TX-02), the inventory claim that the
originator is the last path element (relays append; first element is correct), the
`find(b"\x00") == -1` last-byte truncation (unreachable), the `0x50` off-by-one, the hello
length-byte 35 vs 36 mismatch, `transform_mh` breaking on absent `SRC/GW/PP`, the FCS and
`msg_id` endianness "mismatches", and three webapp `--set*` literals that exist only as test
fixtures.

## 7. Suggested order of work

1. TX-03 outbound queue with an inter-frame gap, and TX-04 send-failure reporting.
2. TX-01: accepted, no change.
3. RX-01 timestamp trailer and RX-04 flag booleans together, then gate push on `app_offline`.
4. RX-03 push exclusion for `response` (mc-chat contract first, then subtree pull).
5. RX-07 doc correction in `CLAUDE.md` and `ble_protocol.py`.
6. TX-02 residual: byte-aware fallback in `_chunk_response`, one regression test.
7. The rest as backlog.

The four inventories and two diff files behind this report live in the session scratchpad and
are not committed; ask if they should be preserved under `doc/`.
