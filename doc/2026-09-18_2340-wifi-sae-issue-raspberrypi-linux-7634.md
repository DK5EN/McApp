# Wi-Fi/SAE upstream report: raspberrypi/linux issue 7634

**Filed:** 2026-09-18 21:26 UTC as <https://github.com/raspberrypi/linux/issues/7634> (open, no
reaction as of 2026-09-19).
**Context:** `doc/2026-09-18_2320-wifi-outage-postmortem.md`, `doc/2026-09-18_2330-bootstrap-network-safety-plan.md`.
**Companion:** `doc/2026-09-18_2340-wifi-sae-debian-1147955-mail.txt` — the mail to
`1147955@bugs.debian.org` warning the Debian bug that carries the same patch.

Text below is the issue body as submitted, in the tracker's form sections.

---

### Describe the bug

Since `wpasupplicant 2:2.10-24+rpt1` (archive.raspberrypi.com, trixie, 2026-09-17) a Raspberry Pi Zero 2 W loses Wi-Fi permanently on a WPA2/WPA3 transition-mode access point. The link drops at the moment the package upgrade restarts `wpa_supplicant.service` and does not come back after a reboot. On a headless box this is a full loss of access.

Mechanism (all three parts verified, details below):

1. `2:2.10-24+rpt1` carries one patch, `0020-dbus-Fix-reporting-of-the-SAE-KeyMgmt.patch` (upstream 26c7f1bc, Debian #1147955). wpa_supplicant now lists `sae` in its D-Bus `KeyMgmt` capabilities when the driver sets `NL80211_FEATURE_SAE`.
2. brcmfmac in `rpi-6.18.y` sets `NL80211_FEATURE_SAE` when `BRCMF_FEAT_SAE_EXT` is enabled (`cfg80211.c`, `brcmf_setup_wiphy`). The shipped `/usr/lib/modprobe.d/rpi-brcmfmac.conf` (`feature_disable=0x282000`, firmware-nonfree 348a967) clears FWSUP (bit 13), SAE (bit 19) and DUMP_OBSS (bit 21), but **not SAE_EXT, which is bit 25 in the 6.18 driver's `BRCMF_FEAT_LIST`**. So the driver still advertises SAE: `wpa_cli get_capability auth_alg` prints `OPEN SHARED LEAP SAE` on the Zero 2 W.
3. NetworkManager 1.52.1 (trixie) appends ` SAE` to `key_mgmt` for every `wpa-psk` profile in station mode as soon as the supplicant reports SAE, PMF and BIP (`nm-supplicant-config.c`). wpa_supplicant 2.10 prefers SAE over PSK when both are offered, the BCM43430/1 firmware (`7.45.96.s1`, 2023-06-14) does not complete the external-auth SAE handshake, and NetworkManager never falls back to WPA2. The upstream NetworkManager fix that makes `pmf=disable` opt out in station mode (b00c6749, 2025-06-05) is only in NetworkManager 1.56+, so a trixie Pi has no configuration-level escape.

Before the upgrade, the old supplicant only reported `sae` from `capa.key_mgmt`, which brcmfmac never populates, so NetworkManager stayed on `WPA-PSK WPA-PSK-SHA256 FT-PSK` and everything worked. This is the same failure shape as #4976 (closed), where the community workaround `brcmfmac.feature_disable=0x82000` was the fix, and the Zero 2 W data point that was missing in #6130 / #7610.

### Steps to reproduce the behaviour

1. Raspberry Pi Zero 2 W, Raspberry Pi OS trixie (Lite), kernel `6.18.50+rpt-rpi-v8`, NetworkManager `1.52.1-1+rpt4`, firmware-brcm80211 `1:20260519-1~bpo13+1+rpt1`, connected via NetworkManager (`key-mgmt=wpa-psk`) to an AP in WPA2/WPA3 transition mode (Netgear Orbi here; `nmcli dev wifi list` shows `RSN-FLAGS pair_ccmp group_ccmp psk sae`).
2. `apt-get upgrade` to `wpasupplicant 2:2.10-24+rpt1`.
3. The postinst restarts `wpa_supplicant.service`; the station never re-associates. Reboot: same.
4. Roll back to Debian's `wpasupplicant 2:2.10-24` and hold it: NetworkManager logs `Config: added 'key_mgmt' value 'WPA-PSK WPA-PSK-SHA256 FT-PSK'` and the association completes within a second.

Control: a second Zero 2 W on the same SSID running bookworm (`wpasupplicant 2:2.10-12+deb12u3`, NetworkManager 1.42.4) associates with `key_mgmt=WPA2-PSK`, `pmf=1`.

### Device (s)

Raspberry Pi Zero 2 W

### System

```
Raspberry Pi Zero 2 W Rev 1.0
Debian GNU/Linux 13 (trixie), 6.18.50+rpt-rpi-v8
brcmfmac: Firmware: BCM43430/1 wl0: Jun 14 2023 07:27:45 version 7.45.96.s1 (gf031a129) FWID 01-70bd2af7 es7
wpasupplicant 2:2.10-24+rpt1 (broken) / 2:2.10-24 (works)
network-manager 1.52.1-1+rpt4
firmware-brcm80211 1:20260519-1~bpo13+1+rpt1, /usr/lib/modprobe.d/rpi-brcmfmac.conf = options brcmfmac roamoff=1 feature_disable=0x282000
```

### Logs

Last journal lines before the link died (shipped to a peer via systemd-journal-upload, the box itself has volatile logs):

```
22:26:53 sudo: COMMAND=/usr/bin/bash /tmp/bootstrap/mcapp.sh --dev --tag v2.0.10-dev.1   (runs apt-get upgrade)
22:27:42 /usr/sbin/wpa_supplicant replaced (inode ctime), /var/lib/dpkg/info/wpasupplicant.list 22:27:43
22:27:47 systemd[1]: Stopping wpa_supplicant.service - WPA supplicant...
         (nothing after this; NetworkManager's /var/lib/NetworkManager/timestamps kept the profile at 22:27:48 across the next two boots)
```

With the 2:2.10-24 binary, same boot config:

```
NetworkManager[535]: device (wlan0): state change: need-auth -> prepare
NetworkManager[535]: Config: added 'key_mgmt' value 'WPA-PSK WPA-PSK-SHA256 FT-PSK'
wpa_supplicant[536]: wlan0: Associated with 5a:af:97:2e:2b:8b
wpa_supplicant[536]: wlan0: CTRL-EVENT-CONNECTED - Connection to 5a:af:97:2e:2b:8b completed
```

Capabilities on the Zero 2 W (2:2.10-24, i.e. what the driver exposes):

```
$ sudo wpa_cli get_capability key_mgmt
NONE IEEE8021X WPA-EAP WPA-PSK WPA-EAP-SUITE-B OWE DPP FT-PSK FT-EAP
$ sudo wpa_cli get_capability auth_alg
OPEN SHARED LEAP SAE
```

The +rpt1 patch makes the second line leak into the first over D-Bus.

### Additional context

Suggested fixes, any one of which breaks the chain:

- **firmware-nonfree:** extend `rpi-brcmfmac.conf` to also clear `SAE_EXT`, i.e. `feature_disable=0x2282000` on 6.18 kernels, at least for the 43430 family. Note the mask is a raw bit index into `BRCMF_FEAT_LIST` and shifts when the enum grows; `SAE_EXT` did not exist when 0x282000 was chosen.
- **kernel:** do not set `NL80211_FEATURE_SAE` for chips/firmware where external SAE authentication is known not to work (43430/1 with 7.45.96.s1).
- **wpa packaging:** hold the D-Bus SAE-reporting patch back until the driver side no longer advertises SAE on those chips, or ship it together with the modprobe change.
- **network-manager (trixie):** backport b00c6749 so `wifi-sec.pmf=disable` opts a `wpa-psk` profile out of SAE transition mode in station mode. That would at least give a configuration-level workaround.

Current workaround on the affected box: `apt-mark hold wpasupplicant` at `2:2.10-24`, plus an apt pin. Alternatively `brcmfmac.feature_disable=0x2282000` on the kernel command line, per #4976, which I have not tested on this box.

Related: #4976 (identical symptom, Zero 2 W, closed with the feature_disable workaround), #6130, #7610 (SAE capability discussion, 2026-09-10, where a Zero 2 W data point was missing), #7528 (the issue the Debian bug references).
