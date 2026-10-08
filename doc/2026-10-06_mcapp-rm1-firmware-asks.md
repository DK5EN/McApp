# RM1 remote management: three asks from McApp to the firmware

Date: 2026-10-06. From: McApp (MCProxy + webapp), Node Admin. To: the MeshCom firmware maintainer, branch `fork-dev`
(checked at `91d378cb`). Context: McApp is building a human-readable remote view on top of RM1 and plans to support
the extended commands of `docs/rm-gui/extended-commands-concept.md` (draft 2). A design review of that work
(`MCProxy/doc/2026-10-06_1100-node-admin-remote-view-concept.md`, §11) found three points that only the firmware can
settle.

McApp is a second RM1 **sender** next to the firmware's own Remote page: it builds the signed `RM1 <ctr> <cmd> <tag>`
text and hands it to the attached node as a DM, over BLE (0xA0) or Extern-UDP. The node then transmits it like any DM.

## Ask 1: decide where the `rm=2` capability token goes

**Problem.** Draft 2 §2.1 says the capability token `rm=2` appears "in `sync`/`status`" and, in the same section,
that replies of the existing commands stay at 63 characters or less (version skew with older operator firmware). Both
cannot hold for `status`:

- `status` worst case today is 61-62 characters (`rm_sender_policy.h:361-385`, e.g.
  `ok v=4.40a up=71582788 bat=100 heap=115 s=GTDMWL p=22/22 led=0` = 62).
- Plus ` rm=2` = 66-67, over the 63 that older firmware (`remote_cmd.cpp:577`) and older McApp accept.

If only one reply type carries the token and a consumer treats "newest of sync/status" as the truth, the capability
flips back to 1 on every reply of the other type.

**Proposal.** Put `rm=<n>` in the `sync` reply only: `ok ctr=<hwm> v=<ver> rm=2` (about 30 characters, always fits).
`status` never carries it. State this in the concept as a rule for every future capability token.

Alternative, if it must be in `status` too: drop the redundant `led=` there (the `L` letter already says it), and say
so explicitly.

**What McApp does either way.** Treats the token as sticky per reply type: only a reply type that carries it can set
or clear it. It resets on a firmware version change in `v=`, a verified `reboot`, or a `no_reply` to a new command.
McApp also raises its own result limit from 63 to 108 in its next release, so a longer reply is no longer dropped.

**Done when.** The concept names exactly one carrier (or both, with the `led=` change), and `remote_cmd_vectors.json`
has a `sync` reply carrying the token.

## Ask 2: no DM retry ladder for `RM1 ` frames, from any origin

**Problem.** The node retransmits every DM it originates up to 4 times (`{NNN` suffix, retry ladder,
`loop_functions.cpp:4359-4361, 4413-4421`). Only `{CET}`, `{MCP}`, `{SET}` and pings are exempt. That includes the
RM1 commands McApp hands over via BLE (0xA0 -> `sendMessage`) and Extern-UDP (`getExtern` -> `sendMessage`).

Draft 2 §5 item 4 removes the suffix and the ladder only for the firmware's own Remote page sender (`bookAndSend`),
not for frames that arrive from a connected app.

**Failure scenario.** A command gets no answer within the reply window. The operator sends the next one. A late
retry keying of the old frame then reaches the target after the new one:

- if the new one was accepted, the old ctr is at or below the high-water mark: a **counted replay reject**, with the
  right password;
- otherwise it lands inside the 10 s rate window: a silent drop of the new command.

Three counted rejects in 90 s lock RM for 5 minutes. A sender cannot see the late keyings, so it cannot avoid them.

**Proposal.** In the DM send path, treat a payload starting with `RM1 ` like `{CET}`/`{MCP}`/`{SET}`: no `{NNN`
suffix, `user_msg_status = 0xFF` (no retransmission), regardless of whether it came from the Remote page, BLE or
Extern-UDP. An RM1 reply is the acknowledgement; a DM ACK adds nothing.

**What McApp does either way.** Holds a target for 180 s after any unanswered command before sending the next one.
With the firmware change that hold can become shorter; without it, it stays.

**Done when.** A `RM1 ` DM handed over via BLE or Extern-UDP goes on air exactly once, without `{NNN`, and a native
test pins it.

## Ask 3: compact `status` replies into `remote_cmd_vectors.json`

**Problem.** `tools/tests/remote_cmd_vectors.json` holds 4 reply vectors. The only `status` reply is the old form
`ok v=4.40a up=125 bat=87 heap=212 gw=0 mesh=1`. The compact form with `s=` letters and `p=cur/max` that the firmware
sends today exists only as literals in the native test (`test/test_rm_sender_policy/test_main.cpp:553-633`).

McApp copies the vectors file byte for byte and pins its sha256. Today it would have to hand-write the compact
strings it parses, which pins McApp's reading of the format, not the firmware's.

**Proposal.** Add these replies to the vectors (via `tools/remote_cmd.py`, the generator), each with the parse
result the firmware's `rmStatusParse` produces:

1. A 6-letter `s=` with LED: `ok v=4.40a up=417 bat=0 heap=115 s=gtdMwl p=2/22 led=0` (live DK5EN-1 sample).
2. A 5-letter `s=` without LED (no `L`, no `led=`).
3. The 62-character worst case (all upper case, 8-digit uptime, `bat=100`, `p=22/22`).
4. The `sync` reply with the capability token from Ask 1.
5. Once draft 2 lands: one reply per new command at its worst-case length (result 108, wire 140).

A native check that parses every JSON reply through `rmStatusParse` would keep JSON and C++ in step.

**Done when.** The vectors carry 1-4 with expected parse fields. McApp then copies the file, updates its sha256 pin
and drops its hand-copied literals.

## For reference: what McApp needs from draft 2 besides these asks

- Reply wire at most 140 characters, result at most 108, charset as in draft 2 §2.3. McApp parses with that budget.
- Names for the `..` counters in the `txq` and `mbox` replies. McApp labels them only once the formatter exists.
- `busy` is a refusal of the firmware's own sender, never a node reply. McApp leaves it out of its error texts unless
  the node starts sending it.
