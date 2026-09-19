# Winlink over MeshCom — account creation and test campaign

**Status:** planned 2026-09-19, not started. Blocked on one thing only: `DK5EN` has no Winlink
account. Everything past phase 0 needs it.

Companion documents: `doc/2026-09-19_1100-winlink-wlnk1-command-test-plan.md` holds the command
reference and the firmware findings; this one is the execution runbook — how to get the account,
then what to test and what to record.

## BLUF

`WLNK-1` is Winlink's APRSLink gateway, reachable from MeshCom because the MeshCom server hands
DMs addressed to it into APRS-IS. Two of its three command classes are already usable without an
account (`?`, `I`); the mailbox — `L`, `R#`, `SP`, `SMS` — needs a Winlink account on the **base**
callsign and a challenge/response login over the air. The account cannot be created on a web form:
it is created by connecting to the CMS, which is why part 1 exists at all.

---

# Part 1 — Account creation

## 1.1 What is actually required

| Requirement         | Detail                                                                                               |
| ------------------- | ---------------------------------------------------------------------------------------------------- |
| Callsign            | `DK5EN` — the **base**. The SSID is mesh routing only and is never part of the account identity.     |
| Account creation    | By **connecting to the CMS**, not by a web signup. First connection carries **no** password.         |
| Password delivery   | The system posts a message containing your password **to that account**; fetch it on a 2nd connect.  |
| Radio email address | `DK5EN@winlink.org` once the account exists.                                                         |
| Client              | Anything that speaks the CMS Telnet protocol: Winlink Express (Windows) or **Pat** (cross-platform). |

## 1.2 Client choice

| Option                            | Verdict                                                                                                                                                 |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Pat on this Mac** (recommended) | `pat_1.0.0_darwin_amd64.pkg` from la5nta/pat. The Mac is arm64 but **Rosetta 2 is already installed**, so the amd64 build runs. No Go toolchain needed. |
| Winlink Express in a Windows VM   | The reference client and the best-documented path, but it needs a Windows VM that does not currently exist.                                             |
| Pat on a Pi                       | Works (`linux_arm64` / `linux_armhf` .deb) but puts a new service on a box that runs production. Not worth it for a one-off.                            |

**Do the whole of part 1 over Telnet, not over the air.** A failed login over LoRa costs roughly
four keyings across two minutes and risks the gateway's temporary block after repeated failures; a
failed Telnet login costs nothing.

## 1.3 Runbook

| Step | Action                                                                                          | Success looks like                         |
| ---- | ----------------------------------------------------------------------------------------------- | ------------------------------------------ |
| A1   | Install Pat from the darwin `.pkg`; `pat version`                                               | a version prints                           |
| A2   | `pat configure` — set `mycall: DK5EN`, leave `secure_login_password` **empty**                  | config written, password field empty       |
| A3   | `pat connect telnet` (CMS at `cms.winlink.org:8772`)                                            | session completes; the account now exists  |
| A4   | `pat connect telnet` a second time                                                              | a message from the system arrives          |
| A5   | Read it (`pat http` web UI, or the mailbox on disk) and note the password                       | password in hand                           |
| A6   | Change it to a password used **nowhere else** (see 1.4), then put it in `secure_login_password` | third connect succeeds with secure login   |
| A7   | Send yourself a test mail to `DK5EN@winlink.org` and read it back over Telnet                   | round trip works before any RF is involved |

**`CMSTELNET` is not your password.** It is a common key every client uses for Telnet transport;
do not confuse it with the account password the challenge/response uses.

## 1.4 Security posture — decide before A6

APRSLink's login never sends the password itself. It sends a **three-digit challenge** naming
character _positions_, and you answer with those three characters plus three filler characters.
That answer travels in clear across the MeshCom mesh, the MeshCom server and APRS-IS, and it
discloses three password characters **by position** every time.

Consequences to accept deliberately:

- Use a password that exists nowhere else and protects nothing else.
- Assume anyone listening to the mesh can, over several logins, reconstruct most of it.
- The session lasts ~2 hours; it is not a per-message credential.
- Repeated failed attempts get the callsign temporarily blocked — on doubt, re-send `Login` and
  answer a **fresh** challenge rather than guessing at an old one.

## 1.5 Failure modes

| Symptom                                     | Likely cause / next step                                                                                                                |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| A3 completes but no password message at A4  | Give it a few minutes and reconnect; the message is queued, not instant.                                                                |
| Secure login fails after A6                 | The password was set in Pat but never accepted by the CMS — reconnect once with it blank, confirm the account state, then set it again. |
| Challenge answered correctly, still refused | Blocked after earlier failures. Wait, then re-`Login` for a fresh challenge.                                                            |
| No response at all over RF, Telnet fine     | Not an account problem — see phase 0 of part 2; the gateway needs an uplink.                                                            |

---

# Part 2 — Test campaign

## 2.1 Ground rules

- **One command at a time**, wait for the reply. A DM to `WLNK-1` is ~4 keyings over 2 minutes if
  unacked, so a 5-line email is ~20 keyings of airtime.
- **A gateway with an internet uplink must be in RF range.** The firmware sets
  `bMeshDestination = false` for `WLNK-1` (`lora_functions.cpp:1386-1392`), so it is never
  mesh-relayed — it leaves via a gateway's IP uplink or not at all.
- **Never run `K#`** against anything but a message you sent yourself for the purpose.
- **Run `B` (logout) last** in any session that still needs the login.
- Record every send/receive time; the latency table is half the deliverable.

## 2.2 Phase 0 — transport (no account needed)

Proves the path works and that our stack ships text byte-exact.

| ID   | Send                | Expect                                     | Proves                                            |
| ---- | ------------------- | ------------------------------------------ | ------------------------------------------------- |
| T0.1 | `?`                 | the 13-command list                        | dst `WLNK-1` survives our schema; routing works   |
| T0.2 | `?SMS`              | `Send short message: SMS <addr> <message>` | `?`+cmd parsing; text byte-exact                  |
| T0.3 | `? SMS`             | the **generic** list, not the SMS help     | the space survived — we neither trim nor collapse |
| T0.4 | `I`                 | APRSLink info                              | a second stateless command                        |
| T0.5 | `?PR`               | whatever `PR` is                           | closes a gap no public source documents           |
| T0.6 | `?B`                | whatever `B` is (expected: logout)         | same                                              |
| T0.7 | `?G` `?P` `?F` `?A` | detailed help                              | completes the command reference                   |

T0.2 vs T0.3 is a **regression check on our send path**, not a Winlink question — HB9VQQ's
screenshot already shows both outcomes.

## 2.3 Phase 1 — what our stack did with it

Run on mcapp.local after phase 0. This is where the code shipped in v2.0.11-dev.3 gets its first
real traffic.

| ID   | Check                                                                                                                                                                   | Why it matters                                                                                                                   |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| T1.1 | `messages` rows for `dst='WLNK-1'` / `src LIKE 'WLNK%'`: exact src spelling, `conversation_key` = `DK5EN<>WLNK`, `delivery_status`, `acked`, `ack_kind`                 | the conversation must key as a normal DM                                                                                         |
| T1.2 | The sidebar shows **WLNK** as a personal chat and its badge clears on read and stays cleared                                                                            | issue #11's fix under real traffic                                                                                               |
| T1.3 | `SELECT count(*) FROM messages WHERE msg GLOB 'ack[0-9][0-9][0-9][0-9]' OR msg GLOB 'ack[0-9][0-9][0-9][0-9][0-9]'` — and whether any such row is VISIBLE in the webapp | the bare-APRS-ack filter's first live test; it was written from firmware source, never from a captured frame                     |
| T1.4 | Classifier verdict on the replies: `category`, `info_score`, `template_hash`                                                                                            | a help list is template-shaped and may score as a beacon                                                                         |
| T1.5 | Did a push fire for a WLNK reply, and what text did it carry?                                                                                                           | **the help text is longer than 120 chars** — this is the first real exercise of `MAX_TEXT_LEN` truncation in a delivered payload |
| T1.6 | `stall_events` around each send                                                                                                                                         | the outbound path under a new correspondent                                                                                      |
| T1.7 | If any OTHER station's Winlink traffic is heard: does a `<THEM><>WLNK` conversation appear?                                                                             | Wave 2's digit-less pair key under real data — today it is pinned only by test                                                   |

## 2.4 Phase 2 — login (needs the account)

`Login` → three-digit challenge → 6-character answer → confirm the session.

| ID   | Check                                                                                                                                                                                              |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T2.1 | Does the challenge arrive, and in how long?                                                                                                                                                        |
| T2.2 | Is the answer accepted on the first attempt?                                                                                                                                                       |
| T2.3 | Do our retransmissions cause duplicate answers to reach WLNK? (the gateway acks `WLNK-1` DMs itself — `lora_functions.cpp:1302` — so retransmission should stop early; verify against the ✓ marks) |
| T2.4 | Roughly how long does the session actually last before commands are refused?                                                                                                                       |

## 2.5 Phase 3 — mailbox

`L` → `R1` → `SMS <own address> test from MeshCom` → `SP <own address> Test` + body + `/EX` →
`AL` → `A test=<addr>` → `AL` → `G` → `PR` → `B`.

Record for each: reply text verbatim, latency, and whether it matches the documented syntax.

## 2.6 Phase 4 — the engineering questions

This is the part worth the airtime.

| ID   | Test                                                                  | Question                                                                                                           |
| ---- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| T4.1 | `SP` with four numbered body lines `L1 …` … `L4 …`, then `/EX`        | does the **order** survive LoRa + retransmit + APRS-IS, or does the mail arrive scrambled?                         |
| T4.2 | Body lines of 60 / 100 / 140 characters                               | where is the real per-message cap — our `{dst}msg` frame, the firmware's, or APRSLink's?                           |
| T4.3 | A body line with `äöü` and one with an emoji                          | does `decode_text`'s CP1252 policy survive the whole chain into the mailbox?                                       |
| T4.4 | A body line ending in `{123`                                          | the firmware treats `{NNN` as an ack request and our push payload strips it — does the text reach the mail intact? |
| T4.5 | t(send) → t(reply) per command; t(`/EX`) → mail actually in the inbox | HB9VQQ's site says 20 min+ for the mail; how long for command replies?                                             |

## 2.7 Results template

| Command | Works | Reply (verbatim) | Latency | Notes |
| ------- | ----- | ---------------- | ------- | ----- |
|         |       |                  |         |       |

Plus a defects list from phase 1, each with the row id / query that shows it.

## 2.8 Exit criteria

The campaign is done when:

1. Every command in the reference has a works / does-not / reply / latency row, `PR` and `B`
   included — those two are undocumented in every public source found and are the reason T0.5/T0.6
   exist.
2. Phase 1 has a verdict on each of T1.1–T1.7, with the query or row id that produced it.
3. Phase 4's five questions each have an answer backed by an observation, not an inference.
4. Anything phase 1 turns up is either fixed or filed with a reproduction.

---

## Resume point for the next session

1. **Blocked on:** part 1. Nothing past phase 0 can run until `DK5EN@winlink.org` exists.
   Creating it is outward-facing and needs the operator's go-ahead.
2. **Not blocked:** phase 0 and phase 1 can run today — they need no account, and phase 1 is the
   first real traffic for three things shipped in v2.0.11-dev.3 that are currently pinned only by
   tests: the bare-APRS-ack filter, the digit-less pair key, and payload truncation on a long
   inbound text.
3. Backlog entry: `doc/backlog.md` B5.
