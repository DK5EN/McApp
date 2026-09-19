# Winlink over MeshCom (`WLNK-1`) — command reference and on-air test plan

**Status:** research done 2026-09-19, test not yet run. Trigger: HB9VQQ reported (mail,
2026-09-19) that `?` to `WLNK-1` answers on v2.0.10 but did not on v2.0.7 — a claim no code
change of ours can explain, so the point of this plan is to find out what actually works and
where our stack touches it.

## 1. What `WLNK-1` is

It is **APRSLink**, Winlink's own APRS gateway, reachable from MeshCom because the MeshCom
server hands DMs addressed to `WLNK-1` into APRS-IS. It is not a MeshCom service and nobody in
our fleet operates it. The command set is Winlink's, documented on winlink.org, and the help
string HB9VQQ received (`SP, SMS, L, R#, K#, Y#, F#, P, G, A, I, PR, B (? + cmd for more)`)
matches APRSLink's verbatim.

The **firmware special-cases the destination** in four places — worth knowing before blaming the
proxy for anything:

| Site                           | Behaviour                                                                                             |
| ------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `regex_functions.cpp:42`       | `WLNK-1` is on the callsign **allowlist**; it fails the normal callsign regex                         |
| `lora_functions.cpp:1302`      | A gateway **acks** DMs to `WLNK-1` itself (like `*`, `APRS2SOTA`, group calls)                        |
| `lora_functions.cpp:1386-1392` | `bMeshDestination = false` — gateways do **not** mesh-relay it; it leaves via the gateway's IP uplink |
| `loop_functions.cpp:5028`      | The ack payload to `WLNK-1` is `ack%04i` — **no `CALL     :` prefix**, 4 digits, not 3                |

Consequences: you need a gateway with an internet uplink in range (a pure mesh path does not
reach Winlink), and the `ack0087` ack shape does **not** match our `*:ack[0-9]*` noise filter.
That ack is built in `SendAckMessage()` and never goes to `addBLEOutBuffer`, so it should be
invisible to us — test T1.4 below checks that it really is.

## 2. Command reference (APRSLink)

All commands are plain DM text to `WLNK-1` (**with** the `-1`). Help is also returned whenever
APRSLink does not understand a command.

| Command | Syntax                                  | Meaning                                                              |
| ------- | --------------------------------------- | -------------------------------------------------------------------- |
| `H`     | `H`                                     | Brief help                                                           |
| `?`     | `?` / `?L`                              | Same help; `?` + command letter, **no space**, gives detailed help   |
| `L`     | `L`                                     | List pending messages, **max 5**; later `#` commands index this list |
| `R#`    | `R2`                                    | Read message #                                                       |
| `Y#`    | `Y2`                                    | Reply to message #                                                   |
| `F#`    | `F2 you@home.net`                       | Forward message # to an address or callsign                          |
| `K#`    | `K3`                                    | Kill (mark deleted) message #                                        |
| `SP`    | `SP <email\|callsign\|alias> <subject>` | Start an email; every following DM is a body line, ended by `/EX`    |
| `/EX`   | `/EX`                                   | Send the composed message; a confirmation comes back                 |
| `P`     | `P`                                     | Playback the body lines composed so far                              |
| `SMS`   | `SMS <email\|callsign\|alias> <text>`   | One-line message, no `/EX` needed                                    |
| `A`     | `A sam=sam@long.domain.net` / `A sam=`  | Create / delete an alias                                             |
| `AL`    | `AL`                                    | List aliases                                                         |
| `G#`    | `G` / `G3`                              | Nearest active RMS Packet gateway(s), default 1                      |
| `I`     | `I`                                     | Information about APRSLink                                           |
| `PR`    | —                                       | **Undocumented in the sources found** — probe with `?PR`             |
| `B`     | `B` / `BYE`                             | Logout (ends the session) — **test last**                            |

`?` and `I` answer without a login. Everything touching the mailbox (`L`, `R#`, `SP`, `SMS`, …)
needs one.

### Login

1. Send any text (HB9VQQ's site says literally `Login`) as a DM to `WLNK-1`.
2. The CMS answers with a **three-digit challenge**, e.g. `[453]` — the character **positions**
   in your Winlink password.
3. Answer with **6 characters**: the 3 password characters at those positions plus 3 arbitrary
   filler characters, in any order. Password `ABC123`, challenge `425` → `1B2AZ5` is valid, and
   so is `ABZ21TY` (it contains `1`, `B`, `2`).
4. The session expires after **~2 hours**. Repeated failed attempts get the callsign temporarily
   blocked — on doubt, re-send `Login` and answer a fresh challenge rather than guessing.

The password itself is never on the wire, but the response is: it discloses three password
characters **by position** each time, in clear, across the MeshCom server and APRS-IS. Use a
Winlink password that exists nowhere else.

## 3. Registration — yes, DK5EN needs an account

Your guess is right. APRSLink authenticates against a Winlink CMS account, and the account is on
the **base callsign** (`DK5EN`); the SSID is only mesh routing. No account, no password, no
challenge that can be answered — the mailbox commands are unreachable, `?` and `I` are not.

Creation is the standard Winlink flow, **not** a web signup: connect to the CMS once **without**
a password (Telnet is fine), which creates the account; the system then posts a message
containing your password to that account, which you fetch on a second connection and change to
one of your own. On Windows that is Winlink Express; on macOS the usual client is **Pat**
(`pat connect telnet`) — worth verifying before relying on it, the account-creation path is
documented for Winlink Express.

**Do this before any of phase 2 below**, and confirm the account works over Telnet first — a
failed login over LoRa costs 4 keyings and a block, a failed login over Telnet costs nothing.

## 4. Test plan

One command at a time, wait for the reply before the next. Every DM to `WLNK-1` is ~4 keyings
over 2 minutes if unacked, so a 5-line email is ~20 keyings of airtime.

### Phase 0 — transport, no account needed

| ID   | Send                   | Expect                                     | Proves                                            |
| ---- | ---------------------- | ------------------------------------------ | ------------------------------------------------- |
| T0.1 | `?`                    | The 13-command list                        | dst `WLNK-1` survives our schema and routing      |
| T0.2 | `?SMS`                 | `Send short message: SMS <addr> <message>` | `?`+cmd parsing, text byte-exact                  |
| T0.3 | `? SMS`                | The **generic** list, not the SMS help     | The space survived — we neither trim nor collapse |
| T0.4 | `I`                    | APRSLink info                              | A second stateless command                        |
| T0.5 | `?PR`                  | Whatever `PR` is                           | Closes a gap the public docs do not cover         |
| T0.6 | `?B`                   | Whatever `B` is (expected: logout)         | Same                                              |
| T0.7 | `?G`, `?P`, `?F`, `?A` | Detailed help                              | Completes the reference above                     |

T0.2 vs T0.3 is the one pair worth keeping: HB9VQQ's screenshot already shows both outcomes, so
it is a **regression check on our send path**, not a Winlink question.

### Phase 1 — what our stack did with it (run after phase 0, on mcapp.local)

| ID   | Check                                                                                                                                                                                                      |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1.1 | `messages` rows for `dst='WLNK-1'` / `src` starting `WLNK`: exact src spelling, `conversation_key` = `DK5EN<>WLNK`, `delivery_status`, `acked`, `ack_kind`                                                 |
| T1.2 | Sidebar badge for `WLNK` clears on read and stays cleared (issue #11 fix, live since v2.0.10)                                                                                                              |
| T1.3 | Classifier verdict on the replies: `category`, `info_score`, `template_hash` — a help list is template-shaped and may score as a beacon                                                                    |
| T1.4 | `SELECT count(*) FROM messages WHERE msg GLOB 'ack[0-9]*'` — must be **0**. A hit means the firmware's `ack%04i` shape reaches us and our `*:ack[0-9]*` noise filter misses it, polluting history and push |
| T1.5 | Did a push fire for the replies, and what text did it carry?                                                                                                                                               |
| T1.6 | `stall_events` for the send path around each attempt                                                                                                                                                       |

### Phase 2 — mailbox, needs the account

`Login` → challenge → response → `L` → `R1` → `SMS <own address> test from MeshCom` →
`SP <own address> Test` + body + `/EX` → `AL` → `A test=<addr>` → `AL` → `G` → `PR` → `B` last.
Do not run `K#` against anything but a message you sent yourself for the purpose.

### Phase 3 — the engineering questions (this is the interesting part)

| ID   | Test                                                                    | Question                                                                                                            |
| ---- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| T3.1 | `SP` with four numbered body lines `L1 …` … `L4 …`, then `/EX`          | Does the **order** survive LoRa + retransmit + APRS-IS, or does the email arrive scrambled?                         |
| T3.2 | Body lines of 60 / 100 / 140 characters                                 | Where is the real per-message cap — our `{dst}msg` frame limit, the firmware's, or APRSLink's?                      |
| T3.3 | A body line with `äöü` and one with an emoji                            | Does `decode_text`'s CP1252 policy survive the whole chain into the mailbox?                                        |
| T3.4 | A body line ending in `{123`                                            | The firmware treats `{NNN` as an ack request and our push payload strips it — does the text reach the email intact? |
| T3.5 | Record t(send) → t(reply) per command, and t(`/EX`) → mail in the inbox | HB9VQQ's site says 20 minutes and more for the mail; how long for the command replies?                              |

**Deliverable:** a table of command → works / does not / reply text / latency, plus whatever
T1.x turns up on our side. That table is the answer to "what is possible", and it is the thing
worth putting back into `doc/`.

## Sources

- APRSLink command list (SA7SKY compilation of winlink.org, 2022-06-06)
- winlink.org, "Passwords with Keyboard Mode and APRSLink" (challenge/response, 2 h expiry)
- ki4hdu.com, Winlink/APRSLink notes (login procedure, session timeout)
- meshcom.hb9vqq.ch (MeshCom-specific: `WLNK-1` not `WLNK`, `Login`, account on the base
  callsign, 20 min+ mail delivery, block after repeated failures)
- icssw.org MeshCom 4.30 release notes (WLNK-1 usage over LoRa)
- `MeshCom-Firmware-DEV-Main`: `regex_functions.cpp:42`, `lora_functions.cpp:1302,1386-1392`,
  `loop_functions.cpp:4104,5028`
