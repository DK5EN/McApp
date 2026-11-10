# McApp: reading and setting the node time zone over BLE

Date: 2026-10-04. Firmware: MeshCom fork `fork-dev`, campaign TZ-01 (`docs/ntp-tz-rtc-wave-plan.md`
in the firmware repo). Status: the firmware side is implemented; the integration wave (W3) is in its
review gate and not yet committed, and nothing has been tested on hardware. McApp (MCProxy) has no
changes yet. This document is the contract for when it does.

## BLUF

- The node now stores a **POSIX TZ rule** (`node_tz`, e.g. `CET-1CEST,M3.5.0,M10.5.0/3`). When a
  rule is set, the node derives its UTC offset itself and switches DST on its own, within a minute.
- **Read:** the rule arrives as key `TZ` in the `SN1` register (`0x44` JSON frame). The derived
  offset keeps arriving as `UTCOF` in `SN`.
- **Write:** send `--settz <rule>` as an ordinary `0xA0` text command. `--settz none` clears it. No
  new frame type.
- **Breaking interaction:** `--utcoff` now **clears** a set TZ rule. McApp's `set_time()` sends
  `--utcoff` on every sync and on every DST change it detects (`ble_service/src/ble_adapter.py:1488`,
  `:1749`). Against a node with a rule, that wipes the rule. McApp must stop doing that when the node
  reports a rule (section 5).

## 1. Firmware model

| Item          | Meaning                                                                                                                                                  |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `node_tz`     | POSIX TZ string, max 39 chars, persisted. Empty = no rule.                                                                                               |
| `node_utcoff` | Float hours, east-positive (`+2.0` = CEST). Without a rule: the fixed value from `--utcoff`. With a rule: derived.                                       |
| Node clock    | Holds **node-local time** = UTC + `node_utcoff`. Not UTC.                                                                                                |
| DST switch    | With a rule, the node checks once a minute and moves its clock by the offset change (normally 1 h). Nothing is written to flash then.                    |
| Default       | A freshly flashed or `--clean` ESP32 node starts with `CET-1CEST,M3.5.0,M10.5.0/3`. Updated nodes and nRF52 nodes start empty (fixed offset, as before). |

Accepted rule grammar: `std offset [dst [offset] ,Mm.w.d[/time],Mm.w.d[/time]]`

- Names: 3+ letters, or quoted `<+0530>` with letters, digits, `+`, `-`.
- Offsets use the **POSIX sign**: `CET-1` means UTC+1.
- Only `M` rules (month.week.day; week 5 = last). `Jn` and plain-`n` day rules are rejected.
- A DST name without rules (`CET-1CEST`) is rejected; the firmware does not guess.
- Examples that work: `CET-1CEST,M3.5.0,M10.5.0/3`, `GMT0BST,M3.5.0/1,M10.5.0`,
  `EST5EDT,M3.2.0,M11.1.0`, `UTC0`, `<+0530>-5:30`, `NZST-12NZDT,M9.5.0,M4.1.0/3`.

## 2. Reading the TZ setting

### When the node sends it

`SN` and `SN1` are sent back to back by `sendNodeSetting()`:

- in the post-hello config burst (the `--nodeset` step),
- after `--settz`, `--utcoff` and other node-setting commands that came in over BLE,
- on demand: send `--nodeset` as a `0xA0` command, receive `SN` + `SN1`.

### Frames

`0x44` JSON register frames, `TYP` selects the register:

```json
{"TYP":"SN", ..., "UTCOF":2.0, ...}
{"TYP":"SN1","VIA":true,"VIACALL":"OE1KFR-12","WSPWD":"","ASYM":false,"TZ":"CET-1CEST,M3.5.0,M10.5.0/3"}
```

| Key          | Register | Type   | Meaning                                                                                       |
| ------------ | -------- | ------ | --------------------------------------------------------------------------------------------- |
| `UTCOF`      | `SN`     | number | Offset in hours now in effect (derived when a rule is set). Unchanged key, unchanged meaning. |
| `TZ`         | `SN1`    | string | The rule, or `""` when none is set.                                                           |
| `TZ` missing | `SN1`    | -      | **Firmware without TZ-01.** Treat as "TZ not supported": never send `--settz` to it.          |

Notes for the parser:

- `TZ` is the last key of `SN1`. If an `SN1` document ever overflowed the 244-byte BLE JSON budget,
  the firmware's fail-soft drops trailing keys first, so `TZ` would be the one missing. Worst case
  today is 168 bytes, so this does not happen with valid values.
- `SN1` is already in McApp's dispatcher and `cached_ble_registers` and is correctly not in
  `REQUIRED_BLE_REGISTER_TYPES` (old firmware never sends it). Read `TZ` from
  `cached_ble_registers["SN1"].get("TZ")`, with `None` meaning "unsupported".
- `UTCOF` alone cannot tell a rule from a fixed offset. Always look at `TZ`.

## 3. Setting the TZ rule

Send a `0xA0` text command (McApp: `send_command()`, frame `[len][0xA0][ascii...]`):

| Command                 | Effect                                                                                                                                               |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--settz <rule>`        | Validates the rule. On success: stores it, re-anchors the clock at once if the clock is already set, saves the setting, pushes `SN` + `SN1` (+ `G`). |
| `--settz none`          | Clears the rule. `UTCOF` keeps its last value as the new fixed offset. Saves, pushes `SN` + `SN1`.                                                   |
| `--settz` (no argument) | Same as `none`.                                                                                                                                      |
| `--utcoff <+h.h>`       | Sets a fixed offset **and clears a set rule** (decision D1: a manual offset wins). Pushes `SN` + `SN1`.                                              |

Feedback and errors:

- The human-readable reply (`TZ <rule>, now CEST UTC+2.0`, or
  `settz: rejected <rule>: <reason>`) goes to the node's serial/debug console, not to BLE.
- **Over BLE, success is visible only through the `SN1` push.** A rejected rule changes nothing and
  pushes nothing. Recommended client logic: send `--settz <rule>`, wait about 2 s for an `SN1` whose
  `TZ` equals the rule; if none arrives, send `--nodeset` and compare; still different = rejected.
- The rule must not contain spaces (none of the valid ones do). The firmware does not lowercase or
  filter the `0xA0` payload, so `, / < > + - . :` and letter case arrive intact.
- Max 39 characters. Longer is rejected.

### Where McApp gets the rule from

On Linux and macOS the system zone file ends with exactly the POSIX string the node needs (TZif v2+
footer):

```python
from pathlib import Path


def host_posix_tz(zone: str = "Europe/Berlin") -> str | None:
    """Last line of the TZif file, e.g. 'CET-1CEST,M3.5.0,M10.5.0/3'."""
    path = Path("/usr/share/zoneinfo") / zone
    try:
        footer = path.read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1].decode("ascii")
    except (OSError, UnicodeDecodeError):
        return None
    return footer or None  # empty footer = zone the node cannot express
```

`/etc/localtime` (a symlink into zoneinfo) works the same way for "the host's zone". Before sending,
check the result against the node's limits: at most 39 chars, `M` rules only. Otherwise fall back to
`--utcoff`.

## 4. Time handling once a rule is set

- **Clock sync stays the same:** the `0x20` time frame carries **UTC** (4-byte LE Unix time). The
  node adds the rule's offset itself. Send `0x20` after hello as today.
- **Do not send `--utcoff` to a node that reports a rule** (section 5).
- **Node-local timestamps jump at DST.** Anything the node reports as local wall-clock date/time (for
  example the MH frames' DATE/TIME, which are UTC + `node_utcoff`) moves by an hour at the switch,
  within a minute of it. The node-local epoch is not monotonic across a switch, and the autumn hour
  02:00-03:00 occurs twice.
- **Recommendation for McApp's future time handling:** convert node-local times to UTC as soon as
  they arrive, using the offset in effect at that moment (`UTCOF`, or the rule evaluated for that
  instant), and store and compare only UTC. Render local time in the UI from UTC. Today
  `timestamp_from_date_time()` (`src/mcapp/ble_protocol.py:419`) parses node wall-clock strings as
  naive host-local times; that is correct only while the host's and the node's offsets agree.
- Frames that already carry a Unix timestamp (for example the ACK reception trailer) are UTC and need
  no conversion.

## 5. Required McApp change: `set_time()` and the DST watcher

Today (`ble_service/src/ble_adapter.py`):

- `set_time()` (`:1488`) sends `--utcoff <host offset>`, then the `0x20` timestamp.
- `_dst_check_loop()` (`:1749`) calls `set_time()` hourly when the host's offset changed.

Against TZ-01 firmware, that first `--utcoff` clears the node's rule on every connect and at every
DST change, and the node falls back to a fixed offset. Proposed logic:

```text
on connect, after the config burst:
    tz = cached SN1 .get("TZ")             # None = old firmware, "" = no rule, else rule
    if tz is None:                          # old firmware: unchanged behaviour
        set_time() as today (--utcoff, then 0x20)
    else:
        if want_node_tz_from_host and tz != host_posix_tz():
            send "--settz <host rule>"      # verify via SN1 as in section 3
        elif tz == "":
            send "--utcoff <host offset>"   # node has no rule: keep today's behaviour
        send 0x20 timestamp                 # UTC, always

DST watcher:
    if the node has a rule: do nothing (the node switches by itself)
    else: as today
```

Whether McApp should push the host's zone onto the node (`want_node_tz_from_host`) or only respect a
rule set on the node (serial, web GUI, a future app setting) is a product decision. The safe default
is: never overwrite a non-empty `TZ`, and never send `--utcoff` while `TZ` is non-empty.

## 6. Compatibility matrix

| Firmware           | `SN1.TZ` | McApp should                                                                |
| ------------------ | -------- | --------------------------------------------------------------------------- |
| Before TZ-01       | absent   | Behave as today (`--utcoff` + `0x20`, DST watcher).                         |
| TZ-01, no rule set | `""`     | Behave as today, or offer `--settz`.                                        |
| TZ-01, rule set    | rule     | Send only `0x20`; no `--utcoff`; DST watcher idle; show the rule in the UI. |

## 7. Firmware references

| What                         | Where (firmware repo)                                                            |
| ---------------------------- | -------------------------------------------------------------------------------- |
| `--settz`, `--utcoff` (D1)   | `src/command_functions.cpp`, `commandAction()`                                   |
| `SN` / `SN1` builder         | `src/command_functions.cpp`, `sendNodeSetting()`                                 |
| Rule parser                  | `src/tz_rule.{h,cpp}`                                                            |
| Clock re-anchor, minute tick | `src/clock.cpp`, `Clock::SetClock(time_t, bool)`, `CheckEvent()`, `tzApplyNow()` |
| `0x20` / `0xA0` handling     | `src/phone_commands.cpp`, `readPhoneCommand()`                                   |
| Wire format spec             | `docs/architecture/11-wire-format.md` sections 4.2 and 4.4                       |
| Plan and decisions           | `docs/ntp-tz-rtc-wave-plan.md`, `docs/ntp-tz-rtc-findings.md`                    |
