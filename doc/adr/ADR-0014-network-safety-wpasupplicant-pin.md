# ADR-0014: Network safety: wpasupplicant pin, held network packages

| Field | Value |
|---|---|
| Status | Accepted. Pin in force until the removal criteria in the plan hold. |
| Date | 2026-09-18 (system epoch 5) |
| Deciders | Martin Werner |
| Affects | `bootstrap/lib/packages.sh` (`hold_network_packages`, `configure_wpasupplicant_pin`, `run_apt_detached`), `bootstrap/lib/system.sh` (`verify_link_state`, `configure_journald`) |
| Supersedes | the unrestricted in-session `apt-get upgrade` |
| Superseded by | - |

## Context

On 2026-09-18 a bootstrap run in an SSH session upgraded wpasupplicant to Raspberry Pi's
`2:2.10-24+rpt1`. Its patch makes the supplicant advertise WPA3-SAE. NetworkManager 1.52 then forces
SAE for every `wpa-psk` profile. The Zero 2 W's brcmfmac43436 never completes SAE against a
WPA2/WPA3 transition-mode access point, so mcapp.local dropped off the network
(`doc/archive/2026-09-18_2320-wifi-outage-postmortem.md`, upstream raspberrypi/linux#7634).

## Decision

1. **wpasupplicant and network-manager are never upgraded inline.** `hold_network_packages` puts them on
   `apt-mark hold` for the apt phase. `report_deferred_network_upgrades` prints what was kept back.
   An operator's existing hold is never released.
2. **wpasupplicant is pinned to Debian `2:2.10-24` on trixie**, priority 1001
   (`/etc/apt/preferences.d/mcapp-wpasupplicant`). The rest of the system still upgrades.
3. **apt runs under `systemd-run --wait --collect`**, so a dropped SSH session cannot interrupt dpkg.
   The exit status is captured with `|| rc=$?`.
4. **The default route must be back after the apt phase** (`verify_link_state`, 45 s), or the run stops.
5. **A changed `key_mgmt` is warned about**, naming the 2026-09-18 cause.
6. **The journal is persistent** (16 MB, bind-mounted from `/var/lib/mcapp/journal`), so the next outage
   leaves evidence. Boards with an armbian or orangepi ramlog service are excluded (epoch 6).

## Consequences

- Security updates to the rest of the system still apply on every run.
- The Pi cannot take the newer wpasupplicant until the removal criteria hold: NetworkManager 1.56 or
  newer, or a driver change. Until then the pin stays, and so does its warning.
- Rule for changes here: do not "simplify" the hold into a skipped upgrade. That would stop the rest
  of the system from upgrading too.

## Alternatives considered

- Skip the upgrade entirely: rejected, leaves the system unpatched.
- Pin on NetworkManager configuration: rejected, NM 1.52 has no configuration-level opt-out for SAE in
  station mode on trixie.

## Evidence

- `../archive/2026-09-18_2320-wifi-outage-postmortem.md`
- `../archive/2026-09-18_2330-bootstrap-network-safety-plan.md`
- `../archive/2026-09-18_2340-wifi-sae-issue-raspberrypi-linux-7634.md`
- `../policy/POL-004-change-guards.md` (guards and their verification commands)
