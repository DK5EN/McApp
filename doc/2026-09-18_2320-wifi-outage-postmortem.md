# Post mortem: mcapp.local off the network after `mcapp.sh --dev` (2026-09-18)

**BLUF.** A routine `mcapp.sh --dev --tag v2.0.10-dev.1` run took mcapp.local off WiFi at
22:27:47 and it stayed off across a power cycle. Root cause: the bootstrap's unconditional
`apt-get upgrade` installed Raspberry Pi's `wpasupplicant 2:2.10-24+rpt1`, whose single patch
makes the supplicant advertise WPA3-SAE to NetworkManager. NetworkManager 1.52 then offers SAE
for every `wpa-psk` profile, the access point (ORBI63) runs WPA2/WPA3 transition mode, and the
Zero 2W's `brcmfmac43436` cannot complete an SAE handshake. Recovery was done offline on the SD
card: wpasupplicant rolled back to Debian's `2:2.10-24`, held and pinned. The box was back at
23:11. Total outage 44 minutes, no data loss. The outage was extended by 30 minutes by a
mistake during the offline repair (writes to an ext4 filesystem with an unreplayed journal).

## Timeline (CEST)

| Time  | Event                                                                                                                                                            |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 22:26 | `sudo bash /tmp/bootstrap/mcapp.sh --dev --tag v2.0.10-dev.1` started over SSH from the Mac.                                                                     |
| 22:27 | `setup_system` ran (five `daemon-reload` cycles, aiops-metrics, firewall). `install_packages` began, `apt-get upgrade` found one package: wpasupplicant.         |
| 22:27 | dpkg replaced `/usr/sbin/wpa_supplicant` (ctime 22:27:42), its postinst restarted `wpa_supplicant.service`. Last line ever shipped to rpizero, at 22:27:47.      |
| 22:28 | The bootstrap kept running past the link loss: `system-epoch` written at 22:28, dpkg status closed 22:27:51. The deploy phase needed GitHub and did not run.     |
| 22:31 | Operator power-cycled the Pi. It booted, McApp started on BLE, NetworkManager never associated (`timestamps` for the ORBI63 profile stayed at 22:27:48).         |
| 22:35 | Last DB write on that boot. Operator pulled power again and put the SD card into the Mac.                                                                        |
| 22:44 | Offline analysis via `debugfs`. dpkg consistent, filesystem clean, one upgraded package identified. Patch and NetworkManager source confirmed the mechanism.     |
| 22:58 | Rollback written to the card with `debugfs -w`. **Error:** the filesystem still had `needs_recovery` set from the 22:35 power cut; the journal was not replayed. |
| 23:01 | Boot attempt: the kernel refused the root mount on checksum mismatches. Superblock "last mount" stayed at 22:31:56.                                              |
| 23:06 | `e2fsck -fy` replayed the journal and repaired bitmaps. The replay had re-pointed `/usr/sbin/wpa_supplicant` at a stale inode; rewritten and re-verified.        |
| 23:11 | Boot. NetworkManager passed `key_mgmt WPA-PSK WPA-PSK-SHA256 FT-PSK`, association completed in under a second. All services active.                              |

## Root cause

Three layers, each necessary:

1. **wpasupplicant 2:2.10-24+rpt1** (Raspberry Pi, built 2026-09-17) carries exactly one
   change over Debian's 2:2.10-24: upstream patch `dbus: Fix reporting of the SAE KeyMgmt`
   (hostap `26c7f1bc`, Debian bug 1147955, raspberrypi/linux issue 7528). The D-Bus
   `Capabilities.KeyMgmt` list now includes `sae` when the driver sets `NL80211_FEATURE_SAE`,
   which brcmfmac does for external SAE authentication.
2. **NetworkManager 1.52.1** appends ` SAE` to the supplicant `key_mgmt` for every `wpa-psk`
   profile in station mode whenever the supplicant reports SAE, PMF and BIP
   (`nm-supplicant-config.c`, the `wpa-psk` branch). The `pmf=disable` escape hatch only applies
   in AP mode in this version, so there is no profile-level or `NetworkManager.conf` setting that
   turns this off.
3. **The AP and the chip.** ORBI63 advertises `psk sae` (WPA2/WPA3 transition mode). With SAE
   offered, wpa_supplicant 2.10 prefers it over PSK. The `brcmfmac43436` firmware on the Zero 2W
   advertises external SAE but does not complete the handshake, so every association attempt
   fails and NetworkManager never falls back to WPA2.

Control case: rpizero.local is the same hardware on the same SSID, still on bookworm's
wpasupplicant `2:2.10-12+deb12u3`, and associates with `key_mgmt=WPA2-PSK`.

Verified from the card: `/var/lib/dpkg/info/wpasupplicant.list` was the only package file list
touched that day; the old and new `.deb` differ in three binaries and the changelog; NetworkManager's
`timestamps` file showed the ORBI63 profile last active at 22:27:48 across both failed boots.

## Contributing factors

- **The bootstrap runs `apt-get upgrade -y` of the whole system inside the operator's SSH
  session** (`install_apt_deps` in `bootstrap/lib/packages.sh`). Any upgrade of wpasupplicant or
  network-manager restarts the link the session rides on. Nothing detaches the run, nothing logs
  it to disk, nothing holds network-critical packages back.
- **All logs are volatile by design** (`/var/log` is tmpfs, journald `Storage=volatile`). Across
  a power cycle nothing survives. The only record of the failure existed on rpizero, through the
  journal-upload shipping, and it ended at the exact second the link dropped. Without that peer
  copy the cause would have been guesswork.
- **Offline repair without replaying the ext4 journal.** The read-only `e2fsck -n` warning
  "journal recovery skipped" was cut off by a `tail`. Writing with `debugfs -w` onto a
  `needs_recovery` filesystem produced checksum mismatches, one failed boot, and a journal replay
  that later reverted one of the writes. Cost: about 30 minutes.
- **macOS raw device semantics.** `/dev/rdiskN` rejects the 1 KiB-offset superblock write with
  `EINVAL`, so `debugfs` closed with `ext2fs_close: Invalid argument` and the first `e2fsck -fy`
  could not clear the recovery flag. The buffered `/dev/diskN` node works.
- **The AP is in transition mode.** WPA3 on ORBI63 has been enabled for a while; nothing broke
  before because no client on the LAN had a supplicant that reported SAE together with a driver
  that could not do it.

## What was done

On the SD card, through `debugfs` after a full `e2fsck -fy` on `/dev/disk4s2`:

- `/usr/sbin/wpa_supplicant`, `/usr/sbin/wpa_cli`, `/usr/bin/wpa_passphrase` and the Debian
  changelog replaced with the files from `wpasupplicant_2.10-24_arm64.deb` (deb.debian.org),
  md5-verified after read-back, `root:root 0755`.
- `/var/lib/dpkg/status`: wpasupplicant set to `Version: 2:2.10-24`, `Status: hold ok installed`;
  `/var/lib/dpkg/info/wpasupplicant.md5sums` from the same package, so dpkg's view matches the
  files.
- `/etc/apt/preferences.d/wpasupplicant-pin`: `Pin: version 2:2.10-24`, priority 1001, with the
  reason as a comment. Belt and braces with the dpkg hold.
- One orphan remains: `lost+found/#376607` is the +rpt1 `md5sums` file the replay detached.
  Harmless, 2.8 KB, remove at leisure.

Temporary diagnostics, **to be reverted** once the follow-ups below are in:

- `/etc/fstab`: the `tmpfs /var/log` line commented out with a `DEBUG-2026-09-18` marker.
- `/etc/systemd/journald.conf.d/zz-debug-persistent.conf`: `Storage=persistent`,
  `SystemMaxUse=64M`.

## Current state (23:17)

- mcapp.local up since 23:11:30 on `netplan-wlan0-ORBI63`, `key_mgmt=WPA2-PSK`, `pmf=1`.
- `wpasupplicant 2:2.10-24 installed hold`; apt candidate is 2:2.10-24 (pin effective).
- mcapp, mcapp-ble, lighttpd, caddy, journal-upload and journal-remote active; `/health` OK;
  active slot `slot-1`; system epoch 4; no reboot-required marker.
- The v2.0.10-dev.1 deploy from the 22:26 run never happened; the slot symlink was untouched by it.

## Follow-ups

1. **Bootstrap** (see the plan in `doc/2026-09-18_2330-bootstrap-network-safety-plan.md` once
   written): never upgrade network-critical packages inside the session that drives the run;
   detach the system phase from the SSH session; log to disk; hold wpasupplicant on the fleet
   until the WPA3 situation is resolved; make the failure visible instead of silent.
2. **Fleet.** rpizero.local and dk5en-14.local sit on the same SSID. rpizero is on bookworm and
   not exposed to the +rpt1 build, dk5en-14 needs checking. Any trixie Pi with `brcmfmac43436`
   on a WPA2/WPA3 transition AP will lose WiFi on its next unattended-upgrades run that pulls
   wpasupplicant 2:2.10-24+rpt1.
3. **The real fix is upstream or on the AP.** Either the Orbi SSID goes WPA2-only, or brcmfmac
   firmware for the 43436 learns SAE, or NetworkManager grows a way to disable SAE for `wpa-psk`
   in station mode. Reported 2026-09-18 as raspberrypi/linux issue 7634 (the packaging gap:
   `rpi-brcmfmac.conf` clears SAE but not `SAE_EXT`, bit 25 in 6.18; NetworkManager's station-mode
   opt-out b00c6749 is in 1.56+ only). Debian bug 1147955 warned by mail. Until then the hold and
   pin stay.
4. **Revert the debug instrumentation** on mcapp.local (fstab tmpfs line, journald drop-in)
   after the bootstrap change ships, or fold a small persistent journal into the bootstrap
   deliberately. 16 MB of journal on the SD card per boot is the trade-off.
5. **Correct the parallel finding.** A same-day note attributed the SSH drop to a hardware
   watchdog reset. The evidence on the card rules that out: the box kept writing until the
   operator's power cycle, and the drop coincides with dpkg's restart of wpa_supplicant.

## Offline SD card repair, the rules learned

- **Always `e2fsck -fy` before any write.** A power-cut card carries `needs_recovery`. Writing
  underneath an unreplayed journal is corruption; the replay later overwrites metadata blocks
  including inodes just allocated.
- **On macOS use `/dev/diskNsM`, not `/dev/rdiskNsM`, for e2fsprogs writes.** The raw node
  needs aligned I/O and silently fails the superblock write.
- **Read the whole `e2fsck -n` output.** The journal warning is the first line, not the last.
- **After `debugfs write`, set mode, uid and gid explicitly** and verify by dumping the file back
  and comparing hashes. `debugfs` does not preserve ownership.
- **Volatile logs on the target mean the peer holds the evidence.** The rpizero journal-remote
  copy (`journalctl -D /run/journalxship`, needs `sudo -n`) is the first place to look when
  mcapp goes dark, and it explains why the AIOps log shipping is worth its cost.
