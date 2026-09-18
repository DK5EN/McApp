# Bootstrap network safety (system epoch 5)

Follow-up to `doc/2026-09-18_2320-wifi-outage-postmortem.md`. Goal: a `mcapp.sh` run must never
take the box off the network, and when the network does go, the operator must be able to see why
from the box itself. Shipped 2026-09-18 on `development`, converged on mcapp.local the same night,
reported upstream as raspberrypi/linux issue 7634.

## What changed

| #   | Rule                                                                        | Where                                                                                                   |
| --- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 1   | Network-critical packages are held for the apt phase, upgrades reported     | `hold_network_packages` / `unhold_network_packages` / `report_deferred_network_upgrades`, `packages.sh` |
| 2   | wpasupplicant pinned to Debian 2:2.10-24 on trixie                          | `configure_wpasupplicant_pin`, `system.sh`; `SYSTEM_EPOCH` 5                                            |
| 3   | apt runs as a transient systemd unit, immune to a dropped session           | `run_apt_detached`, `packages.sh`                                                                       |
| 4   | Every run is mirrored to `/var/lib/mcapp/bootstrap.log` (one `.1` kept)     | `start_bootstrap_log`, `mcapp.sh`                                                                       |
| 5   | The default route must be back after apt, or the run stops with a diagnosis | `snapshot_link_state` / `verify_link_state`, `packages.sh`                                              |
| 6   | Persistent 16 MB journal on a bind mount under the tmpfs `/var/log`         | `configure_journald` + `_mcapp_fstab_ensure_line`, `system.sh`; `/var/lib/mcapp/journal`                |

Suite: `scripts/bootstrap_network_safety_tests.py`, six cases driving the real bash functions
against stub binaries, wired into `run_startup_tests.py`. Epoch parity is covered by the existing
`system_converge_tests.py`.

## Design notes

- **Hold, never skip.** `apt-mark hold` for the duration of `apt-get upgrade` keeps the rest of
  the system upgrading as before. A hold the operator already had (or that the pin story created)
  is detected via `apt-mark showhold` and never released by us. What was kept back is printed
  from `apt-get -s upgrade` with the console command to apply it.
- **The pin downgrades a box that already took +rpt1.** Priority 1001 lets apt downgrade, but
  rule 1 holds wpasupplicant during the in-session upgrade, so the downgrade is reported, not
  applied, and the operator does it from a console. That is deliberate: the downgrade restarts
  the supplicant too.
- **`systemd-run --wait --collect`** propagates the exit status of apt. Output goes to the
  bootstrap log (apt is `-qq` anyway); the last 20 lines are printed on failure. Without a
  booted systemd (`/run/systemd/system` absent) apt runs inline as before. The exit status is
  captured with `|| rc=$?`, not read after an `if`, which the suite pinned after the first
  version got that wrong.
- **`tee -p`** is what makes the log mirror safe: a plain `tee` dies of SIGPIPE when the ssh side
  of the pipe goes away and takes the bootstrap's stdout with it. GNU-only, so the mirror is
  skipped where `tee -p` is unavailable and the run behaves as before.
- **The link guard compares default-route interfaces, not IPs**, and waits up to 45 s because a
  NetworkManager reconnect after a supplicant restart takes about 10 s. A box that was offline
  before apt is not a failure. A changed `key_mgmt` is a warning, not a failure: it is exactly
  the signal that would have named the 2026-09-18 cause in one line.
- **Journal on a bind mount** keeps `/var/log` tmpfs for everything else. `/var/log/journal` is
  created by the existing tmpfiles entry in the tmpfs, and the fstab bind line sits directly after
  the tmpfs line inside the McApp block so systemd orders the two mounts by path. The journal
  drop-in replaces `mcapp-volatile.conf` with `mcapp-journal.conf` (`Storage=persistent`,
  `SystemMaxUse=16M`, `SystemMaxFileSize=4M`, one week retention). SD wear: journald syncs every
  five minutes, capped at 16 MB. That is the price of on-box evidence; the alternative, relying
  on the rpizero peer copy, worked on 2026-09-18 only because shipping ran until the second the
  link died.
- **Not done: detaching the whole run.** Wrapping the entire bootstrap in `screen` or
  `systemd-run --pipe` was considered and rejected for now: interactive config prompts, piped
  `curl | bash` mode and the update runner's stdout capture all interact with it, and rules 1, 3
  and 4 already cover the failure that happened.

## Verification

- Local gate: `uvx ruff check`, `uvx ruff format --check .`, `uv run mypy src/mcapp ble_service/src`,
  `uv run python scripts/run_startup_tests.py`, all green, new suite six of six.
- mcapp.local: `mcapp.sh --converge` from the local tree, epoch 4 to 5, health check all OK.
  The log shows `Link before apt: wlan0 (WPA2-PSK)`, `Holding network-critical packages for the
apt phase: network-manager` (wpasupplicant was already under the recovery hold), the pin
  written, the journald drop-in written, the bind line in fstab. Rebooted afterwards; see the
  post-reboot check in the same commit's report.

## Fleet

- rpizero.local: bookworm, not exposed to +rpt1, and not provisioned by this bootstrap.
- dk5en-14.local: check with `dpkg-query -W wpasupplicant` and `lsb_release -cs`; on trixie
  run the bootstrap's `--converge` once it carries epoch 5.

## Removal criteria for the pin

Any one of: the Orbi SSID switched to WPA2-only; a brcmfmac firmware for the 43430 family that
completes SAE; Raspberry Pi's `rpi-brcmfmac.conf` extended to clear `SAE_EXT` for those chips;
trixie's NetworkManager carrying b00c6749 so `wifi-sec.pmf=disable` opts out. Then drop
`configure_wpasupplicant_pin` from `setup_system`, delete the preferences file in the same
function for one more epoch, and `apt-mark unhold wpasupplicant` on mcapp.local by hand.
