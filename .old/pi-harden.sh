#!/usr/bin/env bash
# pi-harden.sh — Idempotent SD-card longevity & security hardening for Raspberry Pi (Debian Trixie)
# Safe to run repeatedly. Each section checks state before modifying.
set -euo pipefail

MARKER="# pi-harden"

info()  { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[SKIP]\033[0m %s\n' "$*"; }
step()  { printf '\033[1;36m>>>\033[0m %s\n' "$*"; }

[[ $(id -u) -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

# ── 1. Disable unnecessary services ──────────────────────────────────────────
step "Disabling unnecessary services"

SERVICES=(
  ModemManager cloud-init-local cloud-init-main cloud-init-network
  cloud-config cloud-final udisks2 e2scrub_reap serial-getty@ttyS0
)
TIMERS=(e2scrub_all.timer man-db.timer)

for svc in "${SERVICES[@]}"; do
  if systemctl is-enabled "$svc" &>/dev/null; then
    systemctl disable --now "$svc" 2>/dev/null && info "Disabled $svc"
  else
    warn "$svc already disabled or absent"
  fi
done

for tmr in "${TIMERS[@]}"; do
  if systemctl is-enabled "$tmr" &>/dev/null; then
    systemctl disable --now "$tmr" 2>/dev/null && info "Disabled $tmr"
  else
    warn "$tmr already disabled or absent"
  fi
done

# ── 2. Unattended upgrades ───────────────────────────────────────────────────
step "Configuring unattended upgrades"

apt-get install -y -qq unattended-upgrades >/dev/null 2>&1 && info "unattended-upgrades installed"

cat > /etc/apt/apt.conf.d/50unattended-upgrades <<'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
info "50unattended-upgrades written"

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
info "20auto-upgrades written"

# ── 3. tmpfs for /var/log ────────────────────────────────────────────────────
step "Setting up tmpfs for /var/log"

if grep -q "$MARKER" /etc/fstab 2>/dev/null; then
  warn "/var/log tmpfs already in fstab"
else
  printf 'tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,noexec,size=30M 0 0 %s\n' "$MARKER" >> /etc/fstab
  info "Added /var/log tmpfs to fstab"
fi

# Ensure log subdirs exist after tmpfs mount
cat > /etc/tmpfiles.d/pi-harden.conf <<'EOF'
d /var/log/journal     0755 root root -
d /var/log/apt         0755 root root -
d /var/log/private     0700 root root -
d /var/log/chrony      0755 _chrony _chrony -
d /var/log/unattended-upgrades 0755 root root -
EOF
info "tmpfiles.d/pi-harden.conf written"

if findmnt -n /var/log | grep -q tmpfs; then
  warn "/var/log already mounted as tmpfs"
else
  # Create dirs before mount so services don't break
  systemd-tmpfiles --create /etc/tmpfiles.d/pi-harden.conf 2>/dev/null || true
  mount /var/log 2>/dev/null && info "Mounted /var/log tmpfs" || warn "/var/log mount deferred to next boot"
fi

# ── 4. Journald volatile ────────────────────────────────────────────────────
step "Configuring journald volatile storage"

mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/volatile.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=20M
EOF
info "journald volatile.conf written"
systemctl restart systemd-journald && info "journald restarted"

# ── 5. Fast SSH login ───────────────────────────────────────────────────────
step "Speeding up SSH login"

# Disable MOTD scripts
if [ -d /etc/update-motd.d ]; then
  for f in /etc/update-motd.d/*; do
    [ -x "$f" ] && chmod -x "$f" && info "Disabled motd script: $(basename "$f")"
  done
fi

# Comment out pam_motd in sshd PAM config
if [ -f /etc/pam.d/sshd ]; then
  if grep -qE '^\s*session.*pam_motd' /etc/pam.d/sshd; then
    sed -i 's/^\(\s*session.*pam_motd\)/#\1/' /etc/pam.d/sshd
    info "Commented out pam_motd in /etc/pam.d/sshd"
  else
    warn "pam_motd already commented or absent"
  fi
fi

# ── 6. SSHD crypto hardening ────────────────────────────────────────────────
step "Hardening SSHD configuration"

mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-pi-harden.conf <<'EOF'
HostKey /etc/ssh/ssh_host_ed25519_key
Ciphers aes256-gcm@openssh.com,chacha20-poly1305@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org
UseDNS no
PermitRootLogin no
MaxAuthTries 3
X11Forwarding no
EOF
info "sshd_config.d/10-pi-harden.conf written"

# Validate before restarting
if sshd -t 2>/dev/null; then
  systemctl restart sshd && info "sshd restarted with hardened config"
else
  echo "ERROR: sshd config validation failed! Reverting."
  rm -f /etc/ssh/sshd_config.d/10-pi-harden.conf
  exit 1
fi

# ── 7. Reduce logrotate retention ───────────────────────────────────────────
step "Reducing logrotate retention"

if [ -f /etc/logrotate.conf ]; then
  if grep -qE '^\s*rotate\s+2\b' /etc/logrotate.conf; then
    warn "logrotate already set to rotate 2"
  else
    sed -i 's/^\s*rotate\s\+[0-9]\+/rotate 2/' /etc/logrotate.conf
    info "Set logrotate to rotate 2"
  fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────
printf '\n\033[1;32m✓ Pi hardening complete.\033[0m Reboot recommended.\n'
