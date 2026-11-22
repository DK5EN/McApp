# Ed25519 Message Signatures — Scout Report

Status: scouting only, nothing implemented. 2026-09-27.

Question: what does it take to authenticate MeshCom text messages (`*`, groups, DMs) with an
Ed25519 signature appended to the end of the message text? Constraint: the tail must be 7-bit
ASCII and survive every character parser in firmware, proxy and webapp.

## BLUF

- Reserve **~95 characters** for the signature tail. That leaves **~55 characters** of real text
  within the 150-byte budget. This is the main cost.
- Feasible for `*`, group and DM with a **base64url** tail placed **before** the firmware's DM
  `{NNN` ack suffix.
- Sign on the **proxy** (`cryptography` is already in the lock file). No firmware change needed.
- The hard part is **public-key distribution**, not signing.

## Signature Size and Encoding

| Encoding           | Chars for 64 bytes | Parser-safe?                                                 |
| ------------------ | ------------------ | ------------------------------------------------------------ |
| Hex                | 128                | yes                                                          |
| base64url, no pad  | 86                 | yes — alphabet `A-Za-z0-9-_`                                 |
| Base85 / Z85       | 80                 | no — alphabet contains `{ } : ~`                             |
| basE91             | ~79                | no — alphabet contains `{ } : "`                             |
| custom safe base-N | ~80                | yes, but non-standard; saves 6 chars over base64url for pain |

- An Ed25519 signature is always 64 bytes. It cannot be truncated without losing its security.
- Base85/Base91 collide with: the firmware's DM `{` → `(` rewrite (`dm_text_escape.h`,
  `loop_functions.cpp:4270-4275`), and the `{NNN` / `:ack` / `:rej` / `:sto` tail parsers in all
  three repos. **Use base64url.**

## Proposed Tail Layout (~95 chars)

```
<text> ~s<kid><ts><sig86>[{NNN]
```

| Field  | Chars | Purpose                                                                |
| ------ | ----- | ---------------------------------------------------------------------- |
| ` ~s`  | 2-3   | separator + tag, so receivers can find and strip the tail              |
| `kid`  | 1-2   | key id, enables key rotation                                           |
| `ts`   | ~5    | base64url timestamp (e.g. minutes); replay protection                  |
| `sig`  | 86    | Ed25519 signature, base64url, no padding                               |
| `{NNN` | 4     | DM only, appended by the firmware AFTER our tail — not ours to reserve |

- **The timestamp is mandatory.** The `msg_id` is minted by the node after the sender signs, so
  it cannot be bound into the signature. Without a timestamp any signed message can be replayed.
- **Signed data:** `src`, `dst`, `ts`, and the message text as UTF-8 bytes. Binding `dst` stops a
  signed message being re-sent into a different group or DM.
- The verifier strips the firmware's `{NNN` (`util.py:66`, `ACK_SUFFIX_RE`) before parsing the
  tail.

## Length Budget

| Limit                               | Value     | Where                                 |
| ----------------------------------- | --------- | ------------------------------------- |
| Extern-UDP `msg` accepted by node   | 150 bytes | `udp_handler.py:78`, `schemas.py:93`  |
| Extern-UDP wire frame (`:{dst}msg`) | 159 bytes | `udp_handler.py:85`                   |
| Webapp compose cap                  | 150 − dst | webapp `ChatInput.vue:24,107-111`     |
| Firmware `sendMessage` text length  | 1..160    | firmware `loop_functions.cpp:4126`    |
| RF LoRa TX buffer                   | 255 bytes | firmware `configuration_global.h:269` |
| BLE `D{` register JSON              | 244 bytes | firmware `configuration_global.h:567` |
| Push notification text              | 120 chars | `push_delivery.py:61`                 |

- The firmware **rejects** a text over 160 (`BP_SEND_INVALID`); it does not truncate and never
  fragments.
- 150 − ~95 = **~55 characters of text**.

## Impact per Component

### Firmware (MeshCom-Firmware-DEV-Main)

- **No change required.** No crypto, hash or auth field exists in the frame header or code.
- Charset filter strips C0/C1 controls and DEL only (`charset_filter.h`); printable ASCII passes
  unchanged, no mid-relay rewrite.
- Tail-parsed markers: `{NNN` (DM ack request), `:ackNNN`/`:rejNNN` (`lora_functions.cpp:1599,
1789`), `:stoNNN` (`msgstore_glue.cpp`, `sto_notice.cpp`). Reserved prefixes: `{ping}`,
  `{pong}`, `{SET}`, `{CET}`, `{MCP}`.
- Unaware stations display ~90 characters of base64 noise at the end of the message.

### Proxy (MCProxy)

- **Signing:** Ed25519 via `cryptography` (already a transitive dependency of `pywebpush`,
  `cryptography 50.0.1` in `uv.lock`). Key at `/var/lib/mcapp/`, `0600`, same handling as the
  VAPID key (`push_delivery.py:69-90`).
- **Verification at ingest:** strip `{NNN`, then strip and verify the tail, store the verdict in a
  new column (schema migration + `LATEST_SCHEMA_VERSION` bump).
- **Charset:** `text_decode.py` fast-paths 0x20-0x7E; a base64url tail is never altered.
  Non-ASCII message bodies are re-decoded (CP1252 fallback) — the signature must cover the bytes
  as sent, so define canonicalisation explicitly (sign UTF-8 of the decoded text, reject if the
  receiver's decode differs).
- **Dedup:** unaffected — `_find_duplicate_row_id` (`storage/ingest.py:314-324`) and conversation
  dedup key on `msg_id`/sender/dst/time, not text. Both transport copies carry the same tail anyway.
- **Push:** `build_push_payload` truncates at 120 after `strip_ack_suffix`
  (`push_delivery.py:49,168`); the signature tail must be stripped before the truncation, and the
  push contract (mc-chat upstream) needs a clause for it.
- **Classifier / suppression / commands:** end-anchored classifier rules (`classifier/seed.py`,
  e.g. bare URL at `:333`, ping at `:51`) would stop matching a signed message — they must run on
  the stripped text. Commands and link-check are prefix-only (`suppression.py:18-20`,
  `linkcheck.py:41,49`).
- **Contract parity:** tail grammar + strip rule belong in a shared corpus in mc-chat, like the
  ack-suffix vectors.

### Webapp

- **Compose cap:** reserve ~95 in `ChatInput.vue:107-111` for signed sends.
- **Optimistic echo / dedup:** fallback dedup key `c:${src}|${dst}|${msg}` (`dedup.ts:80`) and the
  RF-monitor content key (`wireFrameNormalizer.ts:246`) use the full text. If the proxy appends the
  tail server-side, the optimistic bubble no longer matches its echo → duplicate bubbles. Strip the
  tail wherever `stripAckRequestSuffix` is applied (`optimisticSend.ts:45-46`).
- **Rendering:** plain text, no markdown/linkify (`ChatBubble.vue:364`) — nothing gets mangled.
  Replace the raw tail with a "verified / unknown key / invalid" badge.
- **Filters:** `isTextBlocked` and push noise gates are substring checks (`predicates.ts:87-91`,
  `pushFilter.ts:147-154`); a tail does not trigger them.
- **Browser signing is a poor fit:** WebCrypto Ed25519 needs a secure context; `http://mcapp.local`
  is the default. Browser-side signing would need a pure-JS library (e.g. noble-ed25519). Signing
  on the proxy avoids this.

## Key Distribution (the actual hard part)

Receivers need callsign → public key. Options:

1. **TOFU** — trust on first use, pin per callsign, warn on change.
2. **Curated list on GitHub**, fetched like `sperrliste.json` (conditional GET, 15 min).
3. **On-air announcement** — a key beacon; costs airtime, still needs TOFU or a web of trust.

The `kid` field in the tail allows key rotation under any of these.

## Open Points

- **Regulatory check:** amateur radio forbids obscuring a message's meaning. A signature does not
  encrypt and the text stays readable, but confirm before shipping.
- **Alternative if ~55 chars is too tight:** send the signature as a separate frame that
  references the `msg_id`. Full 150 chars of text, double the airtime, and the sig frame can be
  lost independently.
- Timestamp granularity and replay window (proposal: minutes, ±1 h acceptance) not decided.
- Canonicalisation of non-ASCII text not decided.
