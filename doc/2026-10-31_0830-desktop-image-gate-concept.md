# Desktop image gate: open the bootstrap to Pi 4/5 desktops

**Status:** Ideation, backlog. 2026-10-31. No code changed. Operator decisions below are
final; everything else is a proposal. Reworked after an advisor review (2026-10-31).

## BLUF

- **Today any desktop image hard-aborts the bootstrap**, on every board. The rule exists for the
  Pi Zero 2W (512 MB) and is wrong for a Pi 4/5 with 2-8 GB that can run a desktop and McApp.
- **The gate becomes a RAM-based policy**, decided on `MemTotal`, never on the model name:
  under 1 GB refuse, 1-2 GB warn and confirm, 2 GB and up proceed.
- **Detection gets precise.** "Desktop active" means the default target is `graphical.target` AND a
  display manager is enabled or active. `graphical.target` alone never counts: it is the Debian
  default and Lite images may report it too (unverified, check on mcapp.local before implementing).
  Installed desktop packages alone no longer abort (and the `rc` false positive is fixed).
- **A persisted profile (`headless` | `desktop`) decides which tuning runs.** The Zero-2W tuning
  (`gpu_mem=16`, `cma-64`, `/tmp` tmpfs, service and package removal) is gated off on desktop boxes.
  `--converge` and the runner's `--skip` read the marker first and never prompt.
- **A box that was already tuned and becomes a desktop gets a revert path** for the McApp
  `config.txt` block. A pre-image helps only boxes tuned after this ships.
- **Epoch bump is conditional:** `SYSTEM_EPOCH` 7 only if `setup_system` output changes for existing
  headless boxes (the current design does not, so the recommendation is no bump; Q4). Four waves,
  one new bootstrap test suite.

## Problem

`check_desktop_image` (`bootstrap/lib/detect.sh:61`) is called at `bootstrap/mcapp.sh:733-736`
and exits 1 when `dpkg -l xserver-xorg-core` succeeds. It runs before the `--check` branch
(`mcapp.sh:742`), before `--converge` and `--skip` (`mcapp.sh:785`) and before every other phase,
so it blocks all modes.

- **Wrong population.** The message says "will cause out-of-memory errors on Pi Zero 2W (512MB
  RAM)" (`detect.sh:66-69`). It applies the Zero-2W limit to every board.
- **False positive (bug).** `dpkg -l <pkg>` exits 0 for a package in `rc` state (removed, config
  files kept), because it only checks for a status record. A Lite box that once
  had Xorg installed and removed again, or a Lite image with a leftover `xserver-xorg-core` record,
  is reported as a desktop image. Only the `ii` state means installed.
- **Wrong signal.** Package presence says nothing about whether the desktop runs. A Lite box with
  `xserver-xorg-core` pulled in as a dependency of some tool boots headless and is fine.
- **No RAM or hardware detection exists to build on.** `grep` for `MemTotal` and `device-tree` in
  `bootstrap/` finds nothing; the only hardware probe is `vcgencmd get_mem gpu` at
  `lib/system.sh:444-445`. (`uname -m` appears only as the fallback after
  `dpkg --print-architecture` in `ssl-tunnel-setup.sh:215,259`, an unrelated download-arch probe.)
- **The headless tuning is not desktop-safe either.** Lifting only the gate would let
  `setup_system` shrink a Pi 4 desktop to `gpu_mem=16` and `cma-64`, mount a 150 MB `/tmp`
  tmpfs under the browser, disable `udisks2` and purge packages. The gate and the tuning have to
  change together.
- **The gate also sits on unattended paths.** The update runner calls the active slot's bootstrap
  with `--converge` (`scripts/update-runner.py:565`, `:863`) and with `--skip`
  (`update-runner.py:455`), and the app's converge watchdog (`src/mcapp/system_converge.py:191-283`)
  re-triggers converge. Any new refusal or prompt on these paths breaks the update (a prompt
  hangs it, a refusal loops the watchdog), so the gate must be marker-first and prompt-free there.

## Decision table

Decided by the operator. `MemTotal` from `/proc/meminfo`.

| Desktop state (see Detection)                     | MemTotal < 1 GB                                                                          | 1 GB <= MemTotal < 2 GB                     | MemTotal >= 2 GB |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------- | ---------------- |
| Desktop active (graphical target AND DM)          | **Refuse** (exit)                                                                        | **Warn + confirm**; `--allow-desktop` skips | **Proceed**      |
| Desktop packages present, desktop not active      | Proceed + info                                                                           | Proceed + info                              | Proceed + info   |
| No desktop                                        | Proceed                                                                                  | Proceed                                     | Proceed          |
| `--headless` given (nothing else changes the row) | Set `multi-user.target`, write reboot-required, **exit**; re-run `mcapp.sh` after reboot | Proceed, headless (no prompt)               | Proceed headless |

- **Refusal is final under 1 GB for an install.** `--allow-desktop` does not override it; the
  Zero-2W OOM case stays protected. The only way forward on such a box is `--headless`: it
  changes the boot target, marks reboot-required and exits without installing anything. The operator
  reboots into `multi-user.target` and re-runs; detection then sees no desktop.
- **Real `MemTotal` is below the nominal size.** The kernel reports less than the marketing figure
  (a 512 MB Zero 2W reports about 415 MB per CLAUDE.md, "Memory Footprint"). A 2 GB board therefore
  reads roughly 1.8-1.9 GB (unverified), and a literal `>= 2097152 kB` would put every 2 GB Pi in
  the confirm band. Thresholds are named constants set below the nominal values (see Open
  questions, Q1).
- **The profile follows the decision.** Desktop active and allowed: `desktop`. Everything else:
  `headless`.

## Detection design

```bash
# Proposed: bootstrap/lib/detect.sh, next to check_desktop_image (which is replaced).
MCAPP_PROC_MEMINFO="${MCAPP_PROC_MEMINFO:-/proc/meminfo}"
MCAPP_DT_MODEL="${MCAPP_DT_MODEL:-/proc/device-tree/model}"

get_mem_total_kb() {            # prints integer kB, 0 if unreadable or MemTotal missing
  local v
  v="$(awk '/^MemTotal:/ { print $2; exit }' "$MCAPP_PROC_MEMINFO" 2>/dev/null)"
  echo "${v:-0}"                # awk exits 0 with empty output on a file without MemTotal
}

get_board_model() {             # log line only, never a decision input
  tr -d '\0' < "$MCAPP_DT_MODEL" 2>/dev/null || echo "unknown"
}

desktop_packages_installed() {  # 'ii' only; 'rc' / 'un' do not count
  local p s
  for p in xserver-xorg-core labwc wayfire; do
    s="$(dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null)" || continue
    [[ "$s" == ii* ]] && return 0
  done
  return 1
}

desktop_active() {              # BOTH signals; graphical.target alone never counts
  [[ "$(systemctl get-default 2>/dev/null)" == "graphical.target" ]] || return 1
  systemctl is-enabled --quiet display-manager.service 2>/dev/null \
    || systemctl is-active --quiet display-manager.service 2>/dev/null
}

have_tty() {                    # /dev/tty can exist yet fail to open (ENXIO)
  { : </dev/tty; } 2>/dev/null  # under systemd-run or the update runner
}
```

- **Primary signal: graphical default target AND a display manager.** `systemctl get-default`
  returning `graphical.target` is necessary, not sufficient. Debian's default target is
  `graphical.target`, and a Raspberry Pi OS Lite image may well report it as well (unverified: could
  not be checked on mcapp.local; check there before implementing). Counting the target alone
  would classify every marker-less Zero 2W as a desktop, refuse it under 1 GB, break the runner's
  `--converge`/`--skip` and loop the converge watchdog. The DM requirement is what separates a real
  desktop from a Lite image with the Debian default target.
- **A DM that is active while the default target is `multi-user.target`** (started by hand) is not
  counted; it is a transient state and the info line covers it. A desktop that is running because it
  was switched at runtime is rare on this population.
- **Secondary signal: package state `ii`** of `xserver-xorg-core`, `labwc`, `wayfire`. Used only for
  the info line ("desktop packages present, desktop not active: proceeding as headless"),
  for `--check` output, and for the purge guard (see profile table).
- **Why three packages.** Older Raspberry Pi OS desktops are X11 (`xserver-xorg-core` is what the
  current check keys on). Bookworm moved to Wayland with `wayfire`, and labwc became Bookworm's
  default in late 2024 (unverified; Trixie also unverified: not checked against a desktop image
  from this tree). The package list is deliberately secondary so a wrong guess costs an info line,
  not a refusal.
- **Display manager unit name.** `display-manager.service` is the Debian alias that resolves to
  `lightdm` on Raspberry Pi OS (unverified on Trixie, and `is-enabled` on the alias is unverified
  too). Do not hard-code `lightdm`.
- **Model is logged, never decided on.** `/proc/device-tree/model` is read once for the log line
  (`Board: Raspberry Pi 4 Model B Rev 1.4, 3.7 GB`). A NUL byte terminates the string, hence `tr -d`.
  A board without the file (possibly non-Pi, as in the epoch-6 ramlog work) logs `unknown`; the
  RAM policy still applies.
- **Fail closed on unreadable RAM.** `get_mem_total_kb` returning 0 is treated as under 1 GB when a
  desktop is active, so an odd environment never silently takes the permissive path.

## Headless vs desktop profile: every bootstrap step

`setup_system` (`lib/system.sh:10-26`) is the single list; the profile only changes what it calls.

| Step (function)                                              | Location                 | Desktop profile | Why                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------ | ------------------------ | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `disable_unused_services`                                    | `system.sh:79-125`       | **Partial**     | Skip only `cups`, `cups-browsed`, `ModemManager`, `udisks2` (printing, WWAN, automount). `cloud-init-*`, `cloud-config`, `cloud-final`, `triggerhappy`, `e2scrub_reap`, `serial-getty@ttyS0`, `e2scrub_all.timer`, `man-db.timer` stay DISABLED as today (`system.sh:83-103`).                     |
| `remove_bloat_packages` (camera purge + autoremove)          | `system.sh:132-156`      | **Skip**        | It purges `rpicam-apps-core`, `librpicam-app1`, `libcamera-ipa`, `libcamera0.6/0.7` and then runs `autoremove --purge`, which removes whatever the desktop pulled in. Skip whenever any desktop package is `ii` (state packages would be removed). Recommended: skip in the whole desktop profile. |
| `configure_tmpfs` (`/tmp` 150M, `/var/log` 30M)              | `system.sh:210-256`      | **Skip**        | A browser or file manager outgrows a 150 MB `/tmp`. Open question Q2 on `/var/log` alone.                                                                                                                                                                                                          |
| `configure_boot_memory` / `_mcapp_configure_config_txt`      | `system.sh:297-445`      | **Split**       | `gpu_mem=16`, `vc4-kms-v3d,cma-64`, `dtparam=audio=off` break or starve a desktop GPU and audio: the `config.txt` half is skipped. The `cmdline.txt` half is kept (next row).                                                                                                                      |
| `_mcapp_configure_cmdline_txt`                               | `system.sh:357-370`      | **Keep**        | `cgroup_enable=memory` costs nothing and is harmless on a desktop. It is called inside `configure_boot_memory` (`system.sh:415-425`), so it must be split out (see shape below).                                                                                                                   |
| `configure_journald`                                         | `system.sh:514-569`      | **Adapt**       | Drop-in and 16M cap stay. The bind-mount line does not: see shape below.                                                                                                                                                                                                                           |
| `configure_logrotate`                                        | `system.sh:624`          | **Keep**        | RAM-neutral.                                                                                                                                                                                                                                                                                       |
| `configure_firewall` / nftables                              | `system.sh:645`, `659`   | **Keep**        | Security posture is not a headless-only property.                                                                                                                                                                                                                                                  |
| `configure_bluetooth` (rfkill unblock unit)                  | `system.sh:950`          | **Keep**        | A desktop also needs Bluetooth for McApp's BLE path.                                                                                                                                                                                                                                               |
| `disable_ipv6`                                               | `system.sh:1008`         | **Keep**        | Unrelated to the desktop (Open question Q3).                                                                                                                                                                                                                                                       |
| `configure_unattended_upgrades` (shutdown hook off)          | `system.sh:1043`         | **Keep**        | Saves 7 MB RSS (CLAUDE.md); apt timers keep upgrading.                                                                                                                                                                                                                                             |
| `configure_wpasupplicant_pin`                                | `system.sh:581`          | **Keep**        | Network safety, every board (epoch 5).                                                                                                                                                                                                                                                             |
| `hold_network_packages`, apt under `systemd-run`, link guard | `packages.sh:125-243`    | **Keep**        | Network safety; the desktop's NetworkManager is exactly what the hold protects.                                                                                                                                                                                                                    |
| `ensure_apt_swap`                                            | `packages.sh:42-59`      | **Keep**        | Temporary 256 MB swap, removed afterwards; harmless.                                                                                                                                                                                                                                               |
| `configure_fast_ssh_login`, `configure_ssh_hardening`        | `system.sh:1102`, `1138` | **Keep**        | Not RAM related.                                                                                                                                                                                                                                                                                   |

- **Implementation shape.** `setup_system` reads `MCAPP_PROFILE` and branches the gated calls. Desktop
  mode logs one line per skipped step, so the bootstrap log shows what did not run. Four points are
  not a plain `if` around a call:
  1. **`disable_unused_services` takes the profile**, builds its service list conditionally and
     keeps the rest of the loop unchanged.
  2. **`configure_boot_memory` is split.** `_mcapp_configure_cmdline_txt` is called from inside it
     (`system.sh:415-425`), so "skip boot memory, keep cmdline" is impossible as is. Proposal: a new
     `configure_boot_cmdline` (cgroup, both profiles) and `configure_boot_memory` reduced to
     `config.txt` (headless only).
  3. **The reboot-marker bookkeeping needs its own home.** Skipping `configure_boot_memory` also
     skips the "changed => touch reboot-required / unchanged and live => clear stale marker" tail
     (`system.sh:430-435`). Move it to a shared `_mcapp_sync_reboot_marker` called after both boot
     functions in both profiles; `_mcapp_boot_memory_live` (`system.sh:442`) takes the profile and
     tests only `cgroup_enable=memory` on desktop (no `gpu=16M`).
  4. **`configure_journald` must not bind-mount on a desktop.** The journal bind line is anchored
     after `^tmpfs /var/log tmpfs` (`system.sh:533`). Without the McApp tmpfs block (skipped on
     desktop) `_mcapp_fstab_ensure_line` appends the line at the end of fstab (`system.sh:470`) and
     the bind mount lands on top of the desktop's on-disk `/var/log/journal`. On desktop, skip the
     bind (and its `mkdir`/`mount`) whenever `/var/log` is not tmpfs, exactly as the ramlog branch
     does (`_mcapp_journal_ramlog_cleanup`); the 16M drop-in still applies. That condition is a
     testable function (`findmnt` behind an env override).
- **The shared (Keep) steps run in both profiles**, so a desktop box still gets the network, firewall
  and journal-cap guarantees. The profile is a subtraction, not a second code path.
- **The runtime does not depend on the tuning.** McApp itself needs none of the gated steps; they
  are footprint measures (B4).

## Persistence, converge and revert

`--converge` re-runs `setup_system` whenever `SYSTEM_EPOCH` outruns the marker
(`mcapp.sh:748-766`), non-interactively and often from the update runner. The runner also calls
`mcapp.sh --skip` (`update-runner.py:455`), which reaches the same gate at `mcapp.sh:734` before the
`--skip` branch (`mcapp.sh:785`). Re-deciding there would ask a question nobody can answer and could
flip a box's profile on the strength of a transient `systemctl` state.

- **Marker: `/var/lib/mcapp/system-profile`**, one word, `headless` or `desktop`, beside
  `system-epoch` (`mcapp.sh:138`). Read/write helpers live in `mcapp.sh` next to
  `write_system_epoch` (`mcapp.sh:163`), owned by W1. Written after a successful setup, never by
  `--check` and never by `--skip` (which runs no setup).
- **Resolution order, one function `resolve_system_profile`:**
  1. `--headless` flag: `headless`.
  2. Marker present: that value, no probing.
  3. No marker (fresh install, or an existing epoch-6 box): run the Detection design and the
     decision table.
- **`--converge` and `--skip` share one rule: marker-first, no prompt, no TTY needed.** The runner
  has no TTY, and `have_tty` fails there. With no marker and an active desktop at 1-2 GB, both
  resolve to `desktop` with a WARN (skipping tuning is the non-destructive side; `--converge`
  records it in the marker). Under 1 GB they exit non-zero as today. Only an interactive run (TTY
  present, no flag) asks.
- **Existing epoch-6 Zero 2W fleet:** no marker, `desktop_active` false (default target
  `multi-user.target`, or `graphical.target` without a DM), resolves `headless`, behaviour
  identical to today. This is the regression the tests pin first.
- **An explicit override persists.** `--allow-desktop` on an interactive run writes `desktop`;
  a later `--headless` run rewrites it to `headless` and applies the tuning.

### Revert of an already-tuned box

A box tuned as headless that is later turned into a desktop (marker `headless` or tuned before the
marker existed, operator reinstalls a desktop and re-runs with `--allow-desktop`) carries the
McApp block in `config.txt`.

| Artifact                                      | Revertable today? | Proposal                                                                                                                                                                                                                                                       |
| --------------------------------------------- | ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `# McApp boot memory` block with `gpu_mem=16` | Yes               | Remove the begin/end block **and the `[all]` header written just before it**: `_mcapp_configure_config_txt` appends a blank line and `[all]` OUTSIDE the markers (`system.sh:337-341`), so the markers alone leave a stray `[all]` that shadows later content. |
| `dtoverlay=vc4-kms-v3d,cma-64`                | **No**            | Without a pre-image: rewrite to `dtoverlay=vc4-kms-v3d` (accepted risk, below). With one: restore the original line.                                                                                                                                           |
| `dtparam=audio=off`                           | **No**            | Without a pre-image: restore to `on` (accepted risk, below). With one: restore the original line.                                                                                                                                                              |
| `cgroup_enable=memory cgroup_memory=1`        | Not needed        | Leave; harmless.                                                                                                                                                                                                                                               |
| fstab `# McApp tmpfs` block                   | Yes               | Remove the begin/end block only on explicit `--allow-desktop` transition.                                                                                                                                                                                      |
| Disabled services, purged rpicam packages     | No                | Report only. Not reinstalled; the operator re-enables by hand.                                                                                                                                                                                                 |

- **Accepted risk: revert by value cannot tell McApp's `cma-64` / `audio=off` from the operator's
  own.** The sed rewrites only an un-parameterized overlay line and `audio=on` (`system.sh:325,328`),
  so the written values are known, but a hand-set `audio=off` or `cma-64` looks identical afterwards.
  Without a pre-image the revert flips such a line anyway; this is accepted unless Q5 adds a
  pre-image. Mitigation: restore only lines that still carry the exact McApp-written value, and log
  each changed line.
- **Pre-image, scoped to what is actually saveable.** No backup is kept today
  (`system.sh:318-351` edits a temp copy and overwrites). Proposal: save
  `/var/lib/mcapp/config.txt.pre-mcapp` once, **only when `config.txt` has no
  `# McApp boot memory` block yet**, i.e. on the first tune of an untouched file. Every existing box
  is already tuned, so none ever gets one; they keep the inference rows. Saving it from an
  already-tuned file would store McApp's own values as "original".
- **Revert marks reboot-required** via `/var/lib/mcapp/reboot-required` (shared helper above), and
  `_mcapp_boot_memory_live` takes the profile, or it keeps testing for `gpu=16M` and the marker
  never clears.
- **Revert is a function, not a default.** It runs only on an explicit transition (marker
  `headless`, resolved `desktop`, operator opted in). A `--converge` never reverts.
- **Risk: a desktop box that Activates a pre-new-code slot.** The runner runs the ACTIVE slot's own
  bootstrap. A desktop box (marker `desktop`) that Activates an older slot gets that slot's old
  `check_desktop_image`, the hard abort, on every later `--skip`/`--converge`. Not fixable from the
  new code; documented, and the update page should not hide the failing phase.

## CLI and UX

New flags in `parse_args` (`mcapp.sh:556-600`, which today knows `--check --force --reconfigure
--fix --skip --converge --dev --tag --ref --quiet --version --help`):

| Flag              | Effect                                                                                                                                                              |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--allow-desktop` | Skip the 1-2 GB confirmation. Never overrides the under-1 GB refusal.                                                                                               |
| `--headless`      | Profile `headless`; `systemctl set-default multi-user.target`; reboot-required. Installs and uninstalls nothing, but the headless tuning (incl. camera purge) runs. |

- **`--headless` "uninstalls nothing" is only true of the flag itself.** The headless profile runs
  `remove_bloat_packages`, which purges the rpicam/libcamera packages and runs `autoremove --purge`
  (`system.sh:135-155`). On a box whose desktop packages are still `ii` that autoremove can take
  state packages with it. Rule: skip the purge whenever any desktop package is `ii`, regardless of
  profile. (Recommended; the alternative is to accept the purge and say so in the `--headless` help.)
- **Do not reuse `--force`.** `FORCE` is consumed only by `deploy_app` (`mcapp.sh:179`, `561`,
  `845`): it means "redeploy". Making it also waive a RAM safeguard would let an operator who only
  wanted a redeploy skip a memory warning. The operator suggested `--allow-desktop`; this keeps it.
- **`--headless` does not stop the running desktop.** It sets the default target and marks
  reboot-required, so an SSH session on a graphical box is never cut. Under 1 GB it exits after
  that (decision table); at 1 GB and above it continues with the headless tuning. `set-default` is
  reversible with `systemctl set-default graphical.target`.
- **Prompts read `/dev/tty`**, like `lib/config.sh:37,77,300`, so `curl | bash` works. They are
  offered only when `have_tty` succeeds (opening, not mere existence). With no TTY and no flag the
  1-2 GB install case refuses and names `--allow-desktop`; `--converge`/`--skip` never reach it
  (marker-first above).

Messages (BLUF in the first line, then the fix):

```text
[WARN] Desktop detected on Raspberry Pi 4 Model B (MemTotal 1.8 GB).
       McApp runs, but a desktop leaves little headroom at this size.
       Continue as desktop profile (no memory tuning)? [y/N]
       Skip this prompt with --allow-desktop, or run headless with --headless.

[ERROR] Desktop detected on Raspberry Pi Zero 2 W (MemTotal 0.4 GB).
        Under 1 GB a desktop plus McApp runs out of memory.
        Reflash Raspberry Pi OS Lite, or boot this image headless:
        sudo ./mcapp.sh --headless (sets multi-user.target), reboot, then re-run.

[INFO] Desktop packages present (labwc) but no active desktop:
       treating the box as headless.
```

`dry_run_report` (`mcapp.sh:867`) has no desktop section today. `--check` should print the
decision without prompting:

```text
System profile:
  board:          Raspberry Pi 4 Model B Rev 1.4
  MemTotal:       3.7 GB
  default target: graphical.target (display manager: active)
  desktop pkgs:   labwc (ii)
  decision:       proceed, profile=desktop (installed marker: none)
  would skip:     disable_unused_services(cups,udisks2), remove_bloat_packages,
                  configure_tmpfs, configure_boot_memory
```

- **`--check` on a 1-2 GB desktop** prints `decision: would ask to confirm (use --allow-desktop)`
  and exits 0; it must not block on a prompt.

## Epoch and tests

- **Bump only if needed.** The profile marker does not change `setup_system` output on an existing
  headless box (it resolves `headless`, identical call list), and the pre-image is saved only on
  an untuned file. So no bump is required; a bump forces every Zero 2W through one extra converge
  for no behavioural gain (Q4). If the design changes so that it is needed: `SYSTEM_EPOCH` 6 to 7 in
  `bootstrap/mcapp.sh:43` and `REQUIRED_SYSTEM_EPOCH` in `src/mcapp/system_converge.py:47`, plus the
  comment block above (`mcapp.sh:38-42`), **in the same wave and commit**; the parity test
  `scripts/system_converge_tests.py` fails otherwise.
- **Keep the function name `check_desktop_image`.** It is in `REQUIRED_LIB_FUNCTIONS`
  (`mcapp.sh:470`), the canary that rejects a lib tree that is too old. Changing semantics under
  the same name is safe: a new `mcapp.sh` against an old `lib/` still passes the canary but runs the
  old check (no worse than today). Renaming it would force an updated canary and make old
  `--tag`-pinned slots, whose `mcapp.sh` lists the old name, reject the new lib.

New suite `scripts/bootstrap_desktop_gate_tests.py`, registered in `main()` of
`scripts/run_startup_tests.py` beside `run_bootstrap_ramlog_tests` (imported at line 30, run at
`:389-390`, gated in the final all-pass expression at `:478`). Same approach as
`bootstrap_ramlog_tests.py`: real bash functions sourced from `lib/`, stub binaries on `PATH`,
redirected paths (`bootstrap_ramlog_tests.py:35-60,68-122`).

- **Stubs:** `systemctl` (`get-default`, `is-active`, `is-enabled` driven by env), `dpkg-query`
  (status per package), `/proc/meminfo` and `/proc/device-tree/model` through the new
  `MCAPP_PROC_MEMINFO` / `MCAPP_DT_MODEL` variables, `/dev/tty` via the `have_tty` seam.

| #   | Case                                                                      | Expected                                                                                           |
| --- | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| D1  | `xserver-xorg-core` in `rc`, default target multi-user                    | Not a desktop (pins the false-positive fix)                                                        |
| D2  | `xserver-xorg-core` `ii`, default target multi-user, 0.4 GB               | Proceed + info line, profile headless                                                              |
| D3  | graphical target + DM, 0.4 GB                                             | Refuse, exit non-zero, even with `--allow-desktop`                                                 |
| D4  | graphical target + DM, 1.5 GB, flag absent, no TTY                        | Refuse, message names `--allow-desktop`                                                            |
| D5  | graphical target + DM, 1.5 GB, `--allow-desktop`                          | Proceed, profile desktop                                                                           |
| D6  | graphical target + DM, 1.9 GB / 3.7 GB                                    | Proceed, profile desktop (boundary per Q1)                                                         |
| D7  | Unreadable `/proc/meminfo` or no `MemTotal` line, graphical target + DM   | `get_mem_total_kb` prints 0; fail closed (refuse)                                                  |
| D8  | Active `display-manager.service`, default target multi-user               | Not desktop active (info only)                                                                     |
| D9  | Marker `desktop`, `--converge`, any detection output                      | Profile desktop, no probing, no prompt                                                             |
| D10 | Marker absent, headless Zero 2W (today's fleet)                           | Profile headless, `setup_system` call list identical to epoch 6                                    |
| D11 | Profile desktop                                                           | `config.txt`, fstab, package list untouched; only the four named services skipped                  |
| D12 | Headless to desktop transition with `--allow-desktop`                     | McApp block AND its preceding `[all]` removed, cma/audio restored, reboot marker; second run no-op |
| D13 | `--headless` on a graphical box, 3.7 GB / 0.4 GB                          | `set-default multi-user.target` called, no `stop`/`isolate`, reboot marker; 0.4 GB exits after     |
| D14 | `--check` on each RAM band                                                | Report text matches, no prompt, exit 0                                                             |
| D15 | Board model unreadable                                                    | Logged `unknown`, decision unchanged                                                               |
| D16 | `--skip` as the runner calls it: no TTY, marker `desktop`, desktop active | No prompt, no refusal, reaches the deploy branch                                                   |
| D17 | `--skip`, no TTY, no marker, 1.5 GB desktop active                        | No prompt; resolves desktop with WARN; under 0.4 GB exits non-zero                                 |
| D18 | Lite image: `graphical.target` default, no display manager (marker-less)  | Headless, proceed (the pin against refusing the whole Zero 2W fleet)                               |
| D19 | `/dev/tty` exists but fails to open                                       | `have_tty` false; no prompt attempted                                                              |
| D20 | Pre-image on an already-tuned `config.txt` (has the McApp block)          | `config.txt.pre-mcapp` NOT written; on an untuned file it is written exactly once                  |
| D21 | Desktop profile, any desktop package `ii`                                 | `remove_bloat_packages` not run (no purge, no autoremove)                                          |
| D22 | Desktop profile, `/var/log` not tmpfs                                     | No journal bind line in fstab; 16M drop-in still written; cmdline cgroup still applied             |

- **Mutation pins:** D1 must fail against the current `dpkg -l` check; D3 must fail if the refusal
  moves behind the flag; D10 and D18 must fail if the gate is applied without a marker or counts
  the target alone; D16 must fail if `--skip` prompts or refuses before the marker is read.
- Per repo rules the suite must prove it fails against the pre-change `lib/` (a
  `BOOTSTRAP_DESKTOP_LIB_DIR` override, as the ramlog suite does).

## Risks

- **Misdetected profile skips tuning on a Zero 2W.** Needs a 512 MB board with an active graphical
  target and a display manager, which the refusal catches first. The fail-closed RAM read covers the
  remaining hole.
- **Misdetected profile applies tuning to a desktop** (Wayland package names wrong, DM unit name
  differs). Mitigation: the primary signal is the systemd target plus the DM, not packages. If
  `is-enabled` on the DM alias misbehaves (unverified) the failure direction is headless tuning on
  a desktop, so the alias check needs a live check (Q6).
- **`set-default` on a headless-by-intent box can lock out a display.** `--headless` only moves
  toward `multi-user.target`, never the reverse; there is no `--desktop` flag.
- **Revert by inference may restore a value the operator had changed** (accepted risk, Persistence
  section); only a pre-image removes it, and only for boxes tuned after this ships.
- **Older slots keep the old hard abort** (Persistence section, last bullet).
- **Desktop boxes and `unattended-upgrades` kept running** can upgrade the desktop and
  NetworkManager mid-session; the network-safety hold (`packages.sh:125-243`) stays on for that
  reason.
- **Footprint regression is invisible on a desktop.** Stall tracking baselines (B4) were measured on
  a Zero 2W; a desktop box has a different baseline. Documented, not solved.
- **Pi 5 specifics unverified.** `gpu_mem` is ignored on a Pi 5 and its overlay set differs; the
  gating makes this moot for the desktop profile, but a Pi 5 Lite box still runs the headless
  tuning as today (unverified there).

## Out of scope

- Installing or uninstalling a desktop; `--headless` only changes the default target.
- Tuning the desktop (browser memory, compositor choice).
- A `--desktop` flag that forces graphical boot.
- Reinstalling packages or services removed by an earlier headless run.
- Non-Raspberry-Pi boards beyond logging the model (epoch-6 ramlog handling is separate).
- Changing the thresholds from `config.json` (GUI-only rule; this is a
  bootstrap-time decision).

## Open questions

Resolved and removed from this list: `--allow-desktop` vs `--force` (dedicated flag, operator
suggestion) and the non-interactive 1-2 GB desktop on `--converge`/`--skip` (resolves `desktop`,
see Persistence).

- **Q1. Threshold values.** Nominal 1 GB and 2 GB boards report less than nominal `MemTotal`.
  Proposal: refuse below 800000 kB, confirm below 1700000 kB (values unverified on real boards).
  Needs `/proc/meminfo` from a 1 GB, 2 GB and 4 GB Pi.
- **Q2. `/var/log` tmpfs on desktop.** The whole `configure_tmpfs` is skipped here. A 30 MB
  `/var/log` tmpfs would still save SD writes on a Pi 4 desktop; keep it and skip only `/tmp`? (That
  would also keep the journal bind valid and drop the `findmnt` special case.)
- **Q3. `disable_ipv6` on desktop.** Kept for now; a desktop user may expect IPv6.
- **Q4. Epoch bump or not.** Recommended: no bump (the design changes nothing for existing headless
  boxes). Bump to 7 only if a future change alters `setup_system` output for them.
- **Q5. Pre-image for `config.txt`.** Accept the new backup file (first-tune only), or revert by
  inference only with the accepted risk?
- **Q6. Live detection check on mcapp.local before W1.** Record `systemctl get-default`,
  `systemctl is-enabled display-manager.service` and `is-active` on a Lite Zero 2W, and the same on
  a Pi 4 desktop if one is at hand.

## Implementation sketch (waves)

| Wave | Scope                                                                                                                                                                                                                                                                                                                                  | Files (exclusive)                                                                                                                                                                                                                           |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1   | Detection + decision + marker helpers: `get_mem_total_kb`, `have_tty`, `desktop_active`, `desktop_packages_installed`, `resolve_system_profile`, profile marker read/write, new `check_desktop_image` semantics, `--allow-desktop`/`--headless` parsing, marker-first rule for `--converge` and `--skip`, `--check` section, help text | `bootstrap/lib/detect.sh`, `bootstrap/mcapp.sh`                                                                                                                                                                                             |
| W2   | Profile gating in `setup_system`: partial `disable_unused_services`, purge guard, `configure_boot_memory` split, shared reboot-marker helper, profile-aware `_mcapp_boot_memory_live`, journald bind guard                                                                                                                             | `bootstrap/lib/system.sh` (after W1)                                                                                                                                                                                                        |
| W3   | Revert path (block + `[all]`), `config.txt` pre-image, `--headless` target switch                                                                                                                                                                                                                                                      | `bootstrap/lib/system.sh` (after W2)                                                                                                                                                                                                        |
| W4   | Tests, registration, docs. Epoch bump only if Q4 flips: `SYSTEM_EPOCH` and `REQUIRED_SYSTEM_EPOCH` change here, in the same commit                                                                                                                                                                                                     | `scripts/bootstrap_desktop_gate_tests.py`, `scripts/run_startup_tests.py`, `src/mcapp/system_converge.py`, `bootstrap/mcapp.sh` (epoch line and comment only, if bumped), `doc/operations-reference.md`, `bootstrap/README.md`, `CLAUDE.md` |

- **Marker helpers belong to W1 only** (they live in `mcapp.sh`, which W1 owns first); W2/W3 only
  call them. W2 and W3 both edit `system.sh`, so they run in series; W1 can run in parallel with a
  test-skeleton agent only. W4 touches `mcapp.sh` after W1, never concurrently.
- **Verification per wave:** the new suite plus `bootstrap_ramlog_tests`,
  `bootstrap_network_safety_tests`, `bootstrap_pinning_tests`, `bootstrap_tmpdir_tests`,
  `system_converge_tests`; at the end `uv run python scripts/run_startup_tests.py`, ruff, mypy.
- **Field check after W4:** run `--check` on a Pi 4 desktop and on mcapp.local; confirm the latter
  reports `profile=headless` and an unchanged call list, and that an update-runner `--skip` and
  `--converge` pass on mcapp.local.
