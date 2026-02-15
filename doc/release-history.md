# McApp Release History

## Changes since v1.2.0 (ea854e5)

**Date**: February 15, 2026
**Branch**: development
**Bootstrap version**: 2.2.2

---

### Bug Fixes

#### Parse negative temperatures in APRS weather fields (`b5ae31f`)

Regex patterns in `ble_protocol.py` used `[\d.]+` which silently dropped negative temperature values. Added `-?` to temperature capture groups in both the APRS position parser (`/T=` field) and the APRS telemetry parser. Stations reporting sub-zero temperatures now display correctly.

#### Stop conflicting services before activating (`59e0fe5`)

The bootstrap deploy phase now stops Caddy and any rogue `mcapp` processes before starting the systemd service. Prevents port conflicts and stale processes from blocking the new deployment.

#### Handle piped mode (`curl|bash`) for lighttpd template (`c728c99`)

When running the bootstrap via `curl -fsSL ... | sudo bash`, the lighttpd config template wasn't available because piped mode downloads library files but not templates. Added logic to download `templates/lighttpd.conf` from GitHub when running in piped mode.

#### Handle old installations with customized lighttpd.conf (`d6c9411`)

Existing Pi installations with manually edited lighttpd configurations are now handled gracefully. The bootstrap compares the current config against the template and only updates if proxy rules are missing, preserving user customizations. Added the lighttpd template file (`bootstrap/templates/lighttpd.conf`).

#### Fix `((removed++))` crash in bootstrap under `set -e` (`b56a45e`)

Bash arithmetic `((removed++))` returns exit code 1 when `removed` is 0 (because `0++` evaluates to 0, which is falsy). Under `set -e` this crashed the bootstrap. Fixed by using `removed=$((removed + 1))` instead.

#### Escape special chars in BLE API key for systemd unit (`c5c6747`)

Auto-generated BLE API keys containing characters like `%` or `$` broke systemd environment variable parsing. Added proper escaping when writing the key to the BLE service unit file.

#### Move locale config after apt upgrade in bootstrap (`4751882`)

Locale generation (`en_US.UTF-8`, `de_DE.UTF-8`) was running before `apt-get upgrade`, which could install locale packages that reset the configuration. Moved locale setup to after the upgrade step.

---

### Features

#### Automate release.sh — push, publish, include release notes (`0005ca8`)

The release script now pushes the commit and tag to the remote before creating the GitHub release, ensuring the remote tag points to the correct commit. Uses `doc/release-history.md` as release notes body and publishes immediately instead of creating a draft. One command, zero follow-up.

---

### Documentation

#### README overhaul (`84d6970`, `f880b7f`, `4611e17`, `f212d3e`, `eff41f9`)

- Added SD card flashing instructions with 4 Raspberry Pi Imager screenshots
- Added weather screenshot, arranged all screenshots in a 2x2 grid
- Added mHeard view description
- Removed outdated caveats about sensor data, weather, and in-memory database
- Modernized intro text, requirements section, and installation notes
- Compressed map screenshot from 4.9 MB to 3.7 MB

#### Release history reorganization (`b56a45e`)

Renamed `doc/release-history.md` to `doc/release-v1.2.0.md` to archive the v1.2.0 release notes and make room for ongoing release tracking.

---

### Chore

- Bumped bootstrap version to 2.2.2 (`14cd229`)
- Version bump to 1.3.0 then reverted to 1.2.0 for continued development (`d15d457`, `ede24b5`)

---

### Files Changed

| File | Change |
|------|--------|
| `src/mcapp/ble_protocol.py` | Fix negative temperature parsing |
| `bootstrap/lib/deploy.sh` | Stop conflicting services, escape BLE API key |
| `bootstrap/lib/packages.sh` | Lighttpd template handling (piped + upgrade), locale ordering |
| `bootstrap/lib/system.sh` | Fix arithmetic crash, locale ordering |
| `bootstrap/mcapp.sh` | Version bump to 2.2.2, conflicting service stop |
| `bootstrap/templates/lighttpd.conf` | New template file |
| `scripts/release.sh` | Automated push, publish, release notes |
| `README.md` | Major documentation overhaul |
| `doc/release-v1.2.0.md` | Renamed from release-history.md |
| `doc/wx.png` | New weather screenshot |
| `doc/1_Flash-PiZero2W.png` | New SD card flashing screenshot |
| `doc/2_OtherOS.png` | New SD card flashing screenshot |
| `doc/3_LightOS.png` | New SD card flashing screenshot |
| `doc/4_SelectCard.png` | New SD card flashing screenshot |
| `doc/map.png` | Compressed (4.9→3.7 MB) |

---

### Statistics

- **Commits**: 17 (including 1 merge)
- **Files changed**: 15
- **Lines inserted**: 173
- **Lines deleted**: 35
- **Net change**: +138 lines
- **Commit types**: 6 fixes, 1 feature, 5 docs, 3 chore, 1 merge
