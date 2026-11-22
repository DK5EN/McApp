# ADR-0019: Inbound text: CP1252 fallback and blacklist filter

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-09-09 (commit `de7a6ac`) |
| Deciders | Martin Werner |
| Affects | `src/mcapp/text_decode.py`, the UDP and BLE ingest paths, `ble_protocol.py` (message body only) |
| Supersedes | the whitelist `is_allowed_char` and `errors="ignore"` decoding |
| Superseded by | - |

## Context

Firmware CHR-03 relays payload bytes unchanged instead of dropping them. Some senders put umlauts on
the wire as single CP1252 bytes (`ü` is `0xFC`, not `C3 BC`). The old decode with `errors="ignore"`
deleted those characters silently. The filter was a whitelist that had to be extended by hand for
every legitimate character: joined emoji, `Ç`, `Ñ`, the Nordic set, decomposed accents. The UDP path
ran the whitelist over the datagram, and the BLE path ran no character filter at all. The same message
arrives on both transports, and the first copy wins, so the stored text depended on a race.

## Decision

- **Decode:** UTF-8 first. Each byte that is not valid UTF-8 is read as CP1252. Only the five bytes
  undefined in CP1252 (`0x81 0x8D 0x8F 0x90 0x9D`) become U+FFFD.
- **Filter is a blacklist.** It rejects the Unicode categories Cc, Cf, Cs, Co and Cn. Everything else
  passes.
- **Exceptions:** U+200D (zero-width joiner) and the tag range U+E0020 to U+E007F are kept, because they
  bind neighbours into one grapheme. Marks (Mn) and enclosing keycaps (Me) need no exception.
- **Both transports use the same function.** In `ble_protocol.py` it applies to the message body only;
  `path` and `dest` are callsign fields and stay ASCII.
- **The `D{` register decode in `ble_service` stays strict.** There a `UnicodeDecodeError` is the evidence
  of the firmware's 244-byte clamp cutting a codepoint, and a fallback would hide it.

## Consequences

- `Cn` is judged against the running Python's Unicode tables. A codepoint assigned after that release
  reads as unassigned and is dropped until a Python upgrade. This is the one way the filter can still be wrong.
- New scripts are accepted by default. Nothing needs to be added to a list.
- mc-chat carries the same semantics in its own decoder. The two are ported, not imported.

## Alternatives considered

- Keep the whitelist and extend it: rejected, it lost the race every time a new character appeared.
- Decode as Latin-1 for everything: rejected, mangles multi-byte UTF-8.

## Evidence

- Firmware: `MeshCom-Firmware` fork-main `16670de9` and `094636b2` (CHR-03)
- `CLAUDE.md`, section "Inbound Charset"
