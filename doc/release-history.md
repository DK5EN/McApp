# McApp Release History

**Release Period**: February 3 - February 15, 2026 (12 days)

This document summarizes all changes made to McApp (backend) and webapp (frontend) in preparation for the first production release.

---

## 📋 Table of Contents

- [Repository Rebranding](#repository-rebranding)
- [BLE System Overhaul](#ble-system-overhaul)
- [Database & Storage](#database--storage)
- [Bootstrap & Deployment](#bootstrap--deployment)
- [Frontend UI/UX](#frontend-uiux)
- [Security & Networking](#security--networking)
- [Bug Fixes & Stability](#bug-fixes--stability)
- [Documentation](#documentation)
- [Performance Optimizations](#performance-optimizations)

---

## 🏷️ Repository Rebranding

**McAdvChat → McApp**

- Renamed repository from `McAdvChat` to `McApp` across entire codebase
- Updated all documentation, scripts, and configuration files
- Changed GitHub repository name to `github.com/DK5EN/McApp`
- Updated release scripts, deploy scripts, and systemd service files
- Aligned tarball naming and directory structure with new name

**Commits**:
- Backend: `3bea885`, `78a8447`, `6f63a65`, `1671147`
- Frontend: `31a0431`

---

## 🔵 BLE System Overhaul

### Architecture Changes

- **Removed local BLE mode** - deprecated direct D-Bus access from main package
- **Remote BLE service only** - BLE hardware access now via standalone `ble_service/`
- **Extracted protocol decoders** - created `ble_protocol.py` shared module
- **Removed `dbus-next` dependency** from main package (moved to `ble_service/` only)

### Phase 1: Critical Protocol Fixes (Feb 6-7)

- Fixed binary message parsing and header extraction
- Corrected GATT notification handling
- Added comprehensive BLE command reference (A0 commands)
- Fixed FCS (Frame Check Sequence) validation
- Downgraded noisy logging (binary messages, FCS mismatches) to DEBUG level

**Key commits**: `803fb5d`, `e55d5f5`, `e8778d1`, `e8be771`

### Phase 2: Enhanced Reliability (Feb 7-8)

- **Save & Reboot** - implemented 0xF0 command for config persistence
- **Automatic time sync** on BLE connection
- **Retry logic** for failed commands
- **MTU size validation** (prevents message truncation)

**Key commits**: `cbb3eeb`, `7b9d9be`, `38477a5`, `eaf038e`

### Phase 3: Extended Features (Feb 8-9)

- **Extended register queries** - added IO, TM, S1, S2 registers
- **Register caching** - persists across SSE reconnects
- **WiFi settings display** in frontend
- **Sensor and network/power config** display (S1/S2 registers)
- **BLE state machine** with proper connection lifecycle management

**Key commits**: `68c9f92`, `1b010f2`, `4837881`, `d77c866`, `4145607`, `168ad90`
**Frontend commits**: `585e375`, `6619d1f`

### Phase 4: Production Polish (Feb 9-11)

- **Duplicate register query prevention** on reconnect
- **Deployment event logging** for troubleshooting
- **Connection state change logging** (INFO level)
- **Register query downgrade** to DEBUG level (reduced log noise)
- **Complete BLE state machine documentation** with diagrams

**Key commits**: `d7f8baf`, `606b028`, `85ada53`, `0f79337`, `b2f641c`, `7048ded`

### Security Enhancement

- **Auto-generated API keys** - replaced hardcoded default with `secrets`-based generation
- **No insecure fallback** - BLE service requires explicit API key configuration
- **Bootstrap integration** - generates and configures key during installation

**Key commits**: `bb0fc27`, `4b39bbb`

### Frontend BLE Improvements

- **Removed redundant BLE connect** on SSE reconnect (backend handles it)
- **BLE pairing flow UX** - improved dialog button layout and post-pairing flow
- **Status indicator fixes** - shows correct connection state before device info loads
- **Toast notifications** for pairing success/failure
- **Press animations** on BLE command buttons
- **Restored device name persistence** after page refresh
- **BLE command wiring** via command palette

**Key commits**: `654d97a`, `d449456`, `f5e7201`, `f009eec`, `674d210`, `f9e5bea`, `3edbfbe`

---

## 💾 Database & Storage

### Schema Evolution (V1 → V9)

- **Schema V4** - added telemetry table, ACK tracking, conversation keys for DMs
- **Schema V5** - renamed `long` → `lon` for consistency
- **Schema V6** - **Position/Signal Architecture** - separated position and signal data into dedicated tables
  - New tables: `station_positions`, `signal_log`, `signal_buckets`
  - Dual-write compatibility for legacy clients
  - Independent field-group updates (signal never overwrites position, vice versa)
- **Schema V8** - purged empty BLE config messages from chat
- **Schema V9** - reset double-converted altitude values (fix for feet→meters bug)

**ADR**: `doc/2026-02-11_1400-position-signal-architecture-ADR.md`

**Key commits**: `6834c7f`, `b665594`, `66c7500`, `e91a86e`, `efb635f`, `417dc52`

### Storage Improvements

- **WAL mode** enabled for concurrent reads during writes
- **Composite indexes** for efficient pagination and filtering
  - `idx_messages_type_timestamp` for smart initial payload
  - `idx_messages_type_dst_timestamp` for paginated channel queries
  - `idx_signal_log_cs_ts` for signal log time-range queries
- **Cursor-based pagination** replaced LIMIT/OFFSET pattern
- **Per-destination initial load** - sends relevant messages only
- **Bidirectional DM pagination** support
- **Window functions** for per-station message limits in smart_initial
- **Thread-local connections** for query isolation

**Key commits**: `9929586`, `826a6de`, `d50495c`, `acddad9`, `bdaa0ef`, `c1abc8d`, `00a310e`

### Retention & Pruning

- **Type-based retention**:
  - Chat messages: 30 days
  - Position/ACK: 8 days
  - Signal log: 8 days
  - Signal buckets (5-min): 8 days
  - Signal buckets (1-hour): 365 days
  - Station positions: 30 days since last_seen
- **Nightly pruning** at 04:00 with `ANALYZE` for query planner freshness
- **Startup pruning** for immediate cleanup
- **1 GB database size limit** with progressive pruning
- **Backup rotation** (keep 3 most recent)
- **In-memory bucket accumulation** with flush on rollover

**Key commits**: `9929586`, `be92f71`, `f17acb1`

### Migration & Reliability

- **Idempotent migrations** - V2→V3, V4→V5 can retry safely after failures
- **Per-step schema_version persistence** - survives crashes mid-migration
- **Deque → SQLite migration utility** (`migrate_storage.py`)
- **Legacy in-memory backend removed** - SQLite is now the only supported backend
- **Empty BLE config message filtering** - prevents chat pollution

**Key commits**: `d0c6894`, `193b669`, `549f41c`, `011e340`, `6aec073`, `14968ea`

### Telemetry System

- **APRS weather data extraction** from position beacons
- **Telemetry endpoint** (`GET /api/telemetry`) for WX chart data
- **Junk filtering** - removes empty callsign and all-zero rows
- **Sensor routing** - SN sensor data → telemetry storage
- **Altitude normalization** - UDP altitude converted from feet to meters at ingestion
- **QNH calculation** from QFE + altitude

**ADR**: `doc/2026-02-12_HHMM-altitude-normalization-ADR.md` (referenced in commits)

**Key commits**: `ddad209`, `5f94f19`, `66c7500`, `6591604`, `9f85f42`, `2c03978`, `3e36d2a`
**Frontend commits**: `83539b3`, `bdbc4e6`, `a547847`, `32b1c97`

---

## 🚀 Bootstrap & Deployment

### Bootstrap Script v2.0 → v2.1.1

- **Piped execution support** (`curl | bash`) with proper library download
- **Interactive prompts** with validation:
  - Callsign (uppercase, valid ham radio format)
  - Latitude/longitude (decimal degrees)
  - Station name (city)
  - USER_INFO_TEXT (custom user info for `!userinfo` command)
- **Modular architecture** with `lib/` directory (detect, config, system, packages, deploy, health)
- **State detection** - fresh install / migrate / upgrade
- **CLI flags** - `--check`, `--force`, `--fix`, `--reconfigure`, `--quiet`, `--skip`
- **tmpfs configuration** - `/tmp` (150M), `/var/log` (volatile), SSH auth sockets
- **Firewall setup** - nftables (Trixie) or iptables (Bookworm)
- **IPv6 disable** - fixes mDNS timeout issues caused by Happy Eyeballs
- **Bluetooth rfkill unblock** - systemd service for Pi images with default_state=0
- **BLE service deployment** - optional dual-service setup
- **apt-get upgrade** during installation
- **Health checks** - UDP port, SSE endpoint, SQLite DB, lighttpd proxy
- **Success summary** with IP addresses, service URLs, and next steps
- **Version display** in health check output
- **OOM prevention** - zram swap for Pi Zero 2W (512MB RAM)
- **Locale generation** - en_US.UTF-8 and de_DE.UTF-8
- **BLE workspace member** dependency sync

**Key commits**: `77e07c1`, `6e69ed3`, `7bb5bd6`, `fb36959`, `e4ac43a`, `495aec8`, `8ad200f`, `066f7d5`, `39ee73c`, `ea89021`, `562f99f`, `ce2ec28`, `d9f4c10`, `0cf11f3`, `c27281c`, `6120ed0`, `62b0ddf`, `cafe9e0`, `b272ba9`, `301b366`, `524c4c4`, `4a5783a`, `d1e2eec`, `98dce20`, `a07aeb3`

### Deployment Automation

- **deploy-to-pi.sh** improvements:
  - Workspace member dependency sync (`uv sync` for `ble_service/`)
  - BLE service README.md deployment
  - Version bump automation
  - SCP/SFTP compatibility fixes
  - Git dev tag inclusion in version string
- **release.sh** - unified release script with dev/prod branch support
  - Auto-detects branch (main → production, development → pre-release)
  - Creates GitHub release with tarball
  - Cleanup/rollback procedures
  - Version extraction from `version.html`

**Key commits**: `20e3a81`, `67a59b3`, `78a8447`, `6cb521b`, `018b297`, `2feaec4`, `4eb7e9d`

### TLS Remote Access (Optional Addon)

- **ssl-tunnel-setup.sh** - standalone script for internet access with TLS
- **Caddy reverse proxy** with automated Let's Encrypt DNS-01 certificates
- **DDNS support** - DuckDNS, Cloudflare, deSEC.io
- **Cloudflare Tunnel** support
- **Architecture diagrams** and maintenance SOPs

**Key commits**: `01d93a0` (initial implementation)

---

## 🎨 Frontend UI/UX

### Mobile Experience

- **Bottom navigation bar** for iPhone/Android
  - Safe area inset support for iPhone notch
  - Auto-hide when virtual keyboard opens
  - Proper viewport configuration (`interactive-widget=resizes-visual`)
- **Slide-in contacts drawer** with header
- **Chat input overflow fixes** on narrow viewports
- **Destination callsign box** no longer cut off
- **Message grouping** breaks on destination change in All view
- **PWA installability** fixes for Android Chrome
  - Corrected `start_url` and `scope` in manifest
  - Semantic version comparison for update banner

**Key commits**: `91251fa`, `5390b99`, `a18ecf8`, `db3fce8`, `f4b10df`, `cb85766`, `cfd12c1`, `f009eec`, `5011d96`, `d5247f5`, `15c686b`, `fa16ba6`, `155ced0`

### Settings Page Redesign

- **Auto-save on blur** with inline revert and checkmark feedback
- **Simplified layout** - removed redundant sections (Display, Node Info)
- **Card-based design** with improved readability
- **SSE default enabled** - toggle removed
- **Message Groups** wrapped in card, search bar removed
- **Network card** renamed to "McApp Raspi Proxy" with simplified fields
- **Commands and External UDP** converted to inline toggle-style buttons
- **Compact Device Time card** with Callsign/SID collapse
- **Grid reordering** for better visual hierarchy

**Key commits**: `26097c7`, `f009eec`, `f31d21d`, `692280d`, `1653b1c`, `1653b1c`, `425c2be`, `daf4ccf`, `67604ff`, `fb04831`

### Node Configuration

- **Custom Command extracted** from grid for better visibility
- **3/2 column layout** simplification
- **APRS symbol sprite** in card header
- **WiFi settings display** in BLE node config panel
- **S1/S2 register display** for sensor and network/power config
- **"MeshCom Node Registers"** title added to register status card
- **Network/APRS card** position swap for better flow

**Key commits**: `974e73b`, `afcd406`, `1391374`, `9bb49cd`, `f4df541`, `de5c5c7`, `6619d1f`, `585e375`, `11abc8c`

### Weather & Telemetry

- **WX Data page** with temperature, pressure, and altitude charts
- **Quality scoring** for weather stations
- **QFE chart** display
- **Kalman filter** for altitude stabilization
- **24-hour x-axis** with cleaner y-axis ticks
- **Hide charts** with fewer than 2 datapoints (WX) or 10 datapoints (mHeard)
- **Filter mHeard charts** by active time window

**ADR**: Frontend `docs/2026-02-12_HHMM-wx-quality-adr.md` (referenced)

**Key commits**: `8ae2134`, `2f2374f`, `5520d6c`, `bdbc4e6`, `a547847`, `571cb0c`, `a1d72ba`

### Position & Topology

- **Mesh topology link lines** on map (canvas overlay for production compatibility)
- **SNR-based traffic-light coloring** on direct link lines
- **Via paths support** for new backend position architecture
- **Path-aware position dedup** - prefers direct over relayed paths
- **Highlight relay path** on station selection
- **Internet position updates** no longer erase via paths
- **Consistent zero-value handling** in position processing

**Key commits**: `1fa0425`, `eeb812a`, `f498af3`, `f4b10df`, `e773c1a`, `38bc184`, `0dd3a56`, `03f4b20`, `a7e350c`, `b3c2856`, `a03d24c`

### Message Delivery Status

- **BLE ACK support** for delivery tracking
- **Send success indicators** in chat bubbles
- **Inline `:ack` messages** included in smart_initial payload

**Key commits**: `7557295`, `018b297`

### Command Palette

- **Command wiring** completed with all command types
- **Enter key shortcut** for parameterless commands (changed from ⌘+Enter)
- **Auto-focus** on CommandForm div
- **Custom command display** improvements

**Key commits**: `3edbfbe`, `38bc184`, `03f4b20`

### Navigation & Layout

- **Shortened nav labels** with fully clickable version badge
- **Dynamic popup anchor** to avoid route path collision
- **Admin commands section** hidden from help page
- **Version badge** status cards for pre-release detection
- **Thunderstorm icon** added to WX Data nav button
- **Remove redundant headers** (Contacts, Settings, Bluetooth)
- **Bottom padding fixes** for mobile nav bar

**Key commits**: `ce43574`, `6f0a36b`, `51414f9`, `a4e4be1b`, `2a5269b`, `8a0ed34`, `692280d`, `1fbc4fc`

### Infinite Scroll Fixes

- **Eliminate dupes** in pagination
- **Timeout race fixes** preventing permanent blocks
- **Scroll wall prevention** with debounce and post-flush correction
- **Loading overlay spinner** replaces text
- **Trigger server page request** on destination change with no local messages
- **Chart redraw** on window resize (mHeard)

**Key commits**: `93edcdd`, `547a5e9`, `8186278`, `edbfbe`, `fa16ba6`, `f1700ac`

### Toast & Status Indicators

- **Toast stacking fixes** on reconnect
- **Instant PWA resume reconnection**
- **Removed false "Disconnected" toast** on page load
- **MeshCom status color** stays accurate when SSE backend goes down
- **BLE status button** shows correct state after disconnect
- **Internet auto-connect disabled** with toast notifications for MeshCom

**Key commits**: `155ced0`, `1ab1298`, `548c071`, `cbba8b0`, `22b458f`

### Performance & Cleanup

- **Removed excessive debug logging** across codebase
- **Memory leak fixes** in production readiness refactor
- **Race condition fixes**
- **DRY improvements** (Don't Repeat Yourself)
- **Removed vite devtools**
- **SSE-only proxy** (simplified from WebSocket+SSE)

**Key commits**: `b0bb4cb`, `0769203`, `db3fce8`, `550f833`

---

## 🔒 Security & Networking

### Firewall Configuration

- **nftables** on Debian Trixie (modern)
- **iptables** on Debian Bookworm (legacy fallback)
- **Allowed ports**: 22 (SSH), 80 (HTTP), 1799 (UDP MeshCom)
- **Internal-only**: 2981 (FastAPI, proxied via lighttpd)
- **SSH rate limiting**: 6/min external, LAN exempt (RFC 1918 ranges)
- **Silent drops** for common broadcast/multicast traffic:
  - Layer 2 broadcast, multicast (224.0.0.0/4, 239.0.0.0/8)
  - SSDP/UPnP (1900), NetBIOS (137/138), LLMNR (5355)
  - High UDP ports (>30000), IGMP protocol
- **Log rate limiting** - 10 drops/min to prevent spam
- **Subnet broadcast bitmask matching** for accurate filtering

**Key commits**: `f17acb1`, `97bca96`, `a3a7956`, `afe0c43`, `300cfcd`, `534cf16`, `51e949b`, `0f79337`

### Security Hardening

- **BLE API key auto-generation** with `secrets` module (16 chars)
- **No insecure defaults** - BLE service requires explicit key
- **lighttpd proxy** health checks
- **Firewall hardening** - relax SSH rate limit, restrict mDNS to multicast
- **SD card protection** - tmpfs for volatile data, reduced journal writes

**Key commits**: `bb0fc27`, `f17acb1`, `a3a7956`

---

## 🐛 Bug Fixes & Stability

### Critical Fixes

- **OOM prevention** on Pi Zero 2W during `apt-get upgrade` (zram swap)
- **BLE connection drops** under concurrent GATT writes (fixed)
- **Infinite scroll bugs** - dupes, timeout races, scroll walls
- **Message duplication** between mcdump.json and SQLite (fixed)
- **Empty chat bubbles** from BLE config messages (filtered)
- **Double altitude conversion** (feet→meters applied twice) - fixed
- **msg_id UNIQUE constraint** replaced with time-windowed dedup
- **WAL snapshot refresh** on persistent read connection before initial load
- **Thread-local connection** for smart_initial queries (prevents SQLite threading errors)

**Key commits**: `4a5783a`, `616ec1d`, `93edcdd`, `cceb00e`, `14968ea`, `417dc52`, `bdaa0ef`, `7bae54c`, `00a310e`

### Data Quality Fixes

- **Junk telemetry filtering** - empty callsign, all-zero values
- **IP-based pseudo-callsigns** for UDP telemetry storage
- **APRS altitude parsing** for UDP positions
- **QNH calculation** from QFE + altitude (not from wrong field)
- **Hidden groups backend persistence** (source of truth)
- **Backend read counts** persistence for unread badge sync
- **S1/S2 register support** (sensor and network config)
- **Preserve TYP field** in SN transform
- **Filter telemetry packets** from creating "?" sidebar entry

**Key commits**: `0c090c0`, `01f10c9`, `6b3642a`, `2c03978`, `3e36d2a`, `81a8a43`, `7054e8e`, `14968ea`, `bacb3a2`, `4c874ac`

### Protocol Fixes

- **CTCPING** bug fixes:
  - Started message ordering
  - ACK pattern relaxed for whitespace
  - Prevent echo counting as ping result
  - Fix missing msg_id in websocket results
  - Fix summary missing in personal chat
  - Fix inflated RTT measurement
- **BLE binary message parsing** (Phase 1 fixes)
- **FCS mismatch** downgrade to DEBUG level
- **Time signal detection** logging downgrade
- **MHeard throttle** and path-aware position merge
- **UDP health check timeout** increase to 24s for slow Pi first boot
- **BLE startup ordering** - start UDP listener before BLE init

**Key commits**: `1e8d596`, `ac3af4f`, `f2c3c90`, `c78ed4b`, `f04656`, `7d9de8c`, `4976fc1`, `1949f33`, `301b366`

### Configuration & Storage

- **Config JSON validation** on fresh Pi without jq (fixed)
- **Complete config.json generation** with all required fields
- **BLE_MODE default** set to `remote` with API key
- **Chown config.json** to service user after bootstrap writes it
- **Derive node address from callsign** (removed prompt)
- **Replace bc with awk** for lat/lon validation (dependency reduction)
- **Schema migration idempotency** - V2→V3, V4→V5, V5→V6 can retry safely
- **Per-step schema_version persistence** - survives crashes

**Key commits**: `066f7d5`, `66bc6db`, `39ee73c`, `b272ba9`, `d9f4c10`, `495aec8`, `549f41c`, `d0c6894`, `193b669`

### Deployment & Bootstrap

- **Piped mode library download** - fixed stdout pollution and subshell EXIT trap
- **MeshCom node hostname resolution** - exits silently if cannot resolve
- **macOS xattr warnings** suppressed during tar extraction on Pi
- **uv path resolution** and `sudo` usage in bootstrap
- **Include git dev tag** in version string for !stats output
- **lighttpd config idempotency** check missing proxy rules (fixed)
- **BLE service template** improvements
- **Inline bluetooth service unit** to fix template-not-found in piped mode

**Key commits**: `2e02831`, `a374f8b`, `e4ac43a`, `f182afa`, `ea89021`, `4ddbc22`, `4eb7e9d`, `9929586`, `315e29f`, `651a590`

### Frontend Fixes

- **BLE status shows connected** before device info loads (fixed)
- **Alert sound restoration** for incoming messages matching alert text
- **Proxy hostname changes ignored** when SSE is reconnecting (fixed)
- **Purple colors on Chrome/Windows** (fixed)
- **Unread badge showing for currently viewed destination** (fixed)
- **lon/long field name support** for internet WebSocket compatibility
- **Restore wsProxyIP auto-population** from window.location
- **Prevent permanent pagination block** when SSE not connected on initial load
- **Replace loading text with overlay spinner**

**Key commits**: `f816e39`, `0dd028f`, `a4797b9`, `550f833`, `deef212`, `6619d1f`, `f6e6503`, `e773c1a`, `547a5e9`

---

## 📚 Documentation

### New Documentation

- **Architecture diagrams** converted to Mermaid (replacing ASCII art)
- **BLE state machine diagrams** and message flow
- **BLE comprehensive A0 command reference**
- **BLE implementation gap analysis**
- **Position/Signal Architecture ADR** (`doc/2026-02-11_1400-position-signal-architecture-ADR.md`)
- **Altitude Normalization ADR** (referenced in commits)
- **WX Quality Scoring ADR** (frontend, referenced in commits)
- **TLS architecture diagrams** (`doc/tls-architecture.md`)
- **TLS maintenance SOPs** (`doc/tls-maintenance-SOP.md`)
- **Production database querying reference** in CLAUDE.md
- **Remote health check commands** in CLAUDE.md
- **Infinite scroll test results** documentation (`infinite-scroll.md`)
- **Update page rewrite** with bootstrap procedure, architecture, and service management

**Key commits**: `143f438`, `6e69ed3`, `b2f641c`, `7048ded`, `e8be771`, `e55d5f5`, `1721a59`, `12e7556`, `3943874`, `5e1ad87`, `93edcdd`

### Documentation Updates

- **CLAUDE.md updates** across both repos:
  - SQLite retention, pruning, and index details
  - BLE mode documentation (removed local mode)
  - Firewall configuration and log silencing
  - Bootstrap script documentation
  - Deployment procedures
  - USER_INFO_TEXT configuration
  - Production database querying
  - Remote health check commands
  - Release workflow
  - Backend source path and deployment policy
- **README.md** complete rewrite:
  - Translated German → English
  - Architecture diagrams
  - Bootstrap installation guide
  - Screenshots
  - Removed Caddy references (replaced with lighttpd)
- **Deployment testing documentation** revised for uv, dual-service, WS/SSE checks
- **German docs translated to English**
- **Legacy repo URL note** added across all documentation
- **Documentation reorganization** - moved files to `doc/` and `docs/` directories

**Key commits**: `8bf3e1d`, `d919da8`, `42952ed`, `12e7556`, `7bb5bd6`, `b0a44df`, `7039a11`, `4ddbc22`, `e685d70`, `140750c`, `f935821`, `f6e6503`, `133d51a`, `f0c1f8d`, `acc76fc`, `b3c2856`

---

## ⚡ Performance Optimizations

### Database Performance

- **Composite indexes** for efficient queries:
  - `idx_messages_type_timestamp` for smart initial payload
  - `idx_messages_type_dst_timestamp` for paginated channel queries
  - `idx_signal_log_cs_ts` for signal log time-range queries
  - `idx_messages_src` composite index for DM pagination
- **Window functions** replace LIMIT 500 in get_smart_initial (per-station limits)
- **WAL mode** for concurrent reads during writes
- **Persistent read connection** with WAL snapshot refresh for initial load
- **Thread-local connections** for query isolation
- **1 GB size limit enforcement** with progressive pruning

**Key commits**: `9929586`, `97bca96`, `d50495c`, `7bae54c`, `00a310e`, `be92f71`, `c1abc8d`

### Logging Optimizations

- **Downgrade to DEBUG level**:
  - BLE binary messages
  - BLE register queries
  - FCS mismatch messages
  - Time signal detection
  - signal_buckets aggregation
  - Routine BLE packet logging
  - BLE Handler messages
- **Removed excessive debug logging** across frontend codebase
- **Deployment event logging** (INFO level for troubleshooting)
- **BLE remote connection state changes** (INFO level)

**Key commits**: `0f79337`, `82539fe`, `5a0e718`, `5de078c`, `4eb7e9d`, `f086cd5`, `0e8849b`, `9d6f2d3`, `b0bb4cb`, `85ada53`, `606b028`

### Frontend Performance

- **Memory leak fixes** in production readiness refactor
- **Race condition fixes**
- **Infinite scroll debounce** with post-flush correction
- **DRY improvements** (Don't Repeat Yourself)
- **Removed vite devtools** (production builds)
- **Optimize DM pagination** with UNION ALL and src composite index

**Key commits**: `0769203`, `8186278`, `db3fce8`, `c1abc8d`

### System Performance

- **Purge camera stack packages** in bootstrap (prevents bloat on headless Pi)
- **zram swap** for Pi Zero 2W (prevents OOM during apt-get upgrade)
- **tmpfs configuration** for volatile data (SD card wear reduction)
- **IPv6 disable** (fixes mDNS timeout issues)

**Key commits**: `350d2fa`, `4a5783a`, `a3a7956`, `7920753`

---

## 📊 Statistics

### Backend (MCProxy)

- **Total commits**: 234
- **Date range**: February 6 - February 15, 2026 (9 days)
- **Files changed**: 132
- **Lines inserted**: 17,682
- **Lines deleted**: 8,688
- **Net change**: +8,994 lines
- **Commit types**:
  - 106 fixes
  - 52 features
  - 35 chore/refactor
  - 26 documentation
  - 15 performance

### Frontend (webapp)

- **Total commits**: 154
- **Date range**: February 3 - February 15, 2026 (12 days)
- **Files changed**: 95
- **Lines inserted**: 7,036
- **Lines deleted**: 1,912
- **Net change**: +5,124 lines
- **Commit types**:
  - 80 fixes
  - 38 features
  - 20 chore/refactor
  - 10 documentation
  - 6 performance

### Combined

- **Total commits**: 388
- **Total files changed**: 227
- **Total lines inserted**: 24,718
- **Total lines deleted**: 10,600
- **Net lines added**: 14,118
- **Major features added**: 90+
- **Bugs fixed**: 186+
- **Documentation files added/updated**: 36+

---

## 🎯 Production Readiness Checklist

- ✅ **BLE system** completely overhauled (4 phases)
- ✅ **Database schema** finalized (V9)
- ✅ **Bootstrap script** production-ready (v2.1.1)
- ✅ **Security hardening** complete (firewall, API keys, tmpfs)
- ✅ **Frontend UX** polished for mobile and desktop
- ✅ **Performance optimizations** implemented
- ✅ **Memory leaks** and race conditions fixed
- ✅ **Documentation** comprehensive and up-to-date
- ✅ **Testing** completed (infinite scroll, BLE state machine, commands)
- ✅ **Release automation** working (release.sh, deploy-to-pi.sh)
- ✅ **Repository rebranding** complete (McAdvChat → McApp)

---

## 🚀 Next Steps

1. **Review this release history** for accuracy
2. **Create production release** using `release.sh` from `main` branch
3. **Tag release** with semantic version (e.g., v1.1.0)
4. **Deploy to production Pi** using bootstrap script
5. **Monitor logs** for 24-48 hours
6. **Announce release** to users with changelog

---

**Generated**: February 15, 2026
**Repositories**:
- Backend: `github.com/DK5EN/McApp`
- Frontend: `github.com/DK5EN/webapp` (separate repo)
