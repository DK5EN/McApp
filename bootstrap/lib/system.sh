#!/bin/bash
# system.sh - System configuration for McApp bootstrap
# Handles: tmpfs, firewall, journald, service hardening, SSH, logrotate
# Incorporates all pi-harden.sh functionality for SD card longevity & security

#──────────────────────────────────────────────────────────────────
# MAIN SYSTEM SETUP FUNCTION
#──────────────────────────────────────────────────────────────────

setup_system() {
  # configure_locale moved to install_apt_deps() — runs after apt upgrade
  disable_unused_services
  remove_bloat_packages
  configure_tmpfs
  configure_logdirs
  configure_boot_memory
  configure_journald
  configure_logrotate
  configure_firewall
  configure_bluetooth
  disable_ipv6
  configure_unattended_upgrades
  configure_wpasupplicant_pin
  configure_fast_ssh_login
  configure_ssh_hardening
}

#──────────────────────────────────────────────────────────────────
# LOCALE GENERATION
#──────────────────────────────────────────────────────────────────

configure_locale() {
  log_info "Configuring locale..."

  # Ensure locales package is installed
  if ! dpkg -l locales &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq locales
  fi

  local locale_gen="/etc/locale.gen"
  local needs_generate=false

  # Locales to ensure are generated:
  # - en_US.UTF-8: universal fallback
  # - de_DE.UTF-8: common on German systems (SSH forwards LC_ALL from client)
  local -a required_locales=("en_US.UTF-8" "de_DE.UTF-8")

  for loc in "${required_locales[@]}"; do
    # Check if locale is already available
    if locale -a 2>/dev/null | grep -qi "$(echo "$loc" | sed 's/UTF-8/utf8/')"; then
      continue
    fi

    # Uncomment in locale.gen if present but commented out
    if grep -qE "^#\s*${loc}" "$locale_gen" 2>/dev/null; then
      backup_config_file "$locale_gen"
      sed -i "s/^#\s*\(${loc}\)/\1/" "$locale_gen"
      needs_generate=true
      log_info "  Enabled ${loc} in locale.gen"
    elif ! grep -q "^${loc}" "$locale_gen" 2>/dev/null; then
      # Not present at all — add it
      backup_config_file "$locale_gen"
      echo "${loc} UTF-8" >> "$locale_gen"
      needs_generate=true
      log_info "  Added ${loc} to locale.gen"
    fi
  done

  if [[ "$needs_generate" == "true" ]]; then
    locale-gen
    log_ok "  Locales generated"
  else
    log_info "  Required locales already available"
  fi
}

#──────────────────────────────────────────────────────────────────
# DISABLE UNUSED SERVICES & TIMERS
#──────────────────────────────────────────────────────────────────

disable_unused_services() {
  log_info "Disabling unused services..."

  # Services safe to disable on a headless Pi
  local -a disable_services=(
    "cups"
    "cups-browsed"
    "ModemManager"
    "triggerhappy"
    # cloud-init suite (not needed on local network Pi)
    "cloud-init-local"
    "cloud-init-main"
    "cloud-init-network"
    "cloud-config"
    "cloud-final"
    # SD card wear reduction
    "udisks2"
    "e2scrub_reap"
    # Serial console (headless — no display attached)
    "serial-getty@ttyS0"
  )

  local -a disable_timers=(
    "e2scrub_all.timer"
    "man-db.timer"
  )

  for svc in "${disable_services[@]}"; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
      systemctl disable --now "$svc" 2>/dev/null || true
      log_info "  Disabled: $svc"
    fi
  done

  for tmr in "${disable_timers[@]}"; do
    if systemctl is-enabled --quiet "$tmr" 2>/dev/null; then
      systemctl disable --now "$tmr" 2>/dev/null || true
      log_info "  Disabled timer: $tmr"
    fi
  done

  # DO NOT disable these (required for McApp):
  # - avahi-daemon (mDNS for .local resolution)
  # - bluetooth (BLE for ESP32 communication)
  # - wpa_supplicant (WiFi connectivity)

  log_ok "  Unused services disabled"
}

#──────────────────────────────────────────────────────────────────
# REMOVE BLOAT PACKAGES
#──────────────────────────────────────────────────────────────────

remove_bloat_packages() {
  log_info "Removing unused packages..."

  # Camera stack — not needed on headless Pi, pulls in TensorFlow/Mesa/Wayland bloat
  local -a purge_packages=(
    "rpicam-apps-core"
    "librpicam-app1"
    "libcamera-ipa"
    "libcamera0.6"
    "libcamera0.7"
  )

  local removed=0
  for pkg in "${purge_packages[@]}"; do
    if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
      log_info "  Removing: $pkg"
      DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "$pkg" 2>/dev/null || true
      removed=$((removed + 1))
    fi
  done

  if [[ $removed -gt 0 ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y -qq 2>/dev/null || true
    log_ok "  Removed $removed bloat package(s) and orphaned dependencies"
  else
    log_ok "  No bloat packages found"
  fi
}

#──────────────────────────────────────────────────────────────────
# TMPFS CONFIGURATION (SD Card Protection)
#──────────────────────────────────────────────────────────────────

# Overridable for the offline test suite (scripts/bootstrap_ramlog_tests.py).
MCAPP_FSTAB="${MCAPP_FSTAB:-/etc/fstab}"
MCAPP_TMPFILES_DIR="${MCAPP_TMPFILES_DIR:-/etc/tmpfiles.d}"
MCAPP_SYSTEMD_UNIT_DIR="${MCAPP_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
MCAPP_REBOOT_MARKER="${MCAPP_REBOOT_MARKER:-/var/lib/mcapp/reboot-required}"

MCAPP_DEFAULTS_DIR="${MCAPP_DEFAULTS_DIR:-/etc/default}"

# True when the unit's defaults file (/etc/default/<name>) sets ENABLED=false.
# Upstream ramlog scripts exit early on that, so the unit is enabled in systemd
# but mounts nothing. A missing file means enabled (the upstream default). The
# last assignment wins; quotes, whitespace and a trailing comment are tolerated.
_mcapp_ramlog_defaults_disabled() {
  local file="${MCAPP_DEFAULTS_DIR}/${1%.service}" value
  [[ -f "$file" ]] || return 1
  value="$(grep -E '^[[:space:]]*ENABLED[[:space:]]*=' "$file" 2>/dev/null | tail -n 1)" || return 1
  [[ -n "$value" ]] || return 1
  value="${value#*=}"
  value="${value%%#*}"
  value="$(printf '%s' "$value" | tr -d "[:space:]\"'" | tr '[:upper:]' '[:lower:]')"
  [[ "$value" == "false" ]]
}

# Prints the ramlog unit (armbian-ramlog.service or orangepi-ramlog.service)
# when one is installed, enabled AND not switched off in its defaults file,
# else nothing. Such a unit mounts a zram ext4 over /var/log and restores it
# from /var/log.hdd, which shadows a tmpfs /var/log fstab line. It also keeps
# its own persistent journal in /var/log.hdd/journal, so no bind is needed.
detect_ramlog_service() {
  local unit state
  for unit in armbian-ramlog.service orangepi-ramlog.service; do
    state="$(systemctl is-enabled "$unit" 2>/dev/null)" || continue
    if [[ "$state" == "enabled" ]] && ! _mcapp_ramlog_defaults_disabled "$unit"; then
      printf '%s\n' "$unit"
      return 0
    fi
  done
  return 0
}

_mcapp_mark_reboot_required() {
  mkdir -p "$(dirname "$MCAPP_REBOOT_MARKER")"
  touch "$MCAPP_REBOOT_MARKER"
}

configure_tmpfs() {
  log_info "Configuring tmpfs for SD card longevity..."

  local fstab="$MCAPP_FSTAB"
  local marker="# McApp tmpfs"
  local ramlog
  ramlog="$(detect_ramlog_service)"

  if [[ -n "$ramlog" ]]; then
    # The ramlog unit owns /var/log; our tmpfs line would be mounted over or
    # under it. Keep /tmp, drop only the /var/log line from our block.
    local tmpfs_log_re='^tmpfs /var/log tmpfs'
    local in_block='/# McApp tmpfs - begin/ { inb = 1 } /# McApp tmpfs - end/ { inb = 0 }'
    if awk -v re="$tmpfs_log_re" "$in_block"' inb && $0 ~ re { found = 1 } END { exit !found }' "$fstab" 2>/dev/null; then
      backup_config_file "$fstab"
      awk -v re="$tmpfs_log_re" "$in_block"' !(inb && $0 ~ re) { print }' \
        "$fstab" > "${fstab}.mcapp-tmp" && mv -f "${fstab}.mcapp-tmp" "$fstab"
      _mcapp_mark_reboot_required
      log_ok "  ${ramlog} owns /var/log: removed the /var/log tmpfs line from fstab (reboot required)"
    elif grep -qE "(McApp tmpfs|MCProxy tmpfs|pi-harden)" "$fstab" 2>/dev/null; then
      log_info "  ${ramlog} owns /var/log: no /var/log tmpfs line to remove"
    else
      backup_config_file "$fstab"
      cp "$fstab" "${fstab}.bak.$(date +%Y%m%d)"
      cat >> "$fstab" << EOF

${marker} - begin
tmpfs /tmp tmpfs defaults,noatime,nosuid,mode=1777,size=150M 0 0
${marker} - end
EOF
      log_ok "  tmpfs entry for /tmp added to fstab (/var/log is managed by ${ramlog})"
    fi
  # Check if already configured (also accept pi-harden marker or legacy MCProxy marker)
  elif grep -qE "(McApp tmpfs|MCProxy tmpfs|pi-harden)" "$fstab" 2>/dev/null; then
    log_info "  tmpfs already configured in fstab"
  else
    # Backup fstab
    backup_config_file "$fstab"
    cp "$fstab" "${fstab}.bak.$(date +%Y%m%d)"

    # Add tmpfs entries — nodev,noexec on /var/log for security
    cat >> "$fstab" << EOF

${marker} - begin
tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,noexec,size=30M 0 0
tmpfs /tmp tmpfs defaults,noatime,nosuid,mode=1777,size=150M 0 0
${marker} - end
EOF

    log_ok "  tmpfs entries added to fstab"
  fi

  # Create log directory structure in tmpfs after mount
  mkdir -p "$MCAPP_TMPFILES_DIR"
  backup_config_file "${MCAPP_TMPFILES_DIR}/mcapp.conf"
  cat > "${MCAPP_TMPFILES_DIR}/mcapp.conf" << 'EOF'
# McApp + system log directories (recreated on every boot in tmpfs)
d /var/log/mcapp     0755 root     root     -
d /var/log/lighttpd  0755 www-data www-data -
d /var/log/journal   0755 root     root     -
d /var/log/apt       0755 root     root     -
d /var/log/private   0700 root     root     -
d /var/log/unattended-upgrades 0755 root root -
f /var/log/lastlog             0644 root root -
f /var/log/wtmp                0664 root utmp -
f /var/log/btmp                0600 root utmp -
EOF

  # Try to mount /var/log now if not already tmpfs (never on a ramlog board:
  # the ramlog unit owns that mount)
  if [[ -n "$ramlog" ]]; then
    log_info "  /var/log left to ${ramlog}"
  elif findmnt -n /var/log 2>/dev/null | grep -q tmpfs; then
    log_info "  /var/log already mounted as tmpfs"
  else
    # Create dirs before mount so services don't break
    systemd-tmpfiles --create "${MCAPP_TMPFILES_DIR}/mcapp.conf" 2>/dev/null || true
    # Capture the real folders (nginx, ...) while the disk /var/log is still
    # visible: after the mount it is an empty tmpfs and the generator would
    # see nothing, losing custom folders and real owners.
    configure_logdirs
    if mount /var/log 2>/dev/null; then
      log_ok "  Mounted /var/log tmpfs"
      # The earlier create ran on the disk under the mount; redo it on the tmpfs.
      local tf
      for tf in mcapp.conf mcapp-logdirs.conf; do
        [[ -f "${MCAPP_TMPFILES_DIR}/${tf}" ]] || continue
        systemd-tmpfiles --create "${MCAPP_TMPFILES_DIR}/${tf}" 2>/dev/null \
          || log_warn "  systemd-tmpfiles --create ${tf} after the /var/log mount failed"
      done
    else
      log_warn "  /var/log tmpfs mount deferred to next reboot"
    fi
  fi

  # Mount /tmp if not already mounted
  if ! mountpoint -q /tmp 2>/dev/null; then
    mount -t tmpfs -o defaults,noatime,nosuid,mode=1777,size=150M tmpfs /tmp || true
  fi
}

# ---- logdirs: keep other services' /var/log folders across reboots --------
# mcapp.conf above only recreates folders McApp knows. On a tmpfs /var/log
# anything else (nginx, ...) vanishes at reboot and its unit fails with
# "open() /var/log/nginx/error.log failed". A generator script writes one
# `d` line per folder it finds (real folders with their actual mode/owner,
# packaged ones from dpkg *.list with 0755 root adm) into
# mcapp-logdirs.conf, run hourly by a timer. Rewrites only on change (SD card).
# Limit, accepted: a folder owned by a non-root writer that is already gone
# when first seen gets the default owner; `d` also re-applies mode/owner to an
# existing folder, which is why real values must win over the default.
MCAPP_SBIN_DIR="${MCAPP_SBIN_DIR:-/usr/local/sbin}"

# Write $2 (content) to $1 only when it differs; backs up the old file first.
# Returns 0 when it changed the file, 1 when unchanged or on failure.
_mcapp_logdirs_put() {
  local dest="$1" content="$2" mode="${3:-0644}"
  if [[ -f "$dest" ]] && [[ "$(cat "$dest" 2>/dev/null)" == "$content" ]]; then
    return 1
  fi
  mkdir -p "$(dirname "$dest")" || return 1
  backup_config_file "$dest"
  printf '%s\n' "$content" > "${dest}.mcapp-tmp" || return 1
  chmod "$mode" "${dest}.mcapp-tmp" && mv -f "${dest}.mcapp-tmp" "$dest" || return 1
  return 0
}

# A ramlog board owns /var/log: drop a prior install.
_mcapp_logdirs_remove() {
  local script="${MCAPP_SBIN_DIR}/mcapp-logdirs" udir="$MCAPP_SYSTEMD_UNIT_DIR"
  local f gone=0
  if [[ -e "${udir}/mcapp-logdirs.timer" || -e "${udir}/mcapp-logdirs.service" ]]; then
    systemctl disable --now mcapp-logdirs.timer > /dev/null 2>&1 || true
  fi
  for f in "$script" "${udir}/mcapp-logdirs.timer" "${udir}/mcapp-logdirs.service" \
    "${MCAPP_TMPFILES_DIR}/mcapp-logdirs.conf"; do
    if [[ -e "$f" ]]; then
      rm -f "$f" && gone=1
    fi
  done
  if [[ "$gone" == 1 ]]; then
    systemctl daemon-reload > /dev/null 2>&1 || true
    log_ok "  ramlog owns /var/log: removed the mcapp-logdirs install"
  fi
}

_mcapp_logdirs_apply() {
  local script="${MCAPP_SBIN_DIR}/mcapp-logdirs" udir="$MCAPP_SYSTEMD_UNIT_DIR"
  local changed=0 ramlog_unit
  ramlog_unit="$(detect_ramlog_service)"
  if [[ -n "$ramlog_unit" ]]; then
    _mcapp_logdirs_remove
    return 0
  fi

  local gen
  IFS= read -r -d '' gen << 'GENEOF' || true
#!/bin/bash
# Generated by mcapp.sh (configure_logdirs). Writes mcapp-logdirs.conf so
# other services' /var/log folders are recreated on every boot.
set -u
export LC_ALL=C
log_root="${MCAPP_LOG_ROOT:-/var/log}"
tmpdir="${MCAPP_TMPFILES_DIR:-/etc/tmpfiles.d}"
dpkg_info="${MCAPP_DPKG_INFO:-/var/lib/dpkg/info}"
out="${tmpdir}/mcapp-logdirs.conf"
tmp="${out}.mcapp-tmp.$$"

# tmpfiles expands % specifiers and splits on blanks and quotes: only this set
# may reach the file. A function, not an inline case: bash 3.2 cannot parse a
# case pattern inside $( ... ).
safe_name() {
  case "$1" in
    "" | *[!A-Za-z0-9._@+-]*) return 1 ;;
  esac
  return 0
}

# Folders mcapp.conf already owns stay defined there only.
skip=""
if [ -r "${tmpdir}/mcapp.conf" ]; then
  skip="$(awk '$1 !~ /^#/ && $2 ~ /^\/var\/log\/[^\/]+$/ { n = $2; sub(/^\/var\/log\//, "", n); print n }' \
    "${tmpdir}/mcapp.conf" 2>/dev/null)"
fi

real_lines=""
for d in "$log_root"/*/; do
  d="${d%/}"
  [ -d "$d" ] || continue
  [ -L "$d" ] && continue
  name="${d##*/}"
  safe_name "$name" || continue
  # %u/%g first: a UID/GID without a name prints UNKNOWN, which is no valid
  # tmpfiles user, so such an id is written numerically.
  st="$(stat -c '%a %u %g %U %G' "$d" 2>/dev/null)" || continue
  set -- $st
  [ "$#" -ge 3 ] || continue
  # An owner name holding a blank (directory services) shifts the fields: use the ids.
  u="$2"
  g="$3"
  if [ "$#" -eq 5 ]; then
    if [ "$4" != UNKNOWN ] && safe_name "$4"; then u="$4"; fi
    if [ "$5" != UNKNOWN ] && safe_name "$5"; then g="$5"; fi
  fi
  real_lines="${real_lines}${name} ${1} ${u} ${g}
"
done

# Lines an earlier scan wrote. A folder can be missing right now (a scan landed on a
# freshly mounted tmpfs, or tmpfiles failed), and dropping its line would lose it for
# good, or downgrade its owner to the package default. Cost: a folder an admin removed
# stays listed, recreated empty, until its line is deleted by hand.
old_lines=""
if [ -r "$out" ]; then
  old_lines="$(awk -v root="$log_root/" '
    $1 == "d" && NF >= 5 && index($2, root) == 1 {
      print substr($2, length(root) + 1), $3, $4, $5
    }' "$out" 2>/dev/null \
    | while IFS= read -r line; do
      n="${line%% *}"
      safe_name "$n" || continue
      [ -L "$log_root/$n" ] && continue
      if [ -e "$log_root/$n" ] && [ ! -d "$log_root/$n" ]; then continue; fi
      printf '%s\n' "$line"
    done)"
fi

pkg_lines=""
if [ -d "$dpkg_info" ]; then
  pkg_lines="$(cat "$dpkg_info"/*.list 2>/dev/null \
    | grep -h '^/var/log/[^/][^/]*$' 2>/dev/null \
    | sed 's|^/var/log/||' \
    | grep -v '[[:space:]]' | grep -v '\.[A-Za-z0-9_-]*$' \
    | while IFS= read -r n; do
      safe_name "$n" || continue
      [ -L "$log_root/$n" ] && continue
      # A packaged plain file (no extension) is not a folder. Once it is gone it cannot
      # be told apart from a folder: accepted, Debian policy keeps log files out of
      # packages, and only folders were seen in these lists.
      if [ -e "$log_root/$n" ] && [ ! -d "$log_root/$n" ]; then continue; fi
      echo "$n 755 root adm"
    done)"
fi

body="$(
  { printf '%s\n' "$skip" | sed -n 's/^\(.\)/@ \1/p'; printf '%s\n' "$real_lines"; printf '%s\n' "$old_lines"; printf '%s\n' "$pkg_lines"; } \
    | awk -v root="$log_root" '
      $1 == "@" { s[$2] = 1; next }
      NF == 4 && !($1 in s) && !($1 in seen) {
        seen[$1] = 1
        printf "d %s/%s %04d %s %s -\n", root, $1, $2, $3, $4
      }' | LC_ALL=C sort
)"

content="# Generated by mcapp-logdirs (mcapp.sh): other services' log folders, recreated on boot."
if [ -n "$body" ]; then
  content="${content}
${body}"
fi

mkdir -p "$tmpdir" 2>/dev/null || exit 0
if [ -f "$out" ] && [ "$(cat "$out" 2>/dev/null)" = "$content" ]; then
  exit 0
fi
printf '%s\n' "$content" > "$tmp" 2>/dev/null || exit 0
chmod 0644 "$tmp" 2>/dev/null
mv -f "$tmp" "$out" 2>/dev/null || exit 0
systemd-tmpfiles --create "$out" 2>/dev/null || echo "mcapp-logdirs: systemd-tmpfiles failed" >&2
exit 0
GENEOF
  gen="${gen%$'\n'}"

  local svc timer
  svc="[Unit]
Description=Keep other services' /var/log folders across reboots (McApp tmpfs)

[Service]
Type=oneshot
ExecStart=${script}"
  timer="[Unit]
Description=Refresh McApp log folder list hourly

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target"

  _mcapp_logdirs_put "$script" "$gen" 0755 && changed=1
  _mcapp_logdirs_put "${udir}/mcapp-logdirs.service" "$svc" && changed=1
  _mcapp_logdirs_put "${udir}/mcapp-logdirs.timer" "$timer" && changed=1
  if [[ "$changed" == 1 ]]; then
    systemctl daemon-reload > /dev/null 2>&1 || log_warn "  mcapp-logdirs: daemon-reload failed"
  fi
  systemctl enable --now mcapp-logdirs.timer > /dev/null 2>&1 \
    || log_warn "  mcapp-logdirs: could not enable the timer"
  "$script" || log_warn "  mcapp-logdirs: generator failed"
  return 0
}

# Never aborts the bootstrap: every failure is a warning.
configure_logdirs() {
  (
    set +e
    _mcapp_logdirs_apply
  ) || log_warn "  mcapp-logdirs: setup failed (continuing)"
  return 0
}

#──────────────────────────────────────────────────────────────────
# BOOT MEMORY (GPU/CMA reservation + cgroup memory accounting)
#──────────────────────────────────────────────────────────────────
# B4 item 1 (doc/backlog.md): the stock boot config reserves 64MB for the
# GPU and a 256MB CMA pool that nothing on this headless box uses (no HDMI,
# no camera, no audio) -- that reservation is what pushes mcapp/mcapp-ble
# into zram swap under normal load on a 512MB Pi Zero 2 W. Also enables
# cgroup memory accounting, missing today (memory.current reads 0 for every
# cgroup because cgroup_enable=memory is absent from cmdline.txt).
#
# Takes effect only after a reboot -- this function NEVER reboots. It marks
# /var/lib/mcapp/reboot-required so an operator (or a later health check)
# knows a reboot is owed.

# Rewrites config.txt in place: adds gpu_mem=16 under a McApp-owned [all]
# block (a directive appended after a [pi5]/[cm4] filter header would be
# ignored on the Zero 2 W, so the block gets its own explicit [all] header),
# adds a cma-64 parameter to an un-parameterized vc4-kms-v3d overlay line
# (left alone if it already carries a cma- parameter), and turns off the
# onboard-audio dtparam. Returns 0 if the file changed, 1 if it was already
# converged -- idempotent by content, not by a marker check, so re-running
# this after a hand edit still converges it.
_mcapp_configure_config_txt() {
  local file="$1"
  local tmp
  tmp="$(mktemp)"
  cp "$file" "$tmp"

  # Existing KMS overlay: add cma-64 only if it has no cma- parameter yet.
  sed -i.bak -E 's/^dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,cma-64/' "$tmp"

  # Nothing on this headless box drives onboard audio.
  sed -i.bak -E 's/^dtparam=audio=on$/dtparam=audio=off/' "$tmp"

  # gpu_mem must live in [all] or before any section header.
  if grep -q '# McApp boot memory - begin' "$tmp"; then
    sed -i.bak -E '/# McApp boot memory - begin/,/# McApp boot memory - end/ s/^gpu_mem=.*/gpu_mem=16/' "$tmp"
  else
    {
      echo ""
      echo "[all]"
      echo "# McApp boot memory - begin"
      echo "gpu_mem=16"
      echo "# McApp boot memory - end"
    } >> "$tmp"
  fi

  rm -f "${tmp}.bak"

  if ! cmp -s "$file" "$tmp"; then
    backup_config_file "$file"
    cat "$tmp" > "$file"
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# Appends cgroup_enable=memory cgroup_memory=1 to the SAME line of a
# single-line cmdline.txt if not already present. Returns 0 if changed, 1
# if already converged.
_mcapp_configure_cmdline_txt() {
  local file="$1"
  local content
  content="$(cat "$file")"

  if [[ "$content" == *"cgroup_enable=memory"* ]]; then
    return 1
  fi

  backup_config_file "$file"
  printf '%s cgroup_enable=memory cgroup_memory=1\n' "$content" > "$file"
  return 0
}

# Args are optional and exist so tests can point this at fixture files
# instead of the real boot partition; production calls it with no args.
configure_boot_memory() {
  log_info "Configuring boot memory reservation..."

  local config_txt="${1:-}"
  local cmdline_txt="${2:-}"

  if [[ -z "$config_txt" ]]; then
    if [[ -f /boot/firmware/config.txt ]]; then
      config_txt="/boot/firmware/config.txt"
    elif [[ -f /boot/config.txt ]]; then
      config_txt="/boot/config.txt"
    fi
  fi

  if [[ -z "$cmdline_txt" ]]; then
    if [[ -f /boot/firmware/cmdline.txt ]]; then
      cmdline_txt="/boot/firmware/cmdline.txt"
    elif [[ -f /boot/cmdline.txt ]]; then
      cmdline_txt="/boot/cmdline.txt"
    fi
  fi

  if [[ -z "$config_txt" ]] && [[ -z "$cmdline_txt" ]]; then
    log_warn "  No /boot/firmware or /boot found (not a Pi firmware partition?), skipping"
    return 0
  fi

  local changed=false

  if [[ -n "$config_txt" ]]; then
    if [[ -f "$config_txt" ]]; then
      if _mcapp_configure_config_txt "$config_txt"; then
        changed=true
        log_info "  Updated $config_txt (gpu_mem=16, cma-64, audio off)"
      else
        log_info "  $config_txt already configured"
      fi
    else
      log_warn "  $config_txt not found, skipping config.txt changes"
    fi
  fi

  if [[ -n "$cmdline_txt" ]]; then
    if [[ -f "$cmdline_txt" ]]; then
      if _mcapp_configure_cmdline_txt "$cmdline_txt"; then
        changed=true
        log_info "  Updated $cmdline_txt (cgroup_enable=memory)"
      else
        log_info "  $cmdline_txt already configured"
      fi
    else
      log_warn "  $cmdline_txt not found, skipping cmdline.txt changes"
    fi
  fi

  if [[ "$changed" == "true" ]]; then
    mkdir -p /var/lib/mcapp
    touch /var/lib/mcapp/reboot-required
    log_warn "  reboot required for boot memory settings"
  elif [[ -f /var/lib/mcapp/reboot-required ]] && _mcapp_boot_memory_live; then
    # The files were unchanged AND the running kernel shows the values: the
    # reboot happened, so the marker is stale. "Unchanged" alone proves nothing.
    rm -f /var/lib/mcapp/reboot-required
    log_ok "  Boot memory settings live (gpu_mem=16, cgroup memory); reboot marker cleared"
  else
    log_ok "  Boot memory settings already applied"
  fi
}

# True when the RUNNING system reflects the boot memory settings.
_mcapp_boot_memory_live() {
  grep -qw "cgroup_enable=memory" /proc/cmdline 2>/dev/null || return 1
  if command -v vcgencmd >/dev/null 2>&1; then
    [[ "$(vcgencmd get_mem gpu 2>/dev/null)" == "gpu=16M" ]] || return 1
  fi
  return 0
}

#──────────────────────────────────────────────────────────────────
# JOURNALD CONFIGURATION (Volatile Storage)
#──────────────────────────────────────────────────────────────────

# Overridable for the offline test suite (scripts/bootstrap_network_safety_tests.py).
MCAPP_FSTAB="${MCAPP_FSTAB:-/etc/fstab}"
MCAPP_JOURNALD_CONF_DIR="${MCAPP_JOURNALD_CONF_DIR:-/etc/systemd/journald.conf.d}"
MCAPP_JOURNAL_DIR="${MCAPP_JOURNAL_DIR:-/var/lib/mcapp/journal}"
readonly MCAPP_JOURNAL_BIND_LINE="/var/lib/mcapp/journal /var/log/journal none bind 0 0"

# Insert $line into $fstab after the first line matching $after_regex, once.
# Prints nothing; returns 0 when the line is present afterwards.
_mcapp_fstab_ensure_line() {
  local fstab="$1" line="$2" after_regex="$3"
  grep -qxF "$line" "$fstab" 2>/dev/null && return 0
  if grep -qE "$after_regex" "$fstab" 2>/dev/null; then
    # sed -i differs between GNU and BSD; go through a temp file instead.
    backup_config_file "$fstab"
    awk -v line="$line" -v re="$after_regex" 'BEGIN { done = 0 } { print } !done && $0 ~ re { print line; done = 1 }' \
      "$fstab" > "${fstab}.mcapp-tmp" && mv -f "${fstab}.mcapp-tmp" "$fstab"
  else
    backup_config_file "$fstab"
    printf '%s\n' "$line" >> "$fstab"
  fi
  grep -qxF "$line" "$fstab"
}

# Ramlog boards keep a native persistent journal (armbian-ramlog links
# /var/log/journal to /var/log.hdd/journal and syncToDisk() mv's a REAL
# /var/log/journal dir across filesystems), so we bind nothing there. Drops
# what earlier runs left behind: the epoch-5 fstab bind line and a
# var-log-journal.mount unit. Idempotent; reboot-required on change.
_mcapp_journal_ramlog_cleanup() {
  local ramlog="$1"
  local unit_file="${MCAPP_SYSTEMD_UNIT_DIR}/var-log-journal.mount"
  local changed=false

  if grep -qxF "$MCAPP_JOURNAL_BIND_LINE" "$MCAPP_FSTAB" 2>/dev/null; then
    backup_config_file "$MCAPP_FSTAB"
    grep -vxF "$MCAPP_JOURNAL_BIND_LINE" "$MCAPP_FSTAB" > "${MCAPP_FSTAB}.mcapp-tmp" || true
    mv -f "${MCAPP_FSTAB}.mcapp-tmp" "$MCAPP_FSTAB"
    changed=true
  fi

  if [[ -e "$unit_file" ]]; then
    systemctl disable var-log-journal.mount >/dev/null 2>&1 || true
    rm -f "$unit_file"
    systemctl daemon-reload 2>/dev/null || true
    changed=true
  fi

  if [[ "$changed" == "true" ]]; then
    _mcapp_mark_reboot_required
    log_ok "  ${ramlog} keeps its own journal: removed the McApp journal bind (reboot required)"
  else
    log_info "  ${ramlog} keeps its own journal: no McApp bind to remove"
  fi
}

# Persistent journal, capped small. /var/log is tmpfs (configure_tmpfs), so
# /var/log/journal is a bind mount of /var/lib/mcapp/journal on the root
# filesystem: a reboot or power cut keeps the last boots' logs. Before
# 2026-09-18 the journal was volatile and the WiFi outage of that day left
# no on-box evidence at all -- the only record was the copy shipped to
# rpizero, which ended at the second the link dropped
# (doc/archive/2026-09-18_2320-wifi-outage-postmortem.md). 16M of journal with
# journald's 5-minute sync interval is the SD-wear price of that evidence.
configure_journald() {
  log_info "Configuring journald (persistent, SystemMaxUse=16M on ${MCAPP_JOURNAL_DIR})..."

  local conf_dir="$MCAPP_JOURNALD_CONF_DIR"
  local conf_file="${conf_dir}/mcapp-journal.conf"
  local legacy_file="${conf_dir}/mcapp-volatile.conf"

  mkdir -p "$conf_dir"

  local ramlog
  ramlog="$(detect_ramlog_service)"
  if [[ -n "$ramlog" ]]; then
    _mcapp_journal_ramlog_cleanup "$ramlog"
  else
    mkdir -p "$MCAPP_JOURNAL_DIR"
    # journald's own layout: root:systemd-journal, setgid, so journal files
    # inherit the group and `journalctl` works for that group's members.
    chown root:systemd-journal "$MCAPP_JOURNAL_DIR" 2>/dev/null || true
    chmod 2755 "$MCAPP_JOURNAL_DIR" 2>/dev/null || true
    _mcapp_fstab_ensure_line "$MCAPP_FSTAB" "$MCAPP_JOURNAL_BIND_LINE" '^tmpfs /var/log tmpfs' \
      || log_warn "  could not add the journal bind mount to ${MCAPP_FSTAB}"
  fi

  # Idempotent by content, not by file-exists: an install carrying the
  # pre-epoch-5 volatile drop-in must actually converge.
  if [[ -f "$conf_file" ]] && grep -q '^SystemMaxUse=16M$' "$conf_file" && [[ ! -f "$legacy_file" ]]; then
    log_info "  journald already configured (persistent, SystemMaxUse=16M)"
    return 0
  fi

  backup_config_file "$conf_file"
  cat > "$conf_file" << 'EOF'
# McApp: persistent journal, capped small. On a tmpfs /var/log, /var/log/journal
# is a bind mount of /var/lib/mcapp/journal (see /etc/fstab, McApp tmpfs block);
# on an Armbian/Orange Pi ramlog board the ramlog persists it itself. Either
# way the last boots' logs survive a reboot or a power cut.
[Journal]
Storage=persistent
SystemMaxUse=16M
SystemKeepFree=50M
SystemMaxFileSize=4M
RuntimeMaxUse=8M
RuntimeKeepFree=10M
RuntimeMaxFileSize=5M
MaxRetentionSec=1week
EOF
  rm -f "$legacy_file"

  # Mount now when we are on the live box; a test run has no mount to make.
  if [[ -z "$ramlog" && -d /var/log/journal ]] && ! mountpoint -q /var/log/journal 2>/dev/null; then
    mount /var/log/journal 2>/dev/null || log_warn "  /var/log/journal bind mount deferred to next reboot"
  fi

  log_ok "  journald configured (persistent, SystemMaxUse=16M)"

  # Restart journald to apply
  systemctl restart systemd-journald 2>/dev/null || true
}

# Pin wpasupplicant to Debian's 2:2.10-24 on trixie. Raspberry Pi's
# 2:2.10-24+rpt1 (2026-09-17) makes wpa_supplicant advertise WPA3-SAE to
# NetworkManager whenever brcmfmac sets NL80211_FEATURE_SAE; NetworkManager
# 1.52 then forces SAE for every wpa-psk profile, and the Zero 2 W's
# brcmfmac43436 never completes the handshake against a WPA2/WPA3
# transition-mode AP. trixie's NetworkManager has no opt-out (the upstream
# fix is in 1.56+). raspberrypi/linux#7634. Remove once that is resolved.
MCAPP_APT_PREFERENCES_DIR="${MCAPP_APT_PREFERENCES_DIR:-/etc/apt/preferences.d}"

configure_wpasupplicant_pin() {
  log_info "Configuring wpasupplicant pin (WPA3-SAE regression guard)..."

  local codename
  codename=$(get_debian_codename)
  if [[ "$codename" != "trixie" ]]; then
    log_info "  Not trixie (${codename}); no pin needed"
    return 0
  fi

  local file="${MCAPP_APT_PREFERENCES_DIR}/mcapp-wpasupplicant"
  local content
  content=$(cat << 'EOF'
# McApp: keep wpasupplicant on Debian's 2:2.10-24 (raspberrypi/linux#7634).
# Raspberry Pi's 2:2.10-24+rpt1 (2026-09-17) reports WPA3-SAE over D-Bus,
# NetworkManager 1.52 then forces SAE for wpa-psk profiles, and the Zero 2 W's
# brcmfmac43436 cannot complete it against a WPA2/WPA3 transition-mode AP:
# the box drops off WiFi and does not come back after a reboot.
# Managed by McApp bootstrap (configure_wpasupplicant_pin). Remove with the
# bootstrap once the AP is WPA2-only or the driver/firmware does SAE.
Package: wpasupplicant
Pin: version 2:2.10-24
Pin-Priority: 1001
EOF
  )

  if [[ -f "$file" ]] && [[ "$(<"$file")" == "$content" ]]; then
    log_info "  wpasupplicant pin already in place"
  else
    mkdir -p "$MCAPP_APT_PREFERENCES_DIR"
    backup_config_file "$file"
    printf '%s\n' "$content" > "$file"
    chmod 644 "$file"
    log_ok "  wpasupplicant pinned to 2:2.10-24 (${file})"
  fi
  # The hand-written predecessor from the 2026-09-18 recovery on mcapp.local.
  rm -f "${MCAPP_APT_PREFERENCES_DIR}/wpasupplicant-pin"
  return 0
}

#──────────────────────────────────────────────────────────────────
# LOGROTATE (Reduce retention for SD card longevity)
#──────────────────────────────────────────────────────────────────

configure_logrotate() {
  log_info "Reducing logrotate retention..."

  if [[ ! -f /etc/logrotate.conf ]]; then
    log_info "  logrotate.conf not found, skipping"
    return 0
  fi

  if grep -qE '^\s*rotate\s+2\b' /etc/logrotate.conf 2>/dev/null; then
    log_info "  logrotate already set to rotate 2"
    return 0
  fi

  backup_config_file /etc/logrotate.conf
  sed -i 's/^\s*rotate\s\+[0-9]\+/rotate 2/' /etc/logrotate.conf
  log_ok "  logrotate set to rotate 2"
}

#──────────────────────────────────────────────────────────────────
# FIREWALL CONFIGURATION (nftables)
#──────────────────────────────────────────────────────────────────

configure_firewall() {
  log_info "Configuring firewall..."

  local codename
  codename=$(get_debian_codename)

  # Use nftables for Trixie+, iptables for older
  if [[ "$codename" == "trixie" ]] || [[ "$codename" == "sid" ]]; then
    configure_nftables
  else
    configure_iptables_legacy
  fi
}

# Overridable for the offline test suite (scripts/bootstrap_firewall_tests.py).
MCAPP_NFT_CONF="${MCAPP_NFT_CONF:-/etc/nftables.conf}"
MCAPP_NFT_CUSTOM_DIR="${MCAPP_NFT_CUSTOM_DIR:-/etc/mcapp/nftables.d}"
MCAPP_NFT_DROPIN_DIR="${MCAPP_NFT_DROPIN_DIR:-/etc/systemd/system/nftables.service.d}"
MCAPP_IPT_RULES="${MCAPP_IPT_RULES:-/etc/iptables/rules.v4}"

# Operators put their own rules in MCAPP_NFT_CUSTOM_DIR/*.nft; the generated
# ruleset includes that folder. This writes the (non-loading) example file.
_mcapp_nft_ensure_custom_dir() {
  local dir="$MCAPP_NFT_CUSTOM_DIR"
  mkdir -p "$dir" || { log_warn "  could not create $dir"; return 0; }
  local example="$dir/custom.nft.example"
  [[ -f "$example" ]] && return 0
  cat > "$example" << EOF || true
# McApp custom firewall rules
#
# Copy this file to a name ending in .nft (for example custom.nft). Every
# $dir/*.nft file is included by /etc/nftables.conf INSIDE the
# "input" chain of "table inet filter", so put chain rules here, not tables.
# This .example file is never loaded. McApp updates keep this folder.
#
# Example: open TCP port 8080
#
#   tcp dport 8080 accept
#
# Apply with: sudo systemctl reload nftables
EOF
}

# Boot safeguard: if a custom file breaks nftables.conf, retry without the
# include so the box never boots with no firewall at all.
_mcapp_nft_ensure_dropin() {
  local dir="$MCAPP_NFT_DROPIN_DIR"
  local file="$dir/mcapp-fallback.conf"
  local tmp
  tmp=$(mktemp "${TMPDIR:-/tmp}/mcapp-dropin.XXXXXX") || return 0
  cat > "$tmp" << EOF
[Service]
ExecStart=
ExecStart=/bin/sh -c '/usr/sbin/nft -f ${MCAPP_NFT_CONF} || { echo "McApp: nftables.conf failed, retrying without custom rules" >&2; grep -vF "${MCAPP_NFT_CUSTOM_DIR}" ${MCAPP_NFT_CONF} | /usr/sbin/nft -f -; }'
EOF
  if [[ -f "$file" ]] && cmp -s "$tmp" "$file"; then
    rm -f "$tmp"
    return 0
  fi
  backup_config_file "$file"
  if mkdir -p "$dir" && cat "$tmp" > "$file"; then
    systemctl daemon-reload || log_warn "  systemctl daemon-reload failed"
    log_info "  nftables boot fallback drop-in written"
  else
    log_warn "  could not write $file"
  fi
  rm -f "$tmp"
  return 0
}

# Legacy triggers: succeeds (0) when the marked file predates a rule we need.
_mcapp_nft_legacy_needs_update() {
  local nft_conf="$1"
  local needs_update=false

  # Update if ports 2980/2981 are still present (legacy config)
  if grep -q "dport 2980\|dport 2981" "$nft_conf" 2>/dev/null; then
    log_info "  Updating nftables rules (removing legacy ports 2980/2981)..."
    needs_update=true
  fi

  # Update if LAN SSH exemption is missing
  if ! grep -q "192.168.0.0/16" "$nft_conf" 2>/dev/null; then
    log_info "  Updating nftables rules (adding LAN SSH exemption)..."
    needs_update=true
  fi

  # Update if log rate limit is still 5/min (old value)
  if grep -q "limit rate 5/minute" "$nft_conf" 2>/dev/null; then
    log_info "  Updating nftables rules (increasing log rate limit to 10/min)..."
    needs_update=true
  fi

  # Update if port 2985 (update runner) is missing
  if ! grep -q "dport 2985" "$nft_conf" 2>/dev/null; then
    log_info "  Updating nftables rules (adding update runner port 2985)..."
    needs_update=true
  fi

  # Update if port 443 (Caddy HTTPS front door) is missing — Wave 0.
  # Without this clause, already-marked installs would SKIP the rewrite and
  # never open :443, leaving LAN/iPhone HTTPS clients firewall-dropped.
  if ! grep -q "dport 443" "$nft_conf" 2>/dev/null; then
    log_info "  Updating nftables rules (adding Caddy HTTPS port 443)..."
    needs_update=true
  fi

  # Update if AIOps journal cross-shipping port 19532 is missing (AIOps AD-8, B-4).
  if ! grep -q "dport 19532" "$nft_conf" 2>/dev/null; then
    log_info "  Updating nftables rules (adding AIOps journal-xship port 19532)..."
    needs_update=true
  fi

  # Update if UDP 443 (QUIC/HTTP3 for Caddy) is missing.
  if ! grep -q "udp dport 443" "$nft_conf" 2>/dev/null; then
    log_info "  Updating nftables rules (adding Caddy QUIC/HTTP3 UDP port 443)..."
    needs_update=true
  fi

  [[ "$needs_update" == "true" ]]
}

# Apply a changed ruleset. Never `restart`: nftables.service ExecStop flushes
# EVERY table, including other tools' (docker, fail2ban, ...).
# $1 = fresh (enable + reload-or-restart) | existing (reload only if active)
_mcapp_nft_apply() {
  local mode="$1"
  if [[ "$mode" == "fresh" ]]; then
    systemctl enable nftables || log_warn "  systemctl enable nftables failed"
    if ! systemctl reload-or-restart nftables; then
      log_warn "  nftables did not (re)load; check: systemctl status nftables"
    fi
    return 0
  fi
  if systemctl is-active --quiet nftables; then
    if ! systemctl reload nftables; then
      log_warn "  nftables reload failed; the new rules are in ${MCAPP_NFT_CONF} but not live"
    fi
  else
    log_warn "  nftables.service is inactive; leaving it off (firewall switched off by the operator?)"
  fi
  return 0
}

# Does this file hold rules of its own? Debian's stock file has only empty chains, so
# anything beyond the table skeleton counts. A missing file has none.
_mcapp_nft_has_own_rules() {
  [[ -f "$1" ]] || return 1
  # One awk, no pipe: under pipefail a `grep | grep -q` on a large file reports failure
  # through SIGPIPE, which would read as "no rules" and replace the operator's file.
  # `policy drop` counts as a rule: an empty deny-all chain is a firewall.
  local rc=0
  awk '
    /^[[:space:]]*(#.*)?$/ { next }
    /^[[:space:]]*(flush ruleset|table [^{;]*\{?|chain [^{;]*\{?|\})[[:space:]]*$/ { next }
    /^[[:space:]]*type [a-z]+ hook [^;]*;([[:space:]]*policy accept;)?[[:space:]]*$/ { next }
    { found = 1; exit }
    END { exit !found }
  ' "$1" 2>/dev/null || rc=$?
  # 0 = rules found, 1 = none. Anything else means awk could not judge the file: keep it.
  [[ "$rc" -ne 1 ]]
}

# Marked file, no legacy trigger: never rewrite. Stage a copy with the minimal
# edits (custom include, table-scoped delete instead of flush), validate, then
# install.
_mcapp_nft_migrate_existing() {
  local nft_conf="$MCAPP_NFT_CONF"
  local inc_line="    include \"${MCAPP_NFT_CUSTOM_DIR}/*.nft\""
  local staged staged2
  staged=$(mktemp "${TMPDIR:-/tmp}/mcapp-nft.XXXXXX") || return 0
  staged2=$(mktemp "${TMPDIR:-/tmp}/mcapp-nft.XXXXXX") || { rm -f "$staged"; return 0; }
  cat "$nft_conf" > "$staged"

  # Include after the HTTP accept rule, once.
  if ! grep -F -- "$MCAPP_NFT_CUSTOM_DIR" "$staged" | grep -q '^[[:space:]]*include'; then
    if grep -q '^[[:space:]]*tcp dport 80 accept[[:space:]]*$' "$staged"; then
      awk -v inc="$inc_line" '
        { print }
        !done && $0 ~ /^[[:space:]]*tcp dport 80 accept[[:space:]]*$/ { print inc; done = 1 }
      ' "$staged" > "$staged2" && cat "$staged2" > "$staged"
    else
      log_warn "  no 'tcp dport 80 accept' line in ${nft_conf}; custom rules include not added"
    fi
  fi

  # flush ruleset -> `table inet filter` + `delete table inet filter` (the
  # portable idiom: `destroy` needs kernel 6.3+), unless the file owns other
  # tables or has a top-level include, whose rules a flush-less reload would
  # duplicate.
  if grep -q '^[[:space:]]*flush ruleset[[:space:]]*$' "$staged"; then
    if grep -E '^[[:space:]]*table[[:space:]]' "$staged" |
      grep -qvE '^[[:space:]]*table[[:space:]]+inet[[:space:]]+filter([[:space:]]|\{|$)'; then
      log_info "  ${nft_conf} defines more than table inet filter; keeping 'flush ruleset'"
    elif grep -q '^include' "$staged"; then
      log_info "  ${nft_conf} has a top-level include; keeping 'flush ruleset'"
    else
      awk '
        /^[[:space:]]*flush ruleset[[:space:]]*$/ { print "table inet filter"; print "delete table inet filter"; next }
        { print }
      ' "$staged" > "$staged2" && cat "$staged2" > "$staged"
    fi
  fi
  rm -f "$staged2"

  if cmp -s "$nft_conf" "$staged"; then
    rm -f "$staged"
    log_info "  nftables rules already configured"
    _mcapp_nft_ensure_dropin
    return 0
  fi

  # Validate before touching the live file.
  local out
  if ! out=$(nft -c -f "$staged" 2>&1); then
    log_warn "  staged nftables.conf failed validation; ${nft_conf} left unchanged: $out"
    rm -f "$staged"
    return 0
  fi
  if ! grep -q 'dport 22' "$staged"; then
    log_warn "  staged nftables.conf has no SSH rule; ${nft_conf} left unchanged"
    rm -f "$staged"
    return 0
  fi

  backup_config_file "$nft_conf"
  local new="${nft_conf}.mcapp-new"
  if cp -p "$nft_conf" "$new" && cat "$staged" > "$new" && mv -f "$new" "$nft_conf"; then
    log_ok "  nftables.conf updated in place (custom rules kept)"
  else
    rm -f "$new" "$staged"
    log_warn "  could not install the new ${nft_conf}; left unchanged"
    return 0
  fi
  rm -f "$staged"
  _mcapp_nft_ensure_dropin
  _mcapp_nft_apply existing
  return 0
}

# Extra sshd ports (everything but 22), one per line. sshd absent or failing
# yields nothing and never aborts: only the wholesale-replace paths need this,
# so a box on Port 2222 is not locked out by the rewrite.
_mcapp_ssh_extra_ports() {
  local out="" ports="" p seen=" "
  if command -v sshd > /dev/null 2>&1; then
    if out=$(sshd -T 2> /dev/null); then
      # A ListenAddress can carry the port instead, and newer OpenSSH prints "Port".
      ports=$(printf '%s\n' "$out" | awk '
        tolower($1) == "port" { print $2 }
        tolower($1) == "listenaddress" { n = split($2, a, ":"); print a[n] }')
    else
      log_warn "  could not read the SSH ports (sshd -T failed); only port 22 is opened" >&2
    fi
  fi
  # The port this session came in on, in case sshd could not tell us. Field 4 is the
  # server port; sudo may strip the variable, so it is only a second source.
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    ports="$ports $(printf '%s\n' "$SSH_CONNECTION" | awk '{print $4}')"
  fi
  for p in $ports; do
    # An invalid port would make nft refuse the whole ruleset. 10# keeps 022 from octal.
    [[ "$p" =~ ^[0-9]{1,5}$ ]] || continue
    p=$((10#$p))
    ((p >= 1 && p <= 65535)) || continue
    [[ "$p" == "22" ]] && continue
    case "$seen" in *" $p "*) continue ;; esac
    seen="$seen$p "
    printf '%s\n' "$p"
  done
  return 0
}

# Generated full ruleset on stdout. `table inet filter` + `delete table inet
# filter` (not `flush ruleset`, not `destroy`: that needs kernel 6.3+) resets
# our own table and leaves other tools' tables alone.
_mcapp_nft_render() {
  local p
  {
    cat << 'EOF'
#!/usr/sbin/nft -f
# McApp firewall rules
# Generated by mcapp.sh bootstrap
#
# Required ports:
#   22/tcp   - SSH (rate limited, LAN exempt)
#   80/tcp   - HTTP (Caddy front door → lighttpd/FastAPI)
#   443/tcp  - HTTPS (Caddy front door — Wave 0)
#   1799/udp - MeshCom node communication
#   5353/udp - mDNS (avahi for .local resolution)
#   2985/tcp - Update runner SSE stream
#   19532/tcp - AIOps journal cross-shipping (Pi↔Pi, LAN only)
#   443/udp  - QUIC/HTTP3 for Caddy (optional; TCP/443 already covers HTTP/2 fallback)
#
# Internal only (not exposed):
#   2981/tcp - FastAPI SSE/REST (proxied via Caddy→lighttpd)
#   8082/tcp - lighttpd backend (127.0.0.1 only, behind Caddy)
#
# Own rules: put *.nft files into the custom folder included below.

table inet filter
delete table inet filter

table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;

    # Allow loopback interface
    iif lo accept

    # Allow established/related connections
    ct state established,related accept

    # Drop invalid packets
    ct state invalid drop

    # SSH - allow local LAN without rate limiting
    ip saddr { 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12 } tcp dport 22 accept

    # SSH - rate limit external connections (6 new per minute)
    tcp dport 22 ct state new limit rate 6/minute accept
EOF
    for p in $(_mcapp_ssh_extra_ports); do
      printf '\n    # SSH on a non-default port\n    tcp dport %s accept\n' "$p"
    done
    cat << 'EOF'

    # HTTP (Caddy front door → lighttpd/FastAPI)
    tcp dport 80 accept
EOF
    printf '    include "%s/*.nft"\n' "$MCAPP_NFT_CUSTOM_DIR"
    cat << 'EOF'

    # HTTPS (Caddy HTTPS front door — Wave 0; LAN/iPhone clients hit :443)
    tcp dport 443 accept

    # QUIC/HTTP3 for Caddy (optional latency win on LAN; TCP/443 already covers fallback)
    udp dport 443 accept

    # Update runner SSE stream (frontend-triggered updates)
    tcp dport 2985 accept

    # AIOps journal cross-shipping (Pi↔Pi, LAN only — AIOps AD-8/B-4)
    ip saddr { 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12 } tcp dport 19532 accept

    # MeshCom UDP communication
    udp dport 1799 accept

    # mDNS for .local hostname resolution (avahi-daemon)
    # Restrict to multicast destinations per mDNS protocol spec
    ip daddr 224.0.0.251 udp dport 5353 accept

    # ICMP (ping) - useful for diagnostics
    icmp type echo-request accept

    # Silently drop common broadcast/multicast traffic to prevent log spam

    # Broadcast packets (layer 2 broadcast MAC)
    meta pkttype broadcast drop

    # Multicast packets (layer 2 multicast MAC)
    meta pkttype multicast drop

    # Global broadcast
    ip daddr 255.255.255.255 drop

    # Subnet-directed broadcasts
    ip daddr & 0.0.0.255 == 0.0.0.255 drop

    # SSDP/UPnP device discovery
    udp dport 1900 drop

    # mDNS unicast queries (already allowed multicast mDNS above)
    udp sport 5353 drop
    udp dport 5353 drop

    # Common high UDP ports (gaming, P2P, etc)
    udp dport > 30000 drop

    # IGMP multicast group management
    meta l4proto igmp drop

    # NetBIOS Name Service
    udp dport { 137, 138 } drop

    # LLMNR (Link-Local Multicast Name Resolution - Windows)
    udp dport 5355 drop

    # Common multicast addresses
    ip daddr { 224.0.0.0/4, 239.0.0.0/8 } drop

    # Log remaining dropped packets (limited to prevent log spam)
    limit rate 10/minute log prefix "[nftables DROP] " flags all counter drop
  }

  chain forward {
    type filter hook forward priority 0; policy drop;
  }

  chain output {
    type filter hook output priority 0; policy accept;
  }
}
EOF
  }
}

# Render, validate, then install. A failing check (for example a faulty file
# in the custom folder) keeps the existing file and applies nothing; the result
# is reported through _MCAPP_NFT_WROTE, the return code is always 0.
_mcapp_nft_write_full() {
  local nft_conf="$MCAPP_NFT_CONF"
  local staged out new="${MCAPP_NFT_CONF}.mcapp-new"
  _MCAPP_NFT_WROTE=false
  staged=$(mktemp "${TMPDIR:-/tmp}/mcapp-nft.XXXXXX") || {
    log_warn "  could not stage the nftables ruleset; ${nft_conf} left unchanged"
    return 0
  }
  _mcapp_nft_render > "$staged"
  if ! out=$(nft -c -f "$staged" 2>&1); then
    log_warn "  generated nftables ruleset failed validation; ${nft_conf} left unchanged and McApp's firewall rules are NOT active: $out"
    rm -f "$staged"
    return 0
  fi
  if cat "$staged" > "$new" && mv -f "$new" "$nft_conf"; then
    _MCAPP_NFT_WROTE=true
  else
    rm -f "$new"
    log_warn "  could not install the new ${nft_conf}; left unchanged"
  fi
  rm -f "$staged"
  return 0
}

configure_nftables() {
  log_info "  Using nftables (Trixie)"

  # Install nftables if not present
  if ! command -v nft &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq nftables
  fi

  local nft_conf="$MCAPP_NFT_CONF"
  local marker="# McApp firewall rules"
  local had_marker=false legacy=false

  if [[ -f "$nft_conf" ]] && grep -q "$marker" "$nft_conf" 2>/dev/null; then
    had_marker=true
    if _mcapp_nft_legacy_needs_update "$nft_conf"; then
      legacy=true
    fi
  fi

  if [[ "$had_marker" == "true" && "$legacy" == "false" ]]; then
    _mcapp_nft_ensure_custom_dir
    _mcapp_nft_migrate_existing
    return 0
  fi

  # A file with real rules and no marker belongs to the operator or another tool.
  # Replacing it closed an operator's :8080 on a converge (field report 2026-11-06).
  if [[ "$had_marker" == "false" ]] && _mcapp_nft_has_own_rules "$nft_conf"; then
    log_warn "  ${nft_conf} has its own rules and no McApp marker: keeping it, McApp does not manage this firewall. For McApp to work it must allow loopback, 22, 80, 443 and 2985/tcp, and 1799 and 5353/udp."
    return 0
  fi

  _mcapp_nft_ensure_custom_dir

  # Full (re)write: fresh box, a rule-free stock file, or a marked legacy file.
  if [[ -f "$nft_conf" ]]; then
    backup_config_file "$nft_conf"
    cp "$nft_conf" "${nft_conf}.bak.$(date +%Y%m%d)" || true
  fi

  _mcapp_nft_write_full
  if [[ "$_MCAPP_NFT_WROTE" != "true" ]]; then
    return 0
  fi
  _mcapp_nft_ensure_dropin

  if [[ "$had_marker" == "true" ]]; then
    log_warn "  Rewrote ${nft_conf}: custom rules in the old file were not carried over; copy in ${MCAPP_CONFIG_BACKUP_DIR:-/var/lib/mcapp/config-backups}"
    _mcapp_nft_apply existing
  else
    _mcapp_nft_apply fresh
  fi

  log_ok "  nftables firewall configured"
}

# One list of INPUT rules for Bookworm, applied by _mcapp_ipt_add.
# _MCAPP_IPT_MODE: append (fresh install, after a flush) | check (existing box:
# add only what is missing, never flush, so an operator's rules survive).
_mcapp_ipt_add() {
  if [[ "$_MCAPP_IPT_MODE" == "append" ]]; then
    iptables -A INPUT "$@"
  else
    iptables -C INPUT "$@" 2>/dev/null || iptables -A INPUT "$@"
  fi
}

_mcapp_ipt_apply_rules() {
  _MCAPP_IPT_MODE="$1"

  # Allow loopback
  _mcapp_ipt_add -i lo -j ACCEPT

  # Allow established connections
  _mcapp_ipt_add -m state --state ESTABLISHED,RELATED -j ACCEPT

  # SSH - allow local LAN without rate limiting
  _mcapp_ipt_add -p tcp --dport 22 -s 192.168.0.0/16 -j ACCEPT
  _mcapp_ipt_add -p tcp --dport 22 -s 10.0.0.0/8 -j ACCEPT
  _mcapp_ipt_add -p tcp --dport 22 -s 172.16.0.0/12 -j ACCEPT

  # SSH - rate limit external connections (4 per minute)
  _mcapp_ipt_add -p tcp --dport 22 -m state --state NEW -m recent --set
  _mcapp_ipt_add -p tcp --dport 22 -m state --state NEW -m recent --update --seconds 60 --hitcount 4 -j DROP
  _mcapp_ipt_add -p tcp --dport 22 -j ACCEPT

  # SSH on a non-default port: only where the rules replace the old ones
  # wholesale (first install). An existing box only gets additions.
  if [[ "$_MCAPP_IPT_MODE" == "append" ]]; then
    local p
    for p in $(_mcapp_ssh_extra_ports); do
      _mcapp_ipt_add -p tcp --dport "$p" -j ACCEPT
    done
  fi

  # HTTP (Caddy front door → lighttpd/FastAPI)
  _mcapp_ipt_add -p tcp --dport 80 -j ACCEPT

  # HTTPS (Caddy HTTPS front door — Wave 0; LAN/iPhone clients hit :443)
  _mcapp_ipt_add -p tcp --dport 443 -j ACCEPT

  # Update runner SSE stream (frontend-triggered updates)
  _mcapp_ipt_add -p tcp --dport 2985 -j ACCEPT

  # MeshCom UDP communication
  _mcapp_ipt_add -p udp --dport 1799 -j ACCEPT

  # mDNS for .local hostname resolution (restricted to multicast only)
  _mcapp_ipt_add -p udp -d 224.0.0.251 --dport 5353 -j ACCEPT

  # ICMP (ping)
  _mcapp_ipt_add -p icmp --icmp-type echo-request -j ACCEPT

  # Silent drops for common broadcast/multicast traffic (prevents log spam)

  # Broadcast packets
  _mcapp_ipt_add -m addrtype --dst-type BROADCAST -j DROP

  # Common multicast addresses
  _mcapp_ipt_add -d 224.0.0.0/4 -j DROP
  _mcapp_ipt_add -d 239.0.0.0/8 -j DROP

  # Global broadcast
  _mcapp_ipt_add -d 255.255.255.255 -j DROP

  # SSDP/UPnP device discovery
  _mcapp_ipt_add -p udp --dport 1900 -j DROP

  # NetBIOS name service
  _mcapp_ipt_add -p udp --dport 137 -j DROP
  _mcapp_ipt_add -p udp --dport 138 -j DROP

  # LLMNR (Windows name resolution)
  _mcapp_ipt_add -p udp --dport 5355 -j DROP

  # High UDP ports (gaming, P2P, etc)
  _mcapp_ipt_add -p udp --dport 30000:65535 -j DROP

  # IGMP multicast management
  _mcapp_ipt_add -p igmp -j DROP

  # Log remaining drops (rate limited to 10/min to prevent spam)
  _mcapp_ipt_add -m limit --limit 10/min -j LOG --log-prefix "[iptables DROP] " --log-level 7
}

configure_iptables_legacy() {
  log_info "  Using iptables (Bookworm fallback)"

  # Install iptables-persistent if not present
  if ! dpkg -l iptables-persistent &>/dev/null; then
    # Pre-answer debconf questions to avoid interactive prompt
    echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
    echo iptables-persistent iptables-persistent/autosave_v6 boolean true | debconf-set-selections
    apt-get update -qq
    apt-get install -y -qq iptables-persistent
  fi

  # First install only when McApp left no trace: no rules file with our LOG
  # prefix AND no system-epoch marker. A deleted LOG rule or a lost rules.v4
  # on a marked box must not wipe the operator's rules.
  local epoch_file="${SYSTEM_EPOCH_FILE:-/var/lib/mcapp/system-epoch}"
  local first_install=false
  if [[ ! -f "$epoch_file" ]] &&
    ! { [[ -f "$MCAPP_IPT_RULES" ]] && grep -qF '[iptables DROP]' "$MCAPP_IPT_RULES" 2> /dev/null; }; then
    first_install=true
  fi

  if [[ "$first_install" == "true" ]]; then
    # First install: start from a clean slate. Open INPUT first: on a box already at
    # default drop, an abort between the flush and the rules would be a lockout.
    iptables -P INPUT ACCEPT
    iptables -F
    iptables -X

    _mcapp_ipt_apply_rules append

    # Policies last: an abort above must not leave default-drop with partial rules.
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    iptables -P OUTPUT ACCEPT
  else
    # Existing box: a firewall the operator switched off (policy ACCEPT and no
    # INPUT rule at all) stays off, like a stopped nftables service.
    local input_rules=""
    input_rules=$(iptables -S INPUT 2> /dev/null) || input_rules=""
    if grep -qx -- '-P INPUT ACCEPT' <<< "$input_rules" && ! grep -q -- '^-A INPUT' <<< "$input_rules"; then
      log_warn "  iptables INPUT is open (policy ACCEPT, no rules); leaving the firewall off (switched off by the operator?)"
      return 0
    fi
    # Add what is missing, touch nothing else. Policies are not reasserted: an
    # operator's change stands. iptables-save below captures the live rules,
    # so custom rules persist.
    _mcapp_ipt_apply_rules check
  fi

  # Save rules: temp file beside the target, then mv, so a failed iptables-save
  # cannot truncate the file whose LOG rule is the next run's marker.
  mkdir -p "$(dirname "$MCAPP_IPT_RULES")"
  backup_config_file "$MCAPP_IPT_RULES"
  local saved
  saved=$(mktemp "${MCAPP_IPT_RULES}.XXXXXX") || {
    log_warn "  could not stage ${MCAPP_IPT_RULES}; rules are live but not saved"
    return 0
  }
  if iptables-save > "$saved" && chmod 0644 "$saved" && mv -f "$saved" "$MCAPP_IPT_RULES"; then
    log_ok "  iptables firewall configured and saved"
  else
    rm -f "$saved"
    log_warn "  iptables-save failed; ${MCAPP_IPT_RULES} left unchanged (rules are live but not saved)"
  fi
}

#──────────────────────────────────────────────────────────────────
# BLUETOOTH RFKILL FIX
#──────────────────────────────────────────────────────────────────

configure_bluetooth() {
  log_info "Configuring Bluetooth rfkill unblock..."

  local service_file="/etc/systemd/system/unblock-bluetooth.service"

  # Check if already configured
  if [[ -f "$service_file" ]]; then
    log_info "  Bluetooth unblock service already configured"
    return 0
  fi

  # Check if rfkill is blocking bluetooth by default
  # This is common on Pi images with /etc/modprobe.d/rfkill_default.conf
  if [[ -f /etc/modprobe.d/rfkill_default.conf ]]; then
    if grep -q "default_state=0" /etc/modprobe.d/rfkill_default.conf 2>/dev/null; then
      log_info "  Detected rfkill default_state=0 (radios blocked at boot)"
    fi
  fi

  # Write the service unit inline (no external template dependency)
  backup_config_file "$service_file"
  cat > "$service_file" << 'EOF'
# McApp: Unblock Bluetooth at boot
# Some Pi images ship with rfkill default_state=0 which blocks all radios
# This service ensures Bluetooth is unblocked for BLE communication

[Unit]
Description=Unblock Bluetooth for McApp BLE
After=bluetooth.service
Before=mcapp.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/rfkill unblock bluetooth
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

  # Enable the service
  systemctl daemon-reload
  systemctl enable unblock-bluetooth

  # Unblock now if currently blocked
  if command -v rfkill &>/dev/null; then
    if rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; then
      rfkill unblock bluetooth
      log_info "  Bluetooth was blocked, unblocked now"
    fi
  fi

  log_ok "  Bluetooth unblock service installed and enabled"
}

#──────────────────────────────────────────────────────────────────
# DISABLE IPv6
#──────────────────────────────────────────────────────────────────

disable_ipv6() {
  log_info "Disabling IPv6..."

  local sysctl_conf="/etc/sysctl.d/99-disable-ipv6.conf"

  # Check if already configured
  if [[ -f "$sysctl_conf" ]]; then
    if grep -q "net.ipv6.conf.all.disable_ipv6.*=.*1" "$sysctl_conf" 2>/dev/null; then
      log_info "  IPv6 already disabled"
      return 0
    fi
  fi

  # Create sysctl config to disable IPv6
  backup_config_file "$sysctl_conf"
  cat > "$sysctl_conf" << 'EOF'
# McApp: Disable IPv6
# Fixes mDNS advertisements and eliminates Happy Eyeballs timeout
# when IPv6 firewall is closed
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF

  # Apply immediately
  if sysctl -p "$sysctl_conf" &>/dev/null; then
    log_ok "  IPv6 disabled (fixes mDNS timeouts)"
  else
    log_warn "  IPv6 config written, will apply on next reboot"
  fi
}

#──────────────────────────────────────────────────────────────────
# UNATTENDED UPGRADES
#──────────────────────────────────────────────────────────────────

configure_unattended_upgrades() {
  log_info "Configuring unattended security upgrades..."

  # Install if not present
  if ! dpkg -l unattended-upgrades &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq unattended-upgrades
  fi

  local codename
  codename=$(get_debian_codename)

  # Configure which updates to apply
  backup_config_file /etc/apt/apt.conf.d/50unattended-upgrades
  cat > /etc/apt/apt.conf.d/50unattended-upgrades << EOF
// McApp: Automatic security updates
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${codename},label=Debian-Security";
    "origin=Debian,codename=${codename}-security,label=Debian-Security";
};

// Don't automatically reboot
Unattended-Upgrade::Automatic-Reboot "false";

// Auto-fix interrupted dpkg
Unattended-Upgrade::AutoFixInterruptedDpkg "true";

// Clean up old packages
Unattended-Upgrade::Remove-Unused-Dependencies "true";

// Log to syslog
Unattended-Upgrade::SyslogEnable "true";
EOF

  # Enable automatic updates
  backup_config_file /etc/apt/apt.conf.d/20auto-upgrades
  cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

  log_ok "  Unattended upgrades configured (security updates only)"

  # B4 item 6 (doc/backlog.md): disable the unattended-upgrades.service unit
  # itself (7MB RSS + 13.5MB swap on mcapp.local) -- it is ONLY the
  # `unattended-upgrade-shutdown --wait-for-signal` hook that finishes an
  # in-flight upgrade at shutdown. apt-daily-upgrade.timer (untouched) keeps
  # running the actual upgrades on schedule regardless of this unit's state.
  # Do NOT re-enable this as a "fix" for missed updates -- the timer is the
  # thing doing the work; this unit only trims a shutdown-time nicety.
  # packages.sh's wait_for_apt_lock()/restore_unattended_upgrades() gate on
  # is-active/is-enabled, so once this unit reads disabled they correctly
  # treat it as "nothing to stop/restart" and leave it alone.
  systemctl disable --now unattended-upgrades.service 2>/dev/null || true
}

#──────────────────────────────────────────────────────────────────
# FAST SSH LOGIN (disable MOTD delays)
#──────────────────────────────────────────────────────────────────

configure_fast_ssh_login() {
  log_info "Speeding up SSH login..."

  # Disable MOTD scripts (each one adds latency to login)
  if [[ -d /etc/update-motd.d ]]; then
    local disabled=false
    for f in /etc/update-motd.d/*; do
      if [[ -x "$f" ]]; then
        chmod -x "$f"
        disabled=true
      fi
    done
    if [[ "$disabled" == "true" ]]; then
      log_info "  Disabled MOTD scripts"
    else
      log_info "  MOTD scripts already disabled"
    fi
  fi

  # Comment out pam_motd in sshd PAM config (avoids dynamic MOTD generation)
  if [[ -f /etc/pam.d/sshd ]]; then
    if grep -qE '^\s*session.*pam_motd' /etc/pam.d/sshd; then
      backup_config_file /etc/pam.d/sshd
      sed -i 's/^\(\s*session.*pam_motd\)/#\1/' /etc/pam.d/sshd
      log_info "  Disabled pam_motd in /etc/pam.d/sshd"
    else
      log_info "  pam_motd already disabled"
    fi
  fi

  log_ok "  Fast SSH login configured"
}

#──────────────────────────────────────────────────────────────────
# SSH HARDENING (crypto + access control)
#──────────────────────────────────────────────────────────────────

# KexAlgorithms for this box's OpenSSH: post-quantum hybrids first where the build
# has them (mlkem768 from OpenSSH 9.9, sntrup761 from 8.5), curve25519 as fallback.
# An algorithm sshd does not know fails `sshd -t`, so the list follows `ssh -Q kex`.
_mcapp_ssh_kex() {
  local supported alg kex=""
  supported=$(ssh -Q kex 2> /dev/null || true)
  for alg in mlkem768x25519-sha256 sntrup761x25519-sha512 sntrup761x25519-sha512@openssh.com; do
    if grep -qxF "$alg" <<< "$supported"; then
      kex+="${kex:+,}${alg}"
    fi
  done
  echo "${kex:+${kex},}curve25519-sha256,curve25519-sha256@libssh.org"
}

# The drop-in this box should have. RequiredRSASize exists from OpenSSH 9.1 on;
# older builds would fail `sshd -t` on it, so it is added only where known.
_mcapp_ssh_hardening_conf() {
  local rsa_line=""
  if ssh -F /dev/null -G mcapp-probe 2> /dev/null | grep -q '^requiredrsasize '; then
    rsa_line="RequiredRSASize 3072"
  fi
  cat << EOF
# McApp SSH hardening — crypto + access control
HostKey /etc/ssh/ssh_host_ed25519_key
HostKeyAlgorithms ssh-ed25519
Ciphers aes256-gcm@openssh.com,chacha20-poly1305@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
KexAlgorithms $(_mcapp_ssh_kex)
${rsa_line:+${rsa_line}
}UseDNS no
X11Forwarding no
PermitRootLogin no
MaxAuthTries 3
MaxSessions 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
}

configure_ssh_hardening() {
  log_info "Configuring SSH hardening..."

  local ssh_conf_dir="${MCAPP_SSHD_CONF_DIR:-/etc/ssh/sshd_config.d}"
  local ssh_conf="${ssh_conf_dir}/mcapp-hardening.conf"

  mkdir -p "$ssh_conf_dir"

  # pi-harden owns SSH hardening where it is installed
  if [[ -f "${ssh_conf_dir}/10-pi-harden.conf" ]]; then
    log_info "  SSH hardening managed by pi-harden"
    return 0
  fi

  # Our own file is brought up to date, not just created: boxes from before epoch 10
  # carry a curve25519-only KexAlgorithms that shuts out the post-quantum hybrids.
  local desired
  desired=$(_mcapp_ssh_hardening_conf)
  if [[ -f "$ssh_conf" && "$(cat "$ssh_conf")" == "$desired" ]]; then
    log_info "  SSH hardening already configured"
    return 0
  fi

  local had_old=0 old=""
  if [[ -f "$ssh_conf" ]]; then
    had_old=1
    old=$(cat "$ssh_conf")
  fi

  backup_config_file "$ssh_conf"
  printf '%s\n' "$desired" > "$ssh_conf"

  # Validate sshd config before restarting; on failure put back what was there
  if sshd -t 2> /dev/null; then
    systemctl restart sshd || true
    log_ok "  SSH hardening applied (ed25519-only, post-quantum kex, modern ciphers)"
  elif ((had_old)); then
    printf '%s\n' "$old" > "$ssh_conf"
    log_warn "  SSH config invalid, previous hardening kept"
  else
    rm -f "$ssh_conf"
    log_warn "  SSH config invalid, hardening skipped"
  fi
}
