#!/bin/bash
# system.sh - System configuration for MCProxy bootstrap
# Handles: tmpfs, firewall, journald, service hardening, SSH, logrotate
# Incorporates all pi-harden.sh functionality for SD card longevity & security

#──────────────────────────────────────────────────────────────────
# MAIN SYSTEM SETUP FUNCTION
#──────────────────────────────────────────────────────────────────

setup_system() {
  configure_locale
  disable_unused_services
  configure_tmpfs
  configure_journald
  configure_logrotate
  configure_firewall
  configure_bluetooth
  configure_unattended_upgrades
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
      sed -i "s/^#\s*\(${loc}\)/\1/" "$locale_gen"
      needs_generate=true
      log_info "  Enabled ${loc} in locale.gen"
    elif ! grep -q "^${loc}" "$locale_gen" 2>/dev/null; then
      # Not present at all — add it
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

  # DO NOT disable these (required for MCProxy):
  # - avahi-daemon (mDNS for .local resolution)
  # - bluetooth (BLE for ESP32 communication)
  # - wpa_supplicant (WiFi connectivity)

  log_ok "  Unused services disabled"
}

#──────────────────────────────────────────────────────────────────
# TMPFS CONFIGURATION (SD Card Protection)
#──────────────────────────────────────────────────────────────────

configure_tmpfs() {
  log_info "Configuring tmpfs for SD card longevity..."

  local fstab="/etc/fstab"
  local marker="# MCProxy tmpfs"

  # Check if already configured (also accept pi-harden marker)
  if grep -qE "(MCProxy tmpfs|pi-harden)" "$fstab" 2>/dev/null; then
    log_info "  tmpfs already configured in fstab"
  else
    # Backup fstab
    cp "$fstab" "${fstab}.bak.$(date +%Y%m%d)"

    # Add tmpfs entries — nodev,noexec on /var/log for security
    cat >> "$fstab" << EOF

${marker} - begin
tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,noexec,size=30M 0 0
tmpfs /tmp tmpfs defaults,noatime,nosuid,mode=1777,size=50M 0 0
${marker} - end
EOF

    log_ok "  tmpfs entries added to fstab"
  fi

  # Create log directory structure in tmpfs after mount
  mkdir -p /etc/tmpfiles.d
  cat > /etc/tmpfiles.d/mcproxy.conf << 'EOF'
# MCProxy + system log directories (recreated on every boot in tmpfs)
d /var/log/mcproxy   0755 root     root     -
d /var/log/lighttpd  0755 www-data www-data -
d /var/log/journal   0755 root     root     -
d /var/log/apt       0755 root     root     -
d /var/log/private   0700 root     root     -
d /var/log/chrony    0755 _chrony  _chrony  -
d /var/log/unattended-upgrades 0755 root root -
EOF

  # Try to mount /var/log now if not already tmpfs
  if findmnt -n /var/log 2>/dev/null | grep -q tmpfs; then
    log_info "  /var/log already mounted as tmpfs"
  else
    # Create dirs before mount so services don't break
    systemd-tmpfiles --create /etc/tmpfiles.d/mcproxy.conf 2>/dev/null || true
    if mount /var/log 2>/dev/null; then
      log_ok "  Mounted /var/log tmpfs"
    else
      log_warn "  /var/log tmpfs mount deferred to next reboot"
    fi
  fi

  # Mount /tmp if not already mounted
  if ! mountpoint -q /tmp 2>/dev/null; then
    mount -t tmpfs -o defaults,noatime,nosuid,mode=1777,size=50M tmpfs /tmp || true
  fi
}

#──────────────────────────────────────────────────────────────────
# JOURNALD CONFIGURATION (Volatile Storage)
#──────────────────────────────────────────────────────────────────

configure_journald() {
  log_info "Configuring journald for volatile storage..."

  local conf_dir="/etc/systemd/journald.conf.d"
  local conf_file="${conf_dir}/mcproxy-volatile.conf"

  mkdir -p "$conf_dir"

  # Check if already configured (ours or pi-harden's)
  if [[ -f "$conf_file" ]] || [[ -f "${conf_dir}/volatile.conf" ]]; then
    log_info "  journald already configured"
    return 0
  fi

  cat > "$conf_file" << 'EOF'
# MCProxy: Use volatile storage to protect SD card
[Journal]
Storage=volatile
RuntimeMaxUse=20M
RuntimeKeepFree=10M
RuntimeMaxFileSize=5M
MaxRetentionSec=1week
EOF

  log_ok "  journald configured for volatile storage"

  # Restart journald to apply
  systemctl restart systemd-journald || true
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

configure_nftables() {
  log_info "  Using nftables (Trixie)"

  # Install nftables if not present
  if ! command -v nft &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq nftables
  fi

  local nft_conf="/etc/nftables.conf"
  local marker="# MCProxy firewall rules"

  # Check if our rules already exist
  if grep -q "$marker" "$nft_conf" 2>/dev/null; then
    # Update existing rules if SSE port 2981 is missing
    if ! grep -q "dport 2981" "$nft_conf" 2>/dev/null; then
      log_info "  Updating nftables rules (adding SSE port 2981)..."
    else
      log_info "  nftables rules already configured"
      return 0
    fi
  fi

  # Backup existing config
  [[ -f "$nft_conf" ]] && cp "$nft_conf" "${nft_conf}.bak.$(date +%Y%m%d)"

  cat > "$nft_conf" << 'EOF'
#!/usr/sbin/nft -f
# MCProxy firewall rules
# Generated by mcproxy.sh bootstrap

flush ruleset

table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;

    # Allow loopback
    iif lo accept

    # Allow established/related connections
    ct state established,related accept

    # SSH (rate limited)
    tcp dport 22 ct state new limit rate 3/minute accept

    # HTTP (lighttpd webapp)
    tcp dport 80 accept

    # WebSocket (MCProxy)
    tcp dport 2980 accept

    # SSE/REST (MCProxy API)
    tcp dport 2981 accept

    # MeshCom UDP
    udp dport 1799 accept

    # mDNS for .local hostname resolution (avahi)
    udp dport 5353 accept

    # ICMP (ping)
    icmp type echo-request accept
    icmpv6 type echo-request accept

    # Log and drop everything else
    log prefix "[nftables DROP] " flags all counter drop
  }

  chain forward {
    type filter hook forward priority 0; policy drop;
  }

  chain output {
    type filter hook output priority 0; policy accept;
  }
}
EOF

  # Enable and start nftables
  systemctl enable nftables
  systemctl restart nftables

  log_ok "  nftables firewall configured and enabled"
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

  # Flush existing rules
  iptables -F
  iptables -X

  # Default policies
  iptables -P INPUT DROP
  iptables -P FORWARD DROP
  iptables -P OUTPUT ACCEPT

  # Allow loopback
  iptables -A INPUT -i lo -j ACCEPT

  # Allow established connections
  iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

  # SSH with rate limiting
  iptables -A INPUT -p tcp --dport 22 -m state --state NEW -m recent --set
  iptables -A INPUT -p tcp --dport 22 -m state --state NEW -m recent --update --seconds 60 --hitcount 4 -j DROP
  iptables -A INPUT -p tcp --dport 22 -j ACCEPT

  # HTTP, WebSocket, SSE/REST, MeshCom UDP, mDNS
  iptables -A INPUT -p tcp --dport 80 -j ACCEPT
  iptables -A INPUT -p tcp --dport 2980 -j ACCEPT
  iptables -A INPUT -p tcp --dport 2981 -j ACCEPT
  iptables -A INPUT -p udp --dport 1799 -j ACCEPT
  iptables -A INPUT -p udp --dport 5353 -j ACCEPT

  # ICMP
  iptables -A INPUT -p icmp --icmp-type echo-request -j ACCEPT

  # Save rules
  iptables-save > /etc/iptables/rules.v4

  log_ok "  iptables firewall configured and saved"
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

  # Install the service from template
  local template_dir
  if [[ -d "${INSTALL_DIR}/bootstrap/templates" ]]; then
    template_dir="${INSTALL_DIR}/bootstrap/templates"
  elif [[ -d "${SCRIPT_DIR}/templates" ]]; then
    template_dir="${SCRIPT_DIR}/templates"
  else
    log_warn "  Cannot find templates dir, skipping bluetooth service"
    return 0
  fi

  if [[ -f "${template_dir}/unblock-bluetooth.service" ]]; then
    cp "${template_dir}/unblock-bluetooth.service" "$service_file"
  else
    log_warn "  unblock-bluetooth.service template not found, skipping"
    return 0
  fi

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
  cat > /etc/apt/apt.conf.d/50unattended-upgrades << EOF
// MCProxy: Automatic security updates
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
  cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

  log_ok "  Unattended upgrades configured (security updates only)"
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

configure_ssh_hardening() {
  log_info "Configuring SSH hardening..."

  local ssh_conf_dir="/etc/ssh/sshd_config.d"
  local ssh_conf="${ssh_conf_dir}/mcproxy-hardening.conf"

  mkdir -p "$ssh_conf_dir"

  # Check if already configured (ours or pi-harden's)
  if [[ -f "$ssh_conf" ]] || [[ -f "${ssh_conf_dir}/10-pi-harden.conf" ]]; then
    log_info "  SSH hardening already configured"
    return 0
  fi

  cat > "$ssh_conf" << 'EOF'
# MCProxy SSH hardening — crypto + access control
HostKey /etc/ssh/ssh_host_ed25519_key
Ciphers aes256-gcm@openssh.com,chacha20-poly1305@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org
UseDNS no
X11Forwarding no
PermitRootLogin no
MaxAuthTries 3
MaxSessions 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF

  # Validate sshd config before restarting
  if sshd -t 2>/dev/null; then
    systemctl restart sshd || true
    log_ok "  SSH hardening applied (ed25519-only, modern ciphers)"
  else
    rm -f "$ssh_conf"
    log_warn "  SSH config invalid, hardening skipped"
  fi
}
